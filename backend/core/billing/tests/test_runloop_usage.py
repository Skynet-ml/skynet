"""Verify Runloop devbox quotes and settlement against a local ledger, without paid calls."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

from core.billing.budgets import BudgetService
from core.billing.operation_pricing import UnpricedOperationError
from core.billing.runloop_usage import USAGE, RunloopUsageReservation, quote_runloop_devbox, runloop_reported_usd
from core.billing.runtime import BudgetRuntime, UsagePendingError
from core.config import settings
from core.sandbox.metering import usage_adapter
from core.storage.models import Base, BillingCustomerModel, ExecutionOperationModel

CREATE = {"lifetime_ms": 3_600_000, "vcpus": 2, "memory_gb": 4, "disk_gb": 16, "network_disabled": True}
# 2 x 0.108 + 4 x 0.0252 + 16 x 0.00034236 USD for each hour.
HOURLY_USD = Decimal("0.32227776")
REPORT = {
    "id": "dbx-1",
    "status": "shutdown",
    "start_time_ms": 1_000,
    "end_time_ms": 601_000,
    "total_active_seconds": 600,
    "total_elapsed_seconds": 600,
    "vcpu_seconds": 1_200,
    "memory_gb_seconds": 2_400,
    "disk_gb_seconds": 9_600,
}


@pytest.fixture(autouse=True)
def _at_cost_markup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the usage markup to 1.0 so these tests check settlement arithmetic, not pricing policy.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
    """
    monkeypatch.setattr(settings, "usage_markup", 1.0)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Engine]:
    """Create a private funded wallet and ledger.

    Args:
        tmp_path: Per-test directory for the SQLite file.

    Yields:
        The engine bound to the funded ledger.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'runloop.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username="alice", stripe_customer_id="fixture", balance_cents=100, grant_remaining=0)
        )
        session.commit()
    yield engine
    engine.dispose()


def _runtime(database: Engine) -> BudgetRuntime:
    """Bind a real budget runtime to the local ledger.

    Args:
        database: Engine of the funded ledger.

    Returns:
        A runtime for a fresh 50-cent budget owned by ``alice``.
    """
    service = BudgetService(engine=database)
    budget = service.create("alice", 50, idempotency_key="draft")
    return BudgetRuntime(service, username="alice", budget_id=budget.id, generation=0, phase="setup", wait_timeout=0)


def _state(database: Engine) -> str:
    """Return the state of the one operation in the ledger.

    Args:
        database: Engine of the ledger.

    Returns:
        The operation state.
    """
    with Session(database) as session:
        return session.scalar(select(ExecutionOperationModel)).state


def test_the_adapter_is_registered() -> None:
    """Let hosted Skynet find the Runloop usage adapter by its provider key."""
    assert usage_adapter("runloop") is USAGE
    assert USAGE.name == "runloop"


def test_quote_covers_the_whole_lifetime_at_the_published_rates() -> None:
    """Price CPU, memory and disk for every hour of the keep-alive."""
    quote = USAGE.quote(CREATE)
    assert quote.maximum.total == quote.maximum.wallet == HOURLY_USD * 100
    assert quote.price_snapshot["price_version"] == "runloop-public-2026-11-24"
    assert quote.price_snapshot["policy"]["kind"] == "sandbox"


@pytest.mark.parametrize(
    "change",
    [
        {"lifetime_ms": 0},
        {"lifetime_ms": 172_800_001},
        {"lifetime_ms": None},
        {"vcpus": 3},
        {"vcpus": 18},
        {"vcpus": None},
        {"memory_gb": 2},
        {"memory_gb": 34},
        {"disk_gb": 1},
        {"disk_gb": 66},
        {"disk_gb": True},
    ],
)
def test_unbounded_devboxes_are_not_quoted(change: dict[str, Any]) -> None:
    """Refuse a lifetime or size that has no enforceable price.

    Args:
        change: The create fields to replace.
    """
    with pytest.raises(UnpricedOperationError):
        quote_runloop_devbox({**CREATE, **change})


def test_a_reservation_without_a_lifetime_is_not_priced(database: Engine) -> None:
    """Refuse a create request with no keep-alive lifetime before any reserve.

    Args:
        database: The funded ledger.
    """
    runtime = _runtime(database)
    request = {key: value for key, value in CREATE.items() if key != "lifetime_ms"}
    with pytest.raises(UnpricedOperationError, match="keep-alive"):
        RunloopUsageReservation(runtime, request, operation_key="box-one")
    assert runtime.service.get(runtime.budget_id, "alice").reserved_cents == 0


def test_reported_usage_settles_at_its_counters(database: Engine) -> None:
    """Charge the vCPU, memory and disk seconds that Runloop reports, and unpin the hold."""
    runtime = _runtime(database)
    reservation = USAGE.reserve(runtime, CREATE, operation_key="box-one")
    assert reservation.operation.id in runtime._pinned
    USAGE.confirm(reservation, type("Devbox", (), {"id": "dbx-1"})())
    reservation.record_stop(700_000)
    reservation.record_usage(REPORT)
    USAGE.settle(reservation)
    operation = runtime.service.get_operation(reservation.operation.id, "alice")
    expected = (1_200 * Decimal("0.108") + 2_400 * Decimal("0.0252") + 9_600 * Decimal("0.00034236")) / 3600 * 100
    assert operation.state == "settled"
    assert operation.actual_cents == expected
    assert operation.provider_request_id == "dbx-1"
    assert reservation.operation.id not in runtime._pinned


def test_reported_usd_needs_final_usage_of_the_same_devbox() -> None:
    """Refuse a report of another box, a running box, or a missing counter."""
    snapshot = quote_runloop_devbox(CREATE).price_snapshot
    for report in (
        {**REPORT, "id": "dbx-2"},
        {**REPORT, "status": "running"},
        {**REPORT, "end_time_ms": None},
        {**REPORT, "vcpu_seconds": None},
        {**REPORT, "disk_gb_seconds": -1},
    ):
        with pytest.raises(UsagePendingError):
            runloop_reported_usd(report, devbox_id="dbx-1", price_snapshot=snapshot)


def test_a_missing_usage_report_falls_back_to_measured_time(database: Engine) -> None:
    """Settle from the measured time at the full rate when the report is not final."""
    runtime = _runtime(database)
    reservation = RunloopUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.bind("dbx-1")
    reservation.record_stop(599_001)
    reservation.record_usage({**REPORT, "status": "running", "end_time_ms": None})
    reservation.settle()
    operation = runtime.service.get_operation(reservation.operation.id, "alice")
    assert operation.actual_cents == 600 * HOURLY_USD / 3600 * 100
    assert operation.state == "settled"


def test_an_unconfirmed_stop_without_a_report_stays_pending(database: Engine) -> None:
    """Keep the hold pending, not charge the full keep-alive, with no report and no confirmed shutdown."""
    runtime = _runtime(database)
    reservation = RunloopUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.bind("dbx-1")
    with pytest.raises(UsagePendingError, match="not confirmed"):
        reservation.settle()
    assert _state(database) == "pending"
    assert runtime.service.get(runtime.budget_id, "alice").billed_cents == 0


def test_an_unconfirmed_stop_with_a_final_report_settles_from_it(database: Engine) -> None:
    """Settle from Runloop's final report even when the shutdown was not confirmed."""
    runtime = _runtime(database)
    reservation = RunloopUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.bind("dbx-1")
    reservation.record_usage(REPORT)
    reservation.settle()
    operation = runtime.service.get_operation(reservation.operation.id, "alice")
    assert operation.state == "settled"
    assert operation.actual_cents < operation.max_cents


def test_measured_time_uses_the_admitted_rates(database: Engine) -> None:
    """Price measured time from the admitted price snapshot, not the current module rates."""
    runtime = _runtime(database)
    reservation = RunloopUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.bind("dbx-1")
    reservation.quote.price_snapshot["cpu_hour_usd"] = "0"
    reservation.record_stop(3_600_000)
    reservation.settle()
    operation = runtime.service.get_operation(reservation.operation.id, "alice")
    assert operation.actual_cents == (HOURLY_USD - 2 * Decimal("0.108")) * 100


def test_a_failed_bind_leaves_the_devbox_unbound(database: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the devbox id unset when the ledger refuses the dispatch record, so a retry binds it.

    Args:
        database: The funded ledger.
        monkeypatch: The pytest monkeypatch fixture.
    """
    runtime = _runtime(database)
    reservation = RunloopUsageReservation(runtime, CREATE, operation_key="box-one")

    def refuse(*args: Any) -> None:
        """Fail the dispatch record.

        Args:
            *args: The mark_dispatched arguments.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("ledger down")

    monkeypatch.setattr(runtime.service, "mark_dispatched", refuse)
    with pytest.raises(RuntimeError):
        reservation.bind("dbx-1")
    assert reservation.devbox_id is None
    monkeypatch.delattr(runtime.service, "mark_dispatched")
    reservation.bind("dbx-1")
    assert reservation.devbox_id == "dbx-1"


def test_settle_clamps_to_the_reservation(database: Engine) -> None:
    """Never charge past the reservation, even when Runloop counts more time."""
    runtime = _runtime(database)
    reservation = RunloopUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.bind("dbx-1")
    reservation.record_usage({**REPORT, "vcpu_seconds": 72_000, "memory_gb_seconds": 144_000})
    reservation.settle()
    operation = runtime.service.get_operation(reservation.operation.id, "alice")
    assert operation.actual_cents == operation.max_cents
    assert operation.actual_wallet_cents == operation.max_wallet_cents
    assert operation.state == "settled"


def test_a_second_settle_charges_once(database: Engine) -> None:
    """Keep quiet when the hold already settled."""
    runtime = _runtime(database)
    reservation = RunloopUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.bind("dbx-1")
    reservation.record_usage(REPORT)
    reservation.settle()
    billed = runtime.service.get(runtime.budget_id, "alice").billed_cents
    reservation.settle()
    assert runtime.service.get(runtime.budget_id, "alice").billed_cents == billed


def test_a_failed_settle_keeps_the_hold_pending(database: Engine) -> None:
    """Keep the hold for reconciliation when the admitted price snapshot is unusable."""
    runtime = _runtime(database)
    reservation = RunloopUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.bind("dbx-1")
    reservation.record_usage(REPORT)
    reservation.quote.price_snapshot["policy"] = None
    with pytest.raises(UsagePendingError, match="cent conversion"):
        reservation.settle()
    assert _state(database) == "pending"


def test_a_failed_pending_mark_keeps_the_settle_error(database: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Raise the settle failure, not the ledger error of the pending mark after it.

    Args:
        database: The funded ledger.
        monkeypatch: The pytest monkeypatch fixture.
    """
    runtime = _runtime(database)
    reservation = RunloopUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.bind("dbx-1")

    def refuse() -> None:
        """Fail the pending mark.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("ledger down")

    monkeypatch.setattr(reservation, "pending", refuse)
    with pytest.raises(UsagePendingError, match="not confirmed"):
        reservation.settle()


def test_a_devbox_that_was_never_created_releases_its_hold(database: Engine) -> None:
    """Return the whole hold when the create call was never sent or Runloop refused it."""
    runtime = _runtime(database)
    reservation = RunloopUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.release("The create call was never sent.")
    assert runtime._pinned == {}
    assert runtime.service.get(runtime.budget_id, "alice").reserved_cents == 0
    assert _state(database) == "released"


def test_a_dispatched_operation_is_not_claimed_twice(database: Engine) -> None:
    """Refuse a second reservation of a devbox that is already dispatched."""
    runtime = _runtime(database)
    RunloopUsageReservation(runtime, CREATE, operation_key="box-one")
    with pytest.raises(UsagePendingError, match="already dispatched"):
        RunloopUsageReservation(runtime, CREATE, operation_key="box-one")


def test_extend_funds_a_longer_lifetime(database: Engine) -> None:
    """Raise the hold before a longer keep-alive, and refuse one that does not grow."""
    runtime = _runtime(database)
    request = {**CREATE, "lifetime_ms": 600_000}
    reservation = RunloopUsageReservation(runtime, request, operation_key="box-one")
    before = reservation.operation.max_cents
    USAGE.extend(reservation, 1_200_000, deadline=0.0)
    assert reservation.operation.max_cents == 2 * before
    assert reservation.lifetime_ms == 1_200_000
    with pytest.raises(ValueError, match="lengthen"):
        reservation.extend(1_200_000, deadline=0.0)
