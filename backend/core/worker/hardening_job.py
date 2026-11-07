"""Run a metric-hardening job in the worker: gate, strip, reserve, then attack until the user must act.

The SKY-355 core holds the state logic. This module gives it a real
repository. It stages the tree the same way a run does, runs the full
preflight gate (SKY-352) before any sandbox opens, and gives the attacker only
the stripped tree (SKY-353). It reads ``[metric] exploits`` and ``[metric]
reference`` from the base commit's ``optimize.toml`` (SKY-351). A cloud job
holds rounds × cap before paid work (D46); a local job holds nothing. Every
sandbox closes before the job returns at a wait state, so a job that waits for
the user holds no sandbox.
"""

from __future__ import annotations

import logging
import tarfile
from collections.abc import Callable, Collection
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..models.hardening import HardeningJob
from ..service_gateway.optimization.blackbox.hardening import HardeningRunner, run_until_wait
from ..service_gateway.optimization.blackbox.repo_constraints import ConstraintsError, MetricSpec, read_constraints
from ..service_gateway.optimization.blackbox.repo_tree import REPO_SNAPSHOT_KEY
from .repo_gate import GateFailure, Place, RepoGateError, check_repository, sandbox_place
from .repo_staging import StagedRepository, stage_repository

logger = logging.getLogger(__name__)

_ACTIVE_STATUSES = frozenset({"preflight", "running"})


@dataclass(frozen=True)
class HardeningWorkspace:
    """The trees and metric paths one hardening pass works on.

    ``agent_archive`` is the stripped tree the attacker's sandbox unpacks.
    ``trusted_root`` is the full base-commit tree; only the parent's scorer
    reads it, so a metric fix and the exploit suite score with the real metric.
    Each pass stages a fresh tree, so the suite already holds the exploits of
    earlier fixed rounds. ``applied_fixes`` lists those rounds' approved fixes
    in order; the runner applies them before it scores. ``commit`` is the
    commit the tree came from; the caller pins later passes to it.
    """

    agent_archive: Path
    trusted_root: Path
    exploits_path: Path
    reference: Path | None
    commit: str | None
    applied_fixes: tuple[str, ...]
    secrets: dict[str, str] = field(repr=False)


RunnerFactory = Callable[[HardeningWorkspace, HardeningJob], AbstractContextManager[HardeningRunner]]
Reserve = Callable[[HardeningJob, float], None]


class Stage(Protocol):
    """Fetches and strips a repository tree, as :func:`stage_repository` does."""

    def __call__(
        self, payload: dict[str, Any], *, username: str, binding_id: str, engine: Any
    ) -> tuple[dict[str, Any], StagedRepository]:
        """Stage the payload's repository.

        Args:
            payload: The run-shaped payload.
            username: The job's owner.
            binding_id: The budget the vaulted secrets are bound to.
            engine: SQLAlchemy engine for the vaults.

        Returns:
            The staged payload and the staged repository.
        """


class Gate(Protocol):
    """The preflight gate, as :func:`check_repository` is."""

    def __call__(self, archive: Path, secret_names: Collection[str], *, place: Place) -> list[GateFailure]:
        """Check a full tree before any sandbox opens.

        Args:
            archive: The full base-commit tree.
            secret_names: The run's secret names; never values.
            place: Where the sandboxes run.

        Returns:
            One failure for each failed check.
        """


def required_reservation(job: HardeningJob) -> float:
    """Return the dollars a job must hold before paid work (D46).

    Args:
        job: The job.

    Returns:
        Rounds × the per-round cap for a cloud job; 0 for a local job.
    """
    return job.rounds * job.round_cap_usd if job.place == "cloud" else 0.0


def run_hardening_job(
    job: HardeningJob,
    *,
    payload: dict[str, Any],
    binding_id: str,
    username: str,
    engine: Any,
    local_mode: bool,
    runner_factory: RunnerFactory,
    reserve: Reserve,
    stage: Stage = stage_repository,
    gate: Gate = check_repository,
) -> HardeningJob:
    """Advance a hardening job on its repository until it waits for the user or ends.

    Args:
        job: The stored job.
        payload: A run-shaped payload whose ``target.repo`` names the
            repository, as a run submit gives :func:`stage_repository`.
        binding_id: The execution budget the vaulted secrets are bound to.
        username: The job's owner.
        engine: SQLAlchemy engine for the vaults.
        local_mode: Whether this worker runs jobs on the user's machine.
        runner_factory: Opens the attacker, judge and scorer sandboxes for one
            pass and closes them on exit.
        reserve: Holds the given dollars on the job's budget before paid work.
            Each pass calls it again, so it must hold once per job.
        stage: Fetches and strips the tree; :func:`stage_repository` by default.
        gate: The preflight gate; :func:`check_repository` by default.

    Returns:
        The job in a waiting or final state. A job that is not in preflight or
        running comes back unchanged, and no tree is fetched for it.
    """
    if job.status not in _ACTIVE_STATUSES:
        return job
    # A close error of an earlier pass is a note, not a state; this pass starts clean.
    job = job.model_copy(update={"error": None})
    hold = required_reservation(job)
    if hold:
        try:
            reserve(job, hold)
        except Exception as exc:
            return _end(job, "failed", error=str(exc) or type(exc).__name__)
    try:
        staged_payload, staged = stage(payload, username=username, binding_id=binding_id, engine=engine)
    except RepoGateError as exc:
        return _preflight_failed(job, [failure.reason for failure in exc.failures])
    except ConstraintsError as exc:
        return _preflight_failed(job, [str(exc)])
    except Exception as exc:
        return _end(job, "failed", error=str(exc) or type(exc).__name__)
    try:
        result: HardeningJob | None = None
        try:
            failures = gate(staged.snapshot.archive, frozenset(staged.secrets), place=sandbox_place(local_mode))
            if failures:
                return _preflight_failed(job, [failure.reason for failure in failures])
            workspace = _workspace(staged, staged_payload, job)
            with runner_factory(workspace, job) as runner:
                result = run_until_wait(
                    job, runner=runner, exploits_path=workspace.exploits_path, reference=workspace.reference
                )
            return result
        except Exception as exc:
            if result is None:
                return _end(job, "failed", error=str(exc) or type(exc).__name__)
            # The pass already reached its state. Failing it now would make a
            # retry pay for a new attack round and leave this round unreviewable.
            # Only the type is kept: the message may quote sandbox output.
            logger.warning("Hardening job %s: a sandbox failed to close (%s).", job.id, type(exc).__name__)
            if result.status != "failed":
                result.error = f"A sandbox failed to close ({type(exc).__name__})."
            return result
    finally:
        staged.close_scorers()
        staged.cleanup()


def _workspace(staged: StagedRepository, staged_payload: dict[str, Any], job: HardeningJob) -> HardeningWorkspace:
    """Unpack the trusted tree, resolve the metric paths, and restore earlier rounds' suite.

    Args:
        staged: The staged repository.
        staged_payload: The payload :func:`stage_repository` returned.
        job: The job, whose fixed rounds the suite must hold again.

    Returns:
        The workspace for one pass.
    """
    archive = Path(staged.snapshot.archive)
    trusted_root = staged.workdir / "trusted-tree"
    trusted_root.mkdir(parents=True, exist_ok=True)
    # The "data" filter refuses absolute paths, ".." parts and links that leave
    # the tree; the caller ends the job as failed when it refuses.
    with tarfile.open(archive) as tree:
        tree.extractall(trusted_root, filter="data")
    constraints = read_constraints(archive)
    metric = constraints.metric if constraints is not None else MetricSpec()
    exploits_path = trusted_root / metric.exploits
    fixed = [r for r in job.rounds_detail if r.status == "fixed"]
    for exploit in (e for r in fixed for e in r.exploits if e.user_verdict == "confirmed"):
        folder = exploits_path / exploit.id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "patch.diff").write_text(exploit.patch_diff, encoding="utf-8")
        (folder / "why.md").write_text(exploit.why_md, encoding="utf-8")
    return HardeningWorkspace(
        agent_archive=Path(staged_payload[REPO_SNAPSHOT_KEY]["archive"]),
        trusted_root=trusted_root,
        exploits_path=exploits_path,
        reference=trusted_root / metric.reference if metric.reference else None,
        commit=staged_payload.get("target", {}).get("repo", {}).get("commit"),
        applied_fixes=tuple(r.fix.diff for r in fixed if r.fix is not None),
        secrets=staged.secrets,
    )


def _preflight_failed(job: HardeningJob, failures: list[str]) -> HardeningJob:
    """End a job whose repository failed the preflight checks.

    Args:
        job: The job.
        failures: One message for each failed check.

    Returns:
        A changed copy of the job.
    """
    ended = _end(job, "preflight_failed")
    ended.preflight_failures = failures
    return ended


def _end(job: HardeningJob, status: str, *, error: str | None = None) -> HardeningJob:
    """End a job outside the core loop and release what it holds.

    Args:
        job: The job.
        status: ``failed`` or ``preflight_failed``.
        error: The failure message, if any.

    Returns:
        A changed copy of the job.
    """
    ended = job.model_copy(deep=True)
    ended.status = status  # type: ignore[assignment]
    ended.error = error
    ended.reserved_usd = ended.spent_usd
    return ended
