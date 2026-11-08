"""Verify managed DSPy execution transports results and recovery state without host execution."""

from __future__ import annotations

import base64
import json
import logging
import queue
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.billing.vercel_usage import proposer_hosts
from core.exceptions import DETERMINISTIC_FAILURE, INFRASTRUCTURE_INTERRUPTION, ServiceError
from core.service_gateway.optimization.blackbox import remote_sandbox
from core.service_gateway.optimization.blackbox.sandbox import CommandResult
from core.service_gateway.optimization.blackbox.sandbox_broker import SandboxBroker
from core.service_gateway.optimization.blackbox.tests.test_sandbox_broker import IMAGE, FakeRuntime
from core.worker import vercel_dspy
from core.worker.isolated_runner import SANDBOX_PROTOCOL


class FakeSession:
    """Emulate only the declared remote session transport with no provider calls."""

    def __init__(self) -> None:
        """Start an empty managed workspace."""
        self.files: dict[str, str] = {}
        self.closed = False
        self.path = "gepa_state.bin"
        self.steps: list[str] = []
        self.requirements_error: Exception | None = None

    def write_files(self, files: dict[str, str]) -> None:
        """Record the staged request."""
        self.steps.append("write_files")
        self.files.update(files)

    def write_codex_requirements(self) -> None:
        """Record the parent's requirements write, or fail it the way a bad read-back does."""
        self.steps.append("codex_requirements")
        if self.requirements_error is not None:
            raise self.requirements_error

    def run(self, command: str, **kwargs: Any) -> CommandResult:
        """Answer the protocol probe, then emit fragmented checkpoint and optimizer frames."""
        self.steps.append("probe" if command == vercel_dspy.PROTOCOL_PROBE else "optimizer")
        if command == vercel_dspy.PROTOCOL_PROBE:
            assert not self.files
            return CommandResult(0, stdout=f"{SANDBOX_PROTOCOL}\n")
        [request_path] = [path for path in self.files if path.endswith("/request.json")]
        assert list(self.files) == [request_path]
        assert command.startswith("PYTHONPATH=/app python3 -m core.worker.isolated_runner")
        request = json.loads(self.files[request_path])
        assert "_budget_gateway_descriptor" not in request["payload"]
        assert "runtime_identity" not in request
        assert request["protocol"] == SANDBOX_PROTOCOL
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
    """Mirror real event framing and close the managed session on success."""
    session = FakeSession()
    specs = []

    class Runtime:
        """Capture the requested managed image and network profile."""

        def __init__(self, url: str, token: str) -> None:
            """Accept only the parent's opaque descriptor."""
            assert token == "control"

        def open(self, spec: Any) -> FakeSession:
            """Return one fake paid session without any live execution."""
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


def _descriptor_payload(tmp_path: Path) -> dict[str, Any]:
    """Build a run payload that carries only the parent's opaque sandbox descriptor."""
    return {
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
    """Write the root-owned file after the protocol check and before any request or optimizer command."""
    session = FakeSession()
    monkeypatch.setattr(vercel_dspy, "RemoteSandboxRuntime", lambda *_args: SimpleNamespace(open=lambda _spec: session))
    events: queue.Queue = queue.Queue()

    vercel_dspy.run_vercel_dspy(_descriptor_payload(tmp_path), "job-req", events, "spawn")

    assert session.steps[:2] == ["probe", "codex_requirements"]
    assert session.steps.index("codex_requirements") < session.steps.index("write_files")
    assert session.steps[-1] == "optimizer"
    assert events.get_nowait()["type"] == "result"


def test_a_failed_codex_requirements_write_stops_the_run_before_the_optimizer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed: a missing or user-owned file means no request and no optimizer command."""
    session = FakeSession()
    session.requirements_error = ServiceError("Codex cannot start: the sandbox did not keep the file.")
    monkeypatch.setattr(vercel_dspy, "RemoteSandboxRuntime", lambda *_args: SimpleNamespace(open=lambda _spec: session))
    events: queue.Queue = queue.Queue()

    vercel_dspy.run_vercel_dspy(_descriptor_payload(tmp_path), "job-req-fail", events, "spawn")

    assert "optimizer" not in session.steps
    assert not session.files
    assert session.closed
    event = events.get_nowait()
    assert event["type"] == "error"
    assert "Codex cannot start" in json.dumps(event)


def test_remote_preflight_never_reads_submitted_checkpoint_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exclude supervisor checkpoint files from setup even if a private field reaches the runner."""
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
        """Keep the traceback the guest would print."""
        super().__init__()
        self.stderr = stderr

    def run(self, command: str, **kwargs: Any) -> CommandResult:
        """Pass the protocol probe, then stream only stderr and exit 1 without framing any event."""
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
    """Carry the guest's last stderr line into the failure and keep the traceback in the log."""
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


class ProbedSession(FakeSession):
    """Answer the protocol probe the way a given image would and record every command."""

    def __init__(self, probe: CommandResult) -> None:
        """Keep the image's probe answer."""
        super().__init__()
        self.probe = probe
        self.commands: list[str] = []

    def run(self, command: str, **kwargs: Any) -> CommandResult:
        """Return the probe answer; any later command means the request was sent."""
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
    ],
)
def test_incompatible_image_is_refused_before_the_request_is_sent(
    probe: CommandResult, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Name the stale image instead of letting its older models reject the request."""
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
    """Ship the fetched tree in upload-sized pieces and hand the guest their paths instead of the parent's file."""
    session = FakeSession()

    class Runtime:
        """Return the one fake session."""

        def __init__(self, url: str, token: str) -> None:
            """Accept the descriptor."""

        def open(self, spec: Any) -> FakeSession:
            """Return the fake session."""
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
        "_repo_snapshot": {"archive": str(archive), "commit": "c" * 40, "readonly_paths": [], "secret_names": ["K"]},
    }

    vercel_dspy.run_vercel_dspy(payload, "job-repo", queue.Queue(), "spawn")

    [request_path] = [path for path in session.files if path.endswith("/request.json")]
    snapshot = json.loads(session.files[request_path])["payload"]["_repo_snapshot"]
    assert snapshot["archive"] is None
    assert snapshot["secret_names"] == ["K"]
    assert len(snapshot["chunks"]) == 3
    assert b"".join(base64.b64decode(session.files[path]) for path in snapshot["chunks"]) == b"0123456789"
    assert str(archive) not in session.files[request_path]


@pytest.mark.parametrize(
    ("proposer_hosts", "offline"),
    [(["files.pythonhosted.org", "pypi.org", "registry.npmjs.org"], False), ([], True)],
)
def test_the_outer_box_carries_the_staged_proposer_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, proposer_hosts: list[str], offline: bool
) -> None:
    """Open the protected box with the proposer's granted hosts, and keep it offline when it has none (E57)."""
    session = FakeSession()
    specs = []

    class Runtime:
        """Capture the requested network profile."""

        def __init__(self, url: str, token: str) -> None:
            """Accept the descriptor."""

        def open(self, spec: Any) -> FakeSession:
            """Record the spec and return the fake session."""
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
    """Open a setup check's box offline without a grant, and a run's box with its granted hosts."""
    broker = SandboxBroker(FakeRuntime(), image=IMAGE, max_lifetime_seconds=600)
    hosts = proposer_hosts(None)
    if granted:
        broker.allow_guest_hosts(hosts)

    def handle(_self: Any, action: str, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        """Send each control call straight to the real broker."""
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
