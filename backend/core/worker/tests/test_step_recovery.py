"""Tests for step recovery: the lease and generation fence, the resume cap, and ``wake_at`` (SKY-711).

Each test runs against a real job store and step log, on a SQLite file and,
when ``APP_TEST_DB_URL`` names a disposable PostgreSQL database, on a fresh
schema of it. A "killed" worker is a step log that stops being used: its
lease runs out, and its buffered steps never flush in its own generation.
"""

from __future__ import annotations

import contextlib
import pickle
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from gepa.core.state import GEPAState, ValsetEvaluation
from sqlalchemy import create_engine, text, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

import core.storage.remote as remote_mod
from core.billing.budgets import BudgetService
from core.config import app_env
from core.constants import WORKER_RESTART_CAUSE
from core.storage.models import Base, BillingCustomerModel, GepaCheckpointModel, JobModel
from core.storage.object_store import LocalObjectStore
from core.storage.remote import MAX_AUTOMATIC_RESUMES, RemoteDBJobStore
from core.storage.step_log_store import StepFenceError, StepLogStore
from core.worker.checkpoint_compat import checkpoint_manifest
from core.worker.step_recovery import last_committed_step, open_step_log, park_run

TEST_DB_URL = app_env("APP_TEST_DB_URL")
TREE = "c" * 64
AGENT = "agent"
RUN = "run-1"


class StepJobStore(RemoteDBJobStore):
    """A real job store on a given URL that skips the PostgreSQL bootstrap of ``__init__``."""

    def __init__(self, db_url: str, connect_args: dict[str, Any] | None = None) -> None:
        """Build the engine and create the tables that have no vector column.

        Args:
            db_url: SQLAlchemy URL.
            connect_args: Driver options, such as the PostgreSQL search path.
        """
        self._engine = create_engine(db_url, connect_args=connect_args or {}, poolclass=NullPool)
        # The vector columns need the pgvector extension, which a bare test schema does not see.
        tables = [
            table
            for table in Base.metadata.sorted_tables
            if not any(type(column.type).__name__ == "VECTOR" for column in table.columns)
        ]
        Base.metadata.create_all(self._engine, tables=tables)
        self._session_factory = sessionmaker(bind=self._engine)
        self._checkpoints = remote_mod.PostgresCheckpointBlobStore(self._engine)
        self._agent_runs = remote_mod.PostgresAgentRunStore(self._engine)


def make_sqlite_store(tmp_path: Path) -> StepJobStore:
    """Return a job store on a SQLite file, which the step log's flusher thread can share.

    Args:
        tmp_path: Pytest's temporary folder.

    Returns:
        The store.
    """
    return StepJobStore(f"sqlite:///{tmp_path / 'jobs.sqlite'}")


@pytest.fixture(params=["sqlite", "postgres"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[StepJobStore]:
    """Yield a job store on SQLite, or on a fresh PostgreSQL schema that the test drops after.

    Args:
        request: Pytest request with the database kind.
        tmp_path: Pytest's temporary folder.

    Yields:
        The store.
    """
    if request.param == "sqlite":
        yield make_sqlite_store(tmp_path)
        return
    if not TEST_DB_URL or not TEST_DB_URL.startswith("postgresql"):
        pytest.skip("APP_TEST_DB_URL not set to a postgresql:// URL")
    schema = f"steprec_{uuid.uuid4().hex[:10]}"
    admin = create_engine(TEST_DB_URL)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    job_store = StepJobStore(TEST_DB_URL, {"options": f"-csearch_path={schema}"})
    try:
        yield job_store
    finally:
        job_store.engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def objects(tmp_path: Path) -> LocalObjectStore:
    """Return the object store that holds the packs.

    Args:
        tmp_path: Pytest's temporary folder.

    Returns:
        The store.
    """
    return LocalObjectStore(tmp_path / "objects")


@pytest.fixture
def step_logs(store: StepJobStore, objects: LocalObjectStore) -> Iterator[Any]:
    """Yield a factory of step logs, one for each pod name, and close each one after the test.

    Args:
        store: The job store whose engine the logs use.
        objects: The pack store.

    Yields:
        ``open_log(pod)``, which opens a pod's step log as a restarted worker does.
    """
    opened: list[StepLogStore] = []

    def open_log(pod: str) -> StepLogStore:
        """Open one pod's step log with a long window, so only an explicit flush writes.

        Args:
            pod: The pod name.

        Returns:
            The step log.
        """
        log = StepLogStore(store.engine, objects, pod, flush_window_seconds=60.0)
        opened.append(log)
        return log

    yield open_log
    for log in opened:
        # A killed or fenced log can fail its last flush; the test already checked what matters.
        with contextlib.suppress(Exception):
            log.close()


def _funded_run(store: StepJobStore, run_id: str = RUN) -> None:
    """Create a pending run with reconciled funding, so the orphan sweep may requeue it.

    Args:
        store: The job store.
        run_id: The run id.
    """
    store.create_job(run_id)
    with Session(store.engine) as session:
        session.add(
            BillingCustomerModel(
                username="owner", stripe_customer_id="local-owner", balance_cents=50, grant_remaining=0
            )
        )
        session.commit()
    service = BudgetService(engine=store.engine)
    budget = service.create("owner", 20, idempotency_key=run_id)
    service.attach_to_job(budget.id, "owner", run_id, expected_revision=budget.revision)
    store.update_job(
        run_id,
        username="owner",
        payload={"execution_budget_id": budget.id, "execution_budget_generation": 0},
        execution_budget_id=budget.id,
        execution_budget_generation=0,
        code_version=None,
    )


def _claim(store: StepJobStore, pod: str, run_id: str = RUN) -> int:
    """Claim the run as a pod and return the generation that the pod owns.

    Args:
        store: The job store.
        pod: The claiming pod.
        run_id: The run that the claim must return.

    Returns:
        The run's execution generation after the claim.
    """
    record = store.claim_next_job(pod, 60.0)
    assert record is not None
    assert record["optimization_id"] == run_id
    store.update_job(run_id, status="running")
    return int(store.get_job(run_id)["execution_generation"])


def _kill(store: StepJobStore, run_id: str = RUN) -> None:
    """Let the run's lease run out, as when its worker dies.

    Args:
        store: The job store.
        run_id: The run.
    """
    with Session(store.engine) as session:
        session.execute(
            update(JobModel)
            .where(JobModel.optimization_id == run_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        session.commit()


def _commit(log: StepLogStore, seq: int, generation: int, output: str = "ok", agent: str = AGENT) -> str:
    """Commit one command step of an agent.

    Args:
        log: The step log.
        seq: The step position.
        generation: The generation the agent works under.
        output: The command output in the body.
        agent: The agent that commits.

    Returns:
        The step id.
    """
    return log.commit(
        run_id=RUN,
        agent_id=agent,
        seq=seq,
        kind="command",
        execution_generation=generation,
        grant_list_id="grants",
        body={"output": output},
        tree_id=TREE,
    )


def test_a_kill_between_steps_continues_after_the_last_committed_step(store: StepJobStore, step_logs: Any) -> None:
    """After a worker dies between steps, the sweep bumps the generation and the next worker continues."""
    _funded_run(store)
    old_generation = _claim(store, "pod-a")
    log_a = step_logs("pod-a")
    for seq in (0, 1):
        _commit(log_a, seq, old_generation)
    log_a.flush()
    _kill(store)

    assert store.recover_orphaned_jobs() == 1
    job = store.get_job(RUN)
    assert job["status"] == "pending"
    assert job["execution_generation"] > old_generation
    assert job["message"] == "Resuming after a worker restart"

    new_generation = _claim(store, "pod-b")
    log_b = step_logs("pod-b")
    last = last_committed_step(store.engine, log_b, RUN, AGENT)
    assert last is not None
    assert last["seq"] == 1
    step_id = _commit(log_b, last["seq"] + 1, new_generation)
    log_b.flush()
    log_b.durable(step_id, new_generation)


def test_a_kill_mid_step_drops_the_step_that_was_not_durable(store: StepJobStore, step_logs: Any) -> None:
    """A step still in the dead worker's buffer is not committed, and it never lands after the bump."""
    _funded_run(store)
    old_generation = _claim(store, "pod-a")
    log_a = step_logs("pod-a")
    _commit(log_a, 0, old_generation)
    log_a.flush()
    lost = _commit(log_a, 1, old_generation, output="lost")
    _kill(store)
    assert store.recover_orphaned_jobs() == 1
    new_generation = _claim(store, "pod-b")
    log_b = step_logs("pod-b")

    last = last_committed_step(store.engine, log_b, RUN, AGENT)
    assert last is not None
    assert last["seq"] == 0

    # The dead worker's flusher wakes up late: its step fails the fence.
    log_a.flush()
    with pytest.raises(StepFenceError):
        log_a.durable(lost, old_generation)
    redo = _commit(log_b, 1, new_generation, output="redo")
    log_b.flush()
    log_b.durable(redo, new_generation)
    step = log_b.get_step(redo)
    assert step is not None
    assert step["body"] == {"output": "redo"}


def test_a_crashed_pack_is_fenced_when_its_pod_restarts(
    store: StepJobStore, objects: LocalObjectStore, step_logs: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pod that crashed after its pack PUT recovers that pack on restart, and the fence drops its step."""
    _funded_run(store)
    old_generation = _claim(store, "pod-a")
    log_a = step_logs("pod-a")
    _commit(log_a, 0, old_generation)
    log_a.flush()
    _commit(log_a, 1, old_generation, output="lost")

    def crash(*args: Any) -> Any:
        """Stand for the process dying between the pack PUT and its transaction.

        Args:
            *args: The apply arguments.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("killed")

    monkeypatch.setattr(log_a, "_apply", crash)
    with pytest.raises(RuntimeError, match="killed"):
        log_a.flush()
    _kill(store)
    assert store.recover_orphaned_jobs() == 1
    _claim(store, "pod-b")

    restarted = open_step_log(store.engine, objects, "pod-a")
    try:
        last = last_committed_step(store.engine, restarted, RUN, AGENT)
    finally:
        restarted.close()
    assert last is not None
    assert last["seq"] == 0


def test_a_stale_worker_commit_after_the_bump_fails_with_the_fence_error(store: StepJobStore, step_logs: Any) -> None:
    """A commit of the old worker after the sweep's bump fails at its flush, then at its next commit."""
    _funded_run(store)
    old_generation = _claim(store, "pod-a")
    log_a = step_logs("pod-a")
    _kill(store)
    assert store.recover_orphaned_jobs() == 1

    stale = _commit(log_a, 0, old_generation)
    log_a.flush()
    with pytest.raises(StepFenceError) as raised:
        log_a.durable(stale, old_generation)
    assert raised.value.step_id == stale
    with pytest.raises(StepFenceError):
        _commit(log_a, 1, old_generation)


def test_the_fifth_automatic_resume_fails_the_run(store: StepJobStore, step_logs: Any) -> None:
    """A step log run resumes after each of four crashes, and the fifth crash fails it with a clear reason."""
    _funded_run(store)
    generation = _claim(store, "pod-0")
    log = step_logs("pod-0")
    _commit(log, 0, generation)
    log.flush()

    for resume in range(1, MAX_AUTOMATIC_RESUMES + 1):
        _kill(store)
        assert store.recover_orphaned_jobs() == 1
        job = store.get_job(RUN)
        assert job["status"] == "pending"
        assert job["attempts"] == resume
        _claim(store, f"pod-{resume}")

    _kill(store)
    assert store.recover_orphaned_jobs() == 1
    job = store.get_job(RUN)
    assert job["status"] == "failed"
    assert job["stop_reason"] == "interrupted"
    assert f"limit of {MAX_AUTOMATIC_RESUMES} automatic resumes" in job["message"]


def test_a_waiting_run_holds_no_lease_and_is_claimed_again_after_wake_at(store: StepJobStore, step_logs: Any) -> None:
    """A parked run frees its lease, stays parked until ``wake_at``, then the next sweep makes it claimable."""
    _funded_run(store)
    generation = _claim(store, "pod-a")
    log_a = step_logs("pod-a")

    assert park_run(store.engine, RUN, expected_generation=generation, wake_at=datetime.now(UTC) + timedelta(hours=1))
    job = store.get_job(RUN)
    assert job["status"] == "paused"
    assert job["recovery"]["phase"] == "waiting"
    assert job["execution_generation"] == generation + 1
    with Session(store.engine) as session:
        row = session.get(JobModel, RUN)
        assert row is not None
        assert (row.claimed_by, row.claimed_at, row.lease_expires_at) == (None, None, None)

    store.recover_orphaned_jobs()
    assert store.get_job(RUN)["status"] == "paused"
    assert store.claim_next_job("pod-b", 60.0) is None
    stale = _commit(log_a, 0, generation)
    log_a.flush()
    with pytest.raises(StepFenceError):
        log_a.durable(stale, generation)

    store.update_job(
        RUN, recovery={**job["recovery"], "wake_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}
    )
    store.recover_orphaned_jobs()
    store.recover_orphaned_jobs()
    job = store.get_job(RUN)
    assert job["status"] == "pending"
    assert job["recovery"] is None
    assert job["execution_generation"] == generation + 2
    assert _claim(store, "pod-b") == generation + 3


def test_park_run_refuses_a_stale_generation(store: StepJobStore) -> None:
    """A worker that no longer owns the generation cannot park the run."""
    _funded_run(store)
    generation = _claim(store, "pod-a")

    assert not park_run(store.engine, RUN, expected_generation=generation - 1, wake_at=datetime.now(UTC))
    assert store.get_job(RUN)["status"] == "running"


def _bill(store: StepJobStore, key: str, cents: int) -> None:
    """Settle one run-phase model call of the run's first budget generation.

    Args:
        store: The job store.
        key: The operation identity.
        cents: The charge.
    """
    service = BudgetService(engine=store.engine)
    budget_id = store.get_job(RUN)["execution_budget_id"]
    operation = service.reserve(
        budget_id,
        "owner",
        operation_key=key,
        generation=0,
        phase="run",
        cost_kind="model",
        request_fingerprint=key,
        price_snapshot={"version": "fixture-v1"},
        max_cents=cents,
    )
    service.mark_dispatched(operation.id, "owner")
    service.settle(operation.id, "owner", evidence_key=key, actual_cents=cents, evidence={"usage": key})
    # The waiver compares settle times with step times, so keep them apart.
    time.sleep(0.02)


def _set_job(store: StepJobStore, **values: Any) -> None:
    """Write job columns directly, as another worker or a race would.

    Args:
        store: The job store.
        **values: The columns to set.
    """
    with Session(store.engine) as session:
        session.execute(update(JobModel).where(JobModel.optimization_id == RUN).values(**values))
        session.commit()


def test_the_waiver_starts_at_the_agent_whose_newest_step_is_oldest(store: StepJobStore, step_logs: Any) -> None:
    """Work that settled after the oldest of the agents' newest steps is waived; work before it stays billed."""
    _funded_run(store)
    generation = _claim(store, "pod-a")
    log_a = step_logs("pod-a")
    _bill(store, "kept", 1)
    _commit(log_a, 0, generation, agent="first")
    log_a.flush()
    time.sleep(0.02)
    _bill(store, "repeated-by-first", 2)
    _commit(log_a, 0, generation, agent="second")
    log_a.flush()
    time.sleep(0.02)
    _bill(store, "repeated-by-both", 3)
    with Session(store.engine) as session:
        assert session.get(BillingCustomerModel, "owner").balance_cents == 44
    _kill(store)

    assert store.recover_orphaned_jobs() == 1

    assert store.get_job(RUN)["status"] == "pending"
    budget = BudgetService(engine=store.engine).get(store.get_job(RUN)["execution_budget_id"], "owner")
    assert budget.billed_cents == 1
    with Session(store.engine) as session:
        assert session.get(BillingCustomerModel, "owner").balance_cents == 49


def test_park_run_folds_the_finished_leg_into_the_run_time(store: StepJobStore) -> None:
    """The time the run ran before it parks goes into ``accumulated_runtime_seconds``, and the timer stops."""
    _funded_run(store)
    generation = _claim(store, "pod-a")
    _set_job(store, started_at=datetime.now(UTC) - timedelta(seconds=30), accumulated_runtime_seconds=5.0)

    assert park_run(store.engine, RUN, expected_generation=generation, wake_at=datetime.now(UTC))
    with Session(store.engine) as session:
        row = session.get(JobModel, RUN)
        assert row is not None
        assert 35.0 <= row.accumulated_runtime_seconds < 40.0
        assert row.started_at is None


@pytest.mark.parametrize("status", ["cancelled", "pending"])
def test_park_run_refuses_a_run_that_is_not_active(store: StepJobStore, status: str) -> None:
    """A run that is not validating or running is not parked, even with the worker's generation."""
    _funded_run(store)
    generation = _claim(store, "pod-a")
    _set_job(store, status=status)

    assert not park_run(store.engine, RUN, expected_generation=generation, wake_at=datetime.now(UTC))
    job = store.get_job(RUN)
    assert job["status"] == status
    assert job["execution_generation"] == generation


def test_a_step_log_resume_unpins_the_worker_version(store: StepJobStore, step_logs: Any) -> None:
    """A run of an old worker version resumes from its steps, and any current worker may claim it."""
    _funded_run(store)
    generation = _claim(store, "pod-a")
    _set_job(store, code_version="old-version")
    log_a = step_logs("pod-a")
    _commit(log_a, 0, generation)
    log_a.flush()
    _kill(store)

    assert store.recover_orphaned_jobs() == 1

    job = store.get_job(RUN)
    assert job["status"] == "pending"
    assert job["code_version"] is None


def test_waking_a_run_unpins_the_worker_version(store: StepJobStore) -> None:
    """A woken run carries no worker version, so a worker of the current version claims it."""
    _funded_run(store)
    generation = _claim(store, "pod-a")
    assert park_run(store.engine, RUN, expected_generation=generation, wake_at=datetime.now(UTC))
    _set_job(store, code_version="old-version")

    store.recover_orphaned_jobs()

    job = store.get_job(RUN)
    assert job["status"] == "pending"
    assert job["code_version"] is None


def test_a_worker_restart_does_not_count_against_the_resume_cap(store: StepJobStore, step_logs: Any) -> None:
    """A run at the cap still resumes after a planned worker restart, and its count stays the same."""
    _funded_run(store)
    generation = _claim(store, "pod-a")
    log_a = step_logs("pod-a")
    _commit(log_a, 0, generation)
    log_a.flush()
    _set_job(
        store,
        attempts=MAX_AUTOMATIC_RESUMES,
        recovery={"state": "recovering", "phase": "requeue", "cause": WORKER_RESTART_CAUSE},
    )
    _kill(store)

    assert store.recover_orphaned_jobs() == 1

    job = store.get_job(RUN)
    assert job["status"] == "pending"
    assert job["attempts"] == MAX_AUTOMATIC_RESUMES


def test_the_wake_skips_a_run_that_changed_after_the_read(store: StepJobStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """A run woken, claimed and parked again between the sweep's read and its update stays parked."""
    _funded_run(store)
    generation = _claim(store, "pod-a")
    assert park_run(store.engine, RUN, expected_generation=generation, wake_at=datetime.now(UTC))
    read = store._due_waiting_runs

    def race(now: datetime) -> list[tuple[str, int]]:
        """Read the due runs, then let another sweep and worker wake, claim and park the run.

        Args:
            now: The sweep time.

        Returns:
            The due runs as the first read saw them.
        """
        due = read(now)
        _set_job(store, status="pending", execution_generation=generation + 2, recovery=None)
        regeneration = _claim(store, "pod-b")
        assert park_run(
            store.engine, RUN, expected_generation=regeneration, wake_at=datetime.now(UTC) + timedelta(hours=1)
        )
        return due

    parked_generation = generation + 4
    monkeypatch.setattr(store, "_due_waiting_runs", race)
    assert store._wake_waiting_runs() == 0
    job = store.get_job(RUN)
    assert job["status"] == "paused"
    assert job["execution_generation"] == parked_generation


@pytest.mark.parametrize("wake_at", ["not a time", "2020-01-01T00:00:00"])
def test_a_bad_wake_at_is_skipped_and_other_runs_still_wake(store: StepJobStore, wake_at: str) -> None:
    """A waiting run with an unreadable or naive ``wake_at`` stays paused, and the sweep still wakes the others."""
    waiting = {"state": "recovering", "phase": "waiting", "reason": "test"}
    for identifier, value in (("bad", wake_at), ("good", "2020-01-01T00:00:00+00:00")):
        store.create_job(identifier)
        with Session(store.engine) as session:
            session.execute(
                update(JobModel)
                .where(JobModel.optimization_id == identifier)
                .values(status="paused", recovery={**waiting, "wake_at": value})
            )
            session.commit()

    store.recover_orphaned_jobs()

    assert store.get_job("bad")["status"] == "paused"
    assert store.get_job("good")["status"] == "pending"


def _save_checkpoint(store: StepJobStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the run a GEPA payload and a real checkpoint pinned to the current worker version.

    Args:
        store: The job store.
        monkeypatch: Pytest fixture that binds the current sandbox profile.
    """
    monkeypatch.setattr(remote_mod.settings, "vercel_sandbox_max_lifetime_seconds", 60.0)
    job = store.get_job(RUN)
    payload = {
        "strategy": {"mode": "single", "engine": "gepa"},
        "dataset": [{"x": "sample"}],
        "execution_runtime": "vercel",
        "execution_budget_id": job["execution_budget_id"],
        "execution_budget_generation": 0,
    }
    store.update_job(RUN, payload=payload, code_version=store._current_code_version)
    state = GEPAState({"predict": "seed"}, ValsetEvaluation({0: "answer"}, {0: 0.5}))
    state.i = 3
    data = pickle.dumps(state.__dict__)
    store.save_gepa_checkpoint(
        RUN, data, 3, manifest=checkpoint_manifest(data, payload, store.get_job(RUN)["code_version"])
    )


def test_the_waiver_starts_at_a_checkpoint_older_than_the_steps(
    store: StepJobStore, step_logs: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a checkpoint and steps, the earlier of the two bounds the waiver: work after the checkpoint is waived."""
    _funded_run(store)
    generation = _claim(store, "pod-a")
    _bill(store, "kept", 1)
    _save_checkpoint(store, monkeypatch)
    time.sleep(0.02)
    _bill(store, "after-checkpoint", 2)
    log_a = step_logs("pod-a")
    _commit(log_a, 0, generation)
    log_a.flush()
    time.sleep(0.02)
    _bill(store, "after-step", 3)
    _kill(store)

    assert store.recover_orphaned_jobs() == 1

    job = store.get_job(RUN)
    assert job["status"] == "pending"
    assert job["code_version"] == store._current_code_version
    budget = BudgetService(engine=store.engine).get(job["execution_budget_id"], "owner")
    assert budget.billed_cents == 1


def test_a_manual_resume_of_an_interrupted_step_log_run_resumes(store: StepJobStore, step_logs: Any) -> None:
    """A user resumes a run the sweep stopped as interrupted; it resumes from its steps on any worker version."""
    _funded_run(store)
    generation = _claim(store, "pod-a")
    log_a = step_logs("pod-a")
    _commit(log_a, 0, generation)
    log_a.flush()
    _set_job(
        store,
        status="failed",
        code_version="old-version",
        recovery={"state": "interrupted", "interrupted_budget_generation": 0},
    )

    assert store.requeue_for_resume(RUN, budget_service=BudgetService(engine=store.engine)) == 1

    job = store.get_job(RUN)
    assert job["status"] == "pending"
    assert job["attempts"] == 1
    assert job["code_version"] is None


def test_waking_a_run_with_a_checkpoint_keeps_the_worker_version(store: StepJobStore) -> None:
    """A woken run with a checkpoint stays pinned to the worker version that wrote the checkpoint."""
    _funded_run(store)
    generation = _claim(store, "pod-a")
    assert park_run(store.engine, RUN, expected_generation=generation, wake_at=datetime.now(UTC))
    _set_job(store, code_version="old-version")
    with Session(store.engine) as session:
        session.add(GepaCheckpointModel(optimization_id=RUN, data=b"state"))
        session.commit()

    assert store._wake_waiting_runs() == 1

    job = store.get_job(RUN)
    assert job["status"] == "pending"
    assert job["code_version"] == "old-version"


def test_waking_a_sandbox_image_run_with_a_checkpoint_unpins_the_worker_version(store: StepJobStore) -> None:
    """A woken run with a pinned sandbox image runs on any worker version, so its checkpoint does not pin it."""
    _funded_run(store)
    generation = _claim(store, "pod-a")
    assert park_run(store.engine, RUN, expected_generation=generation, wake_at=datetime.now(UTC))
    _set_job(store, code_version="old-version", sandbox_image="fixture@sha256:" + "a" * 64)
    with Session(store.engine) as session:
        session.add(GepaCheckpointModel(optimization_id=RUN, data=b"state"))
        session.commit()

    assert store._wake_waiting_runs() == 1

    job = store.get_job(RUN)
    assert job["status"] == "pending"
    assert job["code_version"] is None


def test_a_failed_wake_does_not_stop_the_orphan_recovery(
    store: StepJobStore, step_logs: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the wake raises, the sweep logs it and still requeues a run whose worker died."""
    _funded_run(store)
    generation = _claim(store, "pod-a")
    log_a = step_logs("pod-a")
    _commit(log_a, 0, generation)
    log_a.flush()
    _kill(store)

    def broken_wake() -> int:
        """Fail as a wake with a database error would.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("wake failed")

    monkeypatch.setattr(store, "_wake_waiting_runs", broken_wake)

    assert store.recover_orphaned_jobs() == 1
    assert store.get_job(RUN)["status"] == "pending"
