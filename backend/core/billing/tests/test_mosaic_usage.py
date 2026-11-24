"""Verify Mosaic sandbox quotes, holds and settles against a local ledger, with a fake SDK handle and no paid calls."""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from core.billing import mosaic_usage
from core.billing.budgets import BudgetService
from core.billing.mosaic_usage import (
    USAGE,
    MosaicUsageReservation,
    billing_units,
    history_usd,
    quote_mosaic_sandbox,
)
from core.billing.operation_pricing import UnpricedOperationError
from core.billing.runtime import BudgetRuntime, UsagePendingError
from core.config import settings
from core.sandbox.metering import usage_adapter
from core.storage.models import Base, BillingCustomerModel

REQUEST = {"vcpu": 2, "memory_mb": 4096, "lifetime_ms": 3_600_000}


def _record(**overrides: Any) -> dict[str, Any]:
    """Return one closed, metered usage-history record of a one-unit box.

    Args:
        **overrides: Record fields to replace.

    Returns:
        The record.
    """
    return {
        "id": "seg-1",
        "started_at": "2026-11-24 10:00:00",
        "ended_at": "2026-11-24 10:30:00",
        "vcpu": 2,
        "memory_mb": 4096,
        "billing_units": 1,
        "unit_seconds": 1800,
        "accounting_status": "metered",
        "billing_snapshot": {"rate": {"currency": "USD", "micros_per_unit_hour": 50_000}},
        **overrides,
    }


class FakeHandle:
    """Stand in for the SDK ``Sandbox`` that a Mosaic create returns."""

    def __init__(self, *, vcpu: int = 2, memory_mb: int = 4096, history: Any = None) -> None:
        """Keep the shape Mosaic reports and the usage history it returns.

        Args:
            vcpu: The vCPUs that ``info()`` reports.
            memory_mb: The memory that ``info()`` reports.
            history: The ``usage_history`` page, or an exception to raise.
        """
        self.id = "sbx-1"
        self._summary = SimpleNamespace(vcpu=vcpu, memory_mb=memory_mb)
        self._history = history

    def info(self) -> SimpleNamespace:
        """Return the box summary.

        Returns:
            An object with the vCPU count and memory size.
        """
        return self._summary

    def usage_history(self, *, limit: int) -> Any:
        """Return the usage history, or raise the configured error.

        Args:
            limit: The page size.

        Returns:
            The history page.

        Raises:
            Exception: The configured history error.
        """
        assert limit == 500
        if isinstance(self._history, Exception):
            raise self._history
        return self._history


@pytest.fixture(autouse=True)
def _at_cost_markup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the usage markup to 1.0 so the tests check the settle arithmetic, not the pricing policy.

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
    engine = create_engine(f"sqlite:///{tmp_path / 'mosaic.db'}")
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


def _clock(monkeypatch: pytest.MonkeyPatch, *seconds: float) -> None:
    """Make the reservation's clock return these instants in order.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        *seconds: The instants, in seconds.
    """
    instants = itertools.chain(seconds)
    monkeypatch.setattr(mosaic_usage, "_clock", lambda: next(instants))


def _run_box(reservation: MosaicUsageReservation, handle: FakeHandle) -> None:
    """Dispatch, confirm and stop one box.

    Args:
        reservation: The box's reservation.
        handle: The fake created box.
    """
    reservation.dispatched()
    USAGE.confirm(reservation, handle)
    reservation.stopped()


def _operation(runtime: BudgetRuntime, reservation: MosaicUsageReservation) -> Any:
    """Read the reservation's operation from the ledger.

    Args:
        runtime: The budget runtime.
        reservation: The box's reservation.

    Returns:
        The operation snapshot.
    """
    return runtime.service.get_operation(reservation.operation.id, "alice")


def test_mosaic_has_a_usage_adapter() -> None:
    """Find the Mosaic adapter through the provider-neutral loader."""
    adapter = usage_adapter("mosaic")
    assert adapter is USAGE
    assert adapter.name == "mosaic"


def test_quote_funds_the_whole_lifetime_at_the_published_unit_rate() -> None:
    """Price one unit for one hour at five cents, and bill a larger shape in whole units."""
    quote = USAGE.quote(REQUEST)
    assert quote.maximum.total == Decimal(5)
    assert quote.price_snapshot["provider"] == "mosaic"
    assert quote.price_snapshot["billing_units"] == 1
    assert billing_units(1, 2048) == 1
    assert billing_units(4, 8192) == 2
    assert billing_units(2, 8192) == 2
    assert quote_mosaic_sandbox({**REQUEST, "vcpu": 4, "memory_mb": 8192}).maximum.total == Decimal(10)


@pytest.mark.parametrize(
    "change",
    [{"vcpu": None}, {"memory_mb": None}, {"lifetime_ms": 0}, {"vcpu": True}, {"vcpu": 65}, {"memory_mb": 64}],
)
def test_an_unbounded_request_is_not_quoted(change: dict[str, Any]) -> None:
    """Refuse a request without an explicit shape and a bounded lifetime.

    Args:
        change: The field that breaks the request.
    """
    with pytest.raises(UnpricedOperationError):
        quote_mosaic_sandbox({**REQUEST, **change})


def test_settle_charges_the_metered_usage_history(database: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Settle at Mosaic's own metered unit-seconds, not at the longer wall time."""
    _clock(monkeypatch, 0.0, 3000.0)
    runtime = _runtime(database)
    reservation = USAGE.reserve(runtime, REQUEST, operation_key="box")
    _run_box(reservation, FakeHandle(history={"records": [_record()], "next_cursor": None}))
    USAGE.settle(reservation)
    operation = _operation(runtime, reservation)
    assert operation.state == "settled"
    assert operation.actual_cents == Decimal("2.5")


def test_settle_falls_back_to_measured_time_when_the_history_fails(
    database: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Charge the measured wall time when the usage history cannot be read."""
    _clock(monkeypatch, 0.0, 1800.0)
    runtime = _runtime(database)
    reservation = USAGE.reserve(runtime, REQUEST, operation_key="box")
    _run_box(reservation, FakeHandle(history=RuntimeError("billing:read scope missing")))
    USAGE.settle(reservation)
    operation = _operation(runtime, reservation)
    assert operation.state == "settled"
    assert operation.actual_cents == Decimal("2.5")


def test_settle_clamps_to_the_reservation(database: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Charge no more than the hold when the box ran past its funded lifetime."""
    _clock(monkeypatch, 0.0, 7200.0)
    runtime = _runtime(database)
    reservation = USAGE.reserve(runtime, REQUEST, operation_key="box")
    _run_box(reservation, FakeHandle(history={"records": [_record(unit_seconds=9000)], "next_cursor": None}))
    USAGE.settle(reservation)
    operation = _operation(runtime, reservation)
    assert operation.state == "settled"
    assert operation.actual_cents == operation.max_cents == Decimal(5)


def test_settle_without_a_confirmed_destroy_keeps_the_hold(database: Engine) -> None:
    """Keep the hold pending when the destroy of the box is not confirmed."""
    runtime = _runtime(database)
    reservation = USAGE.reserve(runtime, REQUEST, operation_key="box")
    reservation.dispatched()
    USAGE.confirm(reservation, FakeHandle())
    with pytest.raises(UsagePendingError):
        USAGE.settle(reservation)
    assert _operation(runtime, reservation).state == "pending"


def test_a_box_with_another_shape_is_not_confirmed(database: Engine) -> None:
    """Refuse a created box whose shape differs from the funded one."""
    runtime = _runtime(database)
    reservation = USAGE.reserve(runtime, REQUEST, operation_key="box")
    with pytest.raises(UsagePendingError):
        USAGE.confirm(reservation, FakeHandle(vcpu=4, memory_mb=8192))


def test_release_returns_the_hold_of_a_refused_create(database: Engine) -> None:
    """Release the hold when Mosaic refused the create, so no box exists."""
    runtime = _runtime(database)
    reservation = USAGE.reserve(runtime, REQUEST, operation_key="box")
    reservation.release("SandboxRejectedError")
    operation = _operation(runtime, reservation)
    assert operation.state != "pending"
    assert operation.actual_cents == 0


@pytest.mark.parametrize(
    "history",
    [
        {"records": [], "next_cursor": None},
        {"records": [_record()], "next_cursor": "more"},
        {"records": [_record(ended_at=None)], "next_cursor": None},
        {"records": [_record(accounting_status="estimated")], "next_cursor": None},
        {"records": [_record(billing_units=2)], "next_cursor": None},
        {"records": [_record(unit_seconds=None)], "next_cursor": None},
        {"records": [_record(billing_snapshot={"rate": {"currency": "EUR", "micros_per_unit_hour": 1}})]},
    ],
)
def test_an_incomplete_history_is_not_used(history: dict[str, Any]) -> None:
    """Use the history only when every record is closed, metered, the funded shape and in USD.

    Args:
        history: A history page that must not settle the box.
    """
    assert history_usd(history, units=1) is None


def test_a_record_without_a_tariff_uses_the_published_rate() -> None:
    """Price a record with no recorded tariff at the published unit rate."""
    history = {"records": [_record(billing_snapshot={"rate": None})], "next_cursor": None}
    assert history_usd(history, units=1) == Decimal("0.025")
