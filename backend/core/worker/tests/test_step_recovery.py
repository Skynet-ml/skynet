"""Tests for step recovery: the lease and generation fence, the resume cap, and ``wake_at`` (SKY-711).

Each test runs against a real job store and step log, on a SQLite file and,
when ``APP_TEST_DB_URL`` names a disposable PostgreSQL database, on a fresh
schema of it. A "killed" worker is a step log that stops being used: its
lease runs out, and its buffered steps never flush in its own generation.
"""

from __future__ import annotations

import contextlib
import dataclasses
import pickle
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from gepa.core.state import GEPAState, ValsetEvaluation
from sqlalchemy import create_engine, event, text, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

import core.storage.remote as remote_mod
from core.api.auth import AuthenticatedUser, get_authenticated_user
from core.api.routers.optimizations import create_optimizations_router
from core.billing.budgets import BudgetService
from core.config import app_env, settings
from core.constants import WORKER_RESTART_CAUSE
from core.storage.models import Base, BillingCustomerModel, GepaCheckpointModel, JobModel
from core.storage.object_store import LocalObjectStore
from core.storage.remote import MAX_AUTOMATIC_RESUMES, RemoteDBJobStore
from core.storage.step_log_store import StepFenceError, StepLogStore
from core.worker.checkpoint_compat import checkpoint_manifest
from core.worker.step_recovery import last_committed_step, open_step_log, park_run, still_owns_run

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
        assert _automatic_resumes(store) == resume
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


@pytest.mark.parametrize(
    ("status", "offset", "owns"),
    [
        ("running", 0, True),
        ("validating", 0, True),
        ("paused", 1, False),
        ("pending", 2, False),
        ("running", 3, False),
    ],
)
def test_still_owns_run_is_true_only_for_an_active_run_at_the_owned_generation(
    store: StepJobStore, status: str, offset: int, owns: bool
) -> None:
    """Only a run still active at the owned generation belongs to the worker; a parked, woken or reclaimed run does not."""
    _funded_run(store)
    generation = _claim(store, "pod-a")
    with Session(store.engine) as session:
        session.execute(
            update(JobModel)
            .where(JobModel.optimization_id == RUN)
            .values(status=status, execution_generation=generation + offset)
        )
        session.commit()

    assert still_owns_run(store.engine, RUN, generation) is owns


def test_park_run_refuses_a_wake_time_with_no_time_zone(store: StepJobStore) -> None:
    """A naive wake time raises and leaves the run as it is, so the host time zone cannot move the wake."""
    _funded_run(store)
    generation = _claim(store, "pod-a")

    with pytest.raises(ValueError):
        park_run(store.engine, RUN, expected_generation=generation, wake_at=datetime(2026, 1, 1, 12, 0))
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


def _automatic_resumes(store: StepJobStore) -> int:
    """Read the run's automatic resume count from its row.

    Args:
        store: The job store.

    Returns:
        The ``automatic_resumes`` column of the run.
    """
    with Session(store.engine) as session:
        return int(session.query(JobModel.automatic_resumes).filter(JobModel.optimization_id == RUN).one()[0])


def _manual_resume(store: StepJobStore) -> None:
    """End the run as failed and resume it by hand, as the user's resume does.

    Args:
        store: The job store.
    """
    _set_job(store, status="failed")
    assert store.requeue_for_resume(RUN, budget_service=BudgetService(engine=store.engine)) is not None
    assert store.get_job(RUN)["status"] == "pending"


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
        automatic_resumes=MAX_AUTOMATIC_RESUMES,
        recovery={"state": "recovering", "phase": "requeue", "cause": WORKER_RESTART_CAUSE},
    )
    _kill(store)

    assert store.recover_orphaned_jobs() == 1

    job = store.get_job(RUN)
    assert job["status"] == "pending"
    assert job["attempts"] == MAX_AUTOMATIC_RESUMES
    assert _automatic_resumes(store) == MAX_AUTOMATIC_RESUMES


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


@pytest.mark.parametrize(
    "recovery",
    [
        None,
        {"wake_at": "2020-01-01T00:00:00+00:00"},
        {"state": "recovering", "phase": "waiting_for_usage", "wake_at": "2020-01-01T00:00:00+00:00"},
    ],
)
def test_the_wake_leaves_a_run_that_is_not_waiting_alone(store: StepJobStore, recovery: dict[str, str] | None) -> None:
    """A paused run with no waiting phase stays paused, even with a past ``wake_at``, while a waiting run wakes."""
    waiting = {"state": "recovering", "phase": "waiting", "wake_at": "2020-01-01T00:00:00+00:00"}
    for identifier, value in (("user-paused", recovery), ("waiting", waiting)):
        store.create_job(identifier)
        with Session(store.engine) as session:
            session.execute(
                update(JobModel).where(JobModel.optimization_id == identifier).values(status="paused", recovery=value)
            )
            session.commit()

    store.recover_orphaned_jobs()

    assert store.get_job("user-paused")["status"] == "paused"
    assert store.get_job("waiting")["status"] == "pending"


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
    _set_job(store, code_version=store._current_code_version)
    with Session(store.engine) as session:
        session.add(GepaCheckpointModel(optimization_id=RUN, data=b"state"))
        session.commit()

    assert store._wake_waiting_runs() == 1

    job = store.get_job(RUN)
    assert job["status"] == "pending"
    assert job["code_version"] == store._current_code_version


def test_waking_a_checkpoint_run_of_a_gone_worker_version_fails_it(store: StepJobStore) -> None:
    """A woken run pinned by its checkpoint to a worker version that is gone fails, as a requeue would fail it."""
    _funded_run(store)
    generation = _claim(store, "pod-a")
    assert park_run(store.engine, RUN, expected_generation=generation, wake_at=datetime.now(UTC))
    _set_job(store, code_version="old-version")
    with Session(store.engine) as session:
        session.add(GepaCheckpointModel(optimization_id=RUN, data=b"state"))
        session.commit()

    assert store._wake_waiting_runs() == 0

    job = store.get_job(RUN)
    assert job["status"] == "failed"
    assert job["stop_reason"] == "interrupted"
    assert job["message"] == (
        f"{remote_mod.ORPHANED_RUN_RECOVERY_FAILED_MESSAGE} "
        "No compatible worker version is available for this checkpoint."
    )
    assert job["recovery"] is None
    assert store.take_ended_orphans() == [(RUN, generation + 1)]


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


def test_manual_resumes_do_not_use_up_the_automatic_resume_cap(
    store: StepJobStore, step_logs: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With manual resumes mixed in, the run still gets four automatic resumes, and the fifth crash fails it."""
    monkeypatch.setattr(remote_mod.settings, "job_max_attempts", 10)
    _funded_run(store)
    generation = _claim(store, "pod-0")
    log = step_logs("pod-0")
    _commit(log, 0, generation)
    log.flush()

    _manual_resume(store)
    assert _automatic_resumes(store) == 0
    for resume in range(1, MAX_AUTOMATIC_RESUMES + 1):
        _claim(store, f"pod-{resume}")
        if resume == 3:
            _manual_resume(store)
            _claim(store, f"pod-{resume}-manual")
        _kill(store)
        assert store.recover_orphaned_jobs() == 1
        assert store.get_job(RUN)["status"] == "pending"
        assert _automatic_resumes(store) == resume

    job = store.get_job(RUN)
    assert job["attempts"] == MAX_AUTOMATIC_RESUMES + 2
    _claim(store, "pod-last")
    _kill(store)
    assert store.recover_orphaned_jobs() == 1
    job = store.get_job(RUN)
    assert job["status"] == "failed"
    assert f"limit of {MAX_AUTOMATIC_RESUMES} automatic resumes" in job["message"]
    assert _automatic_resumes(store) == MAX_AUTOMATIC_RESUMES


def test_automatic_resumes_use_up_the_manual_attempt_limit(
    store: StepJobStore, step_logs: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a manual limit of three attempts, automatic resumes use up the manual limit, not the reverse."""
    monkeypatch.setattr(remote_mod.settings, "job_max_attempts", 3)
    _funded_run(store)
    generation = _claim(store, "pod-0")
    log = step_logs("pod-0")
    _commit(log, 0, generation)
    log.flush()
    _kill(store)
    assert store.recover_orphaned_jobs() == 1
    _claim(store, "pod-1")
    _manual_resume(store)
    _claim(store, "pod-2")
    _kill(store)
    assert store.recover_orphaned_jobs() == 1
    assert store.get_job(RUN)["attempts"] == settings.job_max_attempts
    assert _automatic_resumes(store) == 2

    # The automatic cap counts only automatic resumes, so it still allows two more.
    for resume in (3, MAX_AUTOMATIC_RESUMES):
        _claim(store, f"pod-{resume}")
        _kill(store)
        assert store.recover_orphaned_jobs() == 1
        assert store.get_job(RUN)["status"] == "pending"
        assert _automatic_resumes(store) == resume

    _claim(store, "pod-last")
    # A crash stop with no worker restart cause counts against the manual limit.
    _set_job(store, status="failed", recovery={"state": "interrupted", "interrupted_budget_generation": 0})
    app = FastAPI()
    app.include_router(create_optimizations_router(job_store=store, get_worker_ref=lambda: None))
    # The run's payload names no owner, so only an admin reaches it, as in the API tests.
    admin = AuthenticatedUser(username="alice", role="admin", groups=("skynet-admins",))
    app.dependency_overrides[get_authenticated_user] = lambda: admin
    response = TestClient(app, raise_server_exceptions=False).post(f"/optimizations/{RUN}/resume")
    assert response.status_code == 409
    assert "maximum attempts" in response.json()["detail"]
    assert store.get_job(RUN)["status"] == "failed"


def test_a_rerun_resets_the_automatic_resume_count(store: StepJobStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """A rerun from scratch starts the run with no automatic resumes used."""
    # The step store creates no vector tables, so the rerun must not delete embeddings.
    monkeypatch.setattr(remote_mod.settings, "embeddings_enabled", False)
    _funded_run(store)
    _set_job(store, status="failed", attempts=MAX_AUTOMATIC_RESUMES, automatic_resumes=MAX_AUTOMATIC_RESUMES)

    assert store.requeue_for_rerun(RUN)

    assert store.get_job(RUN)["attempts"] == 0
    assert _automatic_resumes(store) == 0


def test_two_sweeps_cannot_both_resume_a_run_past_the_cap(
    store: StepJobStore, step_logs: Any, request: pytest.FixtureRequest
) -> None:
    """A sweep whose read of the count is stale fails the run with the cap error when another sweep used the last resume."""
    if "postgres" in request.node.callspec.id:
        pytest.skip("the row lock on PostgreSQL makes the second sweep wait; SQLite shows the guarded update")
    _funded_run(store)
    generation = _claim(store, "pod-a")
    log = step_logs("pod-a")
    _commit(log, 0, generation)
    log.flush()
    _set_job(store, automatic_resumes=MAX_AUTOMATIC_RESUMES - 1)
    _kill(store)
    listed = store._checkpoints.list_for_optimization

    def other_sweep_first(optimization_id: str) -> Any:
        """Let another sweep use the last automatic resume after this sweep read the row.

        Args:
            optimization_id: The run whose checkpoints the requeue lists.

        Returns:
            The run's checkpoints.
        """
        _set_job(store, automatic_resumes=MAX_AUTOMATIC_RESUMES)
        return listed(optimization_id)

    store._checkpoints.list_for_optimization = other_sweep_first  # type: ignore[method-assign]

    store.recover_orphaned_jobs()

    job = store.get_job(RUN)
    assert job["status"] == "failed"
    assert f"limit of {MAX_AUTOMATIC_RESUMES} automatic resumes" in job["message"]
    assert _automatic_resumes(store) == MAX_AUTOMATIC_RESUMES
    assert store.take_ended_orphans() == [(RUN, job["execution_generation"])]
    budget = BudgetService(engine=store.engine).get(job["execution_budget_id"], "owner")
    assert (budget.state, budget.blocked_reason) == ("closed", "job_failed")


# A worker claim keeps the generation, so only the generation guard protects the claimed run.
@pytest.mark.parametrize("claimed_by", [None, "pod-b"], ids=["requeued", "requeued-and-claimed"])
def test_a_stale_sweep_leaves_alone_a_run_that_another_sweep_requeued(
    store: StepJobStore,
    step_logs: Any,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    claimed_by: str | None,
) -> None:
    """A sweep that loses the last automatic resume to another sweep does not fail the run that sweep requeued.

    Args:
        store: The job store.
        step_logs: The factory of step logs for a pod.
        request: The pytest request, to read the store backend.
        monkeypatch: The pytest monkeypatch fixture.
        claimed_by: The worker that claimed the requeued run, or None when no worker claimed it yet.
    """
    if "postgres" in request.node.callspec.id:
        pytest.skip("the row lock on PostgreSQL makes the other sweep wait; SQLite shows the guarded update")
    _funded_run(store)
    generation = _claim(store, "pod-a")
    log = step_logs("pod-a")
    _commit(log, 0, generation)
    log.flush()
    _set_job(store, automatic_resumes=MAX_AUTOMATIC_RESUMES - 1)
    _kill(store)
    waive = BudgetService.waive_interrupted_work

    def other_sweep_requeues(self: BudgetService, *args: Any, **kwargs: Any) -> Any:
        """Let another sweep requeue the run with the last automatic resume after this sweep fenced it.

        Args:
            self: The budget service of this sweep.
            *args: The waiver's positional arguments.
            **kwargs: The waiver's keyword arguments.

        Returns:
            The waiver's result.
        """
        result = waive(self, *args, **kwargs)
        fenced = int(store.get_job(RUN)["execution_generation"])
        _set_job(
            store,
            status="running" if claimed_by else "pending",
            execution_generation=fenced + 1,
            automatic_resumes=MAX_AUTOMATIC_RESUMES,
            claimed_by=claimed_by,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=1) if claimed_by else None,
        )
        return result

    monkeypatch.setattr(BudgetService, "waive_interrupted_work", other_sweep_requeues)

    store.recover_orphaned_jobs()

    job = store.get_job(RUN)
    assert job["status"] == ("running" if claimed_by else "pending")
    with Session(store.engine) as session:
        row = session.get(JobModel, RUN)
        assert row is not None
        assert row.claimed_by == claimed_by
    assert _automatic_resumes(store) == MAX_AUTOMATIC_RESUMES
    assert store.take_ended_orphans() == []
    budget = BudgetService(engine=store.engine).get(job["execution_budget_id"], "owner")
    assert budget.blocked_reason is None


def test_a_stale_sweep_leaves_alone_a_run_that_the_user_cancelled(
    store: StepJobStore, step_logs: Any, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sweep that cannot count its resume does not fail a run that the user cancelled at the same generation.

    Args:
        store: The job store.
        step_logs: The factory of step logs for a pod.
        request: The pytest request, to read the store backend.
        monkeypatch: The pytest monkeypatch fixture.
    """
    if "postgres" in request.node.callspec.id:
        pytest.skip("the row lock on PostgreSQL makes the cancel wait; SQLite shows the guarded update")
    _funded_run(store)
    generation = _claim(store, "pod-a")
    log = step_logs("pod-a")
    _commit(log, 0, generation)
    log.flush()
    _set_job(store, automatic_resumes=MAX_AUTOMATIC_RESUMES - 1)
    _kill(store)
    waive = BudgetService.waive_interrupted_work
    read = BudgetService.get
    held: dict[str, int] = {}

    def waived(self: BudgetService, *args: Any, **kwargs: Any) -> Any:
        """Note that this sweep waived the interrupted work, so its next budget read is the last one.

        Args:
            self: The budget service of this sweep.
            *args: The waiver's positional arguments.
            **kwargs: The waiver's keyword arguments.

        Returns:
            The waiver's result.
        """
        held["waived"] = 1
        return waive(self, *args, **kwargs)

    def user_cancels(self: BudgetService, *args: Any, **kwargs: Any) -> Any:
        """Cancel the run as the cancel route does, after this sweep's last budget read.

        Args:
            self: The budget service of this sweep.
            *args: The read's positional arguments.
            **kwargs: The read's keyword arguments.

        Returns:
            The budget as this sweep read it, before the cancel.
        """
        result = read(self, *args, **kwargs)
        if held.pop("waived", None) is None:
            return result
        job = store.get_job(RUN)
        held["generation"] = int(job["execution_generation"])
        self.stop_admission(job["execution_budget_id"], "owner", reason="user_cancelled")
        assert store.update_job_if_status(
            RUN, ("pending", "validating", "running"), status="cancelled", completed_at=datetime.now(UTC)
        )
        # A cancel keeps the generation, so only the status guard protects the run. The
        # sweep's locked budget read before its claim now sees the cancel and stops this
        # sweep before the count write, which would take the path that ends the run
        # (SKY-814). test_a_user_stop_during_the_lost_count_write_wins_over_the_failure
        # covers a stop that lands after that read.
        _set_job(store, automatic_resumes=MAX_AUTOMATIC_RESUMES)
        return result

    monkeypatch.setattr(BudgetService, "waive_interrupted_work", waived)
    monkeypatch.setattr(BudgetService, "get", user_cancels)

    store.recover_orphaned_jobs()

    job = store.get_job(RUN)
    assert job["status"] == "cancelled"
    assert job["execution_generation"] == held["generation"]
    assert store.take_ended_orphans() == []
    budget = BudgetService(engine=store.engine).get(job["execution_budget_id"], "owner")
    assert (budget.state, budget.blocked_reason) == ("closed", "user_cancelled")


def test_a_cancel_after_the_last_read_is_not_lost_to_the_requeue_write(
    store: StepJobStore, step_logs: Any, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancel between the sweep's last read and its requeue write keeps the run cancelled (SKY-814).

    Args:
        store: The job store.
        step_logs: The factory of step logs for a pod.
        request: The pytest request, to read the store backend.
        monkeypatch: The pytest monkeypatch fixture.
    """
    if "postgres" in request.node.callspec.id:
        pytest.skip("the row lock on PostgreSQL makes the cancel wait; only SQLite has this window")
    _funded_run(store)
    generation = _claim(store, "pod-a")
    log = step_logs("pod-a")
    _commit(log, 0, generation)
    log.flush()
    _kill(store)
    waive = BudgetService.waive_interrupted_work
    read = BudgetService.get
    held: dict[str, int] = {}

    def waived(self: BudgetService, *args: Any, **kwargs: Any) -> Any:
        """Note that this sweep waived the interrupted work, so its next budget read is the last one.

        Args:
            self: The budget service of this sweep.
            *args: The waiver's positional arguments.
            **kwargs: The waiver's keyword arguments.

        Returns:
            The waiver's result.
        """
        held["waived"] = 1
        return waive(self, *args, **kwargs)

    def user_cancels(self: BudgetService, *args: Any, **kwargs: Any) -> Any:
        """Cancel the run as the cancel route does, after this sweep's last read and before its write.

        Args:
            self: The budget service of this sweep.
            *args: The read's positional arguments.
            **kwargs: The read's keyword arguments.

        Returns:
            The budget as this sweep read it, before the cancel.
        """
        result = read(self, *args, **kwargs)
        if held.pop("waived", None) is None:
            return result
        job = store.get_job(RUN)
        held["generation"] = int(job["execution_generation"])
        self.stop_admission(job["execution_budget_id"], "owner", reason="user_cancelled")
        assert store.update_job_if_status(
            RUN, ("pending", "validating", "running"), status="cancelled", completed_at=datetime.now(UTC)
        )
        return result

    monkeypatch.setattr(BudgetService, "waive_interrupted_work", waived)
    monkeypatch.setattr(BudgetService, "get", user_cancels)

    assert store.recover_orphaned_jobs() == 0

    job = store.get_job(RUN)
    assert job["status"] == "cancelled"
    assert job["recovery"] == INTERRUPTED
    assert job["execution_generation"] == held["generation"]
    assert _automatic_resumes(store) == 0
    assert store.take_ended_orphans() == []
    budget = BudgetService(engine=store.engine).get(job["execution_budget_id"], "owner")
    assert (budget.state, budget.blocked_reason) == ("closed", "user_cancelled")


def test_a_status_write_after_the_locked_read_is_not_lost_to_the_requeue_claim(
    store: StepJobStore, step_logs: Any, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancel status write after the sweep's locked budget read loses no race to the claim (SKY-814).

    The cancel writes only its status, so the locked read sees an open budget and only
    the status condition of the claim's compare-and-set keeps the run cancelled.

    Args:
        store: The job store.
        step_logs: The factory of step logs for a pod.
        request: The pytest request, to read the store backend.
        monkeypatch: The pytest monkeypatch fixture.
    """
    if "postgres" in request.node.callspec.id:
        pytest.skip("the row lock on PostgreSQL makes the cancel wait; only SQLite has this window")
    _funded_run(store)
    generation = _claim(store, "pod-a")
    log = step_logs("pod-a")
    _commit(log, 0, generation)
    log.flush()
    _kill(store)
    read = BudgetService.get
    held: dict[str, int] = {}

    def user_cancels(self: BudgetService, *args: Any, **kwargs: Any) -> Any:
        """Write the cancel status after the sweep's locked budget read, which passes its session.

        Args:
            self: The budget service of this sweep.
            *args: The read's positional arguments.
            **kwargs: The read's keyword arguments.

        Returns:
            The budget as this sweep read it, before the cancel.
        """
        result = read(self, *args, **kwargs)
        if kwargs.get("session") is None or held:
            return result
        held["generation"] = int(store.get_job(RUN)["execution_generation"])
        assert store.update_job_if_status(
            RUN, ("pending", "validating", "running"), status="cancelled", completed_at=datetime.now(UTC)
        )
        return result

    monkeypatch.setattr(BudgetService, "get", user_cancels)

    assert store.recover_orphaned_jobs() == 0

    assert "generation" in held
    job = store.get_job(RUN)
    assert job["status"] == "cancelled"
    assert job["execution_generation"] == held["generation"]
    assert _automatic_resumes(store) == 0
    assert store.take_ended_orphans() == []


def test_a_lost_requeue_keeps_the_recovery_marker_of_a_newer_generation(
    store: StepJobStore, step_logs: Any, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sweep whose requeue loses to a newer fence leaves that fence's marker alone (SKY-814).

    Args:
        store: The job store.
        step_logs: The factory of step logs for a pod.
        request: The pytest request, to read the store backend.
        monkeypatch: The pytest monkeypatch fixture.
    """
    if "postgres" in request.node.callspec.id:
        pytest.skip("the row lock on PostgreSQL makes the other fence wait; SQLite shows the guarded update")
    _funded_run(store)
    generation = _claim(store, "pod-a")
    log = step_logs("pod-a")
    _commit(log, 0, generation)
    log.flush()
    _kill(store)
    waive = BudgetService.waive_interrupted_work
    marker = {"state": "recovering", "phase": "waiting_for_usage", "execution_generation": 99}

    def other_fence(self: BudgetService, *args: Any, **kwargs: Any) -> Any:
        """Fence the run again as another writer does, after this sweep's own fence.

        Args:
            self: The budget service of this sweep.
            *args: The waiver's positional arguments.
            **kwargs: The waiver's keyword arguments.

        Returns:
            The waiver's result.
        """
        result = waive(self, *args, **kwargs)
        _set_job(store, execution_generation=int(store.get_job(RUN)["execution_generation"]) + 1, recovery=marker)
        return result

    monkeypatch.setattr(BudgetService, "waive_interrupted_work", other_fence)

    assert store.recover_orphaned_jobs() == 0

    job = store.get_job(RUN)
    assert job["status"] == "running"
    assert job["recovery"] == marker


# The marker a run keeps when the sweep gives it up to a user's stop. The sweep fenced
# budget generation 0, so a later manual resume waives generation 0 (SKY-406 D3).
INTERRUPTED = {"state": "interrupted", "interrupted_budget_generation": 0}
# The cancel and pause routes stop admission first, then compare-and-set the status.
USER_STOPS = [
    ("user_cancelled", ("pending", "validating", "running"), "cancelled"),
    ("user_paused", ("running", "pending"), "paused"),
]


def _manual_resume_waives(store: StepJobStore, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Resume the run by hand, as the user's resume does, and record the generations it waives.

    Args:
        store: The job store.
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        The interrupted budget generations that the manual resume waived.
    """
    waive = BudgetService.waive_interrupted_work
    waived: list[int] = []

    def spy(self: BudgetService, *args: Any, **kwargs: Any) -> Any:
        """Record the waived generation, then waive it.

        Args:
            self: The budget service of the resume.
            *args: The waiver's positional arguments.
            **kwargs: The waiver's keyword arguments.

        Returns:
            The waiver's result.
        """
        waived.append(kwargs["generation"])
        return waive(self, *args, **kwargs)

    monkeypatch.setattr(BudgetService, "waive_interrupted_work", spy)
    assert store.requeue_for_resume(RUN, budget_service=BudgetService(engine=store.engine)) is not None
    monkeypatch.undo()
    assert store.get_job(RUN)["status"] == "pending"
    return waived


@pytest.mark.parametrize(("reason", "expected", "status"), USER_STOPS, ids=["cancel", "pause"])
def test_a_user_stop_right_after_the_fence_commit_keeps_the_interrupted_generation(
    store: StepJobStore,
    step_logs: Any,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    expected: tuple[str, ...],
    status: str,
) -> None:
    """A stop just after the sweep's fence commit leaves an ``interrupted`` marker that a manual resume waives (SKY-814).

    Args:
        store: The job store.
        step_logs: The factory of step logs for a pod.
        monkeypatch: The pytest monkeypatch fixture.
        reason: The reason the user's route closes admission with.
        expected: The statuses that the route's status write requires.
        status: The status that the route writes.
    """
    _funded_run(store)
    generation = _claim(store, "pod-a")
    log = step_logs("pod-a")
    _commit(log, 0, generation)
    log.flush()
    _kill(store)
    fence = BudgetService.fence_generation
    held: dict[str, int] = {}

    def fenced(self: BudgetService, *args: Any, **kwargs: Any) -> Any:
        """Note that this sweep fenced the run, so its next commit is the fence commit.

        Args:
            self: The budget service of this sweep.
            *args: The fence's positional arguments.
            **kwargs: The fence's keyword arguments.

        Returns:
            The fence's result.
        """
        held["fenced"] = 1
        return fence(self, *args, **kwargs)

    def user_stops(session: Session) -> None:
        """Stop the run as the user's route does, once the sweep's fence commit ends.

        Args:
            session: The session that committed.
        """
        if held.pop("fenced", None) is None:
            return
        job = store.get_job(RUN)
        held["generation"] = int(job["execution_generation"])
        BudgetService(engine=store.engine).stop_admission(job["execution_budget_id"], "owner", reason=reason)
        assert store.update_job_if_status(RUN, expected, status=status, completed_at=datetime.now(UTC))

    monkeypatch.setattr(BudgetService, "fence_generation", fenced)
    event.listen(Session, "after_commit", user_stops)
    try:
        assert store.recover_orphaned_jobs() == 0
    finally:
        event.remove(Session, "after_commit", user_stops)
    monkeypatch.undo()

    job = store.get_job(RUN)
    assert job["status"] == status
    # A recovering marker would hide the status in the UI; an interrupted one keeps the crash waivable.
    assert job["recovery"] == INTERRUPTED
    assert job["execution_generation"] == held["generation"]
    assert store.take_ended_orphans() == []
    budget = BudgetService(engine=store.engine).get(job["execution_budget_id"], "owner")
    assert (budget.state, budget.blocked_reason) == ("closed", reason)
    assert _manual_resume_waives(store, monkeypatch) == [0]


@pytest.mark.parametrize("status_write", ["during_sweep", "never"])
@pytest.mark.parametrize(("reason", "expected", "status"), USER_STOPS, ids=["cancel", "pause"])
def test_a_sweep_does_not_reopen_a_budget_that_the_user_closed_before_its_resume(
    store: StepJobStore,
    step_logs: Any,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    status_write: str,
    reason: str,
    expected: tuple[str, ...],
    status: str,
) -> None:
    """A user stop that closes admission after the sweep fenced the run, but before its resume, ends the run.

    The route can stop between its admission close and its status write. Then
    the sweep writes the user's status itself, and the run never stays
    ``running`` (SKY-814).

    Args:
        store: The job store.
        step_logs: The factory of step logs for a pod.
        request: The pytest request, to read the store backend.
        monkeypatch: The pytest monkeypatch fixture.
        status_write: When the user's status write lands: inside the sweep, or never.
        reason: The reason the user's route closes admission with.
        expected: The statuses that the route's status write requires.
        status: The status that the route writes.
    """
    if status_write == "during_sweep" and "postgres" in request.node.callspec.id:
        pytest.skip("the row lock on PostgreSQL makes the status write wait for the sweep")
    _funded_run(store)
    generation = _claim(store, "pod-a")
    log = step_logs("pod-a")
    _commit(log, 0, generation)
    log.flush()
    _kill(store)
    waive = BudgetService.waive_interrupted_work

    def user_stops(self: BudgetService, *args: Any, **kwargs: Any) -> Any:
        """Close admission as the user's route does, after this sweep fenced the run.

        Args:
            self: The budget service of this sweep.
            *args: The waiver's positional arguments.
            **kwargs: The waiver's keyword arguments.

        Returns:
            The waiver's result.
        """
        result = waive(self, *args, **kwargs)
        self.stop_admission(store.get_job(RUN)["execution_budget_id"], "owner", reason=reason)
        if status_write == "during_sweep":
            assert store.update_job_if_status(RUN, expected, status=status, completed_at=datetime.now(UTC))
        return result

    monkeypatch.setattr(BudgetService, "waive_interrupted_work", user_stops)

    assert store.recover_orphaned_jobs() == 0
    monkeypatch.undo()
    ended = int(store.get_job(RUN)["execution_generation"])
    # Later sweeps leave the ended run alone.
    assert store.recover_orphaned_jobs() == 0
    assert store.recover_orphaned_jobs() == 0

    job = store.get_job(RUN)
    assert job["status"] == status
    assert job["execution_generation"] == ended
    # The sweep fenced the run before the user's stop; it must not leave a recovering marker.
    assert job["recovery"] == INTERRUPTED
    # The user stopped the run, so its owner needs no notification.
    assert store.take_ended_orphans() == []
    budget = BudgetService(engine=store.engine).get(job["execution_budget_id"], "owner")
    assert (budget.state, budget.blocked_reason) == ("closed", reason)
    assert _manual_resume_waives(store, monkeypatch) == [0]


@pytest.mark.parametrize("claimed", [False, True], ids=["requeued", "requeued-and-claimed"])
def test_a_cancel_after_the_requeue_commit_closes_the_budget(
    store: StepJobStore, step_logs: Any, claimed: bool
) -> None:
    """A cancel through the route after the sweep requeued the run ends it and closes its budget for good.

    Args:
        store: The job store.
        step_logs: The factory of step logs for a pod.
        claimed: Whether a worker claimed the requeued run before the cancel.
    """
    _funded_run(store)
    generation = _claim(store, "pod-a")
    log = step_logs("pod-a")
    _commit(log, 0, generation)
    log.flush()
    _kill(store)
    assert store.recover_orphaned_jobs() == 1
    assert store.get_job(RUN)["status"] == "pending"
    if claimed:
        _claim(store, "pod-b")

    response = _router_client(store).post(f"/optimizations/{RUN}/cancel")

    assert response.status_code == 200
    # A later sweep sees the cancelled run and its closed budget, and leaves both alone.
    _kill(store)
    store.recover_orphaned_jobs()
    job = store.get_job(RUN)
    assert job["status"] == "cancelled"
    budget = BudgetService(engine=store.engine).get(job["execution_budget_id"], "owner")
    assert (budget.state, budget.blocked_reason) == ("closed", "user_cancelled")


@pytest.mark.parametrize("action", ["cancel", "pause"])
def test_a_user_stop_while_the_sweep_waits_for_usage_ends_the_recovering_marker(
    store: StepJobStore, step_logs: Any, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """A stop through the route while the sweep waits for in-flight usage shows its status (SKY-814).

    Later sweeps skip the stopped run, so only the route's status write can end the
    ``recovering`` marker that hides the status in the UI.

    Args:
        store: The job store.
        step_logs: The factory of step logs for a pod.
        monkeypatch: The pytest monkeypatch fixture.
        action: The route the user calls.
    """
    _funded_run(store)
    generation = _claim(store, "pod-a")
    log = step_logs("pod-a")
    _commit(log, 0, generation)
    log.flush()
    service = BudgetService(engine=store.engine)
    budget_id = store.get_job(RUN)["execution_budget_id"]
    operation = service.reserve(
        budget_id,
        "owner",
        operation_key="in-flight",
        generation=0,
        phase="run",
        cost_kind="model",
        request_fingerprint="in-flight",
        price_snapshot={"version": "fixture-v1"},
        max_cents=3,
    )
    service.mark_dispatched(operation.id, "owner")
    _kill(store)
    assert store.recover_orphaned_jobs() == 0
    job = store.get_job(RUN)
    assert (job["status"], job["recovery"]["state"]) == ("running", "recovering")
    # The step log is the run's resumable state; the pause route asks for a checkpoint.
    monkeypatch.setattr(store, "has_compatible_gepa_checkpoint", lambda _run: True, raising=False)

    response = _router_client(store).post(f"/optimizations/{RUN}/{action}")

    assert response.status_code == 200
    monkeypatch.undo()
    _kill(store)
    store.recover_orphaned_jobs()
    job = store.get_job(RUN)
    assert job["status"] == ("cancelled" if action == "cancel" else "paused")
    assert job["recovery"] == INTERRUPTED
    assert store.take_ended_orphans() == []
    # The old pod's request settles; then the user's manual resume runs the run again.
    service.settle(operation.id, "owner", evidence_key="in-flight", actual_cents=3, evidence={"usage": "in-flight"})
    assert _manual_resume_waives(store, monkeypatch) == [0]


def _router_client(store: StepJobStore, worker: Any = None) -> TestClient:
    """Return a client of the optimizations router on the store, signed in as an admin.

    Args:
        store: The job store.
        worker: The in-process worker that the routes signal, or ``None``.

    Returns:
        The test client.
    """
    app = FastAPI()
    app.include_router(create_optimizations_router(job_store=store, get_worker_ref=lambda: worker))
    # The run's payload names no owner, so only an admin reaches it, as in the API tests.
    admin = AuthenticatedUser(username="alice", role="admin", groups=("skynet-admins",))
    app.dependency_overrides[get_authenticated_user] = lambda: admin
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(("reason", "expected", "status"), USER_STOPS, ids=["cancel", "pause"])
def test_a_user_stop_after_the_last_read_ends_the_run_before_the_claim(
    store: StepJobStore,
    step_logs: Any,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    expected: tuple[str, ...],
    status: str,
) -> None:
    """A stop that closes admission after the sweep's last budget read ends the run, not requeues it (SKY-814).

    The route's status write never lands, as when the API stops between its two
    writes. A worker would then claim a requeued run and fail on the closed admission.

    Args:
        store: The job store.
        step_logs: The factory of step logs for a pod.
        monkeypatch: The pytest monkeypatch fixture.
        reason: The reason the user's route closes admission with.
        expected: The statuses that the route's status write requires; unused here.
        status: The status that the sweep must write for the user.
    """
    _funded_run(store)
    generation = _claim(store, "pod-a")
    log = step_logs("pod-a")
    _commit(log, 0, generation)
    log.flush()
    _kill(store)
    waive = BudgetService.waive_interrupted_work
    read = BudgetService.get
    held: dict[str, int] = {}

    def waived(self: BudgetService, *args: Any, **kwargs: Any) -> Any:
        """Note that this sweep waived the interrupted work, so its next budget read is the last unlocked one.

        Args:
            self: The budget service of this sweep.
            *args: The waiver's positional arguments.
            **kwargs: The waiver's keyword arguments.

        Returns:
            The waiver's result.
        """
        held["waived"] = 1
        return waive(self, *args, **kwargs)

    def user_stops(self: BudgetService, *args: Any, **kwargs: Any) -> Any:
        """Close admission as the user's route does, after this sweep's last unlocked budget read.

        Args:
            self: The budget service of this sweep.
            *args: The read's positional arguments.
            **kwargs: The read's keyword arguments.

        Returns:
            The budget as this sweep read it, before the stop.
        """
        result = read(self, *args, **kwargs)
        if held.pop("waived", None) is None:
            return result
        job = store.get_job(RUN)
        held["generation"] = int(job["execution_generation"])
        BudgetService(engine=store.engine).stop_admission(job["execution_budget_id"], "owner", reason=reason)
        return result

    monkeypatch.setattr(BudgetService, "waive_interrupted_work", waived)
    monkeypatch.setattr(BudgetService, "get", user_stops)

    assert store.recover_orphaned_jobs() == 0
    monkeypatch.undo()

    job = store.get_job(RUN)
    assert job["status"] == status
    assert job["recovery"] == INTERRUPTED
    assert job["execution_generation"] == held["generation"]
    assert store.take_ended_orphans() == []
    budget = BudgetService(engine=store.engine).get(job["execution_budget_id"], "owner")
    assert (budget.state, budget.blocked_reason) == ("closed", reason)
    assert _manual_resume_waives(store, monkeypatch) == [0]


@pytest.mark.parametrize(("reason", "expected", "status"), USER_STOPS, ids=["cancel", "pause"])
def test_a_user_stop_during_the_lost_count_write_wins_over_the_failure(
    store: StepJobStore,
    step_logs: Any,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    expected: tuple[str, ...],
    status: str,
) -> None:
    """A stop that lands while the sweep loses its last automatic resume ends the run as the user asked (SKY-814).

    The sweep rolls back its requeue, and the rollback ends its budget row lock.
    The user's stop that closes admission then must not become a failed run with
    a failure notification.

    Args:
        store: The job store.
        step_logs: The factory of step logs for a pod.
        request: The pytest request, to read the store backend.
        monkeypatch: The pytest monkeypatch fixture.
        reason: The reason the user's route closes admission with.
        expected: The statuses that the route's status write requires; unused here.
        status: The status that the sweep must write for the user.
    """
    if "postgres" in request.node.callspec.id:
        pytest.skip("the budget row lock on PostgreSQL makes the stop wait for the rollback; SQLite shows the window")
    _funded_run(store)
    generation = _claim(store, "pod-a")
    log = step_logs("pod-a")
    _commit(log, 0, generation)
    log.flush()
    _set_job(store, automatic_resumes=MAX_AUTOMATIC_RESUMES - 1)
    _kill(store)
    read = BudgetService.get
    held: dict[str, int] = {}

    def user_stops(self: BudgetService, *args: Any, **kwargs: Any) -> Any:
        """Close admission and use up the last resume, after this sweep's locked read before its claim.

        Args:
            self: The budget service of this sweep.
            *args: The read's positional arguments.
            **kwargs: The read's keyword arguments.

        Returns:
            The budget as this sweep read it, before the stop.
        """
        result = read(self, *args, **kwargs)
        if kwargs.get("session") is None or held:
            return result
        job = store.get_job(RUN)
        held["generation"] = int(job["execution_generation"])
        BudgetService(engine=store.engine).stop_admission(job["execution_budget_id"], "owner", reason=reason)
        # Another sweep uses the last resume, so this sweep's count write misses and rolls back.
        _set_job(store, automatic_resumes=MAX_AUTOMATIC_RESUMES)
        return result

    monkeypatch.setattr(BudgetService, "get", user_stops)

    assert store.recover_orphaned_jobs() == 0
    monkeypatch.undo()

    job = store.get_job(RUN)
    assert job["status"] == status
    assert job["recovery"] == INTERRUPTED
    assert job["execution_generation"] == held["generation"]
    assert store.take_ended_orphans() == []
    budget = BudgetService(engine=store.engine).get(job["execution_budget_id"], "owner")
    assert (budget.state, budget.blocked_reason) == ("closed", reason)


def test_a_pause_after_the_requeue_commit_pauses_the_run(
    store: StepJobStore, step_logs: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pause whose running pre-check came before the sweep's requeue commit still pauses the run (SKY-814).

    Args:
        store: The job store.
        step_logs: The factory of step logs for a pod.
        monkeypatch: The pytest monkeypatch fixture.
    """
    _funded_run(store)
    generation = _claim(store, "pod-a")
    log = step_logs("pod-a")
    _commit(log, 0, generation)
    log.flush()
    _kill(store)

    def requeued(_run: str) -> bool:
        """Requeue the run as a sweep does, after the route's running pre-check.

        Args:
            _run: The run that the route checks.

        Returns:
            True: the step log is the run's resumable state.
        """
        assert store.recover_orphaned_jobs() == 1
        assert store.get_job(RUN)["status"] == "pending"
        return True

    monkeypatch.setattr(store, "has_compatible_gepa_checkpoint", requeued, raising=False)

    response = _router_client(store).post(f"/optimizations/{RUN}/pause")

    assert response.status_code == 200
    monkeypatch.undo()
    job = store.get_job(RUN)
    assert job["status"] == "paused"
    budget = BudgetService(engine=store.engine).get(job["execution_budget_id"], "owner")
    assert (budget.state, budget.blocked_reason) == ("closed", "user_paused")
    # No worker can claim the paused run and fail it on its closed budget.
    assert store.claim_next_job("pod-b", 60.0) is None


class _SweepFirstWorker:
    """A worker signal that lets a sweep run between the route's admission close and its status write."""

    def __init__(self, store: StepJobStore) -> None:
        """Keep the store that the sweep recovers.

        Args:
            store: The job store.
        """
        self.store = store

    def cancel_job(self, optimization_id: str) -> None:
        """Run the sweep, which sees the closed admission and writes the user's status itself.

        Args:
            optimization_id: The run that the route stops.
        """
        assert optimization_id == RUN
        assert self.store.recover_orphaned_jobs() == 0


@pytest.mark.parametrize(
    ("action", "status", "reason"),
    [
        ("cancel", "cancelled", "user_cancelled"),
        ("pause", "paused", "user_paused"),
        ("bulk-cancel", "cancelled", "user_cancelled"),
    ],
)
def test_a_user_stop_that_the_sweep_wrote_first_reports_success(
    store: StepJobStore, step_logs: Any, monkeypatch: pytest.MonkeyPatch, action: str, status: str, reason: str
) -> None:
    """A route whose status write loses to the sweep's write of the same status reports success (SKY-814).

    The MCP ``stop_run`` tool shares the cancel route's path.

    Args:
        store: The job store.
        step_logs: The factory of step logs for a pod.
        monkeypatch: The pytest monkeypatch fixture.
        action: The route the user calls.
        status: The status that the user's stop writes.
        reason: The reason the user's stop closes admission with.
    """
    _funded_run(store)
    generation = _claim(store, "pod-a")
    log = step_logs("pod-a")
    _commit(log, 0, generation)
    log.flush()
    _kill(store)
    # The step log is the run's resumable state; the pause route asks for a checkpoint.
    monkeypatch.setattr(store, "has_compatible_gepa_checkpoint", lambda _run: True, raising=False)
    client = _router_client(store, _SweepFirstWorker(store))

    if action == "bulk-cancel":
        response = client.post("/optimizations/bulk-cancel", json={"optimization_ids": [RUN]})
        assert response.status_code == 200
        assert response.json()["cancelled"] == [RUN]
    else:
        response = client.post(f"/optimizations/{RUN}/{action}")
        assert response.status_code == 200
        assert response.json()["status"] == status
    monkeypatch.undo()
    job = store.get_job(RUN)
    assert job["status"] == status
    assert job["recovery"] == INTERRUPTED
    budget = BudgetService(engine=store.engine).get(job["execution_budget_id"], "owner")
    assert (budget.state, budget.blocked_reason) == ("closed", reason)


@pytest.mark.parametrize("raises", ["resume_cap", "checkpoint"])
@pytest.mark.parametrize(("reason", "expected", "status"), USER_STOPS, ids=["cancel", "pause"])
def test_a_user_stop_wins_over_a_requeue_that_cannot_go_on(
    store: StepJobStore,
    step_logs: Any,
    raises: str,
    reason: str,
    expected: tuple[str, ...],
    status: str,
) -> None:
    """A stop whose status write never lands ends the run as the user asked, when the requeue raises (SKY-814).

    The requeue raises before it reads the budget, so only the sweep's failure
    write can see the user's closed admission.

    Args:
        store: The job store.
        step_logs: The factory of step logs for a pod.
        raises: Why the requeue cannot go on: the resume cap, or a checkpoint of a gone worker version.
        reason: The reason the user's route closes admission with.
        expected: The statuses that the route's status write requires; unused here.
        status: The status that the sweep must write for the user.
    """
    _funded_run(store)
    generation = _claim(store, "pod-a")
    log = step_logs("pod-a")
    _commit(log, 0, generation)
    log.flush()
    if raises == "resume_cap":
        _set_job(store, automatic_resumes=MAX_AUTOMATIC_RESUMES)
    else:
        _set_job(store, code_version="old-version")
        with Session(store.engine) as session:
            session.add(GepaCheckpointModel(optimization_id=RUN, data=b"state"))
            session.commit()
    job = store.get_job(RUN)
    BudgetService(engine=store.engine).stop_admission(job["execution_budget_id"], "owner", reason=reason)
    _kill(store)

    assert store.recover_orphaned_jobs() == 0

    job = store.get_job(RUN)
    assert job["status"] == status
    assert job["execution_generation"] == generation
    assert store.take_ended_orphans() == []
    budget = BudgetService(engine=store.engine).get(job["execution_budget_id"], "owner")
    assert (budget.state, budget.blocked_reason) == ("closed", reason)


@pytest.mark.parametrize(("reason", "expected", "status"), USER_STOPS, ids=["cancel", "pause"])
def test_a_user_stop_wins_over_an_expired_usage_wait(
    store: StepJobStore,
    step_logs: Any,
    reason: str,
    expected: tuple[str, ...],
    status: str,
) -> None:
    """A stop whose status write never lands ends the run as the user asked, when its usage wait expires (SKY-814).

    Args:
        store: The job store.
        step_logs: The factory of step logs for a pod.
        reason: The reason the user's route closes admission with.
        expected: The statuses that the route's status write requires; unused here.
        status: The status that the sweep must write for the user.
    """
    _funded_run(store)
    generation = _claim(store, "pod-a")
    log = step_logs("pod-a")
    _commit(log, 0, generation)
    log.flush()
    service = BudgetService(engine=store.engine)
    budget_id = store.get_job(RUN)["execution_budget_id"]
    operation = service.reserve(
        budget_id,
        "owner",
        operation_key="in-flight",
        generation=0,
        phase="run",
        cost_kind="model",
        request_fingerprint="in-flight",
        price_snapshot={"version": "fixture-v1"},
        max_cents=3,
    )
    service.mark_dispatched(operation.id, "owner")
    _kill(store)
    assert store.recover_orphaned_jobs() == 0
    job = store.get_job(RUN)
    assert (job["status"], job["recovery"]["phase"]) == ("running", "waiting_for_usage")
    service.stop_admission(budget_id, "owner", reason=reason)
    expired = datetime.now(UTC) - remote_mod.ORPHANED_USAGE_WAIT - timedelta(minutes=1)
    _set_job(store, recovery={**job["recovery"], "waiting_since": expired.isoformat()})
    _kill(store)

    assert store.recover_orphaned_jobs() == 0

    job = store.get_job(RUN)
    assert job["status"] == status
    assert job["recovery"] == INTERRUPTED
    assert store.take_ended_orphans() == []
    budget = service.get(budget_id, "owner")
    assert (budget.state, budget.blocked_reason) == ("closed", reason)


@pytest.mark.parametrize(("reason", "expected", "status"), USER_STOPS, ids=["cancel", "pause"])
def test_a_user_stop_wins_over_a_budget_stop_of_the_requeue(
    store: StepJobStore,
    step_logs: Any,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    expected: tuple[str, ...],
    status: str,
) -> None:
    """A stop that lands after the sweep read a spent budget ends the run as the user asked (SKY-814).

    Args:
        store: The job store.
        step_logs: The factory of step logs for a pod.
        monkeypatch: The pytest monkeypatch fixture.
        reason: The reason the user's route closes admission with.
        expected: The statuses that the route's status write requires; unused here.
        status: The status that the sweep must write for the user.
    """
    _funded_run(store)
    generation = _claim(store, "pod-a")
    log = step_logs("pod-a")
    _commit(log, 0, generation)
    log.flush()
    _kill(store)
    waive = BudgetService.waive_interrupted_work
    read = BudgetService.get
    held: dict[str, int] = {}

    def waived(self: BudgetService, *args: Any, **kwargs: Any) -> Any:
        """Note that this sweep waived the interrupted work, so its next budget read is the last unlocked one.

        Args:
            self: The budget service of this sweep.
            *args: The waiver's positional arguments.
            **kwargs: The waiver's keyword arguments.

        Returns:
            The waiver's result.
        """
        held["waived"] = 1
        return waive(self, *args, **kwargs)

    def spent_then_stopped(self: BudgetService, *args: Any, **kwargs: Any) -> Any:
        """Show a spent budget to this sweep, then close admission as the user's route does.

        Args:
            self: The budget service of this sweep.
            *args: The read's positional arguments.
            **kwargs: The read's keyword arguments.

        Returns:
            The budget as this sweep read it, before the stop, with nothing left.
        """
        result = read(self, *args, **kwargs)
        if held.pop("waived", None) is None:
            return result
        BudgetService(engine=store.engine).stop_admission(
            store.get_job(RUN)["execution_budget_id"], "owner", reason=reason
        )
        return dataclasses.replace(result, available_cents=0)

    monkeypatch.setattr(BudgetService, "waive_interrupted_work", waived)
    monkeypatch.setattr(BudgetService, "get", spent_then_stopped)

    assert store.recover_orphaned_jobs() == 0
    monkeypatch.undo()

    job = store.get_job(RUN)
    assert job["status"] == status
    assert job["recovery"] == INTERRUPTED
    assert store.take_ended_orphans() == []
    budget = BudgetService(engine=store.engine).get(job["execution_budget_id"], "owner")
    assert (budget.state, budget.blocked_reason) == ("closed", reason)


class _ParkingWorker:
    """A worker signal that parks the run, as a waiting engine does, in the pause route's window."""

    def __init__(self, store: StepJobStore, generation: int) -> None:
        """Keep the store and the generation that the worker owns.

        Args:
            store: The job store.
            generation: The generation that the parking worker owns.
        """
        self.store = store
        self.generation = generation

    def cancel_job(self, optimization_id: str) -> None:
        """Park the run with a wake time in the past, before the route's status write.

        Args:
            optimization_id: The run that the route pauses.
        """
        assert park_run(
            self.store.engine,
            optimization_id,
            expected_generation=self.generation,
            wake_at=datetime.now(UTC) - timedelta(seconds=1),
        )


def test_a_pause_that_loses_to_a_park_still_holds(
    store: StepJobStore, step_logs: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pause whose status write finds a parked run ends the wait, so the wake does not requeue it (SKY-814).

    Args:
        store: The job store.
        step_logs: The factory of step logs for a pod.
        monkeypatch: The pytest monkeypatch fixture.
    """
    _funded_run(store)
    generation = _claim(store, "pod-a")
    log = step_logs("pod-a")
    _commit(log, 0, generation)
    log.flush()
    # The step log is the run's resumable state; the pause route asks for a checkpoint.
    monkeypatch.setattr(store, "has_compatible_gepa_checkpoint", lambda _run: True, raising=False)

    response = _router_client(store, _ParkingWorker(store, generation)).post(f"/optimizations/{RUN}/pause")

    assert response.status_code == 200
    monkeypatch.undo()
    job = store.get_job(RUN)
    assert job["status"] == "paused"
    assert job["recovery"] is None
    store.recover_orphaned_jobs()
    assert store.get_job(RUN)["status"] == "paused"
    assert store.claim_next_job("pod-b", 60.0) is None


def test_a_cancel_that_loses_to_a_park_ends_the_wait(store: StepJobStore, step_logs: Any) -> None:
    """A cancel whose status write finds a parked run cancels it and ends the wait (SKY-814).

    Without this, the wake moves the run to pending on its closed budget.

    Args:
        store: The job store.
        step_logs: The factory of step logs for a pod.
    """
    _funded_run(store)
    generation = _claim(store, "pod-a")
    log = step_logs("pod-a")
    _commit(log, 0, generation)
    log.flush()

    response = _router_client(store, _ParkingWorker(store, generation)).post(f"/optimizations/{RUN}/cancel")

    assert response.status_code == 200
    job = store.get_job(RUN)
    assert job["status"] == "cancelled"
    assert job["recovery"] is None
    budget = BudgetService(engine=store.engine).get(job["execution_budget_id"], "owner")
    assert (budget.state, budget.blocked_reason) == ("closed", "user_cancelled")
    store.recover_orphaned_jobs()
    assert store.get_job(RUN)["status"] == "cancelled"
    assert store.claim_next_job("pod-b", 60.0) is None


def test_a_cancel_of_a_parked_run_leaves_its_budget_open(store: StepJobStore, step_logs: Any) -> None:
    """The cancel guard refuses a run that is already parked before it closes the budget (SKY-814).

    ``paused`` is a terminal status for the cancel guard, so the wake resumes
    the run on a budget that is still open.

    Args:
        store: The job store.
        step_logs: The factory of step logs for a pod.
    """
    _funded_run(store)
    generation = _claim(store, "pod-a")
    log = step_logs("pod-a")
    _commit(log, 0, generation)
    log.flush()
    _ParkingWorker(store, generation).cancel_job(RUN)

    response = _router_client(store).post(f"/optimizations/{RUN}/cancel")

    assert response.status_code == 409
    job = store.get_job(RUN)
    assert (job["status"], job["recovery"]["phase"]) == ("paused", remote_mod.WAITING_PHASE)
    budget = BudgetService(engine=store.engine).get(job["execution_budget_id"], "owner")
    assert budget.blocked_reason is None
