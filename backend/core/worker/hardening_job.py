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

import tarfile
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models.hardening import HardeningJob
from ..service_gateway.optimization.blackbox.hardening import HardeningRunner, run_until_wait
from ..service_gateway.optimization.blackbox.repo_constraints import MetricSpec, read_constraints
from ..service_gateway.optimization.blackbox.repo_tree import REPO_SNAPSHOT_KEY
from .repo_gate import check_repository, sandbox_place
from .repo_staging import StagedRepository, stage_repository

_ACTIVE_STATUSES = frozenset({"preflight", "running"})


@dataclass(frozen=True)
class HardeningWorkspace:
    """The trees and metric paths one hardening pass works on.

    ``agent_archive`` is the stripped tree the attacker's sandbox unpacks.
    ``trusted_root`` is the full base-commit tree; only the parent's scorer
    reads it, so a metric fix and the exploit suite score with the real metric.
    """

    agent_archive: Path
    trusted_root: Path
    exploits_path: Path
    reference: Path | None
    secrets: dict[str, str]


RunnerFactory = Callable[[HardeningWorkspace, HardeningJob], AbstractContextManager[HardeningRunner]]
Reserve = Callable[[HardeningJob, float], None]
Stage = Callable[..., tuple[dict[str, Any], StagedRepository]]
Gate = Callable[..., list[Any]]


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
    hold = required_reservation(job)
    if hold:
        try:
            reserve(job, hold)
        except Exception as exc:
            return _end(job, "failed", error=str(exc) or type(exc).__name__)
    try:
        staged_payload, staged = stage(payload, username=username, binding_id=binding_id, engine=engine)
    except Exception as exc:
        return _end(job, "failed", error=str(exc) or type(exc).__name__)
    try:
        failures = gate(staged.snapshot.archive, staged.secrets, place=sandbox_place(local_mode))
        if failures:
            job = _end(job, "preflight_failed")
            job.preflight_failures = [failure.reason for failure in failures]
            return job
        workspace = _workspace(staged, Path(staged_payload[REPO_SNAPSHOT_KEY]["archive"]))
        try:
            with runner_factory(workspace, job) as runner:
                return run_until_wait(
                    job, runner=runner, exploits_path=workspace.exploits_path, reference=workspace.reference
                )
        except Exception as exc:
            return _end(job, "failed", error=str(exc) or type(exc).__name__)
    finally:
        staged.close_scorers()
        staged.cleanup()


def _workspace(staged: StagedRepository, agent_archive: Path) -> HardeningWorkspace:
    """Unpack the trusted tree and resolve the metric paths inside it.

    Args:
        staged: The staged repository.
        agent_archive: The stripped tree that :func:`stage_repository` packed.

    Returns:
        The workspace for one pass.
    """
    archive = Path(staged.snapshot.archive)
    trusted_root = staged.workdir / "trusted-tree"
    trusted_root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tree:
        tree.extractall(trusted_root, filter="data")
    constraints = read_constraints(archive)
    metric = constraints.metric if constraints is not None else MetricSpec()
    return HardeningWorkspace(
        agent_archive=agent_archive,
        trusted_root=trusted_root,
        exploits_path=trusted_root / metric.exploits,
        reference=trusted_root / metric.reference if metric.reference else None,
        secrets=staged.secrets,
    )


def _end(job: HardeningJob, status: str, *, error: str | None = None) -> HardeningJob:
    """End a job before its loop starts and release what it holds.

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
