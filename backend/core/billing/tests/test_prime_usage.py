"""Verify Prime sandbox admission and measured-time settlement against a real ledger, with no paid calls."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from core.billing.budgets import BudgetService
from core.billing.operation_pricing import UnpricedOperationError
from core.billing.prime_usage import USAGE, PrimeUsageReservation, prime_actual_usd, quote_prime_sandbox
from core.billing.runtime import BudgetRuntime, OperationCompletedError, UsagePendingError
from core.config import settings
from core.sandbox.metering import usage_adapter
from core.storage.models import Base, BillingCustomerModel

REQUEST = {"cpu_cores": 2.0, "memory_gb": 4.0, "disk_size_gb": 10.0, "timeout_minutes": 60, "gpu_count": 0}
# 2 x 0.02 + 4 x 0.0125 + 10 x 0.0002 USD for each running hour.
HOURLY_USD = Decimal("0.092")
RECORD = {
    "id": "sbx-1",
    "status": "TERMINATED",
    "cpu_cores": 2.0,
    "memory_gb": 4.0,
    "disk_size_gb": 10.0,
    "gpu_count": 0,
    "timeout_minutes": 60,
    "created_at": "2026-11-24T12:00:00+00:00",
    "started_at": "2026-11-24T12:00:20+00:00",
    "terminated_at": "2026-11-24T12:30:00+00:00",
}


@pytest.fixture(autouse=True)
def _at_cost_markup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the usage markup to 1.0 so these tests check settlement arithmetic, not pricing policy."""
    monkeypatch.setattr(settings, "usage_markup", 1.0)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Engine]:
    """Create a private funded wallet and ledger.

    Args:
        tmp_path: Per-test directory for the SQLite file.

    Yields:
        The engine bound to the funded ledger.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'prime.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username="alice", stripe_customer_id="fixture", balance_cents=100, grant_remaining=0)
        )
        session.commit()
    yield engine
    engine.dispose()


def _runtime(engine: Engine) -> BudgetRuntime:
    """Return a budget runtime with a 50 cent run budget.

    Args:
        engine: The ledger engine.

    Returns:
        The runtime.
    """
    service = BudgetService(engine=engine)
    budget = service.create("alice", 50, idempotency_key="draft")
    return BudgetRuntime(service, username="alice", budget_id=budget.id, generation=0, phase="setup", wait_timeout=0)


def _state(reservation: PrimeUsageReservation) -> str:
    """Return the ledger state of a reservation.

    Args:
        reservation: The reservation.

    Returns:
        The operation state.
    """
    runtime = reservation.runtime
    return runtime.service.get_operation(reservation.operation.id, runtime.username).state


def _billed(reservation: PrimeUsageReservation) -> Decimal:
    """Return the exact cents the operation charged, before the budget rounds them.

    Args:
        reservation: The reservation.

    Returns:
        The charged cents.
    """
    runtime = reservation.runtime
    return runtime.service.get_operation(reservation.operation.id, runtime.username).actual_cents


def _created(engine: Engine, request: dict[str, Any] = REQUEST) -> PrimeUsageReservation:
    """Reserve and confirm one sandbox.

    Args:
        engine: The ledger engine.
        request: The create fields.

    Returns:
        The confirmed reservation.
    """
    reservation = USAGE.reserve(_runtime(engine), request, operation_key="box-1")
    USAGE.confirm(reservation, RECORD)
    return reservation


def test_the_adapter_is_found_by_the_provider_name() -> None:
    """Let hosted Skynet offer Prime once production allows it."""
    assert usage_adapter("prime") is USAGE
    assert USAGE.name == "prime"


def test_the_quote_covers_the_whole_timeout() -> None:
    """Price every size at the published rate for the whole timeout."""
    quote = quote_prime_sandbox(REQUEST)
    assert quote.maximum.total == HOURLY_USD * 100
    assert quote.price_snapshot["source"] == "https://docs.primeintellect.ai/sandboxes/overview"
    half = quote_prime_sandbox({**REQUEST, "timeout_minutes": 30})
    assert half.maximum.total == HOURLY_USD * 50


@pytest.mark.parametrize(
    "change",
    [
        {"gpu_count": 1},
        {"disk_size_gb": None},
        {"memory_gb": 0},
        {"cpu_cores": True},
        {"timeout_minutes": 0},
        {"timeout_minutes": 1.5},
    ],
)
def test_the_quote_refuses_what_it_cannot_price(change: dict[str, Any]) -> None:
    """Refuse a GPU, a missing size or a bad timeout instead of guessing a price.

    Args:
        change: The field that makes the request unpriceable.
    """
    with pytest.raises(UnpricedOperationError):
        quote_prime_sandbox({**REQUEST, **change})


def test_reserve_then_settle_charges_the_measured_time(database: Engine) -> None:
    """Charge the 30 minutes from the create to the end, and release the rest of the hold."""
    reservation = _created(database)
    assert _state(reservation) == "dispatched"
    reservation.record = dict(RECORD)
    USAGE.settle(reservation)
    assert _state(reservation) == "settled"
    assert _billed(reservation) == HOURLY_USD * 50
    assert reservation.operation.id not in reservation.runtime._pinned
    USAGE.settle(reservation)
    assert _state(reservation) == "settled"


def test_the_settle_never_passes_the_reservation(database: Engine) -> None:
    """Cap a sandbox that Prime shows running past its timeout at the reserved quote."""
    reservation = _created(database)
    reservation.settle({**RECORD, "terminated_at": "2026-11-24T15:00:00+00:00"})
    assert _billed(reservation) == reservation.operation.max_cents == HOURLY_USD * 100


def test_a_record_with_no_end_keeps_the_hold_pending(database: Engine) -> None:
    """Keep the hold when Prime does not show the end time, and charge nothing yet."""
    reservation = _created(database)
    with pytest.raises(UsagePendingError):
        reservation.settle({**RECORD, "terminated_at": None})
    assert _state(reservation) == "pending"
    assert _billed(reservation) == 0
    reservation.settle(RECORD)
    assert _state(reservation) == "settled"


@pytest.mark.parametrize(
    "change",
    [
        {"id": "sbx-other"},
        {"status": "RUNNING"},
        {"created_at": "not a time"},
        {"terminated_at": "2026-11-24T11:00:00+00:00"},
    ],
)
def test_measured_time_needs_a_consistent_ended_record(change: dict[str, Any]) -> None:
    """Refuse another sandbox, a running one, a bad time or an end before the create.

    Args:
        change: The field that makes the record unusable.
    """
    snapshot = quote_prime_sandbox(REQUEST).price_snapshot
    with pytest.raises(UsagePendingError):
        prime_actual_usd({**RECORD, **change}, sandbox_id="sbx-1", price_snapshot=snapshot)


def test_a_time_with_no_zone_is_read_as_utc() -> None:
    """Read a Prime time with no offset as UTC, so the hold can settle."""
    snapshot = quote_prime_sandbox(REQUEST).price_snapshot
    naive = {**RECORD, "created_at": "2026-11-24T12:00:00", "terminated_at": "2026-11-24T12:30:00"}
    assert prime_actual_usd(naive, sandbox_id="sbx-1", price_snapshot=snapshot) == HOURLY_USD / 2


def test_a_measured_lifetime_settles_a_record_with_no_end(database: Engine) -> None:
    """Charge the lifetime the provider measured when Prime shows no end time, clamped to the hold."""
    reservation = _created(database)
    reservation.settle({}, seconds=15 * 60)
    assert _state(reservation) == "settled"
    assert _billed(reservation) == HOURLY_USD * 25
    other = USAGE.reserve(_runtime(database), REQUEST, operation_key="box-2")
    other.confirm_created({**RECORD, "id": "sbx-2"})
    other.settle({}, seconds=5 * 60 * 60)
    assert _billed(other) == other.operation.max_cents == HOURLY_USD * 100


def test_a_larger_sandbox_settles_at_its_own_size(database: Engine) -> None:
    """Price a box larger than funded at its larger size, and never above the hold."""
    hourly = Decimal("0.142")  # 2 x 0.02 + 8 x 0.0125 + 10 x 0.0002 USD
    larger = USAGE.reserve(_runtime(database), REQUEST, operation_key="box-1")
    with pytest.raises(UsagePendingError):
        larger.confirm_created({**RECORD, "memory_gb": 8.0})
    larger.settle({**RECORD, "memory_gb": 8.0})
    assert _billed(larger) == hourly * 50
    full = USAGE.reserve(_runtime(database), REQUEST, operation_key="box-2")
    with pytest.raises(UsagePendingError):
        full.confirm_created({**RECORD, "id": "sbx-2", "memory_gb": 8.0})
    full.settle({**RECORD, "id": "sbx-2", "memory_gb": 8.0, "terminated_at": "2026-11-24T13:00:00+00:00"})
    assert _billed(full) == full.operation.max_cents == HOURLY_USD * 100


def test_a_larger_sandbox_than_funded_is_not_confirmed(database: Engine) -> None:
    """Refuse a sandbox with more memory than the reservation pays for."""
    reservation = USAGE.reserve(_runtime(database), REQUEST, operation_key="box-1")
    with pytest.raises(UsagePendingError):
        reservation.confirm_created({**RECORD, "memory_gb": 8.0})


def test_a_create_that_made_nothing_is_released(database: Engine) -> None:
    """Release the hold of a create that Prime refused, with no charge."""
    reservation = USAGE.reserve(_runtime(database), REQUEST, operation_key="box-1")
    reservation.release("SandboxRejectedError")
    assert _state(reservation) == "released"
    assert _billed(reservation) == 0
    assert reservation.operation.id not in reservation.runtime._pinned


def test_a_settled_operation_cannot_dispatch_again(database: Engine) -> None:
    """Refuse a second sandbox under a settled operation key."""
    reservation = _created(database)
    reservation.settle(RECORD)
    with pytest.raises((OperationCompletedError, UsagePendingError)):
        USAGE.reserve(reservation.runtime, REQUEST, operation_key="box-1")


def test_extend_funds_a_longer_timeout(database: Engine) -> None:
    """Fund a longer timeout and let the settle use the new cap."""
    reservation = _created(database)
    USAGE.extend(reservation, 90 * 60_000, deadline=0.0)
    assert reservation.request["timeout_minutes"] == 90
    with pytest.raises(ValueError, match="lengthen"):
        reservation.extend(30, deadline=0.0)
    reservation.settle({**RECORD, "terminated_at": "2026-11-24T15:00:00+00:00"})
    assert _billed(reservation) == HOURLY_USD * 150
