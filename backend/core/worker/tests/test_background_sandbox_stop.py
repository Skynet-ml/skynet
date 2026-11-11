"""Verify that a run ends before its boxes stop, and that the stop settles once or leaves the budget Pending.

Fakes stand in for the gateway, the broker, the budget ledger and the Vercel
provider, so no test opens a network connection or starts a box.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, insert
from sqlalchemy.pool import StaticPool

from core.billing.runtime import UsagePendingError
from core.constants import OPTIMIZATION_TYPE_RUN
from core.sandbox import factory
from core.sandbox.factory import PROVIDERS, ProviderEntry
from core.storage.base import JobStore
from core.storage.models import JobModel
from core.worker import engine

from .conftest import FakeJobStore
from .mocks import make_mp_context, make_result_event
from .test_engine_sandbox_sweep import SweepSandbox
from .test_protected_dspy_execution import _payload

JOB = "background-stop"


@dataclass
class _Budget:
    """Budget row fields the worker copies into the terminal evidence."""

    username: str
    status: str
    reserved_cents: int
    settled_cents: int
    account_available_cents: int = 0


class _Ledger:
    """Hold one budget that the broker settles when its boxes stop."""

    def __init__(self) -> None:
        """Start with the box usage reserved and nothing settled."""
        self.budget = _Budget(username="alice", status="open", reserved_cents=7, settled_cents=0)
        self.settles = 0

    def get(self, budget_id: str, username: str) -> _Budget:
        """Return the current budget row.

        Args:
            budget_id: Budget the run owns.
            username: Owner of the budget.

        Returns:
            The budget row.
        """
        del budget_id, username
        return self.budget

    def settle(self) -> None:
        """Move the reserved box usage to settled, as the provider teardown does."""
        self.settles += 1
        self.budget = _Budget(username="alice", status="settled", reserved_cents=0, settled_cents=7)


@dataclass
class _Runtime:
    """Budget runtime fields the worker reads to publish the snapshot."""

    service: _Ledger
    budget_id: str = "budget"
    username: str = "alice"


class _Broker:
    """Stop boxes on request, optionally waiting for the test or failing."""

    def __init__(self, ledger: _Ledger, store: FakeJobStore) -> None:
        """Keep the ledger to settle and the store to read the job status from.

        Args:
            ledger: Budget the stop settles.
            store: Job store whose status the stop records.
        """
        self.ledger = ledger
        self.store = store
        self.release = threading.Event()
        self.release.set()
        self.error: BaseException | None = None
        self.closes = 0
        self.status_at_close: str | None = None

    def close(self) -> None:
        """Record the job status, wait for the test, then settle or fail.

        Raises:
            BaseException: The error the test set.
        """
        self.closes += 1
        self.status_at_close = self.store.get_job(JOB)["status"]
        assert self.release.wait(timeout=10)
        if self.error is not None:
            raise self.error
        self.ledger.settle()


class _Gateway:
    """Hold the bound broker until the worker detaches it."""

    broker: _Broker | None = None
    ledger: _Ledger | None = None

    def __init__(self, runtime: Any, **_kwargs: Any) -> None:
        """Bind the test's ledger and broker in place of real transports.

        Args:
            runtime: The worker's budget runtime, replaced by the fake ledger.
        """
        del runtime
        assert _Gateway.ledger is not None
        self.runtime = _Runtime(_Gateway.ledger)
        self._sandbox = _Gateway.broker
        self.closes = 0

    def protect_payload(self, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        """Return the payload unchanged.

        Args:
            payload: Payload the worker protects.

        Returns:
            The same payload.
        """
        return payload

    def detach_sandbox(self) -> _Broker | None:
        """Take the broker out, as the real gateway does.

        Returns:
            The bound broker, or ``None``.
        """
        broker, self._sandbox = self._sandbox, None
        return broker

    def close(self) -> None:
        """Close the gateway, and the broker only when it is still bound."""
        self.closes += 1
        if self._sandbox is not None:
            self._sandbox.close()


@pytest.fixture
def store() -> FakeJobStore:
    """Seed one protected run job.

    Returns:
        The store with the job.
    """
    store = FakeJobStore()
    store.engine = object()  # type: ignore[attr-defined]
    store.seed_job(
        JOB,
        payload=_payload(Path("/nonexistent/marker"), "vercel"),
        payload_overview={"optimization_type": OPTIMIZATION_TYPE_RUN, "username": "alice", "token_source": "managed"},
        execution_budget_id="budget",
        execution_budget_generation=0,
    )
    return store


@pytest.fixture
def broker(store: FakeJobStore) -> _Broker:
    """Bind a fresh ledger and broker to the fake gateway class.

    Args:
        store: The job store the broker reads.

    Returns:
        The broker the next gateway owns.
    """
    ledger = _Ledger()
    _Gateway.ledger = ledger
    _Gateway.broker = _Broker(ledger, store)
    return _Gateway.broker


def _run_job(store: FakeJobStore, monkeypatch: pytest.MonkeyPatch, *, exitcode: int = 0) -> engine.BackgroundWorker:
    """Run the job through the real worker path with the fake gateway.

    Args:
        store: The seeded job store.
        monkeypatch: Fixture for the parent credential setting.
        exitcode: Exit code of the fake run process; non-zero fails the run.

    Returns:
        The worker, so the test can wait for its background stops.
    """
    worker = engine.BackgroundWorker(job_store=cast(JobStore, store), num_workers=1)
    worker.enqueue_job(JOB)
    events = [make_result_event()] if exitcode == 0 else []
    context, _process = make_mp_context(exitcode=exitcode, result_events=events)
    monkeypatch.setattr(engine.settings, "openrouter_api_key", SecretStr("fixture-only"))
    with (
        patch("core.worker.engine.notify_job_completed"),
        patch("core.worker.engine.record_server_event"),
        patch("core.worker.engine.bind_protected_sandbox"),
        patch("core.worker.engine.payload_uses_token_source", return_value=False),
        patch("core.worker.engine.ModelGateway", _Gateway),
        patch("core.worker.engine.mp.get_context", return_value=context),
        patch.object(worker, "_get_service", return_value=MagicMock()),
        patch.object(worker, "_schedule_embedding_indexing"),
    ):
        worker._process_job(JOB, 0)
    return worker


def _stop_worker(worker: engine.BackgroundWorker) -> None:
    """Run the worker's shutdown join as if one finished worker thread had started the job.

    Args:
        worker: The worker whose background stops to join.
    """
    finished = threading.Thread(target=lambda: None)
    finished.start()
    worker._running = True
    worker._threads = [finished]
    worker.stop(timeout=10)


def _budget_evidence(store: FakeJobStore) -> dict[str, Any]:
    """Return the budget snapshot stored in the job's terminal evidence.

    Args:
        store: The job store.

    Returns:
        The stored budget snapshot.
    """
    return store.get_job(JOB)["terminal_evidence"]["execution_budget"]


def test_the_run_ends_before_its_boxes_stop_and_then_settles_once(
    store: FakeJobStore, broker: _Broker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Write the terminal status first, keep the budget Pending, then publish the settled budget once."""
    broker.release.clear()

    worker = _run_job(store, monkeypatch)

    assert store.get_job(JOB)["status"] == "success"
    assert _budget_evidence(store)["status"] == "open"
    assert _budget_evidence(store)["reserved_cents"] == 7
    assert len(worker._sandbox_stops) == 1

    broker.release.set()
    _stop_worker(worker)

    assert broker.status_at_close == "success"
    assert broker.closes == 1
    assert broker.ledger.settles == 1
    assert worker._sandbox_stops == set()
    assert _budget_evidence(store)["status"] == "settled"
    assert _budget_evidence(store)["settled_cents"] == 7
    assert store.get_job(JOB)["status"] == "success"


def test_a_failed_run_writes_its_status_without_waiting_for_the_box_stop(
    store: FakeJobStore, broker: _Broker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Write the failure while the background stop still waits, then settle once."""
    broker.release.clear()

    worker = _run_job(store, monkeypatch, exitcode=1)

    assert store.get_job(JOB)["status"] == "failed"
    assert broker.ledger.settles == 0
    broker.release.set()
    _stop_worker(worker)

    assert store.get_job(JOB)["status"] == "failed"
    assert broker.closes == 1
    assert broker.ledger.settles == 1
    assert _budget_evidence(store)["status"] == "settled"


@pytest.mark.parametrize(
    "error",
    [RuntimeError("Vercel refused the stop"), UsagePendingError("usage awaits reconciliation")],
    ids=["stop-error", "usage-pending"],
)
def test_a_failed_stop_keeps_the_run_result_and_leaves_the_budget_pending(
    store: FakeJobStore, broker: _Broker, monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    """Keep the success status and the reserved budget when the background stop fails."""
    broker.error = error

    worker = _run_job(store, monkeypatch)
    _stop_worker(worker)

    assert store.get_job(JOB)["status"] == "success"
    assert broker.closes == 1
    assert broker.ledger.settles == 0
    assert _budget_evidence(store)["status"] == "open"
    assert _budget_evidence(store)["reserved_cents"] == 7
    assert worker._sandbox_stops == set()


def test_the_sweep_stops_the_box_that_a_failed_background_stop_left(
    store: FakeJobStore, broker: _Broker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Let the terminal-job sweep find and stop the box after the background stop fails."""
    monkeypatch.setattr(factory.settings, "app_environment", "")
    monkeypatch.setattr(engine.settings, "local_mode", False)
    monkeypatch.setitem(PROVIDERS, "vercel", ProviderEntry(SweepSandbox.__module__, "SweepSandbox", None, True))
    monkeypatch.setattr(SweepSandbox, "swept", [])
    monkeypatch.setattr(SweepSandbox, "stopped", 1)
    monkeypatch.setattr(SweepSandbox, "error", None)
    monkeypatch.setattr(SweepSandbox, "unavailable", False)
    monkeypatch.setattr(SweepSandbox, "on_sweep", None)
    broker.error = RuntimeError("worker lost the Vercel connection")

    worker = _run_job(store, monkeypatch)
    _stop_worker(worker)
    jobs = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    JobModel.__table__.create(jobs)
    with jobs.begin() as conn:
        conn.execute(
            insert(JobModel),
            [{"optimization_id": JOB, "status": store.get_job(JOB)["status"], "completed_at": datetime.now(UTC)}],
        )
    store.engine = jobs  # type: ignore[attr-defined]

    swept = worker._sweep_terminal_job_sandboxes()

    assert broker.ledger.settles == 0
    assert SweepSandbox.swept == [JOB]
    assert swept == 1
