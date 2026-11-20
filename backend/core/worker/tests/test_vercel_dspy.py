"""Verify managed DSPy execution transports results and recovery state without host execution."""

from __future__ import annotations

import base64
import json
import logging
import queue
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from vercel.sandbox import SandboxApiError

from core.billing.model_gateway import _sandbox_failure_kind
from core.billing.signals import BudgetReached
from core.billing.vercel_usage import proposer_hosts
from core.exceptions import DETERMINISTIC_FAILURE, INFRASTRUCTURE_INTERRUPTION, ServiceError
from core.service_gateway.optimization.blackbox import remote_sandbox
from core.service_gateway.optimization.blackbox.harness_bridge import hidden_hashes
from core.service_gateway.optimization.blackbox.sandbox import CommandResult
from core.service_gateway.optimization.blackbox.sandbox_broker import SandboxBroker
from core.service_gateway.optimization.blackbox.tests.test_sandbox_broker import IMAGE, FakeRuntime
from core.worker import vercel_dspy
from core.worker.isolated_runner import SANDBOX_PROTOCOL
from core.worker.tests.mocks import SESSION_STOPPED, stopped_box_event


class FakeSession:
    """Emulate only the declared remote session transport with no provider calls."""

    def __init__(self) -> None:
        """Start an empty managed workspace."""
        self.files: dict[str, str] = {}
        self.closed = False
        self.path = "gepa_state.bin"
        self.steps: list[str] = []
        self.requirements_error: Exception | None = None
        self.image_protocol = SANDBOX_PROTOCOL

    def write_files(self, files: dict[str, str]) -> None:
        """Record the staged request.

        Args:
            files: The staged files.
        """
        self.steps.append("write_files")
        self.files.update(files)

    def write_codex_requirements(self) -> None:
        """Record the parent's requirements write, or fail it the way a bad read-back does."""
        self.steps.append("codex_requirements")
        if self.requirements_error is not None:
            raise self.requirements_error

    def run(self, command: str, **kwargs: Any) -> CommandResult:
        """Answer the protocol probe, then emit fragmented checkpoint and optimizer frames.

        Args:
            command: The command the box runs.
            **kwargs: The run options, which the fake ignores.

        Returns:
            The probe answer, or a successful result.
        """
        self.steps.append("probe" if command == vercel_dspy.PROTOCOL_PROBE else "optimizer")
        if command == vercel_dspy.PROTOCOL_PROBE:
            assert not self.files
            return CommandResult(0, stdout=f"{self.image_protocol}\n")
        [request_path] = [path for path in self.files if path.endswith("/request.json")]
        assert list(self.files) == [request_path]
        assert command.startswith("PYTHONPATH=/app python3 -m core.worker.isolated_runner")
        request = json.loads(self.files[request_path])
        assert "_budget_gateway_descriptor" not in request["payload"]
        assert "runtime_identity" not in request
        assert request["protocol"] == self.image_protocol
        prefix = f"{vercel_dspy.EVENT_PREFIX}{request['nonce']} "
        events = []
        if request["export_checkpoints"]:
            events.append(
                {"type": "checkpoint_file", "path": self.path, "data": base64.b64encode(b"checkpoint").decode()}
            )
        events.append({"type": "result", "result": {"program": "done"}})
        text = "".join(prefix + json.dumps(event) + "\n" for event in events)
        for chunk in [text[:15], text[15:61], text[61:]]:
            kwargs["on_output"]("stdout", chunk)
        assert "core.worker.isolated_runner" in command
        return CommandResult(0)

    def close(self) -> None:
        """Confirm cleanup even when checkpoint validation fails."""
        self.closed = True


def test_remote_result_and_checkpoint_stream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirror real event framing and close the managed session on success.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Pytest monkeypatch.
    """
    session = FakeSession()
    specs = []

    class Runtime:
        """Capture the requested managed image and network profile."""

        def __init__(self, url: str, token: str) -> None:
            """Accept only the parent's opaque descriptor.

            Args:
                url: The parent's descriptor URL.
                token: The parent's descriptor token.
            """
            assert token == "control"

        def open(self, spec: Any) -> FakeSession:
            """Return one fake paid session without any live execution.

            Args:
                spec: The sandbox spec.

            Returns:
                The fake session.
            """
            specs.append(spec)
            return session

    monkeypatch.setattr(vercel_dspy, "RemoteSandboxRuntime", Runtime)
    events: queue.Queue = queue.Queue()
    payload = {
        "_gepa_log_dir": str(tmp_path),
        "_budget_gateway_descriptor": {
            "url": "http://127.0.0.1:9876",
            "control_token": "control",
            "image": "backend@sha256:" + "a" * 64,
            "lifetime_seconds": 600,
        },
    }
    vercel_dspy.run_vercel_dspy(payload, "job-g2", events, "spawn")
    assert session.closed
    assert specs[0].network_disabled
    assert specs[0].operation_key == "dspy:job-g2"
    assert (tmp_path / "gepa_state.bin").read_bytes() == b"checkpoint"
    assert events.get_nowait() == {"type": "result", "result": {"program": "done"}}
    assert events.empty()
    session.path = "../escaped.bin"
    session.closed = False
    vercel_dspy.run_vercel_dspy(payload, "job-g3", events, "spawn")
    assert session.closed
    assert events.get_nowait()["type"] == "error"
    assert not (tmp_path.parent / "escaped.bin").exists()


@pytest.mark.parametrize("image_protocol", [12, 16, 21])
def test_an_image_before_the_current_protocol_is_refused_at_the_probe(
    image_protocol: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An image before protocol 22 reads the old model names, so the worker refuses it before it sends work (SKY-776).

    Args:
        image_protocol: The protocol the image answers to the probe.
        tmp_path: Checkpoint directory and the packed tree's folder.
        monkeypatch: Pytest monkeypatch.
    """
    session = FakeSession()
    session.image_protocol = image_protocol
    monkeypatch.setattr(vercel_dspy, "RemoteSandboxRuntime", lambda *_args: SimpleNamespace(open=lambda _spec: session))
    events: queue.Queue = queue.Queue()
    vercel_dspy.run_vercel_dspy(_descriptor_payload(tmp_path), "job-old-image", events, "spawn")

    assert session.steps == ["probe"]
    assert session.files == {}
    assert session.closed
    event = events.get_nowait()
    assert event["type"] == "error"
    assert event["error"] == (
        f"The sandbox image speaks protocol {image_protocol}, but this worker speaks {SANDBOX_PROTOCOL};"
        " rebuild and re-pin the sandbox image."
    )
    assert event["failure_kind"] == DETERMINISTIC_FAILURE
    assert events.empty()


def _run_with_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, image_protocol: int, outcome: dict[str, Any]
) -> tuple[FakeSession, list[dict[str, Any]]]:
    """Run a repository payload whose staged snapshot holds ``outcome`` on an image of ``image_protocol``.

    Args:
        tmp_path: Checkpoint directory and the packed tree's folder.
        monkeypatch: Pytest monkeypatch.
        image_protocol: The protocol the image answers to the probe.
        outcome: The snapshot's ``outcome`` entry.

    Returns:
        The session and every event the run put on the queue.
    """
    session = FakeSession()
    session.image_protocol = image_protocol
    monkeypatch.setattr(vercel_dspy, "RemoteSandboxRuntime", lambda *_args: SimpleNamespace(open=lambda _spec: session))
    archive = tmp_path / "tree.tgz"
    archive.write_bytes(b"tree")
    events: queue.Queue = queue.Queue()
    payload = _descriptor_payload(
        tmp_path, _repo_snapshot={"archive": str(archive), "commit": "c" * 40, "outcome": outcome}
    )
    vercel_dspy.run_vercel_dspy(payload, "job-outcome", events, "spawn")
    seen = []
    while not events.empty():
        seen.append(events.get_nowait())
    return session, seen


# The probe now refuses every image before SANDBOX_PROTOCOL (SKY-776), so a run
# cannot reach the per-feature gates with an older image. The gate tests below
# call each gate directly with the older protocols, and run the current
# protocol through the full run.


@pytest.mark.parametrize("image_protocol", [12, 13, 14, 15])
@pytest.mark.parametrize("outcome", [{"target": 0.9, "require": {}}, {"target": None, "require": {"latency": 0.5}}])
def test_an_older_image_is_refused_for_a_run_with_outcome_values(image_protocol: int, outcome: dict[str, Any]) -> None:
    """An image before 16 drops the file's target or minimums, or gives no run end verdict (SKY-691, SKY-692).

    Args:
        image_protocol: The protocol the image answers to the probe.
        outcome: The snapshot's ``outcome`` entry.
    """
    with pytest.raises(RuntimeError) as refusal:
        vercel_dspy._require_outcome_support(image_protocol, {"outcome": outcome})
    assert str(refusal.value) == vercel_dspy.OUTCOME_IMAGE_MESSAGE


@pytest.mark.parametrize(
    ("image_protocol", "outcome"),
    [
        (12, {"target": None, "require": {}}),
        (14, {}),
        (15, {"target": None, "require": {}}),
        (16, {"target": 0.9, "require": {}}),
    ],
)
def test_an_older_image_passes_the_outcome_gate_when_it_can_read_the_runs_outcome(
    image_protocol: int, outcome: dict[str, Any]
) -> None:
    """An older image passes the gate for a run with no outcome values; an image at 16 passes one with a target.

    Args:
        image_protocol: The protocol the image answers to the probe.
        outcome: The snapshot's ``outcome`` entry.
    """
    vercel_dspy._require_outcome_support(image_protocol, {"outcome": outcome})


def test_the_current_image_runs_a_run_with_a_target_and_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An image at the current protocol runs a run whose outcome sets a target and a limit.

    Args:
        tmp_path: Checkpoint directory and the packed tree's folder.
        monkeypatch: Pytest monkeypatch.
    """
    session, events = _run_with_outcome(
        tmp_path, monkeypatch, SANDBOX_PROTOCOL, {"target": 0.9, "require": {"latency": 0.5}}
    )

    assert session.steps[0] == "probe"
    assert "optimizer" in session.steps
    assert all(event.get("error") != vercel_dspy.OUTCOME_IMAGE_MESSAGE for event in events)


def test_an_image_before_18_is_refused_for_a_run_of_the_files_proposer() -> None:
    """An image before 18 runs the entry as a plain custom command, without setup or ``ASCENT_TASK`` (SKY-753)."""
    with pytest.raises(RuntimeError) as refusal:
        vercel_dspy._require_file_proposer_support(17, {"file_proposer": True})
    assert str(refusal.value) == vercel_dspy.FILE_PROPOSER_IMAGE_MESSAGE


def test_the_current_image_runs_a_run_of_the_files_proposer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An image at the current protocol runs the contract file's proposer.

    Args:
        tmp_path: Checkpoint directory and the packed tree's folder.
        monkeypatch: Pytest monkeypatch.
    """
    session = FakeSession()
    monkeypatch.setattr(vercel_dspy, "RemoteSandboxRuntime", lambda *_args: SimpleNamespace(open=lambda _spec: session))
    archive = tmp_path / "tree.tgz"
    archive.write_bytes(b"tree")
    events: queue.Queue = queue.Queue()
    payload = _descriptor_payload(
        tmp_path, _repo_snapshot={"archive": str(archive), "commit": "c" * 40, "file_proposer": True}
    )

    vercel_dspy.run_vercel_dspy(payload, "job-file-proposer", events, "spawn")
    seen = []
    while not events.empty():
        seen.append(events.get_nowait())

    assert "optimizer" in session.steps
    assert all(event.get("error") != vercel_dspy.FILE_PROPOSER_IMAGE_MESSAGE for event in seen)


def test_an_image_before_19_is_refused_for_a_run_of_a_command_metric() -> None:
    """An image before 19 refuses a command metric request with an unclear error, so the worker names it (SKY-738)."""
    with pytest.raises(RuntimeError) as refusal:
        vercel_dspy._require_command_metric_support(18, {"scorer": {"kind": "python", "command_metric": True}})
    assert str(refusal.value) == vercel_dspy.COMMAND_METRIC_IMAGE_MESSAGE


def test_the_current_image_runs_a_run_of_a_command_metric(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An image at the current protocol runs a command metric.

    Args:
        tmp_path: Checkpoint directory.
        monkeypatch: Pytest monkeypatch.
    """
    session = FakeSession()
    monkeypatch.setattr(vercel_dspy, "RemoteSandboxRuntime", lambda *_args: SimpleNamespace(open=lambda _spec: session))
    events: queue.Queue = queue.Queue()
    payload = _descriptor_payload(tmp_path, scorer={"kind": "python", "command_metric": True})

    vercel_dspy.run_vercel_dspy(payload, "job-command-metric", events, "spawn")
    seen = []
    while not events.empty():
        seen.append(events.get_nowait())

    assert "optimizer" in session.steps
    assert all(event.get("error") != vercel_dspy.COMMAND_METRIC_IMAGE_MESSAGE for event in seen)


@pytest.mark.parametrize(("direction", "refused"), [("minimize", True), ("maximize", False)])
def test_an_image_before_20_is_refused_for_a_minimize_run(direction: str, refused: bool) -> None:
    """An image before 20 ignores the outcome's direction and would maximize a minimize run (SKY-750).

    Args:
        direction: The staged snapshot's ``outcome.direction``.
        refused: Whether the gate refuses the image.
    """
    snapshot = {"outcome": {"target": None, "require": {}, "direction": direction}}
    if refused:
        with pytest.raises(RuntimeError) as refusal:
            vercel_dspy._require_direction_support(19, snapshot)
        assert str(refusal.value) == vercel_dspy.DIRECTION_IMAGE_MESSAGE
    else:
        vercel_dspy._require_direction_support(19, snapshot)


def test_the_current_image_runs_a_minimize_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An image at the current protocol runs a minimize run.

    Args:
        tmp_path: Checkpoint directory and the packed tree's folder.
        monkeypatch: Pytest monkeypatch.
    """
    session, seen = _run_with_outcome(
        tmp_path, monkeypatch, SANDBOX_PROTOCOL, {"target": None, "require": {}, "direction": "minimize"}
    )

    assert "optimizer" in session.steps
    assert all(event.get("error") != vercel_dspy.DIRECTION_IMAGE_MESSAGE for event in seen)


@pytest.mark.parametrize(
    ("scores", "require", "refused"),
    [
        ({"cost_usd": "minimize", "recall": "maximize"}, {"cost_usd": 0.05}, True),
        ({"cost_usd": "minimize"}, {}, True),
        # The old image writes the old verdict keys, so any limit would lose the run record.
        ({"recall": "maximize"}, {"recall": 0.8}, True),
        (None, {"recall": 0.8}, True),
        (None, {}, False),
    ],
)
def test_an_image_before_21_is_refused_for_limits_or_a_minimize_named_score(
    scores: dict[str, str] | None, require: dict[str, float], refused: bool
) -> None:
    """An image before 21 misjudges a minimize limit and writes the old verdict keys (SKY-775).

    Args:
        scores: The staged snapshot's ``outcome.scores``, or ``None`` when it has no key.
        require: The staged snapshot's ``outcome.require``.
        refused: Whether the gate refuses the image.
    """
    outcome: dict[str, Any] = {"target": None, "require": require, "direction": "maximize"}
    if scores is not None:
        outcome["scores"] = scores
    if not refused:
        vercel_dspy._require_score_direction_support(20, {"outcome": outcome})
        return
    with pytest.raises(RuntimeError) as refusal:
        vercel_dspy._require_score_direction_support(20, {"outcome": outcome})
    message = str(refusal.value)
    assert message == vercel_dspy.SCORE_DIRECTION_IMAGE_MESSAGE
    assert "rebuild and re-pin the sandbox image" in message
    # The message names both rules, so it is right for a contract with only [outcome.require] limits too.
    assert "[outcome.require] limits" in message
    assert "[metric.scores]" in message


def test_the_current_image_runs_limits_and_a_minimize_named_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An image at the current protocol runs a minimize score with a limit.

    Args:
        tmp_path: Checkpoint directory and the packed tree's folder.
        monkeypatch: Pytest monkeypatch.
    """
    outcome = {
        "target": None,
        "require": {"cost_usd": 0.05},
        "direction": "maximize",
        "scores": {"cost_usd": "minimize"},
    }
    session, seen = _run_with_outcome(tmp_path, monkeypatch, SANDBOX_PROTOCOL, outcome)

    assert "optimizer" in session.steps
    assert all(event.get("error") != vercel_dspy.SCORE_DIRECTION_IMAGE_MESSAGE for event in seen)


def _descriptor_payload(tmp_path: Path, **extra: Any) -> dict[str, Any]:
    """Build a run payload that carries the parent's opaque sandbox descriptor.

    Args:
        tmp_path: Checkpoint directory.
        **extra: More payload keys, such as the proposer settings.

    Returns:
        The payload.
    """
    return {
        **extra,
        "_gepa_log_dir": str(tmp_path),
        "_budget_gateway_descriptor": {
            "url": "http://127.0.0.1:9876",
            "control_token": "control",
            "image": "backend@sha256:" + "a" * 64,
            "lifetime_seconds": 600,
        },
    }


def test_the_parent_writes_the_codex_requirements_before_the_optimizer_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Write the root-owned file after the protocol check and before any request or optimizer command.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Pytest monkeypatch.
    """
    session = FakeSession()
    monkeypatch.setattr(vercel_dspy, "RemoteSandboxRuntime", lambda *_args: SimpleNamespace(open=lambda _spec: session))
    events: queue.Queue = queue.Queue()

    vercel_dspy.run_vercel_dspy(
        _descriptor_payload(tmp_path, proposer={"harness": "codex"}), "job-req", events, "spawn"
    )

    assert session.steps[:2] == ["probe", "codex_requirements"]
    assert session.steps.index("codex_requirements") < session.steps.index("write_files")
    assert session.steps[-1] == "optimizer"
    assert events.get_nowait()["type"] == "result"


def test_a_failed_codex_requirements_write_stops_the_run_before_the_optimizer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed: a missing or user-owned file means no request and no optimizer command.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Pytest monkeypatch.
    """
    session = FakeSession()
    session.requirements_error = ServiceError("Codex cannot start: the sandbox did not keep the file.")
    monkeypatch.setattr(vercel_dspy, "RemoteSandboxRuntime", lambda *_args: SimpleNamespace(open=lambda _spec: session))
    events: queue.Queue = queue.Queue()

    vercel_dspy.run_vercel_dspy(_descriptor_payload(tmp_path, proposer={}), "job-req-fail", events, "spawn")

    assert "optimizer" not in session.steps
    assert not session.files
    assert session.closed
    event = events.get_nowait()
    assert event["type"] == "error"
    assert "Codex cannot start" in json.dumps(event)


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"proposer": {"harness": "pi"}},
        {"proposer": {"harness": "codex", "host_cli": "codex"}},
    ],
    ids=["no-proposer", "pi", "host-cli"],
)
def test_a_run_without_a_sandbox_codex_proposer_skips_the_root_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: dict[str, Any]
) -> None:
    """Skip the root write when no Codex proposer starts in the box, so a read-only local box still runs.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Pytest monkeypatch.
        extra: The extra payload fields.
    """
    session = FakeSession()
    session.requirements_error = AssertionError("the run must not write the Codex requirements")
    monkeypatch.setattr(vercel_dspy, "RemoteSandboxRuntime", lambda *_args: SimpleNamespace(open=lambda _spec: session))
    events: queue.Queue = queue.Queue()

    vercel_dspy.run_vercel_dspy(_descriptor_payload(tmp_path, **extra), "job-skip", events, "spawn")

    assert "codex_requirements" not in session.steps
    assert events.get_nowait()["type"] == "result"


def test_a_setup_check_never_writes_the_codex_requirements() -> None:
    """A setup check never runs the proposer, so it needs no root write."""
    assert not vercel_dspy._starts_codex({"_preflight": True, "proposer": {"harness": "codex"}})
    assert vercel_dspy._starts_codex({"proposer": {"harness": "codex"}})
    assert vercel_dspy._starts_codex({"proposer": {}})


@pytest.mark.parametrize(
    ("strategy", "starts"),
    [
        ({}, True),
        ({"mode": "auto"}, True),
        ({"mode": "single", "engine": "autosaddler"}, True),
        ({"mode": "single", "engine": "gepa"}, False),
        ({"mode": "single", "engine": "best_of_n"}, False),
    ],
    ids=["default-auto", "auto", "autosaddler", "gepa", "best-of-n"],
)
def test_a_files_proposer_run_that_can_reach_the_autosaddler_fallback_writes_the_codex_requirements(
    strategy: dict[str, str], starts: bool
) -> None:
    """The file's command is ``custom``, but AutoSaddler answers a session outside a checkout with Codex (SKY-753).

    The guest's contained runtime cannot write the root-owned file, so the parent writes it.

    Args:
        strategy: The run's strategy.
        starts: Whether a Codex agent can start in the box.
    """
    payload = {
        "proposer": {"harness": "custom", "run_command": "node agent.js"},
        "strategy": strategy,
        vercel_dspy.REPO_SNAPSHOT_KEY: {"file_proposer": True},
    }

    assert vercel_dspy._starts_codex(payload) is starts
    assert not vercel_dspy._starts_codex({**payload, vercel_dspy.REPO_SNAPSHOT_KEY: {"file_proposer": False}})
    assert not vercel_dspy._starts_codex({**payload, "_preflight": True})


def test_the_parent_writes_the_codex_requirements_for_a_files_proposer_autosaddler_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file proposer AutoSaddler run gets the root write before the optimizer starts (SKY-753).

    Args:
        tmp_path: Checkpoint directory and the packed tree.
        monkeypatch: The remote runtime.
    """
    archive = tmp_path / "tree.tar.gz"
    archive.write_bytes(b"tree")
    session = FakeSession()
    monkeypatch.setattr(vercel_dspy, "RemoteSandboxRuntime", lambda *_args: SimpleNamespace(open=lambda _spec: session))
    events: queue.Queue = queue.Queue()
    payload = _descriptor_payload(
        tmp_path,
        proposer={"harness": "custom", "run_command": "node agent.js"},
        strategy={"mode": "single", "engine": "autosaddler"},
    )
    payload[vercel_dspy.REPO_SNAPSHOT_KEY] = {"archive": str(archive), "file_proposer": True}

    vercel_dspy.run_vercel_dspy(payload, "job-file-proposer", events, "spawn")

    assert session.steps.index("codex_requirements") < session.steps.index("optimizer")


def test_remote_preflight_never_reads_submitted_checkpoint_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exclude supervisor checkpoint files from setup even if a private field reaches the runner.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Pytest monkeypatch.
    """
    marker = tmp_path / "gepa_state.bin"
    marker.write_bytes(b"parent-only")
    session = FakeSession()
    monkeypatch.setattr(vercel_dspy, "RemoteSandboxRuntime", lambda *_args: SimpleNamespace(open=lambda _spec: session))
    events: queue.Queue = queue.Queue()
    vercel_dspy.run_vercel_dspy(
        {
            "_preflight": {"scope": "evaluation"},
            "_gepa_log_dir": str(tmp_path),
            "_budget_gateway_descriptor": {
                "url": "http://127.0.0.1:9876",
                "control_token": "control",
                "image": "backend@sha256:" + "a" * 64,
                "lifetime_seconds": 600,
            },
        },
        "setup",
        events,
        "spawn",
    )
    request = json.loads(session.files[next(path for path in session.files if path.endswith("/request.json"))])
    assert "_gepa_log_dir" not in request["payload"]
    assert request["checkpoints"] == {}
    assert request["export_checkpoints"] is False
    assert marker.read_bytes() == b"parent-only"
    assert session.closed
    assert events.get_nowait() == {"type": "preflight_phase", "phase": "evaluator"}
    assert events.get_nowait()["type"] == "result"


class CrashingSession(FakeSession):
    """Die before the entrypoint, the way an image missing this revision's modules does."""

    def __init__(self, stderr: str) -> None:
        """Keep the traceback the guest would print.

        Args:
            stderr: The traceback the guest prints.
        """
        super().__init__()
        self.stderr = stderr

    def run(self, command: str, **kwargs: Any) -> CommandResult:
        """Pass the protocol probe, then stream only stderr and exit 1 without framing any event.

        Args:
            command: The command the box runs.
            **kwargs: The run options, which the fake ignores.

        Returns:
            The probe answer, or a failed result with the stderr.
        """
        if command == vercel_dspy.PROTOCOL_PROBE:
            return CommandResult(0, stdout=f"{SANDBOX_PROTOCOL}\n")
        kwargs["on_output"]("stderr", self.stderr)
        return CommandResult(1, stderr=self.stderr)


@pytest.mark.parametrize(
    ("stderr", "expected", "kind"),
    [
        (
            "Traceback (most recent call last):\n"
            '  File "/app/core/worker/isolated_runner.py", line 26, in <module>\n'
            "    from core.worker.preflight import run_dspy_preflight\n"
            "ModuleNotFoundError: No module named 'core.service_gateway.datasets.split_counts'\n",
            f"{vercel_dspy.BROKEN_IMAGE_MESSAGE} "
            "ModuleNotFoundError: No module named 'core.service_gateway.datasets.split_counts'",
            DETERMINISTIC_FAILURE,
        ),
        (
            "Traceback (most recent call last):\n"
            '  File "<frozen runpy>", line 198, in _run_module_as_main\n'
            "PermissionError: [Errno 13] Permission denied: '/app/.deno'\n",
            "The optimizer in the cloud sandbox exited without a complete result (exit 1). "
            "PermissionError: [Errno 13] Permission denied: '/app/.deno'",
            INFRASTRUCTURE_INTERRUPTION,
        ),
    ],
)
def test_guest_crash_names_its_cause(
    stderr: str, expected: str, kind: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Carry the guest's last stderr line into the failure and keep the traceback in the log.

    Args:
        stderr: The guest's stderr.
        expected: The cause the failure must name.
        kind: The failure kind.
        monkeypatch: Pytest monkeypatch.
        caplog: Pytest log capture fixture.
    """
    session = CrashingSession(stderr)
    monkeypatch.setattr(vercel_dspy, "RemoteSandboxRuntime", lambda *_args: SimpleNamespace(open=lambda _spec: session))
    events: queue.Queue = queue.Queue()
    with caplog.at_level(logging.ERROR, logger="core.worker.vercel_dspy"):
        vercel_dspy.run_vercel_dspy(
            {
                "_budget_gateway_descriptor": {
                    "url": "http://127.0.0.1:9876",
                    "control_token": "control",
                    "image": "backend@sha256:" + "a" * 64,
                    "lifetime_seconds": 600,
                },
            },
            "job-crash",
            events,
            "spawn",
        )
    assert session.closed
    event = events.get_nowait()
    assert event["type"] == "error"
    assert event["error"] == expected
    assert event["failure_kind"] == kind
    assert "Traceback (most recent call last):" in caplog.text
    assert events.empty()


class BudgetStoppedSession(FakeSession):
    """Pass the probe, then lose the box because the budget refused its next slice."""

    def run(self, command: str, **kwargs: Any) -> CommandResult:
        """Answer the probe, then raise the error frame the parent sends for a budget stop.

        Args:
            command: The probe or the optimizer command.
            **kwargs: The run options, which this fake ignores.

        Returns:
            The probe answer. The optimizer command never returns.

        Raises:
            BudgetReached: The guest-side error for the budget stop. The parent
                error chains to the 410 that reported the stopped session, as
                the provider raises it.
        """
        if command == vercel_dspy.PROTOCOL_PROBE:
            return CommandResult(0, stdout=f"{SANDBOX_PROTOCOL}\n")
        try:
            raise BudgetReached("budget gone") from SandboxApiError(
                httpx.Response(410), "gone", data={"error": {"code": "sandbox_stopped"}}
            )
        except BudgetReached as error:
            stop = error
        remote_sandbox._raise_remote_error(
            {
                "type": "error",
                "error_type": type(stop).__name__,
                "failure_kind": _sandbox_failure_kind(stop),
                "message": str(stop),
            }
        )
        raise AssertionError("unreachable")


def test_a_guest_box_that_stops_for_budget_ends_the_run_budget_reached(monkeypatch: pytest.MonkeyPatch) -> None:
    """A box that Vercel ends after the budget refused its next slice ends the run as a budget stop (SKY-587).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    session = BudgetStoppedSession()
    monkeypatch.setattr(vercel_dspy, "RemoteSandboxRuntime", lambda *_args: SimpleNamespace(open=lambda _spec: session))
    events: queue.Queue = queue.Queue()
    vercel_dspy.run_vercel_dspy(
        {
            "_budget_gateway_descriptor": {
                "url": "http://127.0.0.1:9876",
                "control_token": "control",
                "image": "backend@sha256:" + "a" * 64,
                "lifetime_seconds": 600,
            },
        },
        "job-budget",
        events,
        "spawn",
    )
    assert session.closed
    event = events.get_nowait()
    assert events.empty()
    assert event["type"] == "terminal"
    assert event["outcome"]["status"] == "stopped"
    assert event["outcome"]["stop_reason"] == "budget_reached"
    assert event["outcome"]["message"] == "budget gone"


class ProbedSession(FakeSession):
    """Answer the protocol probe the way a given image would and record every command."""

    def __init__(self, probe: CommandResult) -> None:
        """Keep the image's probe answer.

        Args:
            probe: The image's probe answer.
        """
        super().__init__()
        self.probe = probe
        self.commands: list[str] = []

    def run(self, command: str, **kwargs: Any) -> CommandResult:
        """Return the probe answer; any later command means the request was sent.

        Args:
            command: The command the box runs.
            **kwargs: The run options, which the fake ignores.

        Returns:
            The probe answer.
        """
        self.commands.append(command)
        return self.probe


@pytest.mark.parametrize(
    ("probe", "expected"),
    [
        (
            CommandResult(
                1,
                stderr="Traceback (most recent call last):\n"
                '  File "<string>", line 1, in <module>\n'
                "ModuleNotFoundError: No module named 'core.worker.sandbox_protocol'\n",
            ),
            vercel_dspy.OUTDATED_IMAGE_MESSAGE,
        ),
        (
            CommandResult(0, stdout=f"{SANDBOX_PROTOCOL + 1}\n"),
            f"The sandbox image speaks protocol {SANDBOX_PROTOCOL + 1}, but this worker speaks {SANDBOX_PROTOCOL};"
            " rebuild and re-pin the sandbox image.",
        ),
        (
            CommandResult(0, stdout="11\n"),
            f"The sandbox image speaks protocol 11, but this worker speaks {SANDBOX_PROTOCOL};"
            " rebuild and re-pin the sandbox image.",
        ),
    ],
)
def test_incompatible_image_is_refused_before_the_request_is_sent(
    probe: CommandResult, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Name the stale image instead of letting its older models reject the request.

    Args:
        probe: The image's probe answer.
        expected: A part of the refusal the worker must give.
        monkeypatch: Pytest monkeypatch.
    """
    session = ProbedSession(probe)
    monkeypatch.setattr(vercel_dspy, "RemoteSandboxRuntime", lambda *_args: SimpleNamespace(open=lambda _spec: session))
    events: queue.Queue = queue.Queue()
    vercel_dspy.run_vercel_dspy(
        {
            "_preflight": {"scope": "evaluation"},
            "_budget_gateway_descriptor": {
                "url": "http://127.0.0.1:9876",
                "control_token": "control",
                "image": "backend@sha256:" + "a" * 64,
                "lifetime_seconds": 600,
            },
        },
        "setup",
        events,
        "spawn",
    )
    assert session.commands == [vercel_dspy.PROTOCOL_PROBE]
    assert session.files == {}
    assert session.closed
    event = events.get_nowait()
    assert event["type"] == "error"
    assert event["error"] == expected
    assert event["failure_kind"] == DETERMINISTIC_FAILURE
    assert events.empty()


def test_repository_tree_is_uploaded_as_chunks_and_never_as_a_parent_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ship the fetched tree in upload-sized pieces and hand the guest their paths instead of the parent's file.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Pytest monkeypatch.
    """
    session = FakeSession()

    class Runtime:
        """Return the one fake session."""

        def __init__(self, url: str, token: str) -> None:
            """Accept the descriptor.

            Args:
                url: The parent's descriptor URL.
                token: The parent's descriptor token.
            """

        def open(self, spec: Any) -> FakeSession:
            """Return the fake session.

            Args:
                spec: The sandbox spec.

            Returns:
                The fake session.
            """
            return session

    monkeypatch.setattr(vercel_dspy, "RemoteSandboxRuntime", Runtime)
    monkeypatch.setattr("core.service_gateway.optimization.blackbox.repo_tree.ARCHIVE_CHUNK_BYTES", 4)
    archive = tmp_path / "tree.tgz"
    archive.write_bytes(b"0123456789")
    payload = {
        "_gepa_log_dir": str(tmp_path),
        "_budget_gateway_descriptor": {
            "url": "http://127.0.0.1:9876",
            "control_token": "control",
            "image": "backend@sha256:" + "a" * 64,
            "lifetime_seconds": 600,
        },
        "_repo_snapshot": {
            "archive": str(archive),
            "commit": "c" * 40,
            "readonly_paths": [],
            "secret_names": ["K"],
            "hidden_hashes": hidden_hashes(["eval/zq_scorer.py"]),
        },
    }

    vercel_dspy.run_vercel_dspy(payload, "job-repo", queue.Queue(), "spawn")

    [request_path] = [path for path in session.files if path.endswith("/request.json")]
    snapshot = json.loads(session.files[request_path])["payload"]["_repo_snapshot"]
    assert snapshot["archive"] is None
    assert snapshot["secret_names"] == ["K"]
    assert len(snapshot["chunks"]) == 3
    assert b"".join(base64.b64decode(session.files[path]) for path in snapshot["chunks"]) == b"0123456789"
    assert str(archive) not in session.files[request_path]
    # The guest's proposer guard gets the hidden paths as hashes; no guest file names one (E61, SKY-395).
    assert snapshot["hidden_hashes"] == hidden_hashes(["eval/zq_scorer.py"])
    assert not any("zq_scorer" in content for content in session.files.values())


@pytest.mark.parametrize(
    ("proposer_hosts", "offline"),
    [(["files.pythonhosted.org", "pypi.org", "registry.npmjs.org"], False), ([], True)],
)
def test_the_outer_box_carries_the_staged_proposer_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, proposer_hosts: list[str], offline: bool
) -> None:
    """Open the protected box with the proposer's granted hosts, and keep it offline when it has none (E57).

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Pytest monkeypatch.
        proposer_hosts: The staged proposer hosts.
        offline: Whether the box must stay offline.
    """
    session = FakeSession()
    specs = []

    class Runtime:
        """Capture the requested network profile."""

        def __init__(self, url: str, token: str) -> None:
            """Accept the descriptor.

            Args:
                url: The parent's descriptor URL.
                token: The parent's descriptor token.
            """

        def open(self, spec: Any) -> FakeSession:
            """Record the spec and return the fake session.

            Args:
                spec: The sandbox spec.

            Returns:
                The fake session.
            """
            specs.append(spec)
            return session

    monkeypatch.setattr(vercel_dspy, "RemoteSandboxRuntime", Runtime)
    archive = tmp_path / "tree.tgz"
    archive.write_bytes(b"tree")
    payload = {
        "_budget_gateway_descriptor": {
            "url": "http://127.0.0.1:9876",
            "control_token": "control",
            "image": "backend@sha256:" + "a" * 64,
            "lifetime_seconds": 600,
        },
        "_repo_snapshot": {"archive": str(archive), "commit": "c" * 40, "proposer_hosts": proposer_hosts},
    }

    vercel_dspy.run_vercel_dspy(payload, "job-hosts", queue.Queue(), "spawn")

    [spec] = specs
    assert tuple(spec.allowed_hosts) == tuple(proposer_hosts)
    assert spec.network_disabled is offline


@pytest.mark.parametrize(
    ("preflight", "granted", "expected"),
    [(True, False, ()), (False, True, tuple(sorted(proposer_hosts(None))))],
)
def test_a_real_broker_opens_the_setup_check_box_offline_and_the_run_box_with_its_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preflight: bool, granted: bool, expected: tuple[str, ...]
) -> None:
    """Open a setup check's box offline without a grant, and a run's box with its granted hosts.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Pytest monkeypatch.
        preflight: Whether the job is a setup check.
        granted: Whether the broker grants the proposer hosts.
        expected: The hosts the box must open with.
    """
    broker = SandboxBroker(FakeRuntime(), image=IMAGE, max_lifetime_seconds=600)
    hosts = proposer_hosts(None)
    if granted:
        broker.allow_guest_hosts(hosts)

    def handle(_self: Any, action: str, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        """Send each control call straight to the real broker.

        Args:
            _self: The runtime, which the fake ignores.
            action: The control action.
            payload: The control payload.
            **_kwargs: The other options, which the fake ignores.

        Returns:
            The broker's answer.
        """
        return broker.handle(action, payload)

    monkeypatch.setattr(remote_sandbox.RemoteSandboxRuntime, "_request", handle)
    archive = tmp_path / "tree.tgz"
    archive.write_bytes(b"tree")
    payload: dict[str, Any] = {
        "_budget_gateway_descriptor": {
            "url": "http://127.0.0.1:9",
            "control_token": "control",
            "image": IMAGE,
            "lifetime_seconds": 300,
        },
        "_repo_snapshot": {"archive": str(archive), "commit": "c" * 40, "proposer_hosts": list(hosts)},
    }
    if preflight:
        payload["_preflight"] = {"scope": "evaluation", "identity": "setup"}
    events: queue.Queue[dict[str, Any]] = queue.Queue()

    vercel_dspy.run_vercel_dspy(payload, "job-broker", events, "spawn")

    [spec] = broker._runtime.specs
    assert tuple(spec.allowed_hosts) == expected
    assert spec.network_disabled is (not expected)
    messages = [str(item) for item in events.queue]
    assert not any("cannot open network access" in message for message in messages)


def test_a_guest_box_that_stops_at_its_deadline_is_an_infrastructure_interruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session stop at the end of the guest box lifetime is the run deadline (SKY-586).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    event = stopped_box_event(monkeypatch, lifetime=180, elapsed=181)

    assert event["type"] == "error"
    assert event["failure_kind"] == INFRASTRUCTURE_INTERRUPTION
    assert event["error_type"] == "InfrastructureInterruptionError"
    assert event["error"] == vercel_dspy.DEADLINE_MESSAGE.format(lifetime=180)


def test_a_guest_box_that_stops_exactly_at_its_deadline_is_an_infrastructure_interruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session stop when the elapsed time equals the guest box lifetime is the run deadline.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    event = stopped_box_event(monkeypatch, lifetime=180, elapsed=180)

    assert event["type"] == "error"
    assert event["failure_kind"] == INFRASTRUCTURE_INTERRUPTION
    assert event["error_type"] == "InfrastructureInterruptionError"
    assert event["error"] == vercel_dspy.DEADLINE_MESSAGE.format(lifetime=180)


def test_a_guest_box_that_stops_before_its_deadline_keeps_the_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A session stop before the guest box lifetime ends is not the deadline and stays a failure.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    event = stopped_box_event(monkeypatch, lifetime=180, elapsed=120)

    assert event["type"] == "error"
    assert event["failure_kind"] == DETERMINISTIC_FAILURE
    assert event["error"] == SESSION_STOPPED
