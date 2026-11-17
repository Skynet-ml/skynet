"""Tests for step recovery: the lease and generation fence, the resume cap, and ``wake_at`` (SKY-711).

Each test runs against a real job store and step log, on a SQLite file and,
when ``APP_TEST_DB_URL`` names a disposable PostgreSQL database, on a fresh
schema of it. A "killed" worker is a step log that stops being used: its
lease runs out, and its buffered steps never flush in its own generation.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

import core.storage.remote as remote_mod
from core.billing.budgets import BudgetService
from core.config import app_env
from core.storage.models import Base, BillingCustomerModel, JobModel
from core.storage.object_store import LocalObjectStore
from core.storage.remote import MAX_AUTOMATIC_RESUMES, RemoteDBJobStore
from core.storage.step_log_store import StepFenceError, StepLogStore
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


def _commit(log: StepLogStore, seq: int, generation: int, output: str = "ok") -> str:
    """Commit one command step of the test agent.

    Args:
        log: The step log.
        seq: The step position.
        generation: The generation the agent works under.
        output: The command output in the body.

    Returns:
        The step id.
    """
    return log.commit(
        run_id=RUN,
        agent_id=AGENT,
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
