"""Step recovery: a run continues from its last committed step (SKY-711).

The job lease and ``execution_generation`` are the fence. When a worker
dies, the orphan sweep bumps the generation and requeues the run; the next
claim bumps it again. A step that the old worker commits after that fails
its flush with :class:`~core.storage.step_log_store.StepFenceError`, so only
the steps of the live generation land.

The step log's worker id is the pod name, the same value as
``jobs.claimed_by``. A pod that restarts under its name recovers its own
packs in :func:`open_step_log`. No other worker reads a dead worker's
packs: their steps never became durable, the sweep's generation bump fences
them, and the next worker continues from the last committed step. When a
dead pod comes back under a new name, its unapplied packs stay in object
storage, and no worker applies or removes them.

A run that waits frees its worker: :func:`park_run` moves it to ``paused``
with no lease and stores ``wake_at`` in its recovery state. The orphan sweep
moves it back to ``pending`` once ``wake_at`` passes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ..storage.base import ObjectStore
from ..storage.models import AgentStepModel, JobModel
from ..storage.remote import WAITING_PHASE
from ..storage.step_log_store import StepLogStore


def open_step_log(engine: Engine, objects: ObjectStore, worker_id: str) -> StepLogStore:
    """Open this worker's step log and apply the packs that its last crash left behind.

    Args:
        engine: Engine with the jobs and step log tables.
        objects: Where the packs are.
        worker_id: The pod name, the same value as ``jobs.claimed_by``.

    Returns:
        The open step log.
    """
    store = StepLogStore(engine, objects, worker_id)
    store.recover()
    return store


def last_committed_step(engine: Engine, store: StepLogStore, run_id: str, agent_id: str) -> dict[str, Any] | None:
    """Return the agent's newest committed step, the point its next worker continues from.

    Only stored rows count: a buffered step of a dead worker is not
    committed, and the fence keeps a stale one from ever landing.

    Args:
        engine: Engine with the step log tables.
        store: The step log that reads the step body.
        run_id: The job id.
        agent_id: The agent whose log to read.

    Returns:
        The step fields and ``body``, or ``None`` when the agent committed no step.
    """
    with Session(engine) as session:
        step_id = session.scalar(
            select(AgentStepModel.step_id)
            .where(AgentStepModel.run_id == run_id, AgentStepModel.agent_id == agent_id)
            .order_by(AgentStepModel.seq.desc())
            .limit(1)
        )
    return None if step_id is None else store.get_step(step_id)


def park_run(engine: Engine, optimization_id: str, *, expected_generation: int, wake_at: datetime) -> bool:
    """Move an active run to the wait state: no lease, a new generation, and ``wake_at``.

    The generation bump fences each later commit of the parked worker. The
    finished leg's duration goes into ``accumulated_runtime_seconds``, so the
    wait does not count as run time.

    The API treats ``paused`` as a terminal status, so today a user cannot
    cancel a waiting run. Before an engine calls this function, the cancel
    path must accept a ``paused`` run whose recovery phase is ``waiting``.

    Args:
        engine: Engine with the jobs table.
        optimization_id: The run to park.
        expected_generation: The generation the parking worker owns.
        wake_at: When the orphan sweep makes the run claimable again. It must
            carry a time zone.

    Returns:
        Whether the run was parked; ``False`` when another owner changed it first.

    Raises:
        ValueError: When ``wake_at`` has no time zone.
    """
    # astimezone() reads a naive time as host local time, which moves the wake time on a non-UTC host.
    if wake_at.tzinfo is None or wake_at.utcoffset() is None:
        raise ValueError("wake_at must carry a time zone")
    now = datetime.now(UTC)
    with Session(engine) as session:
        job = session.scalar(
            select(JobModel)
            .where(
                JobModel.optimization_id == optimization_id,
                JobModel.status.in_(("validating", "running")),
                JobModel.execution_generation == expected_generation,
            )
            .with_for_update()
        )
        if job is None:
            return False
        if job.started_at is not None:
            started = job.started_at if job.started_at.tzinfo else job.started_at.replace(tzinfo=UTC)
            job.accumulated_runtime_seconds = float(job.accumulated_runtime_seconds or 0.0) + max(
                0.0, (now - started).total_seconds()
            )
        job.started_at = None
        job.status = "paused"
        job.execution_generation = expected_generation + 1
        job.claimed_by = None
        job.claimed_at = None
        job.lease_expires_at = None
        job.message = "Waiting"
        job.recovery = {
            "state": "recovering",
            "phase": WAITING_PHASE,
            "wake_at": wake_at.astimezone(UTC).isoformat(),
            "reason": "The run waits and holds no worker. It continues at its wake time.",
        }
        session.commit()
    return True


def still_owns_run(engine: Engine, optimization_id: str, expected_generation: int) -> bool:
    """Return whether the run is still active at the generation that the caller owns.

    A park can commit and then raise, for example when the connection drops
    after the commit. The run can then wake and be claimed again before this
    read, so the question is whether the caller still owns the run, not
    whether its park is still visible.

    Args:
        engine: Engine with the jobs table.
        optimization_id: The run that the caller tried to park.
        expected_generation: The generation the parking worker owned.

    Returns:
        ``True`` when the run is validating or running at ``expected_generation``,
        the state before the park.
    """
    with Session(engine) as session:
        row = session.execute(
            select(JobModel.status, JobModel.execution_generation).where(JobModel.optimization_id == optimization_id)
        ).one_or_none()
    if row is None:
        return False
    return row.status in ("validating", "running") and row.execution_generation == expected_generation
