"""Unit tests for RemoteDBJobStore against an in-memory SQLite engine.

SQLite is used to avoid requiring a live Postgres instance for the fast CI gate.
``SQLiteJobStore`` bypasses ``RemoteDBJobStore.__init__`` so the fast suite can
avoid PostgreSQL-only engine options and pgvector bootstrap calls.

Two additional behaviours depend on a real Postgres instance and are explicitly skipped
here with a clear reason:
  - UTC timezone offset is NOT preserved by SQLite's ``DateTime`` column (it stores the
    wall-clock value as a naive string).  Tests that need timezone-aware comparisons are
    structured to compare the time *value* only.
  - Production filters ``optimization_type`` through a top-level indexed column;
    SQLite exercises the same ORM column but not PostgreSQL's planner behaviour.
"""

from __future__ import annotations

import inspect
import pickle
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any

import pytest
from gepa.core.state import GEPAState, ValsetEvaluation
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import core.api.observability as observability_mod
import core.storage.remote as remote_mod
from core.billing.budgets import BudgetService
from core.constants import OPTIMIZATION_TYPE_TAGGING, WORKER_RESTART_CAUSE
from core.storage.base import JobStore
from core.storage.models import (
    Base,
    BillingCustomerModel,
    ExecutionBudgetModel,
    ExecutionOperationModel,
    GepaCheckpointModel,
    JobModel,
    OptimizationShareGrantModel,
    WalletLedgerModel,
)
from core.storage.remote import RemoteDBJobStore
from core.worker.checkpoint_compat import CheckpointCompatibilityError, checkpoint_manifest


class SQLiteJobStore(RemoteDBJobStore):
    """RemoteDBJobStore wired to an in-memory SQLite engine.

    Overrides only ``__init__`` to:
    - Use ``StaticPool`` (required for sqlite:///:memory: across multiple
      session factory calls to share one connection).
    - Skip PostgreSQL-only pgvector bootstrap calls.
    """

    def __init__(self, db_url: str = "sqlite:///:memory:") -> None:
        """Build an in-memory SQLite engine and create the ORM tables.

        Args:
            db_url: SQLAlchemy URL; defaults to in-memory SQLite.
        """
        self._engine = create_engine(
            db_url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self._engine)
        self._session_factory = sessionmaker(bind=self._engine)
        self._checkpoints = remote_mod.PostgresCheckpointBlobStore(self._engine)
        self._grid_pair_results = remote_mod.PostgresGridPairResultStore(self._engine)
        self._agent_runs = remote_mod.PostgresAgentRunStore(self._engine)


_RECOVERY_IMAGE = "fixture@sha256:" + "a" * 64


def _fund_checkpoint(store: SQLiteJobStore, optimization_id: str, monkeypatch: pytest.MonkeyPatch) -> BudgetService:
    """Attach reconciled funding and a real pinned checkpoint to an existing test run.

    Args:
        store: Private fixture database.
        optimization_id: Existing run to make genuinely recoverable.
        monkeypatch: Pytest fixture used to bind the current sandbox profile.

    Returns:
        Authoritative budget service for resume admission and generation fencing.
    """
    monkeypatch.setattr(remote_mod.settings, "dspy_sandbox_image", _RECOVERY_IMAGE)
    monkeypatch.setattr(remote_mod.settings, "vercel_sandbox_max_lifetime_seconds", 60.0)
    with Session(store.engine) as session:
        session.add(
            BillingCustomerModel(
                username="resume-owner", stripe_customer_id="local-resume", balance_cents=50, grant_remaining=0
            )
        )
        session.commit()
    service = BudgetService(engine=store.engine)
    budget = service.create("resume-owner", 20, idempotency_key=optimization_id)
    service.attach_to_job(budget.id, "resume-owner", optimization_id, expected_revision=budget.revision)
    payload = {
        "optimizer_name": "GEPA",
        "dataset": [{"x": "sample"}],
        "execution_runtime": "vercel",
        "execution_budget_id": budget.id,
        "execution_budget_generation": 0,
    }
    store.update_job(
        optimization_id,
        username="resume-owner",
        payload=payload,
        execution_budget_id=budget.id,
        execution_budget_generation=0,
    )
    state = GEPAState({"predict": "seed"}, ValsetEvaluation({0: "answer"}, {0: 0.5}))
    state.i = 3
    state.total_num_evals = 12
    data = pickle.dumps(state.__dict__)
    manifest = checkpoint_manifest(data, payload, store.get_job(optimization_id)["code_version"])
    store.save_gepa_checkpoint(
        optimization_id,
        data,
        3,
        manifest=manifest,
    )
    return service


def _parse_iso_datetime(value: str) -> datetime:
    """Parse an ISO timestamp that may use a trailing ``Z`` UTC marker."""
    return datetime.fromisoformat(value)


@pytest.fixture
def store() -> Iterator[SQLiteJobStore]:
    """Yield a fresh ``SQLiteJobStore`` and drop its tables afterwards."""
    s = SQLiteJobStore()
    yield s
    # Drop all tables so state doesn't leak between tests that share the same
    # in-memory engine handle (StaticPool reuses the connection).
    Base.metadata.drop_all(s.engine)


def test_create_job_returns_pending_status(store: SQLiteJobStore) -> None:
    """Create job returns pending status."""
    result = store.create_job("job-create-1")
    assert result["status"] == "pending"
    assert result["optimization_id"] == "job-create-1"


def test_create_job_with_estimate_stored(store: SQLiteJobStore) -> None:
    """Create job with estimate stored."""
    result = store.create_job("job-create-2", estimated_remaining_seconds=60.0)
    assert result["estimated_remaining_seconds"] == 60.0


def test_create_job_is_retrievable(store: SQLiteJobStore) -> None:
    """Create job is retrievable."""
    store.create_job("job-create-3")
    assert store.job_exists("job-create-3")


def test_create_job_created_at_is_iso_parseable(store: SQLiteJobStore) -> None:
    """Create job created at is iso parseable."""
    result = store.create_job("job-create-4")
    # Real Postgres TIMESTAMPTZ preserves the +00:00 offset; SQLite's DateTime
    # column stores naive datetimes, so we only assert the value round-trips.
    assert result["created_at"] is not None
    datetime.fromisoformat(result["created_at"])


def test_create_job_default_fields_are_empty(store: SQLiteJobStore) -> None:
    """Create job default fields are empty."""
    result = store.create_job("job-create-5")
    assert result["latest_metrics"] == {}
    assert result["payload_overview"] == {}
    assert result["result"] is None
    assert result["message"] is None
    assert result["started_at"] is None
    assert result["completed_at"] is None
    assert result["attempts"] == 0


def test_monthly_active_admission_allows_existing_identity_at_limit(
    store: SQLiteJobStore,
) -> None:
    """A full month blocks a new identity but never ejects an admitted one."""
    month_start = date(2026, 8, 1)

    assert store.admit_monthly_active_user("alice@example.com", month_start, 1) is True
    assert store.admit_monthly_active_user("alice@example.com", month_start, 1) is True
    assert store.admit_monthly_active_user("bob@example.com", month_start, 1) is False
    assert store.count_monthly_active_users(month_start) == 1


def test_get_job_raises_key_error_for_unknown(store: SQLiteJobStore) -> None:
    """Get job raises key error for unknown."""
    with pytest.raises(KeyError, match="no-such-job"):
        store.get_job("no-such-job")


def test_get_job_returns_correct_record(store: SQLiteJobStore) -> None:
    """Get job returns correct record."""
    store.create_job("job-get-1")
    store.update_job("job-get-1", status="running")
    job = store.get_job("job-get-1")
    assert job["optimization_id"] == "job-get-1"
    assert job["status"] == "running"


def test_job_exists_true_after_create(store: SQLiteJobStore) -> None:
    """Job exists true after create."""
    store.create_job("job-exists-1")
    assert store.job_exists("job-exists-1") is True


def test_job_exists_false_for_missing(store: SQLiteJobStore) -> None:
    """Job exists false for missing."""
    assert store.job_exists("missing-id") is False


def test_update_job_status_field(store: SQLiteJobStore) -> None:
    """Update job status field."""
    store.create_job("job-upd-1")
    store.update_job("job-upd-1", status="running")
    assert store.get_job("job-upd-1")["status"] == "running"


def test_update_job_message_field(store: SQLiteJobStore) -> None:
    """Update job message field."""
    store.create_job("job-upd-2")
    store.update_job("job-upd-2", message="all good")
    assert store.get_job("job-upd-2")["message"] == "all good"


def test_update_job_multiple_fields_at_once(store: SQLiteJobStore) -> None:
    """Update job multiple fields at once."""
    store.create_job("job-upd-3")
    store.update_job("job-upd-3", status="success", message="done", estimated_remaining_seconds=0.0)
    job = store.get_job("job-upd-3")
    assert job["status"] == "success"
    assert job["message"] == "done"
    assert job["estimated_remaining_seconds"] == 0.0


def test_update_job_datetime_string_parsed_and_stored(store: SQLiteJobStore) -> None:
    """Update job datetime string parsed and stored."""
    store.create_job("job-upd-4")
    ts_str = "2026-04-14T10:00:00+00:00"
    store.update_job("job-upd-4", started_at=ts_str)
    job = store.get_job("job-upd-4")
    # The value round-trips through SQLite which loses timezone suffix,
    # but the wall-clock time must be preserved.
    started_at = job["started_at"]
    assert isinstance(started_at, str)
    stored = _parse_iso_datetime(started_at)
    expected = datetime(2026, 4, 14, 10, 0, 0)
    assert stored.replace(tzinfo=None) == expected


def test_update_job_latest_metrics_merges(store: SQLiteJobStore) -> None:
    """Update job latest metrics merges."""
    store.create_job("job-upd-5")
    store.update_job("job-upd-5", latest_metrics={"acc": 0.8})
    store.update_job("job-upd-5", latest_metrics={"loss": 0.2})
    metrics = store.get_job("job-upd-5")["latest_metrics"]
    # RemoteDBJobStore merges dicts rather than replacing
    assert metrics.get("acc") == 0.8
    assert metrics.get("loss") == 0.2


def test_update_job_json_result_roundtrips(store: SQLiteJobStore) -> None:
    """Update job json result roundtrips."""
    store.create_job("job-upd-6")
    payload: dict[str, Any] = {"weights": [1, 2, 3], "config": {"k": "v"}}
    store.update_job("job-upd-6", result=payload)
    assert store.get_job("job-upd-6")["result"] == payload


def test_delete_job_removes_the_job(store: SQLiteJobStore) -> None:
    """Delete job removes the job."""
    store.create_job("job-del-1")
    store.delete_job("job-del-1")
    assert not store.job_exists("job-del-1")


def test_delete_job_cascades_logs(store: SQLiteJobStore) -> None:
    """Delete job cascades logs."""
    store.create_job("job-del-2")
    store.append_log("job-del-2", level="INFO", logger_name="t", message="hi")
    store.delete_job("job-del-2")
    assert store.get_logs("job-del-2") == []


def test_delete_job_cascades_progress_events(store: SQLiteJobStore) -> None:
    """Delete job cascades progress events."""
    store.create_job("job-del-3")
    store.record_progress("job-del-3", "step", {"x": 1})
    store.delete_job("job-del-3")
    assert store.get_progress_events("job-del-3") == []


def test_delete_job_cascades_agent_runs(store: SQLiteJobStore) -> None:
    """Delete job removes the job's sandboxed agent runs."""
    store.create_job("job-del-runs")
    store.save_agent_run("job-del-runs", {"run_id": 1, "phase": "version", "status": "running"})
    store.append_agent_run_transcript("job-del-runs", 1, "hi\n")
    before = store.get_agent_run("job-del-runs", 1)
    assert before is not None
    assert before["transcript"] == "hi\n"

    store.delete_job("job-del-runs")

    assert store.get_agent_run("job-del-runs", 1) is None


def test_delete_job_tolerates_nonexistent_id(store: SQLiteJobStore) -> None:
    """Delete job tolerates nonexistent id."""
    store.delete_job("ghost-id")  # must not raise


def test_requeue_for_rerun_resets_row_and_clears_children(store: SQLiteJobStore) -> None:
    """A from-scratch rerun flips the row to pending and wipes the prior attempt in place."""
    store.create_job("rr1")
    store.update_job("rr1", status="failed", result={"score": 0.9}, attempts=3)
    store.append_log("rr1", level="INFO", logger_name="t", message="boom")
    store.record_progress("rr1", "step", {"x": 1})
    store.save_gepa_checkpoint("rr1", b"GEPA-STATE", iteration=7)

    assert store.requeue_for_rerun("rr1") is True

    assert store.job_exists("rr1")
    job = store.get_job("rr1")
    assert job["status"] == "pending"
    assert job["result"] is None
    assert job["attempts"] == 0
    assert store.get_logs("rr1") == []
    assert store.get_progress_events("rr1") == []
    assert store.has_gepa_checkpoint("rr1") is False


def test_requeue_for_rerun_missing_id_returns_false(store: SQLiteJobStore) -> None:
    """Re-running an unknown id is a no-op that reports the miss."""
    assert store.requeue_for_rerun("ghost") is False


def test_get_jobs_status_by_ids_returns_map(store: SQLiteJobStore) -> None:
    """Get jobs status by ids returns map."""
    store.create_job("s1")
    store.create_job("s2")
    store.update_job("s2", status="running")
    result = store.get_jobs_status_by_ids(["s1", "s2"])
    assert result == {"s1": "pending", "s2": "running"}


def test_get_jobs_status_by_ids_omits_missing(store: SQLiteJobStore) -> None:
    """Get jobs status by ids omits missing."""
    store.create_job("s3")
    result = store.get_jobs_status_by_ids(["s3", "missing-xyz"])
    assert "missing-xyz" not in result
    assert result["s3"] == "pending"


def test_get_jobs_status_by_ids_empty_input(store: SQLiteJobStore) -> None:
    """Get jobs status by ids empty input."""
    assert store.get_jobs_status_by_ids([]) == {}


def test_delete_jobs_returns_count_of_deleted(store: SQLiteJobStore) -> None:
    """Delete jobs returns count of deleted."""
    store.create_job("b1")
    store.create_job("b2")
    assert store.delete_jobs(["b1", "b2"]) == 2


def test_delete_jobs_actually_removes_jobs(store: SQLiteJobStore) -> None:
    """Delete jobs actually removes jobs."""
    store.create_job("b3")
    store.create_job("b4")
    store.delete_jobs(["b3", "b4"])
    assert not store.job_exists("b3")
    assert not store.job_exists("b4")


def test_delete_jobs_removes_associated_logs_and_progress(store: SQLiteJobStore) -> None:
    """Delete jobs removes associated logs and progress."""
    store.create_job("b5")
    store.append_log("b5", level="INFO", logger_name="t", message="log entry")
    store.record_progress("b5", "step", {"m": 1})
    store.delete_jobs(["b5"])
    assert store.get_logs("b5") == []
    assert store.get_progress_events("b5") == []


def _attached_budget_with_hold(store: SQLiteJobStore, optimization_id: str) -> tuple[str, str, str]:
    """Give a job a settled charge and an unstarted hold, as a terminal run can leave them.

    Args:
        store: Private fixture database.
        optimization_id: Existing job to attach the budget to.

    Returns:
        The budget id, the settled operation id and the still-reserved operation id.
    """
    with Session(store.engine) as session:
        session.add(
            BillingCustomerModel(
                username="payer", stripe_customer_id="local-payer", balance_cents=50, grant_remaining=0
            )
        )
        session.commit()
    service = BudgetService(engine=store.engine)
    budget = service.create("payer", 20, idempotency_key=optimization_id)
    service.attach_to_job(budget.id, "payer", optimization_id, expected_revision=budget.revision)
    shared = {
        "generation": 0,
        "phase": "run",
        "cost_kind": "model",
        "request_fingerprint": "req",
        "price_snapshot": {"version": "fixture-v1"},
        "max_cents": 5,
    }
    spent = service.reserve(budget.id, "payer", operation_key="spent", **shared)
    service.mark_dispatched(spent.id, "payer")
    service.settle(spent.id, "payer", evidence_key="final", actual_cents=3, evidence={"actual": True})
    held = service.reserve(budget.id, "payer", operation_key="held", **shared)
    return budget.id, spent.id, held.id


@pytest.mark.parametrize("bulk", [False, True])
def test_delete_job_closes_budget_and_keeps_ledger(store: SQLiteJobStore, bulk: bool) -> None:
    """Deleting a run releases its unstarted hold and keeps the budget and ledger as history.

    Args:
        store: Private fixture database.
        bulk: Whether the job goes through ``delete_jobs`` instead of ``delete_job``.
    """
    store.create_job("billed-run")
    budget_id, spent_id, held_id = _attached_budget_with_hold(store, "billed-run")

    if bulk:
        store.delete_jobs(["billed-run"])
    else:
        store.delete_job("billed-run")

    assert not store.job_exists("billed-run")
    with Session(store.engine) as session:
        budget = session.get(ExecutionBudgetModel, budget_id)
        assert budget is not None
        assert budget.job_id is None
        assert (budget.state, budget.blocked_reason) == ("closed", "job_deleted")
        assert (budget.reserved_units, budget.wallet_reserved_units) == (0, 0)
        assert budget.billed_cents == 3
        assert session.get(ExecutionOperationModel, held_id).state == "released"
        assert session.get(ExecutionOperationModel, spent_id).state == "settled"
        ledger = session.query(WalletLedgerModel).filter(WalletLedgerModel.budget_id == budget_id).all()
        assert [row.delta_cents for row in ledger] == [-3]
        assert session.get(BillingCustomerModel, "payer").balance_cents == 47
    snapshot = BudgetService(engine=store.engine).get(budget_id, "payer")
    assert snapshot.reserved_cents == 0
    assert snapshot.job_id is None


def test_delete_jobs_tolerates_missing_ids(store: SQLiteJobStore) -> None:
    """Delete jobs tolerates missing ids."""
    store.create_job("b6")
    removed = store.delete_jobs(["b6", "no-such-id"])
    assert removed == 1


def test_delete_jobs_tolerates_empty_list(store: SQLiteJobStore) -> None:
    """Delete jobs tolerates empty list."""
    assert store.delete_jobs([]) == 0


def test_record_progress_creates_event(store: SQLiteJobStore) -> None:
    """Record progress creates event."""
    store.create_job("p1")
    store.record_progress("p1", "started", {"loss": 0.5})
    events = store.get_progress_events("p1")
    assert len(events) == 1
    assert events[0]["event"] == "started"
    assert events[0]["metrics"] == {"loss": 0.5}


def test_record_progress_none_message_allowed(store: SQLiteJobStore) -> None:
    """Record progress none message allowed."""
    store.create_job("p2")
    store.record_progress("p2", None, {})
    assert store.get_progress_count("p2") == 1


def test_record_progress_replaces_latest_metrics_snapshot(store: SQLiteJobStore) -> None:
    """Record progress replaces latest metrics snapshot."""
    store.create_job("p3")
    store.record_progress("p3", "step", {"acc": 0.9})
    store.record_progress("p3", "next", {"loss": 0.2})
    job = store.get_job("p3")
    assert job["latest_metrics"] == {"loss": 0.2}


def test_record_progress_carries_tqdm_progress_across_non_tqdm_events(store: SQLiteJobStore) -> None:
    """Optimizer tqdm progress persists in latest_metrics across interleaved non-tqdm events."""
    store.create_job("p3-tqdm")
    store.record_progress("p3-tqdm", "optimizer_progress", {"tqdm_percent": 30.0, "tqdm_n": 3, "tqdm_total": 10})
    store.record_progress("p3-tqdm", "minibatch_feedback", {"score": 1.0, "iteration": 2})
    job = store.get_job("p3-tqdm")
    assert job["latest_metrics"]["score"] == 1.0
    assert job["latest_metrics"]["iteration"] == 2
    assert job["latest_metrics"]["tqdm_percent"] == 30.0
    assert job["latest_metrics"]["tqdm_n"] == 3
    assert job["latest_metrics"]["tqdm_total"] == 10


def test_record_progress_refreshes_sticky_tqdm_on_new_optimizer_tick(store: SQLiteJobStore) -> None:
    """A newer optimizer_progress event replaces the carried tqdm snapshot without accumulating fields."""
    store.create_job("p3-tqdm2")
    store.record_progress("p3-tqdm2", "optimizer_progress", {"tqdm_percent": 30.0})
    store.record_progress("p3-tqdm2", "minibatch_feedback", {"score": 0.5})
    store.record_progress("p3-tqdm2", "optimizer_progress", {"tqdm_percent": 70.0})
    job = store.get_job("p3-tqdm2")
    assert job["latest_metrics"]["tqdm_percent"] == 70.0
    assert "score" not in job["latest_metrics"]


def test_record_progress_json_metrics_roundtrip(store: SQLiteJobStore) -> None:
    """Record progress json metrics roundtrip."""
    store.create_job("p4")
    metrics: dict[str, Any] = {"nested": {"a": 1}, "values": [1, 2, 3]}
    store.record_progress("p4", "step", metrics)
    events = store.get_progress_events("p4")
    assert events[0]["metrics"] == metrics


def test_get_progress_events_chronological_order(store: SQLiteJobStore) -> None:
    """Get progress events chronological order."""
    store.create_job("p5")
    for i in range(3):
        store.record_progress("p5", f"step-{i}", {"i": i})
    events = store.get_progress_events("p5")
    names = [e["event"] for e in events]
    assert names == ["step-0", "step-1", "step-2"]


def test_get_progress_count_matches(store: SQLiteJobStore) -> None:
    """Get progress count matches."""
    store.create_job("p6")
    store.record_progress("p6", "a", {})
    store.record_progress("p6", "b", {})
    assert store.get_progress_count("p6") == 2


def test_get_progress_events_empty_for_unknown_job(store: SQLiteJobStore) -> None:
    """Get progress events empty for unknown job."""
    assert store.get_progress_events("unknown-job") == []


def test_progress_silently_ignored_for_deleted_job(store: SQLiteJobStore) -> None:
    """Progress silently ignored for deleted job."""
    store.create_job("p-del")
    store.delete_job("p-del")
    # Logging / progress for a deleted job must not raise
    store.record_progress("p-del", "after delete", {})


def test_append_log_makes_entry_retrievable(store: SQLiteJobStore) -> None:
    """Append log makes entry retrievable."""
    store.create_job("l1")
    store.append_log("l1", level="INFO", logger_name="mylogger", message="hello")
    logs = store.get_logs("l1")
    assert len(logs) == 1
    assert logs[0]["message"] == "hello"
    assert logs[0]["level"] == "INFO"
    assert logs[0]["logger"] == "mylogger"


def test_append_log_pair_index_stored(store: SQLiteJobStore) -> None:
    """Append log pair index stored."""
    store.create_job("l2")
    store.append_log("l2", level="DEBUG", logger_name="lg", message="msg", pair_index=42)
    assert store.get_logs("l2")[0]["pair_index"] == 42


def test_append_log_pair_index_null_when_absent(store: SQLiteJobStore) -> None:
    """Append log pair index null when absent."""
    store.create_job("l3")
    store.append_log("l3", level="INFO", logger_name="lg", message="no pair")
    assert store.get_logs("l3")[0]["pair_index"] is None


def test_append_log_warns_for_deleted_job(store: SQLiteJobStore, caplog: pytest.LogCaptureFixture) -> None:
    """Append log warns and skips the entry for deleted jobs."""
    store.create_job("l-del")
    store.delete_job("l-del")
    store.append_log("l-del", level="INFO", logger_name="lg", message="after delete")
    assert store.get_logs("l-del") == []
    assert "Discarding 1 log entries for missing job l-del" in caplog.text


def test_get_logs_level_filter(store: SQLiteJobStore) -> None:
    """Get logs level filter."""
    store.create_job("l4")
    store.append_log("l4", level="INFO", logger_name="lg", message="info msg")
    store.append_log("l4", level="ERROR", logger_name="lg", message="err msg")
    errors = store.get_logs("l4", level="ERROR")
    assert len(errors) == 1
    assert errors[0]["level"] == "ERROR"


def test_get_logs_offset_pagination(store: SQLiteJobStore) -> None:
    """Get logs offset pagination."""
    store.create_job("l5")
    for i in range(5):
        store.append_log("l5", level="INFO", logger_name="lg", message=f"msg {i}")
    page = store.get_logs("l5", offset=2)
    assert len(page) == 3
    assert page[0]["message"] == "msg 2"


def test_get_logs_limit_pagination(store: SQLiteJobStore) -> None:
    """Get logs limit pagination."""
    store.create_job("l6")
    for i in range(5):
        store.append_log("l6", level="INFO", logger_name="lg", message=f"msg {i}")
    page = store.get_logs("l6", limit=2)
    assert len(page) == 2


def test_get_logs_limit_and_offset_combined(store: SQLiteJobStore) -> None:
    """Get logs limit and offset combined."""
    store.create_job("l7")
    for i in range(10):
        store.append_log("l7", level="INFO", logger_name="lg", message=f"msg {i}")
    page = store.get_logs("l7", limit=3, offset=4)
    assert len(page) == 3
    assert page[0]["message"] == "msg 4"


def test_get_log_count_no_filter(store: SQLiteJobStore) -> None:
    """Get log count no filter."""
    store.create_job("l8")
    store.append_log("l8", level="INFO", logger_name="lg", message="a")
    store.append_log("l8", level="WARN", logger_name="lg", message="b")
    assert store.get_log_count("l8") == 2


def test_get_log_count_with_level_filter(store: SQLiteJobStore) -> None:
    """Get log count with level filter."""
    store.create_job("l9")
    store.append_log("l9", level="INFO", logger_name="lg", message="i")
    store.append_log("l9", level="ERROR", logger_name="lg", message="e1")
    store.append_log("l9", level="ERROR", logger_name="lg", message="e2")
    assert store.get_log_count("l9", level="ERROR") == 2


def test_get_logs_returns_empty_for_unknown_job(store: SQLiteJobStore) -> None:
    """Get logs returns empty for unknown job."""
    assert store.get_logs("unknown-job") == []


def test_set_payload_overview_stores_and_retrieves_data(store: SQLiteJobStore) -> None:
    """Set payload overview stores and retrieves data."""
    store.create_job("o1")
    overview: dict[str, Any] = {"username": "alice", "optimization_type": "gepa", "extra": [1, 2]}
    store.set_payload_overview("o1", overview)
    job = store.get_job("o1")
    assert job["payload_overview"]["username"] == "alice"
    assert job["payload_overview"]["extra"] == [1, 2]


def test_set_payload_overview_overwrites_previous(store: SQLiteJobStore) -> None:
    """Set payload overview overwrites previous."""
    store.create_job("o2")
    store.set_payload_overview("o2", {"username": "old"})
    store.set_payload_overview("o2", {"username": "new"})
    assert store.get_job("o2")["payload_overview"]["username"] == "new"


def test_set_payload_overview_sets_username_column(store: SQLiteJobStore) -> None:
    """Set payload overview sets username column."""
    store.create_job("o3")
    store.set_payload_overview("o3", {"username": "carol"})
    # The store writes to the job.username column as well; verify via list_jobs filter
    jobs = store.list_jobs(username="carol")
    assert any(j["optimization_id"] == "o3" for j in jobs)


def test_set_payload_overview_without_username_preserves_column(store: SQLiteJobStore) -> None:
    """Payload overview updates without username keep the indexed owner."""
    store.create_job("o4")
    store.set_payload_overview("o4", {"username": "dana", "optimization_type": "gepa"})
    store.set_payload_overview("o4", {"optimization_type": "bootstrap"})
    assert store.get_job("o4")["username"] == "dana"
    assert store.list_jobs(username="dana")[0]["optimization_id"] == "o4"


def test_set_payload_overview_hoists_composition_column(store: SQLiteJobStore) -> None:
    """The ``composition`` overview key is hoisted to the indexed job column."""
    store.create_job("o5")
    store.set_payload_overview("o5", {"composition": "workflow"})
    session = store._get_session()
    try:
        composition = session.query(JobModel.composition).filter(JobModel.optimization_id == "o5").scalar()
    finally:
        session.close()
    assert composition == "workflow"


def test_list_jobs_no_filter_returns_all(store: SQLiteJobStore) -> None:
    """List jobs no filter returns all."""
    store.create_job("lj1")
    store.create_job("lj2")
    assert len(store.list_jobs()) == 2


def test_list_jobs_status_filter(store: SQLiteJobStore) -> None:
    """List jobs status filter."""
    store.create_job("lj-pend")
    store.create_job("lj-run")
    store.update_job("lj-run", status="running")
    result = store.list_jobs(status="pending")
    ids = {j["optimization_id"] for j in result}
    assert ids == {"lj-pend"}


def test_list_jobs_username_filter(store: SQLiteJobStore) -> None:
    """List jobs username filter."""
    store.create_job("lj-alice")
    store.set_payload_overview("lj-alice", {"username": "alice"})
    store.create_job("lj-bob")
    store.set_payload_overview("lj-bob", {"username": "bob"})
    result = store.list_jobs(username="alice")
    assert all(j["optimization_id"] == "lj-alice" for j in result)


def test_list_jobs_optimization_type_filter(store: SQLiteJobStore) -> None:
    """List jobs optimization type filter."""
    store.create_job("lj-opt-a")
    store.set_payload_overview("lj-opt-a", {"optimization_type": "opt_a"})
    store.create_job("lj-bs")
    store.set_payload_overview("lj-bs", {"optimization_type": "bootstrap"})
    result = store.list_jobs(optimization_type="opt_a")
    ids = {j["optimization_id"] for j in result}
    assert ids == {"lj-opt-a"}


def test_internal_tagging_jobs_hidden_from_listing_surfaces(store: SQLiteJobStore) -> None:
    """Tagger bulk jobs stay out of every listing/count unless filtered for by type."""
    store.create_job("run-1")
    store.set_payload_overview("run-1", {"username": "alice", "optimization_type": "run"})
    store.create_job("tag-1")
    store.set_payload_overview("tag-1", {"username": "alice", "optimization_type": OPTIMIZATION_TYPE_TAGGING})
    assert {j["optimization_id"] for j in store.list_jobs()} == {"run-1"}
    assert store.count_jobs() == 1
    assert store.count_jobs_by_status() == {"pending": 1}
    assert {j["optimization_id"] for j in store.list_jobs_visible_to("alice")} == {"run-1"}
    assert store.count_jobs_visible_to("alice") == 1
    assert store.count_jobs_by_status_visible_to("alice") == {"pending": 1}
    assert {j["optimization_id"] for j in store.scan_jobs_for_analytics()} == {"run-1"}
    # The explicit type filter stays the escape hatch for whoever needs the rows.
    tagged = store.list_jobs(optimization_type=OPTIMIZATION_TYPE_TAGGING)
    assert {j["optimization_id"] for j in tagged} == {"tag-1"}
    assert store.count_jobs(optimization_type=OPTIMIZATION_TYPE_TAGGING) == 1


def test_legacy_null_type_jobs_survive_internal_exclusion(store: SQLiteJobStore) -> None:
    """Rows with a NULL optimization_type (legacy) are still listed and counted."""
    store.create_job("legacy-1")
    assert {j["optimization_id"] for j in store.list_jobs()} == {"legacy-1"}
    assert store.count_jobs() == 1


def test_list_jobs_limit(store: SQLiteJobStore) -> None:
    """List jobs limit."""
    for i in range(5):
        store.create_job(f"lim-{i}")
    assert len(store.list_jobs(limit=3)) == 3


def test_list_jobs_offset(store: SQLiteJobStore) -> None:
    """List jobs offset."""
    for i in range(5):
        store.create_job(f"off-{i}")
    all_jobs = store.list_jobs()
    paged = store.list_jobs(offset=2)
    assert len(paged) == len(all_jobs) - 2


def test_list_jobs_includes_progress_and_log_counts(store: SQLiteJobStore) -> None:
    """List jobs includes progress and log counts."""
    store.create_job("lj-counts")
    store.append_log("lj-counts", level="INFO", logger_name="t", message="m")
    store.record_progress("lj-counts", "step", {})
    jobs = store.list_jobs()
    job = next(j for j in jobs if j["optimization_id"] == "lj-counts")
    assert job["log_count"] == 1
    assert job["progress_count"] == 1


def test_list_jobs_prunes_result_to_summary_scalars(store: SQLiteJobStore) -> None:
    """List rows carry only the result scalars the cards read; get_job keeps the blob."""
    store.create_job("lr-run", username="alice")
    store.create_job("lr-pending", username="alice")
    full_result = {
        "baseline_test_metric": 0.5,
        "optimized_test_metric": 0.75,
        "runtime_seconds": 12.5,
        "program_artifact": {"optimized_prompt": "x" * 5000},
        "baseline_test_results": [{"index": i, "outputs": {"answer": "y" * 200}} for i in range(50)],
    }
    store.update_job("lr-run", status="success", result=full_result)

    by_id = {row["optimization_id"]: row for row in store.list_jobs()}
    assert by_id["lr-run"]["result"] == {
        "baseline_test_metric": 0.5,
        "optimized_test_metric": 0.75,
        "runtime_seconds": 12.5,
    }
    assert by_id["lr-run"]["payload"] is None
    assert by_id["lr-pending"]["result"] is None
    assert store.get_job("lr-run")["result"] == full_result


def test_list_jobs_grid_result_keeps_best_pair_label_fields(store: SQLiteJobStore) -> None:
    """A grid row keeps the pair counters and best_pair metrics plus its model names."""
    store.create_job("lr-grid", username="alice")
    store.set_payload_overview("lr-grid", {"optimization_type": "grid_search", "total_pairs": 4})
    store.update_job(
        "lr-grid",
        status="success",
        result={
            "completed_pairs": 3,
            "failed_pairs": 1,
            "best_pair": {
                "baseline_test_metric": 0.4,
                "optimized_test_metric": 0.9,
                "runtime_seconds": 30.0,
                "generation_model": "openai/gpt-4o",
                "reflection_model": "openai/o3",
                "pair_index": 2,
                "per_example_outputs": ["z" * 500] * 20,
            },
            "pairs": [{"pair_index": i, "outputs": ["z" * 500] * 20} for i in range(4)],
        },
    )

    [row] = store.list_jobs()
    assert row["result"] == {
        "completed_pairs": 3,
        "failed_pairs": 1,
        "best_pair": {
            "baseline_test_metric": 0.4,
            "optimized_test_metric": 0.9,
            "runtime_seconds": 30.0,
            "generation_model": "openai/gpt-4o",
            "reflection_model": "openai/o3",
        },
    }


def test_shared_list_surfaces_prune_result_the_same_way(store: SQLiteJobStore) -> None:
    """The visible-to and shared-with pages carry the same pruned result as list_jobs."""
    store.create_job("lr-shared", username="alice")
    store.set_payload_overview("lr-shared", {"username": "alice", "optimization_type": "run"})
    store.update_job(
        "lr-shared",
        status="success",
        result={"baseline_test_metric": 0.1, "optimized_test_metric": 0.3, "demos": ["d" * 1000] * 50},
    )
    session = store._get_session()
    try:
        session.add(
            OptimizationShareGrantModel(
                optimization_id="lr-shared",
                grantee_username="bob",
                role="viewer",
                created_by="alice",
                created_at=datetime.now(UTC),
            )
        )
        session.commit()
    finally:
        session.close()

    expected = {"baseline_test_metric": 0.1, "optimized_test_metric": 0.3}
    [visible] = store.list_jobs_visible_to("bob")
    [shared] = store.list_jobs_shared_with("bob")
    assert visible["result"] == expected
    assert shared["result"] == expected
    assert visible["log_count"] == 0
    assert shared["progress_count"] == 0


def test_count_jobs_total(store: SQLiteJobStore) -> None:
    """Count jobs total."""
    store.create_job("cj1")
    store.create_job("cj2")
    assert store.count_jobs() == 2


def test_count_jobs_status_filter(store: SQLiteJobStore) -> None:
    """Count jobs status filter."""
    store.create_job("cj-pend")
    store.create_job("cj-done")
    store.update_job("cj-done", status="success")
    assert store.count_jobs(status="pending") == 1
    assert store.count_jobs(status="success") == 1


def test_count_jobs_username_filter(store: SQLiteJobStore) -> None:
    """Count jobs username filter."""
    store.create_job("cj-alice")
    store.set_payload_overview("cj-alice", {"username": "alice"})
    store.create_job("cj-bob")
    store.set_payload_overview("cj-bob", {"username": "bob"})
    assert store.count_jobs(username="alice") == 1


def test_count_jobs_zero_when_empty(store: SQLiteJobStore) -> None:
    """Count jobs zero when empty."""
    assert store.count_jobs() == 0


def test_recover_orphaned_jobs_resumes_running_job_with_checkpoint(
    store: SQLiteJobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An orphaned funded run with a checkpoint is requeued to resume, counting one attempt.

    Args:
        store: Disposable job store.
        monkeypatch: Pytest fixture used to bind the sandbox profile.
    """
    store.create_job("r1")
    store.update_job("r1", status="running")
    _fund_checkpoint(store, "r1", monkeypatch)
    assert store.recover_orphaned_jobs() == 1
    job = store.get_job("r1")
    assert job["status"] == "pending"
    assert job["attempts"] == 1
    assert job["recovery"] is None
    assert job["stop_reason"] is None
    assert job["completed_at"] is None
    assert job["message"] == "Resuming after a worker restart"
    assert store.get_gepa_checkpoint("r1") is not None


@pytest.mark.parametrize("status", ["running", "validating"])
def test_recover_orphaned_jobs_resumes_worker_restart_without_an_attempt(
    store: SQLiteJobStore, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """A run released by a graceful worker shutdown resumes without using an attempt.

    Args:
        store: Disposable job store.
        monkeypatch: Pytest fixture used to bind the sandbox profile.
        status: Active status the shutdown interrupted.
    """
    store.create_job("restart")
    store.update_job(
        "restart",
        status=status,
        recovery={"state": "recovering", "phase": "requeue", "cause": WORKER_RESTART_CAUSE},
    )
    _fund_checkpoint(store, "restart", monkeypatch)
    assert store.recover_orphaned_jobs() == 1
    job = store.get_job("restart")
    assert job["status"] == "pending"
    assert job["attempts"] == 0


def _bill_run_call(service: BudgetService, username: str, budget_id: str, key: str, cents: int) -> None:
    """Settle one run-phase model call of the first execution generation.

    Args:
        service: Authoritative budget service.
        username: Run owner.
        budget_id: Attached run envelope.
        key: Operation identity.
        cents: Actual charge.
    """
    operation = service.reserve(
        budget_id,
        username,
        operation_key=key,
        generation=0,
        phase="run",
        cost_kind="model",
        request_fingerprint=key,
        price_snapshot={"version": "fixture-v1"},
        max_cents=cents,
    )
    service.mark_dispatched(operation.id, username)
    service.settle(operation.id, username, evidence_key=key, actual_cents=cents, evidence={"usage": key})


def _balance_of(store: SQLiteJobStore, username: str) -> int:
    """Read one account's paid balance.

    Args:
        store: Private fixture database.
        username: Account owner.

    Returns:
        Paid balance in cents.
    """
    with Session(store.engine) as session:
        return session.get(BillingCustomerModel, username).balance_cents


def test_recover_orphaned_jobs_does_not_charge_work_the_resume_repeats(
    store: SQLiteJobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Work billed after the last checkpoint runs again on resume, so its charge is returned.

    Args:
        store: Disposable job store.
        monkeypatch: Pytest fixture used to bind the sandbox profile.
    """
    store.create_job("redo")
    store.update_job(
        "redo",
        status="running",
        recovery={"state": "recovering", "phase": "requeue", "cause": WORKER_RESTART_CAUSE},
    )
    service = _fund_checkpoint(store, "redo", monkeypatch)
    budget_id = store.get_job("redo")["execution_budget_id"]
    _bill_run_call(service, "resume-owner", budget_id, "before-checkpoint", 2)
    time.sleep(0.01)
    with Session(store.engine) as session:
        session.query(GepaCheckpointModel).filter_by(optimization_id="redo").update({"updated_at": datetime.now(UTC)})
        session.commit()
    time.sleep(0.01)
    _bill_run_call(service, "resume-owner", budget_id, "after-checkpoint", 3)
    assert _balance_of(store, "resume-owner") == 45

    assert store.recover_orphaned_jobs() == 1
    assert store.recover_orphaned_jobs() == 0

    assert store.get_job("redo")["status"] == "pending"
    assert _balance_of(store, "resume-owner") == 48
    assert service.get(budget_id, "resume-owner").billed_cents == 2


def test_recover_orphaned_jobs_does_not_charge_a_restart_from_the_beginning(store: SQLiteJobStore) -> None:
    """A run with no checkpoint repeats all its interrupted work, so all of that charge is returned.

    Args:
        store: Disposable job store.
    """
    store.create_job("again")
    store.update_job("again", status="validating")
    service = _fund_without_checkpoint(store, "again")
    budget_id = store.get_job("again")["execution_budget_id"]
    _bill_run_call(service, "restart-owner", budget_id, "lost", 4)

    assert store.recover_orphaned_jobs() == 1

    assert store.get_job("again")["status"] == "pending"
    assert _balance_of(store, "restart-owner") == 50


def test_recover_orphaned_jobs_unpins_a_pinned_run_for_any_live_worker(
    store: SQLiteJobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run pinned to a sandbox image is claimable by workers of any version after recovery.

    Args:
        store: Disposable job store.
        monkeypatch: Pytest fixture used to bind the sandbox profile.
    """
    store.create_job("pinned")
    store.update_job("pinned", status="running")
    _fund_checkpoint(store, "pinned", monkeypatch)
    store.pin_sandbox_image("pinned", _RECOVERY_IMAGE)
    store.update_job("pinned", status="running")
    assert store.recover_orphaned_jobs() == 1
    job = store.get_job("pinned")
    assert job["status"] == "pending"
    assert job["code_version"] is None


def test_recover_orphaned_jobs_restarts_funded_run_without_checkpoint(store: SQLiteJobStore) -> None:
    """An orphaned funded run with no checkpoint yet starts again from the beginning.

    Args:
        store: Disposable job store.
    """
    store.create_job("fresh")
    store.update_job("fresh", status="validating")
    _fund_without_checkpoint(store, "fresh")
    assert store.recover_orphaned_jobs() == 1
    job = store.get_job("fresh")
    assert job["status"] == "pending"
    assert job["attempts"] == 1
    assert job["message"] == "Starting again after a worker restart"


def test_recover_orphaned_jobs_fails_child_run_without_checkpoint(store: SQLiteJobStore) -> None:
    """A child run with no checkpoint cannot start again alone, so it fails with a retryable message.

    Args:
        store: Disposable job store.
    """
    store.create_job("parent")
    store.update_job("parent", status="success")
    store.create_job("child")
    store.update_job("child", status="running", parent_optimization_id="parent")
    _fund_without_checkpoint(store, "child")
    assert store.recover_orphaned_jobs() == 1
    job = store.get_job("child")
    assert job["status"] == "failed"
    assert job["stop_reason"] == "interrupted"
    assert job["message"].startswith(remote_mod.ORPHANED_RUN_RECOVERY_FAILED_MESSAGE)
    assert "No completed compatible checkpoint" in job["message"]


def test_recover_orphaned_jobs_fails_run_at_its_attempt_limit(
    store: SQLiteJobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that keeps losing its worker stops at the attempt cap with a clear reason.

    Args:
        store: Disposable job store.
        monkeypatch: Pytest fixture used to bind the sandbox profile.
    """
    monkeypatch.setattr(remote_mod.settings, "job_max_attempts", 2)
    store.create_job("capped")
    store.update_job("capped", status="running", attempts=1)
    _fund_checkpoint(store, "capped", monkeypatch)
    assert store.recover_orphaned_jobs() == 1
    job = store.get_job("capped")
    assert job["status"] == "failed"
    assert job["stop_reason"] == "interrupted"
    assert "resume attempt limit" in job["message"]


def test_recover_orphaned_jobs_waits_for_in_flight_usage(
    store: SQLiteJobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run with unsettled usage stays active and recovering until a later sweep.

    Args:
        store: Disposable job store.
        monkeypatch: Pytest fixture used to bind the sandbox profile.
    """
    store.create_job("usage")
    store.update_job(
        "usage",
        status="running",
        recovery={"state": "recovering", "phase": "requeue", "cause": WORKER_RESTART_CAUSE},
    )
    service = _fund_checkpoint(store, "usage", monkeypatch)
    budget = store.get_job("usage")["execution_budget_id"]
    reserved = service.get(budget, "resume-owner")
    monkeypatch.setattr(
        BudgetService,
        "get",
        lambda self, budget_id, username, **kwargs: replace(reserved, pending_operations=1),
    )
    assert store.recover_orphaned_jobs() == 0
    job = store.get_job("usage")
    assert job["status"] == "running"
    assert job["recovery"]["phase"] == "waiting_for_usage"
    assert job["recovery"]["cause"] == WORKER_RESTART_CAUSE


def _hold_usage_pending(
    store: SQLiteJobStore, optimization_id: str, service: BudgetService, monkeypatch: pytest.MonkeyPatch
) -> dict[str, int]:
    """Make the run's budget report in-flight usage until the test changes the returned count.

    Args:
        store: Disposable job store.
        optimization_id: Funded run whose budget reads are replaced.
        service: Authoritative budget service of the run.
        monkeypatch: Pytest fixture used to replace budget reads.

    Returns:
        Mutable holder; ``holder["pending"]`` is the pending operation count each read reports.
    """
    budget_id = store.get_job(optimization_id)["execution_budget_id"]
    original = BudgetService.get
    holder = {"pending": 1}

    def _get(self: BudgetService, budget_id_: str, username: str, **kwargs: Any) -> Any:
        """Report the real budget with the held pending operation count.

        Args:
            self: Budget service.
            budget_id_: Budget to read.
            username: Owner.
            **kwargs: Other read options.

        Returns:
            The budget snapshot.
        """
        snapshot = original(self, budget_id_, username, **kwargs)
        if budget_id_ == budget_id and holder["pending"]:
            return replace(snapshot, pending_operations=holder["pending"])
        return snapshot

    monkeypatch.setattr(BudgetService, "get", _get)
    return holder


def _age_usage_wait(store: SQLiteJobStore, optimization_id: str) -> None:
    """Move the run's usage wait start back by the full wait window.

    Args:
        store: Disposable job store.
        optimization_id: Run that waits for usage.
    """
    recovery = dict(store.get_job(optimization_id)["recovery"])
    recovery["waiting_since"] = (datetime.now(UTC) - remote_mod.ORPHANED_USAGE_WAIT).isoformat()
    store.update_job(optimization_id, recovery=recovery)


def test_recover_orphaned_jobs_stops_a_run_whose_usage_never_settles(
    store: SQLiteJobStore, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A run whose in-flight usage makes no progress for the wait window stops as interrupted.

    Args:
        store: Disposable job store.
        monkeypatch: Pytest fixture used to bind the sandbox profile and hold usage pending.
        caplog: Pytest fixture that captures the sweep's skip and stop logs.
    """
    store.create_job("stuck")
    store.update_job(
        "stuck",
        status="running",
        recovery={"state": "recovering", "phase": "requeue", "cause": WORKER_RESTART_CAUSE},
    )
    service = _fund_checkpoint(store, "stuck", monkeypatch)
    _hold_usage_pending(store, "stuck", service, monkeypatch)
    caplog.set_level("INFO", logger=remote_mod.__name__)
    assert store.recover_orphaned_jobs() == 0
    assert store.recover_orphaned_jobs() == 0
    assert "Orphaned run stuck is not requeued yet" in caplog.text
    assert "stopped as interrupted" not in caplog.text
    assert store.get_job("stuck")["status"] == "running"

    _age_usage_wait(store, "stuck")
    assert store.recover_orphaned_jobs() == 1
    job = store.get_job("stuck")
    assert job["status"] == "failed"
    assert job["stop_reason"] == "interrupted"
    assert job["message"] == remote_mod.ORPHANED_RUN_MESSAGE
    assert job["recovery"] == {"state": "interrupted", "interrupted_budget_generation": 0}
    assert "Orphaned run stuck had no usage settlement progress" in caplog.text


def test_recover_orphaned_jobs_keeps_a_usage_wait_that_makes_progress(
    store: SQLiteJobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run whose pending usage keeps settling is not stopped, however long the full wait takes.

    Args:
        store: Disposable job store.
        monkeypatch: Pytest fixture used to bind the sandbox profile and hold usage pending.
    """
    store.create_job("settling")
    store.update_job(
        "settling",
        status="running",
        recovery={"state": "recovering", "phase": "requeue", "cause": WORKER_RESTART_CAUSE},
    )
    service = _fund_checkpoint(store, "settling", monkeypatch)
    holder = _hold_usage_pending(store, "settling", service, monkeypatch)
    holder["pending"] = 3
    assert store.recover_orphaned_jobs() == 0

    _age_usage_wait(store, "settling")
    holder["pending"] = 2
    assert store.recover_orphaned_jobs() == 0
    job = store.get_job("settling")
    assert job["status"] == "running"
    assert job["recovery"]["pending_usage"][0] == 2

    _age_usage_wait(store, "settling")
    holder["pending"] = 0
    assert store.recover_orphaned_jobs() == 1
    assert store.get_job("settling")["status"] == "pending"


def test_recover_orphaned_jobs_gives_a_legacy_usage_wait_the_full_window(
    store: SQLiteJobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A usage wait stored without a start time, as on runs stuck before this fix, gets a full window.

    Args:
        store: Disposable job store.
        monkeypatch: Pytest fixture used to bind the sandbox profile and hold usage pending.
    """
    store.create_job("legacy-wait")
    store.update_job(
        "legacy-wait",
        status="running",
        recovery={"state": "recovering", "phase": "waiting_for_usage", "cause": WORKER_RESTART_CAUSE},
    )
    service = _fund_checkpoint(store, "legacy-wait", monkeypatch)
    store.update_job("legacy-wait", execution_budget_generation=1)
    _hold_usage_pending(store, "legacy-wait", service, monkeypatch)
    assert store.recover_orphaned_jobs() == 0
    job = store.get_job("legacy-wait")
    assert job["status"] == "running"
    assert "waiting_since" in job["recovery"]

    _age_usage_wait(store, "legacy-wait")
    assert store.recover_orphaned_jobs() == 1
    job = store.get_job("legacy-wait")
    assert job["status"] == "failed"
    assert job["recovery"] == {"state": "interrupted", "interrupted_budget_generation": 0}


def test_manual_resume_after_a_usage_wait_stop_does_not_charge_repeated_work(
    store: SQLiteJobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manual resume of a run the usage wait stopped waives the work after its checkpoint once usage settles.

    Args:
        store: Disposable job store.
        monkeypatch: Pytest fixture used to bind the sandbox profile and hold usage pending.
    """
    store.create_job("redo-later")
    store.update_job(
        "redo-later",
        status="running",
        recovery={"state": "recovering", "phase": "requeue", "cause": WORKER_RESTART_CAUSE},
    )
    service = _fund_checkpoint(store, "redo-later", monkeypatch)
    budget_id = store.get_job("redo-later")["execution_budget_id"]
    _bill_run_call(service, "resume-owner", budget_id, "before-checkpoint", 2)
    time.sleep(0.01)
    with Session(store.engine) as session:
        session.query(GepaCheckpointModel).filter_by(optimization_id="redo-later").update(
            {"updated_at": datetime.now(UTC)}
        )
        session.commit()
    time.sleep(0.01)
    _bill_run_call(service, "resume-owner", budget_id, "after-checkpoint", 3)
    holder = _hold_usage_pending(store, "redo-later", service, monkeypatch)
    assert store.recover_orphaned_jobs() == 0
    _age_usage_wait(store, "redo-later")
    assert store.recover_orphaned_jobs() == 1
    assert store.get_job("redo-later")["status"] == "failed"
    assert _balance_of(store, "resume-owner") == 45

    job = store.get_job("redo-later")
    assert (
        store.requeue_for_resume("redo-later", expected_generation=job["execution_generation"], budget_service=service)
        is None
    )
    job = store.get_job("redo-later")
    assert job["recovery"]["phase"] == "waiting_for_usage"
    assert job["recovery"]["interrupted_budget_generation"] == 0
    assert _balance_of(store, "resume-owner") == 45

    holder["pending"] = 0
    assert (
        store.requeue_for_resume("redo-later", expected_generation=job["execution_generation"], budget_service=service)
        is not None
    )
    assert store.get_job("redo-later")["status"] == "pending"
    assert _balance_of(store, "resume-owner") == 48
    assert service.get(budget_id, "resume-owner").billed_cents == 2


def test_manual_resume_restarts_an_interrupted_run_without_a_checkpoint(
    store: SQLiteJobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run stopped with no checkpoint starts again in its own budget, and none of its stopped work is charged.

    Args:
        store: Disposable job store.
        monkeypatch: Pytest fixture used to hold usage pending.
    """
    store.create_job("no-checkpoint")
    store.update_job(
        "no-checkpoint",
        status="running",
        code_version="old-worker",
        recovery={"state": "recovering", "phase": "requeue", "cause": WORKER_RESTART_CAUSE},
    )
    service = _fund_without_checkpoint(store, "no-checkpoint")
    budget_id = store.get_job("no-checkpoint")["execution_budget_id"]
    _bill_run_call(service, "restart-owner", budget_id, "stopped-work", 4)
    holder = _hold_usage_pending(store, "no-checkpoint", service, monkeypatch)
    assert store.recover_orphaned_jobs() == 0
    _age_usage_wait(store, "no-checkpoint")
    assert store.recover_orphaned_jobs() == 1
    assert store.get_job("no-checkpoint")["status"] == "failed"
    assert _balance_of(store, "restart-owner") == 46

    generation = store.get_job("no-checkpoint")["execution_generation"]
    assert store.requeue_for_resume("no-checkpoint", expected_generation=generation, budget_service=service) is None
    holder["pending"] = 0
    generation = store.get_job("no-checkpoint")["execution_generation"]
    assert store.requeue_for_resume("no-checkpoint", expected_generation=generation, budget_service=service) is not None
    job = store.get_job("no-checkpoint")
    assert job["status"] == "pending"
    assert job["code_version"] is None
    assert job["message"] == "Starting again after a worker restart"
    assert _balance_of(store, "restart-owner") == 50
    assert service.get(budget_id, "restart-owner").billed_cents == 0

    service.waive_interrupted_work(budget_id, "restart-owner", generation=0, settled_after=None)
    assert _balance_of(store, "restart-owner") == 50
    with Session(store.engine) as session:
        keys = [row.settlement_key for row in session.query(WalletLedgerModel).filter_by(budget_id=budget_id)]
    assert keys.count(f"restart:{budget_id}:g0") == 1


def test_recover_orphaned_jobs_fails_unfunded_run(store: SQLiteJobStore) -> None:
    """An orphaned run without a cumulative budget keeps failing with the orphan message.

    Args:
        store: Disposable job store.
    """
    store.create_job("legacy")
    store.update_job("legacy", status="running")
    assert store.recover_orphaned_jobs() == 1
    job = store.get_job("legacy")
    assert job["status"] == "failed"
    assert job["attempts"] == 0
    assert job["message"] == remote_mod.ORPHANED_RUN_MESSAGE


@pytest.mark.parametrize("status", ["running", "validating"])
def test_recover_orphaned_jobs_keeps_ended_runs_for_notification(
    store: SQLiteJobStore, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """A run that the sweep fails waits for its notification once; a requeued run does not.

    Args:
        store: Disposable job store.
        monkeypatch: Pytest fixture used to bind the sandbox profile.
        status: Active status the shutdown interrupted.
    """
    restart = {"state": "recovering", "phase": "requeue", "cause": WORKER_RESTART_CAUSE}
    store.create_job("unfunded")
    store.update_job("unfunded", status=status, recovery=restart)
    store.create_job("resumed")
    store.update_job("resumed", status=status, recovery=restart)
    _fund_checkpoint(store, "resumed", monkeypatch)
    assert store.recover_orphaned_jobs() == 2
    assert store.take_ended_orphans() == [("unfunded", store.get_job("unfunded")["execution_generation"])]
    assert store.take_ended_orphans() == []


def test_recover_orphaned_jobs_keeps_budget_stopped_run_for_notification(
    store: SQLiteJobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restarted run that its budget cannot fund stops and waits for its notification.

    Args:
        store: Disposable job store.
        monkeypatch: Pytest fixture used to bind the sandbox profile.
    """
    store.create_job("spent")
    store.update_job(
        "spent",
        status="running",
        recovery={"state": "recovering", "phase": "requeue", "cause": WORKER_RESTART_CAUSE},
    )
    service = _fund_checkpoint(store, "spent", monkeypatch)
    budget = service.get(store.get_job("spent")["execution_budget_id"], "resume-owner")
    monkeypatch.setattr(
        BudgetService,
        "get",
        lambda self, budget_id, username, **kwargs: replace(budget, available_cents=0, blocked_reason="budget_reached"),
    )
    assert store.recover_orphaned_jobs() == 0
    assert store.get_job("spent")["status"] == "stopped"
    assert store.take_ended_orphans() == [("spent", store.get_job("spent")["execution_generation"])]


def test_orphan_sweeper_skips_a_stopped_run_that_its_owner_resumed(
    store: SQLiteJobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A budget-stopped run resumed before the sweeper's pass keeps its later completion notification.

    Args:
        store: Disposable job store.
        monkeypatch: Pytest fixture used to stop the budget and capture notifications.
    """
    sent: list[str] = []
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        observability_mod,
        "notify_job_completed",
        lambda optimization_id, username, status, message=None: sent.append(status),
    )
    monkeypatch.setattr(observability_mod, "record_server_event", lambda engine, **kwargs: events.append(kwargs))
    store.create_job("raised")
    store.update_job(
        "raised",
        status="running",
        recovery={"state": "recovering", "phase": "requeue", "cause": WORKER_RESTART_CAUSE},
    )
    service = _fund_checkpoint(store, "raised", monkeypatch)
    budget = service.get(store.get_job("raised")["execution_budget_id"], "resume-owner")
    with monkeypatch.context() as patched:
        patched.setattr(
            BudgetService,
            "get",
            lambda self, budget_id, username, **kwargs: replace(
                budget, available_cents=0, blocked_reason="budget_reached"
            ),
        )
        assert store.recover_orphaned_jobs() == 0
    stopped = store.get_job("raised")
    assert stopped["status"] == "stopped"
    store.update_job("raised", status="pending", execution_generation=stopped["execution_generation"] + 1)
    observability_mod.OrphanRecoverySweeper(store, interval_seconds=60)._notify_ended_orphans()
    assert sent == []
    assert events == []
    assert store.claim_completion_notification("raised") is True


def test_orphan_sweeper_reports_the_stop_it_claimed_when_the_owner_resumes_right_after(
    store: SQLiteJobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resume that lands after the sweeper's claim cannot change the stop the notification describes.

    Args:
        store: Disposable job store.
        monkeypatch: Pytest fixture used to stop the budget, resume the run and capture notifications.
    """
    sent: list[str] = []
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        observability_mod,
        "notify_job_completed",
        lambda optimization_id, username, status, message=None: sent.append(status),
    )
    monkeypatch.setattr(observability_mod, "record_server_event", lambda engine, **kwargs: events.append(kwargs))
    store.create_job("raced")
    store.update_job(
        "raced",
        status="running",
        recovery={"state": "recovering", "phase": "requeue", "cause": WORKER_RESTART_CAUSE},
    )
    service = _fund_checkpoint(store, "raced", monkeypatch)
    budget = service.get(store.get_job("raced")["execution_budget_id"], "resume-owner")
    with monkeypatch.context() as patched:
        patched.setattr(
            BudgetService,
            "get",
            lambda self, budget_id, username, **kwargs: replace(
                budget, available_cents=0, blocked_reason="budget_reached"
            ),
        )
        assert store.recover_orphaned_jobs() == 0
    generation = store.get_job("raced")["execution_generation"]
    original_get_job = store.get_job

    def resume_then_get_job(optimization_id: str, **kwargs: Any) -> Any:
        """Resume the run just before any later read, as an owner resume would.

        Args:
            optimization_id: Run to read.
            **kwargs: Passed through to the real read.

        Returns:
            The real read after the resume.
        """
        store.update_job(optimization_id, status="pending", execution_generation=generation + 1)
        return original_get_job(optimization_id, **kwargs)

    monkeypatch.setattr(store, "get_job", resume_then_get_job)
    original_claim = store.claim_ended_orphan_notification

    def claim_then_resume(optimization_id: str, claimed_generation: int) -> Any:
        """Claim the notification, then resume the run before the sweeper continues.

        Args:
            optimization_id: Run to claim.
            claimed_generation: Generation the sweep ended.

        Returns:
            The snapshot of the real claim.
        """
        snapshot = original_claim(optimization_id, claimed_generation)
        store.update_job(optimization_id, status="pending", execution_generation=generation + 1)
        return snapshot

    monkeypatch.setattr(store, "claim_ended_orphan_notification", claim_then_resume)
    observability_mod.OrphanRecoverySweeper(store, interval_seconds=60)._notify_ended_orphans()
    assert sent == ["stopped"]
    assert [(event["name"], event["properties"]["status"]) for event in events] == [("run_stopped", "stopped")]


@pytest.mark.parametrize("status", ["running", "validating"])
def test_orphan_sweeper_notifies_the_owner_of_a_run_it_ends(
    store: SQLiteJobStore, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """The periodic sweeper sends one failure notification and outcome event per ended run.

    Args:
        store: Disposable job store.
        monkeypatch: Pytest fixture used to capture notifications and events.
        status: Active status the shutdown interrupted.
    """
    sent: list[tuple[Any, ...]] = []
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        observability_mod,
        "notify_job_completed",
        lambda optimization_id, username, status, message=None: sent.append(
            (optimization_id, username, status, message)
        ),
    )
    monkeypatch.setattr(observability_mod, "record_server_event", lambda engine, **kwargs: events.append(kwargs))
    store.create_job("ended")
    store.update_job(
        "ended",
        status=status,
        username="owner",
        recovery={"state": "recovering", "phase": "requeue", "cause": WORKER_RESTART_CAUSE},
    )
    store.recover_orphaned_jobs()
    sweeper = observability_mod.OrphanRecoverySweeper(store, interval_seconds=60)
    sweeper.sweep_once()
    sweeper.sweep_once()
    assert sent == [("ended", "owner", "failed", remote_mod.ORPHANED_RUN_MESSAGE)]
    assert [event["name"] for event in events] == ["run_failed"]
    assert events[0]["username"] == "owner"


def _fund_without_checkpoint(store: SQLiteJobStore, optimization_id: str) -> BudgetService:
    """Attach reconciled funding to a run that has no checkpoint.

    Args:
        store: Private fixture database.
        optimization_id: Existing run to fund.

    Returns:
        Authoritative budget service for resume admission.
    """
    with Session(store.engine) as session:
        session.add(
            BillingCustomerModel(
                username="restart-owner", stripe_customer_id="local-restart", balance_cents=50, grant_remaining=0
            )
        )
        session.commit()
    service = BudgetService(engine=store.engine)
    budget = service.create("restart-owner", 20, idempotency_key=optimization_id)
    service.attach_to_job(budget.id, "restart-owner", optimization_id, expected_revision=budget.revision)
    store.update_job(
        optimization_id,
        username="restart-owner",
        payload={"execution_budget_id": budget.id, "execution_budget_generation": 0},
        execution_budget_id=budget.id,
        execution_budget_generation=0,
    )
    return service


def test_requeue_for_resume_without_checkpoint_fails(store: SQLiteJobStore) -> None:
    """A resume needs a checkpoint; a run without one is retried or cloned instead.

    Args:
        store: Disposable job store.
    """
    store.create_job("manual")
    store.update_job("manual", status="failed")
    budget_service = _fund_without_checkpoint(store, "manual")
    with pytest.raises(CheckpointCompatibilityError, match="No completed compatible checkpoint"):
        store.requeue_for_resume("manual", budget_service=budget_service)


def test_recover_orphaned_jobs_fails_validating_job(store: SQLiteJobStore) -> None:
    """Recover orphaned jobs fails a validating job."""
    store.create_job("r2")
    store.update_job("r2", status="validating")
    store.recover_orphaned_jobs()
    assert store.get_job("r2")["status"] == "failed"


def test_recover_orphaned_jobs_leaves_terminal_jobs_intact(store: SQLiteJobStore) -> None:
    """Recover orphaned jobs leaves terminal jobs intact."""
    store.create_job("r3")
    store.update_job("r3", status="success")
    store.create_job("r4")
    store.update_job("r4", status="failed")
    store.recover_orphaned_jobs()
    assert store.get_job("r3")["status"] == "success"
    assert store.get_job("r4")["status"] == "failed"


def test_recover_orphaned_jobs_returns_count(store: SQLiteJobStore) -> None:
    """Recover orphaned jobs returns count."""
    store.create_job("r5")
    store.update_job("r5", status="running")
    store.create_job("r6")
    store.update_job("r6", status="validating")
    store.create_job("r7")
    store.update_job("r7", status="success")
    assert store.recover_orphaned_jobs() == 2


def test_recover_orphaned_jobs_returns_zero_when_none_present(store: SQLiteJobStore) -> None:
    """Recover orphaned jobs returns zero when none present."""
    store.create_job("r9")
    store.update_job("r9", status="success")
    assert store.recover_orphaned_jobs() == 0


def test_recover_pending_jobs_returns_only_pending(store: SQLiteJobStore) -> None:
    """Recover pending jobs returns only pending."""
    store.create_job("rp1")
    store.create_job("rp2")
    store.update_job("rp2", status="success")
    pending = store.recover_pending_jobs()
    assert "rp1" in pending
    assert "rp2" not in pending


def test_recover_pending_jobs_skips_incompatible_code_version(
    store: SQLiteJobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pending local restart hints exclude jobs for a different code version."""
    monkeypatch.setattr(remote_mod.settings, "code_version", "v2", raising=False)
    store.create_job("rp-versioned")
    store.update_job("rp-versioned", code_version="v1")

    assert "rp-versioned" not in store.recover_pending_jobs()


def test_recover_pending_jobs_returns_list_of_strings(store: SQLiteJobStore) -> None:
    """Recover pending jobs returns list of strings."""
    store.create_job("rp3")
    result = store.recover_pending_jobs()
    assert isinstance(result, list)
    assert all(isinstance(x, str) for x in result)


def test_recover_pending_jobs_empty_when_none_pending(store: SQLiteJobStore) -> None:
    """Recover pending jobs empty when none pending."""
    store.create_job("rp4")
    store.update_job("rp4", status="success")
    assert store.recover_pending_jobs() == []


def test_duplicate_optimization_id_raises_on_insert(store: SQLiteJobStore) -> None:
    """Duplicate optimization id raises on insert."""
    store.create_job("dup-1")
    with pytest.raises(IntegrityError):
        # Bypass create_job's session so we insert a raw duplicate row
        session = store._get_session()
        try:
            job = JobModel(
                optimization_id="dup-1",
                status="pending",
                created_at=datetime.now(UTC),
                latest_metrics={},
                payload_overview={},
                attempts=0,
            )
            session.add(job)
            session.commit()
        finally:
            session.rollback()
            session.close()


def test_remote_db_job_store_satisfies_job_store_protocol() -> None:
    """Remote db job store satisfies job store protocol."""
    assert isinstance(SQLiteJobStore(), JobStore)


def test_claim_next_job_skips_incompatible_code_version(store: SQLiteJobStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """A v2 worker cannot claim a job submitted by v1."""
    monkeypatch.setattr(remote_mod.settings, "code_version", "v2", raising=False)
    store.create_job("claim-v1")
    store.update_job("claim-v1", code_version="v1", payload={"ok": True})

    assert store.claim_next_job("pod-v2", lease_seconds=60.0) is None


def test_claim_next_job_accepts_null_code_version(store: SQLiteJobStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """Jobs without a code version remain claimable by any worker."""
    monkeypatch.setattr(remote_mod.settings, "code_version", "v2", raising=False)
    store.create_job("claim-null")
    store.update_job("claim-null", code_version=None, payload={"ok": True})

    row = store.claim_next_job("pod-v2", lease_seconds=60.0)

    assert row is not None
    assert row["optimization_id"] == "claim-null"


def test_claim_next_job_skips_pending_row_without_payload(store: SQLiteJobStore) -> None:
    """A pending row stays unclaimable until its payload is written.

    Regression: a submission inserts the row as ``pending`` (``create_job``)
    *before* the worker writes the payload (``submit_job``). A poll tick landing
    in that window must not claim the payload-less row and fail it with "has no
    payload" — it waits, then becomes claimable once the payload lands.
    """
    store.create_job("claim-nopayload")

    assert store.claim_next_job("pod", lease_seconds=60.0) is None

    store.update_job("claim-nopayload", payload={"ok": True})

    row = store.claim_next_job("pod", lease_seconds=60.0)
    assert row is not None
    assert row["optimization_id"] == "claim-nopayload"


def test_claim_next_job_prefers_user_with_fewer_jobs_in_flight(store: SQLiteJobStore) -> None:
    """A one-user burst doesn't monopolize claims while another user waits.

    Alice submits first and already has one job claimed; Bob's younger job
    must be claimed next because Alice has more work in flight — plain FIFO
    would hand Alice the slot and starve Bob behind her whole burst.
    """
    store.create_job("fair-alice-1", username="alice")
    store.update_job("fair-alice-1", payload={"ok": True})
    store.create_job("fair-alice-2", username="alice")
    store.update_job("fair-alice-2", payload={"ok": True})
    store.create_job("fair-bob-1", username="bob")
    store.update_job("fair-bob-1", payload={"ok": True})

    first = store.claim_next_job("pod", lease_seconds=60.0)
    assert first is not None
    assert first["optimization_id"] == "fair-alice-1"

    second = store.claim_next_job("pod", lease_seconds=60.0)
    assert second is not None
    assert second["optimization_id"] == "fair-bob-1"

    third = store.claim_next_job("pod", lease_seconds=60.0)
    assert third is not None
    assert third["optimization_id"] == "fair-alice-2"


def test_claim_next_job_stays_fifo_within_one_user(store: SQLiteJobStore) -> None:
    """With a single user queued, claims stay strictly oldest-first."""
    for i in range(3):
        store.create_job(f"fifo-{i}", username="alice")
        store.update_job(f"fifo-{i}", payload={"ok": True})

    claimed = [store.claim_next_job("pod", lease_seconds=60.0)["optimization_id"] for _ in range(3)]

    assert claimed == ["fifo-0", "fifo-1", "fifo-2"]


def _seed_grid_parent_with_children(store: SQLiteJobStore, parent_id: str = "grid-p") -> list[str]:
    """Create a grid parent with payload/overview and fan out two pair children."""
    store.create_job(parent_id, username="alice")
    store.set_payload_overview(parent_id, {"optimization_type": "grid_search", "username": "alice"})
    store.update_job(
        parent_id,
        payload={"generation_models": [{"name": "g"}], "reflection_models": [{"name": "r1"}, {"name": "r2"}]},
    )
    return store.create_grid_pair_jobs(
        parent_id, 2, username="alice", payload_overview={"optimization_type": "grid_search"}
    )


def test_create_grid_pair_jobs_parks_parent_and_is_idempotent(store: SQLiteJobStore) -> None:
    """Fan-out creates claimable children once and parks the parent unclaimed."""
    child_ids = _seed_grid_parent_with_children(store)

    parent = store.get_job("grid-p")
    assert parent["status"] == "running"
    children = store.get_grid_pair_children("grid-p")
    assert [c["pair_index"] for c in children] == [0, 1]
    assert all(c["status"] == "pending" for c in children)

    again = store.create_grid_pair_jobs("grid-p", 2, username="alice", payload_overview={})
    assert again == child_ids
    assert len(store.get_grid_pair_children("grid-p")) == 2


def test_grid_pair_children_hidden_from_listings_and_counts(store: SQLiteJobStore) -> None:
    """Child rows never surface in user-facing lists or counts, typed or not."""
    _seed_grid_parent_with_children(store)

    listed = {j["optimization_id"] for j in store.list_jobs(username="alice")}
    assert listed == {"grid-p"}
    typed = {j["optimization_id"] for j in store.list_jobs(username="alice", optimization_type="grid_search")}
    assert typed == {"grid-p"}
    assert store.count_jobs(username="alice") == 1
    assert store.count_jobs(username="alice", optimization_type="grid_search") == 1


def test_recover_orphaned_jobs_skips_distributed_parent(store: SQLiteJobStore) -> None:
    """The lease-less running parent is never swept; its children still are."""
    child_ids = _seed_grid_parent_with_children(store)
    claimed = store.claim_next_job("pod", lease_seconds=0.001)
    assert claimed is not None
    assert claimed["optimization_id"] in child_ids
    time.sleep(0.01)

    recovered = store.recover_orphaned_jobs()

    assert recovered == 1
    assert store.get_job("grid-p")["status"] == "running"
    assert store.get_job(claimed["optimization_id"])["status"] == "failed"


def test_requeue_for_rerun_drops_pair_children(store: SQLiteJobStore) -> None:
    """A from-scratch re-run discards the previous attempt's pair rows."""
    _seed_grid_parent_with_children(store)
    store.update_job("grid-p", status="failed")

    assert store.requeue_for_rerun("grid-p") is True

    assert store.get_grid_pair_children("grid-p") == []
    assert store.get_job("grid-p")["status"] == "pending"


def test_delete_job_removes_pair_children(store: SQLiteJobStore) -> None:
    """Deleting a grid parent removes its child rows and their stores."""
    child_ids = _seed_grid_parent_with_children(store)
    store.save_grid_pair_result(child_ids[0], 0, {"pair_index": 0})

    store.delete_job("grid-p")

    assert not store.job_exists("grid-p")
    for child_id in child_ids:
        assert not store.job_exists(child_id)
    assert store.get_grid_pair_results(child_ids[0]) == {}


def test_list_finalizable_grid_parents(store: SQLiteJobStore) -> None:
    """Only running parents whose children are ALL terminal are listed."""
    child_ids = _seed_grid_parent_with_children(store)
    store.update_job(child_ids[0], status="success")

    assert store.list_finalizable_grid_parents() == []

    store.update_job(child_ids[1], status="failed")
    assert store.list_finalizable_grid_parents() == ["grid-p"]

    store.update_job("grid-p", status="success")
    assert store.list_finalizable_grid_parents() == []


def test_delete_grid_pair_result_drops_single_row(store: SQLiteJobStore) -> None:
    """The targeted per-pair delete leaves sibling results in place."""
    store.create_job("grid-single", username="alice")
    store.save_grid_pair_result("grid-single", 0, {"pair_index": 0})
    store.save_grid_pair_result("grid-single", 1, {"pair_index": 1})

    store.delete_grid_pair_result("grid-single", 0)

    assert set(store.get_grid_pair_results("grid-single")) == {1}


def test_update_job_unknown_field_raises_value_error(store: SQLiteJobStore) -> None:
    """Update job unknown field raises value error."""
    store.create_job("uf-1")
    with pytest.raises(ValueError, match="Unknown field"):
        store.update_job("uf-1", bogus_field="x")


def test_update_job_unknown_field_message_contains_field_name(store: SQLiteJobStore) -> None:
    """Update job unknown field message contains field name."""
    store.create_job("uf-2")
    with pytest.raises(ValueError, match="totally_bogus"):
        store.update_job("uf-2", totally_bogus="value")


def test_update_job_unknown_field_does_not_corrupt_existing_data(store: SQLiteJobStore) -> None:
    """Update job unknown field does not corrupt existing data."""
    store.create_job("uf-3")
    store.update_job("uf-3", status="running")
    with pytest.raises(ValueError):
        store.update_job("uf-3", not_a_real_field=42)
    # The earlier valid update must still be in place
    assert store.get_job("uf-3")["status"] == "running"


def test_run_log_is_never_trimmed(store: SQLiteJobStore) -> None:
    """A run's log keeps every line; its size counts against storage instead."""
    store.create_job("no-trim")
    store.append_logs("no-trim", [{"level": "INFO", "logger": "lg", "message": f"msg-{i}"} for i in range(6000)])

    assert store.get_log_count("no-trim") == 6000
    assert store.get_logs("no-trim", limit=1)[0]["message"] == "msg-0"


def test_run_log_fields_round_trip(store: SQLiteJobStore) -> None:
    """Source, event, fields, candidate and case survive the store."""
    store.create_job("fields")
    store.append_log(
        "fields",
        level="INFO",
        logger_name="sandbox.skynet.engine",
        message="Candidate 3 scored 0.9 on case 7",
        source="engine",
        event="case.scored",
        fields={"score": 0.9, "total": 20},
        candidate="3",
        case="7",
    )
    store.append_log("fields", level="INFO", logger_name="core.worker", message="plain")

    scored, plain = store.get_logs("fields")
    assert scored["source"] == "engine"
    assert scored["event"] == "case.scored"
    assert scored["fields"] == {"score": 0.9, "total": 20}
    assert (scored["candidate"], scored["case"]) == ("3", "7")
    assert plain["source"] == "host"
    assert plain["event"] is None


def test_run_log_cuts_text_to_its_column(store: SQLiteJobStore) -> None:
    """A value longer than its column is cut to fit and ends with an ellipsis."""
    store.create_job("long")
    store.append_log("long", level="INFO", logger_name="lg", message="m" * 50_000, event="e" * 400)

    (entry,) = store.get_logs("long")
    assert len(entry["message"]) == 50_000
    assert len(entry["event"]) == 255
    assert entry["event"].endswith("...")


def test_get_logs_resumes_after_an_id(store: SQLiteJobStore) -> None:
    """A live reader gets only the lines written after the last one it saw."""
    store.create_job("resume")
    store.append_logs("resume", [{"level": "INFO", "logger": "lg", "message": f"msg-{i}"} for i in range(5)])
    seen = store.get_logs("resume", limit=2)

    rest = store.get_logs("resume", after_id=seen[-1]["id"])

    assert [entry["message"] for entry in rest] == ["msg-2", "msg-3", "msg-4"]


def test_progress_eviction_oldest_entry_removed_when_cap_reached(
    store: SQLiteJobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Progress eviction oldest entry removed when cap reached."""
    monkeypatch.setattr(remote_mod, "MAX_PROGRESS_EVENTS", 3)
    monkeypatch.setattr(remote_mod, "PROGRESS_TRIM_SAMPLE_RATE", 1)

    store.create_job("evict-prog-1")
    for i in range(3):
        store.record_progress("evict-prog-1", f"step-{i}", {"i": i})

    # Adding a 4th entry must evict step-0 (the oldest)
    store.record_progress("evict-prog-1", "step-3", {"i": 3})

    events = store.get_progress_events("evict-prog-1")
    event_names = [e["event"] for e in events]
    assert "step-0" not in event_names
    assert "step-3" in event_names


def test_progress_eviction_count_stays_at_cap(store: SQLiteJobStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """Progress eviction count stays at cap."""
    monkeypatch.setattr(remote_mod, "MAX_PROGRESS_EVENTS", 3)
    monkeypatch.setattr(remote_mod, "PROGRESS_TRIM_SAMPLE_RATE", 1)

    store.create_job("evict-prog-2")
    for i in range(5):
        store.record_progress("evict-prog-2", f"step-{i}", {"i": i})

    assert store.get_progress_count("evict-prog-2") == 3


def test_progress_eviction_preserves_structural_events(store: SQLiteJobStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """Structural phase markers survive eviction while high-volume rows get dropped."""
    monkeypatch.setattr(remote_mod, "MAX_PROGRESS_EVENTS", 3)
    monkeypatch.setattr(remote_mod, "PROGRESS_TRIM_SAMPLE_RATE", 1)

    store.create_job("evict-prog-3")
    store.record_progress("evict-prog-3", "grid_pair_started", {"pair_index": 0})
    store.record_progress("evict-prog-3", "optimizer_progress", {"step": 0})
    store.record_progress("evict-prog-3", "optimizer_progress", {"step": 1})
    # Adding a 4th entry should evict the oldest optimizer_progress, not the
    # structural grid_pair_started marker.
    store.record_progress("evict-prog-3", "grid_pair_completed", {"pair_index": 0})

    events = store.get_progress_events("evict-prog-3")
    names = [e["event"] for e in events]
    assert "grid_pair_started" in names
    assert "grid_pair_completed" in names
    # Exactly one optimizer_progress survived — the newer one.
    assert names.count("optimizer_progress") == 1
    surviving = next(e for e in events if e["event"] == "optimizer_progress")
    assert surviving["metrics"] == {"step": 1}


def test_progress_eviction_structural_dropped_only_as_last_resort(
    store: SQLiteJobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When every existing row is structural, the oldest structural row is evicted."""
    monkeypatch.setattr(remote_mod, "MAX_PROGRESS_EVENTS", 2)
    monkeypatch.setattr(remote_mod, "PROGRESS_TRIM_SAMPLE_RATE", 1)

    store.create_job("evict-prog-4")
    store.record_progress("evict-prog-4", "grid_pair_started", {"pair_index": 0})
    store.record_progress("evict-prog-4", "baseline_evaluated", {"pair_index": 0})
    # Cap is full and no non-structural rows exist — oldest structural gets evicted.
    store.record_progress("evict-prog-4", "optimized_evaluated", {"pair_index": 0})

    events = store.get_progress_events("evict-prog-4")
    names = [e["event"] for e in events]
    assert "grid_pair_started" not in names
    assert "baseline_evaluated" in names
    assert "optimized_evaluated" in names


@pytest.mark.parametrize("phase", ["explore", "continue"])
def test_auto_run_progress_is_never_trimmed(store: SQLiteJobStore, monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    """A run that recorded an Auto lane keeps every progress event past the cap.

    Args:
        store: In-memory job store.
        monkeypatch: Patch fixture.
        phase: Auto lane phase the run reported.
    """
    monkeypatch.setattr(remote_mod, "MAX_PROGRESS_EVENTS", 3)
    monkeypatch.setattr(remote_mod, "PROGRESS_TRIM_SAMPLE_RATE", 1)

    store.create_job("auto-run")
    store.record_progress("auto-run", "lane_started", {"engine": "gepa", "phase": phase, "budget": 4})
    for i in range(10):
        store.record_progress("auto-run", "optimizer_progress", {"i": i})

    assert store.get_progress_count("auto-run") == 11
    assert store.get_progress_events("auto-run")[1]["metrics"] == {"i": 0}


def test_single_engine_run_progress_keeps_the_cap(store: SQLiteJobStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """A single-engine run's lane marker does not lift the retention cap."""
    monkeypatch.setattr(remote_mod, "MAX_PROGRESS_EVENTS", 3)
    monkeypatch.setattr(remote_mod, "PROGRESS_TRIM_SAMPLE_RATE", 1)

    store.create_job("single-run")
    store.record_progress("single-run", "lane_started", {"engine": "gepa", "phase": "single", "budget": 4})
    for i in range(10):
        store.record_progress("single-run", "optimizer_progress", {"i": i})

    assert store.get_progress_count("single-run") == 3


def test_record_progress_does_not_lock_parent_job_row() -> None:
    """``record_progress`` does not use ``SELECT ... FOR UPDATE`` on jobs."""
    source = inspect.getsource(RemoteDBJobStore.record_progress)

    assert "with_for_update" not in source


def test_update_job_does_not_lock_parent_job_row() -> None:
    """``update_job`` does not use ``SELECT ... FOR UPDATE`` on jobs.

    Row-level locks held across a SELECT+UPDATE round-trip serialise every
    writer touching the same id and become a contention hotspot at scale.
    ``record_progress`` already uses a non-locking pattern; ``update_job``
    must too.
    """
    source = inspect.getsource(RemoteDBJobStore.update_job)

    assert "with_for_update" not in source


def test_progress_trim_is_sampled_and_batched(store: SQLiteJobStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sampled trimming allows a small overshoot but keeps rows near the cap."""
    monkeypatch.setattr(remote_mod, "MAX_PROGRESS_EVENTS", 5000)
    monkeypatch.setattr(remote_mod, "PROGRESS_TRIM_SAMPLE_RATE", 100)

    store.create_job("sampled-trim")
    for i in range(5200):
        store.record_progress("sampled-trim", "optimizer_progress", {"i": i})

    assert 5000 <= store.get_progress_count("sampled-trim") <= 5100


def test_engine_kwargs_use_configured_pool_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Engine kwargs read DB pool settings from configuration."""
    monkeypatch.setattr(remote_mod.settings, "db_pool_size", 12)
    monkeypatch.setattr(remote_mod.settings, "db_pool_max_overflow", 7)
    monkeypatch.setattr(remote_mod.settings, "db_pool_recycle_seconds", 600)

    kwargs = remote_mod._build_engine_kwargs("postgresql://user:pass@localhost/skynet")

    assert kwargs["pool_size"] == 12
    assert kwargs["max_overflow"] == 7
    assert kwargs["pool_recycle"] == 600


def test_pgbouncer_transaction_mode_disables_psycopg_prepared_statements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Psycopg3 URLs disable server-side prepared statements for PgBouncer."""
    monkeypatch.setattr(remote_mod.settings, "db_pgbouncer_transaction_mode", True)

    connect_args = remote_mod._build_connect_args("postgresql+psycopg://user:pass@localhost/skynet")

    assert connect_args["prepare_threshold"] is None


def test_create_job_persists_username_and_idempotency_key(store: SQLiteJobStore) -> None:
    """``create_job`` records the submitter and dedup key when supplied."""
    store.create_job("idem-create-1", username="alice", idempotency_key="abc-123")

    row = store.get_job("idem-create-1")
    assert row["username"] == "alice"


def test_find_job_by_idempotency_key_returns_prior_submission(store: SQLiteJobStore) -> None:
    """Looking up a prior ``(username, idempotency_key)`` pair returns its job id."""
    store.create_job("idem-find-1", username="bob", idempotency_key="key-xyz")

    found = store.find_job_by_idempotency_key("bob", "key-xyz")

    assert found == "idem-find-1"


def test_find_job_by_idempotency_key_scopes_by_username(store: SQLiteJobStore) -> None:
    """Two users sharing the same key resolve to distinct jobs."""
    store.create_job("idem-scope-a", username="alice", idempotency_key="shared")
    store.create_job("idem-scope-b", username="bob", idempotency_key="shared")

    assert store.find_job_by_idempotency_key("alice", "shared") == "idem-scope-a"
    assert store.find_job_by_idempotency_key("bob", "shared") == "idem-scope-b"


def test_find_job_by_idempotency_key_returns_none_for_unknown(store: SQLiteJobStore) -> None:
    """Unknown key returns ``None`` rather than raising."""
    assert store.find_job_by_idempotency_key("alice", "never-seen") is None


def test_find_job_by_idempotency_key_rejects_empty_inputs(store: SQLiteJobStore) -> None:
    """Empty username or key short-circuits to ``None`` (no wildcard match)."""
    store.create_job("idem-empty-1", username="alice", idempotency_key="real-key")

    assert store.find_job_by_idempotency_key("", "real-key") is None
    assert store.find_job_by_idempotency_key("alice", "") is None


def test_claim_completion_notification_first_caller_wins(store: SQLiteJobStore) -> None:
    """The first caller to claim a job's notification wins."""
    store.create_job("notify-claim-1")

    assert store.claim_completion_notification("notify-claim-1") is True


def test_claim_completion_notification_second_caller_loses(store: SQLiteJobStore) -> None:
    """The second caller for the same job loses the CAS race."""
    store.create_job("notify-claim-2")

    store.claim_completion_notification("notify-claim-2")

    assert store.claim_completion_notification("notify-claim-2") is False


def test_claim_completion_notification_missing_job_returns_false(store: SQLiteJobStore) -> None:
    """A missing job id reports ``False`` rather than raising."""
    assert store.claim_completion_notification("no-such-job") is False


def test_claim_job_notification_is_once_per_kind(store: SQLiteJobStore) -> None:
    """Each in-run email kind is claimable once; distinct kinds are independent."""
    store.create_job("run-claim-1")

    assert store.claim_job_notification("run-claim-1", "new_best") is True
    assert store.claim_job_notification("run-claim-1", "new_best") is False
    assert store.claim_job_notification("run-claim-1", "stuck") is True


def test_claim_job_notification_honours_prefix_cap(store: SQLiteJobStore) -> None:
    """Claims sharing a prefix stop at the cap while other kinds still succeed."""
    store.create_job("run-claim-2")

    assert store.claim_job_notification("run-claim-2", "stage:a", cap_prefix="stage:", cap=2) is True
    assert store.claim_job_notification("run-claim-2", "stage:b", cap_prefix="stage:", cap=2) is True
    assert store.claim_job_notification("run-claim-2", "stage:c", cap_prefix="stage:", cap=2) is False
    assert store.claim_job_notification("run-claim-2", "budget") is True


def test_claim_job_notification_missing_job_returns_false(store: SQLiteJobStore) -> None:
    """A missing job id reports ``False`` rather than raising."""
    assert store.claim_job_notification("no-such-run", "new_best") is False


def test_requeue_for_rerun_clears_notification_claims(store: SQLiteJobStore) -> None:
    """A from-scratch rerun may send its in-run emails again."""
    store.create_job("run-claim-3")
    store.claim_job_notification("run-claim-3", "new_best")
    store.update_job("run-claim-3", status="failed")

    assert store.requeue_for_rerun("run-claim-3") is True
    assert store.claim_job_notification("run-claim-3", "new_best") is True


def test_immutable_columns_include_notification_and_idempotency_fields() -> None:
    """``_IMMUTABLE_JOB_COLUMNS`` guards the new dedup columns from ``update_job``."""
    assert "notified_at" in remote_mod._IMMUTABLE_JOB_COLUMNS
    assert "notification_claims" in remote_mod._IMMUTABLE_JOB_COLUMNS
    assert "idempotency_key" in remote_mod._IMMUTABLE_JOB_COLUMNS


def test_gepa_checkpoint_save_get_delete_roundtrip(store: SQLiteJobStore) -> None:
    """A checkpoint saves, reads back with its iteration/size, replaces, and deletes."""
    store.create_job("ck1")
    assert store.has_gepa_checkpoint("ck1") is False
    assert store.get_gepa_checkpoint("ck1") is None

    store.save_gepa_checkpoint("ck1", b"STATE-A", iteration=3)
    assert store.has_gepa_checkpoint("ck1") is True
    cp = store.get_gepa_checkpoint("ck1")
    assert cp is not None
    assert cp.data == b"STATE-A"
    assert cp.iteration == 3
    assert cp.stored_bytes == 7

    store.save_gepa_checkpoint("ck1", b"STATE-B-longer", iteration=9)
    cp = store.get_gepa_checkpoint("ck1")
    assert cp.data == b"STATE-B-longer"
    assert cp.iteration == 9

    store.delete_gepa_checkpoint("ck1")
    assert store.has_gepa_checkpoint("ck1") is False
    store.delete_gepa_checkpoint("ck1")  # idempotent no-op


def test_delete_job_removes_its_checkpoint(store: SQLiteJobStore) -> None:
    """Deleting a job drops its checkpoint row too (no orphaned bytes)."""
    store.create_job("ck2")
    store.save_gepa_checkpoint("ck2", b"STATE", iteration=1)
    store.delete_job("ck2")
    assert store.has_gepa_checkpoint("ck2") is False


def test_requeue_for_resume_flips_to_pending_and_bumps_attempts(
    store: SQLiteJobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resume re-queues in place: same id, pending, claim cleared, attempts+1.

    Args:
        store: Disposable job store.
        monkeypatch: Pytest fixture used to bind the sandbox profile.
    """
    store.create_job("ck3")
    store.update_job("ck3", status="failed", attempts=0, claimed_by="pod-x", completed_at="2026-01-01T00:00:00Z")

    budget_service = _fund_checkpoint(store, "ck3", monkeypatch)
    new_attempt = store.requeue_for_resume("ck3", budget_service=budget_service)
    assert new_attempt == 1
    job = store.get_job("ck3")
    assert job["status"] == "pending"
    assert job["attempts"] == 1
    assert job.get("claimed_by") is None
    assert job.get("completed_at") is None
    assert job.get("recovery") is None


def test_requeue_for_resume_missing_job_returns_none(store: SQLiteJobStore) -> None:
    """Re-queuing an unknown id reports ``None`` rather than raising."""
    assert store.requeue_for_resume("no-such-job") is None


def test_requeue_for_resume_banks_finished_leg_runtime(store: SQLiteJobStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each resume folds the finished leg's duration in; the paused gap is excluded.

    Args:
        store: Disposable job store.
        monkeypatch: Pytest fixture used to bind the sandbox profile.
    """
    store.create_job("ck-acc")
    store.update_job(
        "ck-acc",
        status="failed",
        attempts=0,
        started_at="2026-01-01T00:00:00Z",
        completed_at="2026-01-01T00:01:05Z",  # leg 1 = 65s
    )

    budget_service = _fund_checkpoint(store, "ck-acc", monkeypatch)
    store.requeue_for_resume("ck-acc", budget_service=budget_service)
    job = store.get_job("ck-acc")
    assert job["accumulated_runtime_seconds"] == 65.0
    assert job.get("started_at") is None
    assert job.get("completed_at") is None

    store.update_job(
        "ck-acc",
        status="failed",
        started_at="2026-01-01T00:30:00Z",  # ~29 min paused before this leg
        completed_at="2026-01-01T00:30:30Z",  # leg 2 = 30s
    )
    store.requeue_for_resume("ck-acc", budget_service=budget_service)
    job = store.get_job("ck-acc")
    assert job["accumulated_runtime_seconds"] == 95.0  # 65 + 30, the pause is never counted


def test_gepa_checkpoints_are_isolated_per_grid_pair(store: SQLiteJobStore) -> None:
    """A grid keeps an independent checkpoint per pair index; delete-all clears them."""
    store.create_job("g1")
    store.save_gepa_checkpoint("g1", b"P0", iteration=1, pair_index=0)
    store.save_gepa_checkpoint("g1", b"P1", iteration=2, pair_index=1)
    assert {c.pair_index for c in store.list_gepa_checkpoints("g1")} == {0, 1}
    assert store.get_gepa_checkpoint("g1", 1).data == b"P1"

    store.delete_gepa_checkpoint("g1", 0)
    assert {c.pair_index for c in store.list_gepa_checkpoints("g1")} == {1}
    assert store.has_gepa_checkpoint("g1") is True

    store.delete_all_gepa_checkpoints("g1")
    assert store.list_gepa_checkpoints("g1") == []
    assert store.has_gepa_checkpoint("g1") is False


def test_grid_pair_results_roundtrip(store: SQLiteJobStore) -> None:
    """Completed grid pairs are stored, read as ``{index: result}``, and cleared."""
    store.create_job("g2")
    assert store.has_grid_pair_results("g2") is False
    store.save_grid_pair_result("g2", 0, {"pair_index": 0, "optimized_test_metric": 0.8})
    store.save_grid_pair_result("g2", 3, {"pair_index": 3})
    assert store.has_grid_pair_results("g2") is True

    results = store.get_grid_pair_results("g2")
    assert set(results) == {0, 3}
    assert results[0]["optimized_test_metric"] == 0.8

    store.delete_grid_pair_results("g2")
    assert store.has_grid_pair_results("g2") is False


def test_delete_job_removes_grid_pair_results_and_pair_checkpoints(store: SQLiteJobStore) -> None:
    """Deleting a grid job drops its per-pair checkpoints and stored pair results."""
    store.create_job("g3")
    store.save_gepa_checkpoint("g3", b"P0", iteration=1, pair_index=0)
    store.save_grid_pair_result("g3", 1, {"pair_index": 1})
    store.delete_job("g3")
    assert store.has_gepa_checkpoint("g3") is False
    assert store.has_grid_pair_results("g3") is False


def test_scan_jobs_for_analytics_trims_run_result(store: SQLiteJobStore) -> None:
    """A run row keeps only the scalar metrics; per-example blobs never surface."""
    store.create_job("an-run", username="alice")
    store.set_payload_overview(
        "an-run",
        {
            "optimizer_name": "gepa",
            "model_name": "openai/gpt-4o",
            "optimization_type": "run",
            "dataset_rows": 600,
            "signature_code": "class Sig: ...",
        },
    )
    store.update_job(
        "an-run",
        status="success",
        result={
            "baseline_test_metric": 0.5,
            "optimized_test_metric": 0.75,
            "runtime_seconds": 12.5,
            "baseline_test_results": [{"index": i, "outputs": {"answer": "x" * 200}} for i in range(50)],
        },
    )

    [row] = store.scan_jobs_for_analytics()

    assert row["optimization_id"] == "an-run"
    assert row["status"] == "success"
    assert row["payload_overview"] == {
        "optimizer_name": "gepa",
        "model_name": "openai/gpt-4o",
        "optimization_type": "run",
        "dataset_rows": 600,
    }
    assert row["result"] == {
        "baseline_test_metric": 0.5,
        "optimized_test_metric": 0.75,
        "runtime_seconds": 12.5,
    }


def test_scan_jobs_for_analytics_grid_best_pair_subset(store: SQLiteJobStore) -> None:
    """A grid row carries pair counters plus the trimmed best_pair metrics."""
    store.create_job("an-grid", username="alice")
    store.set_payload_overview(
        "an-grid",
        {"optimizer_name": "gepa", "optimization_type": "grid_search", "total_pairs": 4},
    )
    store.update_job(
        "an-grid",
        status="success",
        result={
            "completed_pairs": 3,
            "failed_pairs": 1,
            "best_pair": {
                "baseline_test_metric": 0.4,
                "optimized_test_metric": 0.9,
                "runtime_seconds": 30.0,
                "pair_index": 2,
                "per_example_outputs": ["y" * 500] * 20,
            },
        },
    )

    [row] = store.scan_jobs_for_analytics()

    assert row["payload_overview"]["total_pairs"] == 4
    assert row["result"] == {
        "completed_pairs": 3,
        "failed_pairs": 1,
        "best_pair": {
            "baseline_test_metric": 0.4,
            "optimized_test_metric": 0.9,
            "runtime_seconds": 30.0,
        },
    }


def test_scan_jobs_for_analytics_null_result_and_filters(store: SQLiteJobStore) -> None:
    """Result-less rows read as None, and status/username/limit narrow the scan."""
    store.create_job("an-pending", username="alice")
    store.set_payload_overview("an-pending", {"optimizer_name": "gepa"})
    store.create_job("an-other", username="bob")
    store.update_job("an-other", status="running")

    rows = store.scan_jobs_for_analytics()
    by_id = {r["optimization_id"]: r for r in rows}
    assert by_id["an-pending"]["result"] is None
    assert by_id["an-pending"]["status"] == "pending"

    alice_rows = store.scan_jobs_for_analytics(username="alice")
    assert {r["optimization_id"] for r in alice_rows} == {"an-pending"}

    running_rows = store.scan_jobs_for_analytics(status="running")
    assert {r["optimization_id"] for r in running_rows} == {"an-other"}

    assert len(store.scan_jobs_for_analytics(limit=1)) == 1


def test_pin_sandbox_image_keeps_the_first_image(store: SQLiteJobStore) -> None:
    """Pin a run to its first image so a redeploy never moves it to another.

    Args:
        store: Disposable job store.
    """
    store.create_job("pin1")
    assert store.pin_sandbox_image("pin1", "img@sha256:first") == "img@sha256:first"
    assert store.pin_sandbox_image("pin1", "img@sha256:second") == "img@sha256:first"
    assert store.get_job("pin1")["sandbox_image"] == "img@sha256:first"
    assert store.requeue_for_rerun("pin1")
    assert store.get_job("pin1")["sandbox_image"] is None
