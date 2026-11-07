"""Tests for the worker's metric-hardening job: gate first, stripped attacker tree, reservation, no idle sandbox."""

from __future__ import annotations

import io
import tarfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from core.i18n_keys import I18nKey
from core.models.hardening import HardeningJob
from core.service_gateway.optimization.blackbox.hardening import Candidate, decide_fix, new_job, record_verdict
from core.service_gateway.optimization.blackbox.repo_constraints import (
    CONSTRAINTS_FILE,
    ConstraintsError,
    read_constraints,
)
from core.service_gateway.optimization.blackbox.repo_tree import REPO_SNAPSHOT_KEY, strip_archive
from core.worker.hardening_job import HardeningWorkspace, required_reservation, run_hardening_job
from core.worker.repo_gate import GateFailure, RepoGateError
from core.worker.repo_staging import StagedRepository

_TOML = b"""
[metric]
files = ["eval/metric.py"]
exploits = "eval/exploits"
reference = "eval/reference.py"
"""
_FILES = {
    CONSTRAINTS_FILE: _TOML,
    "bot/app.py": b"x = 1\n",
    "eval/metric.py": b"def score(): return 1\n",
    "eval/reference.py": b"x = 2\n",
    "eval/exploits/old/patch.diff": b"--- a\n+++ b\n",
    "eval/exploits/old/why.md": b"Old trick.\n",
}
_PAYLOAD = {
    "target": {
        "kind": "repo",
        "repo": {"repository": "octo/bot", "branch": "main", "editable_paths": ["bot"], "secrets": []},
    }
}
_EXPLOIT = {"label": "exploit", "confidence": 0.95, "reason": "Games the metric."}
_FAILURE = GateFailure(
    key="[proposer] tools",
    place="local",
    reason="optimize.toml sets [proposer] tools.",
    code=I18nKey.REPO_GATE_UNENFORCED_LOCAL,
)


def _archive(path: Path, files: dict[str, bytes], links: dict[str, str] | None = None) -> Path:
    """Pack files the way the parent ships a fetched tree.

    Args:
        path: Where the archive goes.
        files: Root-relative path to bytes.
        links: Root-relative path to symbolic link target; fetch_snapshot keeps links.

    Returns:
        The archive.
    """
    with tarfile.open(path, "w:gz") as packed:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            packed.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            packed.addfile(info)
    return path


def _names(archive: Path) -> set[str]:
    """List the file names in an archive.

    Args:
        archive: The archive.

    Returns:
        Its member names.
    """
    with tarfile.open(archive) as packed:
        return {member.name for member in packed.getmembers() if member.isfile()}


class Recorder:
    """Keeps the order of the job's side effects and the sandboxes it holds open."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        failures: list[GateFailure] | None = None,
        files: dict[str, bytes] | None = None,
        links: dict[str, str] | None = None,
    ) -> None:
        """Set up a recorder.

        Args:
            tmp_path: Scratch folder for the staged tree.
            failures: What the fake gate reports.
            files: The repository tree; the metric fixture when None.
            links: Symbolic links to add to the tree.
        """
        self.tmp_path = tmp_path
        self.files = files or _FILES
        self.links = links
        self.suite_ids: list[str] = []
        self.failures = failures or []
        self.events: list[str] = []
        self.open_sandboxes = 0
        self.open_at_wait: int | None = None
        self.workspace: HardeningWorkspace | None = None
        self.staged: StagedRepository | None = None
        self.stage_kwargs: dict[str, Any] = {}
        self.held: list[float] = []
        self.agent_names: set[str] = set()
        self.gate_secrets: Any = None

    def reserve(self, job: HardeningJob, usd: float) -> None:
        """Record a budget hold.

        Args:
            job: The job.
            usd: The dollars held.
        """
        self.events.append("reserve")
        self.held.append(usd)

    def stage(self, payload: dict[str, Any], **kwargs: Any) -> tuple[dict[str, Any], StagedRepository]:
        """Stage the tree with the real strip helper, as stage_repository does.

        Args:
            payload: The run-shaped payload.
            **kwargs: username, binding_id and engine.

        Returns:
            The staged payload and repository.
        """
        self.events.append("stage")
        self.stage_kwargs = {"payload": payload, **kwargs}
        workdir = self.tmp_path / "staged"
        workdir.mkdir()
        full = _archive(workdir / "tree.tgz", self.files, self.links)
        constraints = read_constraints(full)
        assert constraints is not None
        hidden = (*constraints.metric.files, constraints.metric.exploits)
        agent = strip_archive(full, hidden, workdir / "agent-tree.tgz")
        snapshot = type("Snapshot", (), {"archive": full, "commit": "c0ffee", "readonly_paths": ()})()
        self.staged = StagedRepository(
            snapshot=snapshot, secrets={"API_KEY": "x"}, workdir=workdir, hidden_paths=hidden
        )
        target = {**payload["target"], "repo": {**payload["target"]["repo"], "commit": "c0ffee"}}
        return {**payload, "target": target, REPO_SNAPSHOT_KEY: {"archive": str(agent)}}, self.staged

    def gate(self, archive: Path, secrets: Any, *, place: str) -> list[GateFailure]:
        """Record the preflight gate.

        Args:
            archive: The full tree.
            secrets: The run's secret names.
            place: local or cloud.

        Returns:
            The configured failures.
        """
        self.events.append(f"gate:{place}")
        self.gate_secrets = secrets
        return self.failures

    @contextmanager
    def runner_factory(self, workspace: HardeningWorkspace, job: HardeningJob) -> Iterator[FakeRunner]:
        """Open a fake sandbox for one pass and close it on exit.

        Args:
            workspace: The pass's trees and metric paths.
            job: The job.

        Yields:
            A runner over the workspace.
        """
        self.events.append("sandbox")
        self.workspace = workspace
        self.agent_names = _names(workspace.agent_archive)
        self.suite_ids = sorted(path.parent.name for path in workspace.exploits_path.glob("*/patch.diff"))
        self.open_sandboxes += 1
        try:
            yield FakeRunner(self)
        finally:
            self.open_sandboxes -= 1


class FakeRunner:
    """A runner that finds one exploit, so the job waits for the user's review."""

    def __init__(self, recorder: Recorder) -> None:
        """Bind the runner to its recorder.

        Args:
            recorder: The recorder.
        """
        self.recorder = recorder

    def preflight(self, *, view: str) -> list[str]:
        """Pass the runner preflight.

        Args:
            view: What the attacker may see.

        Returns:
            No failures.
        """
        self.recorder.events.append("preflight")
        return []

    def attack(self, **kwargs: Any) -> tuple[list[Candidate], float]:
        """Return one candidate.

        Args:
            **kwargs: The round's inputs.

        Returns:
            One candidate and its cost.
        """
        self.recorder.events.append("attack")
        return [Candidate(kind="case", patch_diff="", why_md="Prints the answer key.")], 0.5

    def judge(self, **kwargs: Any) -> tuple[Any, float]:
        """Label the candidate an exploit.

        Args:
            **kwargs: The candidate and model.

        Returns:
            The verdict and its cost.
        """
        self.recorder.open_at_wait = self.recorder.open_sandboxes
        return _EXPLOIT, 0.1

    def draft_fix(self, **kwargs: Any) -> tuple[str, float]:
        """Draft a metric fix.

        Args:
            **kwargs: The exploits and model.

        Returns:
            The fix diff and its cost.
        """
        self.recorder.events.append("draft_fix")
        return "--- a/eval/metric.py\n+++ b/eval/metric.py\n", 0.2

    def score(self, **kwargs: Any) -> float:
        """Score the base or an exploit on the fixed metric.

        Args:
            **kwargs: The fix and the exploit.

        Returns:
            The same score for every input.
        """
        return 0.5

    def reference_score(self, **kwargs: Any) -> float:
        """Score the reference solution.

        Args:
            **kwargs: The fix and the reference.

        Returns:
            The reference score.
        """
        return 0.5

    def apply_fix(self, *, fix_diff: str) -> None:
        """Record the approved fix.

        Args:
            fix_diff: The fix.
        """
        self.recorder.events.append("apply_fix")


def _run(recorder: Recorder, job: HardeningJob, *, local_mode: bool) -> HardeningJob:
    """Run the job with the recorder's fakes.

    Args:
        recorder: The recorder.
        job: The job.
        local_mode: Whether the worker is local.

    Returns:
        The job after the pass.
    """
    return run_hardening_job(
        job,
        payload=_PAYLOAD,
        binding_id="budget-1",
        username="alice",
        engine=None,
        local_mode=local_mode,
        runner_factory=recorder.runner_factory,
        reserve=recorder.reserve,
        stage=recorder.stage,
        gate=recorder.gate,
    )


def _cloud_job() -> HardeningJob:
    """Build a three-round cloud job.

    Returns:
        The job.
    """
    return new_job(attacker_model="m", judge_model="j", place="cloud", rounds=3, round_cap_usd=2.0)


def test_the_preflight_gate_runs_before_any_sandbox(tmp_path: Path) -> None:
    """The gate runs on the staged tree first; a failure ends the job before a sandbox opens."""
    recorder = Recorder(tmp_path, failures=[_FAILURE])

    job = _run(recorder, _cloud_job(), local_mode=False)

    assert recorder.events == ["reserve", "stage", "gate:cloud"]
    assert job.status == "preflight_failed"
    assert job.preflight_failures == [_FAILURE.reason]
    assert job.reserved_usd == job.spent_usd == 0.0
    assert not recorder.staged.workdir.exists()


def test_a_passing_gate_opens_the_sandbox_and_runs_the_core_preflight(tmp_path: Path) -> None:
    """After the gate passes, the sandbox opens and the SKY-355 preflight runs inside it."""
    recorder = Recorder(tmp_path)

    _run(recorder, _cloud_job(), local_mode=False)

    assert recorder.events[:5] == ["reserve", "stage", "gate:cloud", "sandbox", "preflight"]


def test_the_attacker_tree_has_no_metric_files_or_exploits(tmp_path: Path) -> None:
    """The attacker gets the stripped tree; the trusted tree keeps the metric, the suite and the reference."""
    recorder = Recorder(tmp_path)

    job = _run(recorder, _cloud_job(), local_mode=False)

    workspace = recorder.workspace
    assert workspace is not None
    assert workspace.agent_archive == recorder.staged.workdir / "agent-tree.tgz"
    agent_names = recorder.agent_names
    assert "bot/app.py" in agent_names
    assert not any(name.startswith(("eval/metric.py", "eval/exploits")) for name in agent_names)
    assert workspace.exploits_path == workspace.trusted_root / "eval/exploits"
    assert workspace.reference == workspace.trusted_root / "eval/reference.py"
    assert workspace.commit == "c0ffee"
    assert "API_KEY" not in repr(workspace)
    assert job.suite_count == 1


def test_the_job_reads_the_default_exploits_folder_without_a_metric_section(tmp_path: Path) -> None:
    """A repository without [metric] uses the default suite folder and no reference."""
    recorder = Recorder(tmp_path, files={CONSTRAINTS_FILE: b"version = 1\n", "bot/app.py": b"x = 1\n"})

    _run(recorder, _cloud_job(), local_mode=False)

    workspace = recorder.workspace
    assert workspace is not None
    assert workspace.exploits_path == workspace.trusted_root / ".optimize/exploits"
    assert workspace.reference is None


def test_staging_gets_the_run_shaped_target_and_binding(tmp_path: Path) -> None:
    """The job stages its repository with the same inputs a run submit gives stage_repository."""
    recorder = Recorder(tmp_path)

    _run(recorder, _cloud_job(), local_mode=False)

    assert recorder.stage_kwargs == {"payload": _PAYLOAD, "username": "alice", "binding_id": "budget-1", "engine": None}


def test_a_cloud_job_holds_rounds_times_cap_before_paid_work(tmp_path: Path) -> None:
    """A cloud job holds every round's cap (D46) before it stages, gates or attacks."""
    recorder = Recorder(tmp_path)
    job = _cloud_job()

    _run(recorder, job, local_mode=False)

    assert required_reservation(job) == 6.0
    assert recorder.held == [6.0]
    assert recorder.events.index("reserve") < recorder.events.index("attack")


def test_a_local_job_holds_nothing(tmp_path: Path) -> None:
    """Local work is free, so a local job never asks the budget for a hold."""
    recorder = Recorder(tmp_path)
    job = new_job(attacker_model="m", judge_model="j", place="local", rounds=3, round_cap_usd=2.0)

    after = _run(recorder, job, local_mode=True)

    assert required_reservation(job) == 0.0
    assert recorder.held == []
    assert "gate:local" in recorder.events
    assert after.reserved_usd == 0.0


def test_a_refused_hold_fails_the_job_before_any_staging(tmp_path: Path) -> None:
    """When the budget cannot hold the caps, the job fails and no tree is fetched."""
    recorder = Recorder(tmp_path)

    def refuse(job: HardeningJob, usd: float) -> None:
        """Refuse the hold.

        Args:
            job: The job.
            usd: The dollars asked for.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("Not enough budget.")

    job = run_hardening_job(
        _cloud_job(),
        payload=_PAYLOAD,
        binding_id="budget-1",
        username="alice",
        engine=None,
        local_mode=False,
        runner_factory=recorder.runner_factory,
        reserve=refuse,
        stage=recorder.stage,
        gate=recorder.gate,
    )

    assert recorder.events == []
    assert (job.status, job.error, job.reserved_usd) == ("failed", "Not enough budget.", 0.0)


def test_a_waiting_job_holds_no_sandbox(tmp_path: Path) -> None:
    """The sandbox is open while the attacker works and closed once the job waits for review."""
    recorder = Recorder(tmp_path)

    job = _run(recorder, _cloud_job(), local_mode=False)

    assert job.status == "waiting_review"
    assert recorder.open_at_wait == 1
    assert recorder.open_sandboxes == 0
    assert not recorder.staged.workdir.exists()


@pytest.mark.parametrize("status", ["waiting_review", "waiting_fix", "done", "failed"])
def test_a_job_that_is_not_active_does_no_work(tmp_path: Path, status: str) -> None:
    """A waiting or ended job is returned as is: no hold, no fetch and no sandbox."""
    recorder = Recorder(tmp_path)
    job = _cloud_job().model_copy(update={"status": status})

    assert _run(recorder, job, local_mode=False) == job
    assert recorder.events == []


def test_a_resumed_job_stages_again_and_closes_its_sandbox(tmp_path: Path) -> None:
    """After the user's verdict, a new pass opens a fresh sandbox and closes it at the next wait."""
    first = Recorder(tmp_path / "first")
    (tmp_path / "first").mkdir()
    job = _run(first, _cloud_job(), local_mode=False)
    exploit = job.rounds_detail[-1].exploits[0]
    job = record_verdict(job, exploit.id, confirmed=True)
    second = Recorder(tmp_path / "second")
    (tmp_path / "second").mkdir()

    after = _run(second, job, local_mode=False)

    assert second.events == ["reserve", "stage", "gate:cloud", "sandbox", "draft_fix"]
    assert after.status == "waiting_fix"
    assert second.open_sandboxes == 0


def test_a_sandbox_that_fails_to_open_fails_the_job(tmp_path: Path) -> None:
    """A sandbox start error fails the job, releases its hold and still deletes the staged tree."""
    recorder = Recorder(tmp_path)

    @contextmanager
    def broken(workspace: HardeningWorkspace, job: HardeningJob) -> Iterator[FakeRunner]:
        """Fail to open a sandbox.

        Args:
            workspace: The pass's trees.
            job: The job.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("Sandbox quota reached.")
        yield FakeRunner(recorder)  # pragma: no cover

    job = run_hardening_job(
        _cloud_job(),
        payload=_PAYLOAD,
        binding_id="budget-1",
        username="alice",
        engine=None,
        local_mode=False,
        runner_factory=broken,
        reserve=recorder.reserve,
        stage=recorder.stage,
        gate=recorder.gate,
    )

    assert (job.status, job.error, job.reserved_usd) == ("failed", "Sandbox quota reached.", 0.0)
    assert not recorder.staged.workdir.exists()


def test_a_link_that_leaves_the_tree_fails_the_job(tmp_path: Path) -> None:
    """The trusted tree refuses a link out of the repository; the job fails, opens no sandbox and frees its hold."""
    recorder = Recorder(tmp_path, links={"bot/python": "/usr/bin/python3"})

    job = _run(recorder, _cloud_job(), local_mode=False)

    assert job.status == "failed"
    assert job.error
    assert job.reserved_usd == job.spent_usd == 0.0
    assert "sandbox" not in recorder.events
    assert not recorder.staged.workdir.exists()


@contextmanager
def _leaky(recorder: Recorder, workspace: HardeningWorkspace, job: HardeningJob) -> Iterator[FakeRunner]:
    """Open a fake sandbox that fails on close.

    Args:
        recorder: The recorder.
        workspace: The pass's trees.
        job: The job.

    Yields:
        A runner.

    Raises:
        RuntimeError: On exit, with a message that must not reach the job.
    """
    with recorder.runner_factory(workspace, job) as runner:
        yield runner
    raise RuntimeError("Sandbox stop failed: token=sk-secret")


def _run_leaky(recorder: Recorder, job: HardeningJob) -> HardeningJob:
    """Run a cloud pass whose sandbox fails to close.

    Args:
        recorder: The recorder.
        job: The job.

    Returns:
        The job after the pass.
    """
    return run_hardening_job(
        job,
        payload=_PAYLOAD,
        binding_id="budget-1",
        username="alice",
        engine=None,
        local_mode=False,
        runner_factory=lambda workspace, job: _leaky(recorder, workspace, job),
        reserve=recorder.reserve,
        stage=recorder.stage,
        gate=recorder.gate,
    )


def test_a_close_error_at_review_keeps_the_round_reviewable(tmp_path: Path) -> None:
    """A close error after the pass keeps waiting_review, so the user reviews round 1 and no new attack runs."""
    (tmp_path / "first").mkdir()
    first = Recorder(tmp_path / "first")

    job = _run_leaky(first, _cloud_job())

    assert job.status == "waiting_review"
    assert job.error == "A sandbox failed to close (RuntimeError)."
    assert "sk-secret" not in job.model_dump_json()
    assert job.spent_usd == pytest.approx(0.6)
    assert job.reserved_usd == pytest.approx(6.0)
    assert len(job.rounds_detail) == 1

    job = record_verdict(job, job.rounds_detail[0].exploits[0].id, confirmed=True)
    (tmp_path / "second").mkdir()
    second = Recorder(tmp_path / "second")
    after = _run(second, job, local_mode=False)

    assert "attack" not in second.events
    assert (after.status, after.error, after.round) == ("waiting_fix", None, 1)
    assert len(after.rounds_detail) == 1


def test_a_close_error_at_done_keeps_done(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A close error after a pass that ended the job does not turn done into failed."""
    recorder = Recorder(tmp_path)

    def finish(job: HardeningJob, **kwargs: Any) -> HardeningJob:
        """End the job as done.

        Args:
            job: The job.
            **kwargs: The runner and suite paths.

        Returns:
            A done copy of the job.
        """
        return job.model_copy(update={"status": "done", "spent_usd": 1.5, "reserved_usd": 1.5})

    monkeypatch.setattr("core.worker.hardening_job.run_until_wait", finish)

    job = _run_leaky(recorder, _cloud_job())

    assert (job.status, job.spent_usd) == ("done", 1.5)
    assert job.error == "A sandbox failed to close (RuntimeError)."
    assert recorder.open_sandboxes == 0
    assert not recorder.staged.workdir.exists()


def test_a_gate_that_raises_fails_the_job(tmp_path: Path) -> None:
    """A gate error ends the job as failed, frees the hold, opens no sandbox and removes the workdir."""
    recorder = Recorder(tmp_path)

    def broken(archive: Path, secret_names: Any, *, place: str) -> list[GateFailure]:
        """Fail inside the gate.

        Args:
            archive: The full tree.
            secret_names: The run's secret names.
            place: local or cloud.

        Raises:
            OSError: Always.
        """
        raise OSError("Tree unreadable.")

    recorder.gate = broken  # type: ignore[method-assign]

    job = _run(recorder, _cloud_job(), local_mode=False)

    assert (job.status, job.error) == ("failed", "Tree unreadable.")
    assert job.reserved_usd == job.spent_usd == 0
    assert "sandbox" not in recorder.events
    assert not recorder.staged.workdir.exists()


def test_the_gate_gets_secret_names_only(tmp_path: Path) -> None:
    """The preflight gate sees the secret names, never their values."""
    recorder = Recorder(tmp_path)

    _run(recorder, _cloud_job(), local_mode=False)

    assert recorder.gate_secrets == frozenset({"API_KEY"})


@pytest.mark.parametrize(
    ("error", "failures"),
    [
        (RepoGateError([_FAILURE]), [_FAILURE.reason]),
        (ConstraintsError("optimize.toml is not valid TOML."), ["optimize.toml is not valid TOML."]),
    ],
)
def test_a_staging_check_failure_ends_the_job_as_preflight_failed(
    tmp_path: Path, error: Exception, failures: list[str]
) -> None:
    """A hidden-file leak or a bad optimize.toml found while staging is a preflight failure with its reasons."""
    recorder = Recorder(tmp_path)

    def refuse(payload: dict[str, Any], **kwargs: Any) -> tuple[dict[str, Any], StagedRepository]:
        """Fail the staging checks.

        Args:
            payload: The run-shaped payload.
            **kwargs: username, binding_id and engine.

        Raises:
            Exception: The configured error.
        """
        raise error

    job = run_hardening_job(
        _cloud_job(),
        payload=_PAYLOAD,
        binding_id="budget-1",
        username="alice",
        engine=None,
        local_mode=False,
        runner_factory=recorder.runner_factory,
        reserve=recorder.reserve,
        stage=refuse,
        gate=recorder.gate,
    )

    assert job.status == "preflight_failed"
    assert job.preflight_failures == failures
    assert job.reserved_usd == 0.0
    assert "sandbox" not in recorder.events


def test_a_later_pass_restores_the_suite_and_fixes_of_earlier_rounds(tmp_path: Path) -> None:
    """Each pass stages a fresh tree, so the workspace writes back the fixed rounds' exploits and lists their fixes."""
    recorder = Recorder(tmp_path)
    job = _run(recorder, _cloud_job(), local_mode=False)
    first = job.rounds_detail[0].exploits[0]
    job = _run(recorder, record_verdict(job, first.id, confirmed=True), local_mode=False)
    assert job.status == "waiting_fix"
    job = _run(recorder, decide_fix(job, approve=True), local_mode=False)
    assert job.rounds_detail[0].status == "fixed"
    assert job.status == "waiting_review"
    second = job.rounds_detail[1].exploits[0]

    job = _run(recorder, record_verdict(job, second.id, confirmed=True), local_mode=False)

    assert recorder.suite_ids == sorted(["old", first.id])
    assert recorder.workspace is not None
    assert recorder.workspace.applied_fixes == (job.rounds_detail[0].fix.diff,)
    assert job.status == "waiting_fix"
