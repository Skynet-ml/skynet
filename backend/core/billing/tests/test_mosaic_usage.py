"""Verify Mosaic sandbox quotes, holds and settles against a local ledger, with a fake SDK handle and no paid calls."""

from __future__ import annotations

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
        "running_seconds": 1800,
        "accounting_status": "metered",
        "pricing_status": "recorded",
        "billing_snapshot": {"rate": {"currency": "USD", "micros_per_unit_hour": 50_000}},
        **overrides,
    }


def _page(*records: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """Return one final, complete usage-history page.

    Args:
        *records: The page's records.
        **overrides: Page fields to replace.

    Returns:
        The page.
    """
    return {"records": list(records), "next_cursor": None, "billing_final": True, **overrides}


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

    def usage_history(self, *, limit: int, include_daily_finality: bool) -> Any:
        """Return the usage history, or raise the configured error.

        Args:
            limit: The page size.
            include_daily_finality: Whether Mosaic reports billing finality.

        Returns:
            The history page.

        Raises:
            Exception: The configured history error.
        """
        assert limit == 500
        assert include_daily_finality is True
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
    instants = iter(seconds)
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
    """Settle at Mosaic's own metered unit-seconds, not at the longer wall time.

    Args:
        database: Engine of the funded ledger.
        monkeypatch: The pytest monkeypatch fixture.
    """
    _clock(monkeypatch, 0.0, 3000.0)
    runtime = _runtime(database)
    reservation = USAGE.reserve(runtime, REQUEST, operation_key="box")
    _run_box(reservation, FakeHandle(history=_page(_record())))
    USAGE.settle(reservation)
    operation = _operation(runtime, reservation)
    assert operation.state == "settled"
    assert operation.actual_cents == Decimal("2.5")


def test_the_history_evidence_is_a_summary(database: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep only the record count and the unit-seconds total as evidence, not the raw records.

    Args:
        database: Engine of the funded ledger.
        monkeypatch: The pytest monkeypatch fixture.
    """
    _clock(monkeypatch, 0.0, 3000.0)
    reservation = USAGE.reserve(_runtime(database), REQUEST, operation_key="box")
    _run_box(reservation, FakeHandle(history=_page(_record(), _record(id="seg-2"))))
    _usd, source, evidence = reservation._usage()
    assert source == "usage_history"
    assert evidence == {"records": 2, "unit_seconds": str(2 * Decimal(str(_record()["unit_seconds"])))}


def test_a_box_destroyed_during_its_confirm_is_charged_its_measured_time(
    database: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Charge the measured time at the sent shape when the provider's destroy made the shape read fail.

    Args:
        database: Engine of the funded ledger.
        monkeypatch: The pytest monkeypatch fixture.
    """
    _clock(monkeypatch, 0.0, 1800.0 - mosaic_usage._WALL_MARGIN_MS / 1000)
    runtime = _runtime(database)
    reservation = USAGE.reserve(runtime, REQUEST, operation_key="box")
    handle = FakeHandle(history=RuntimeError("billing:read scope missing"))

    def destroyed_info() -> SimpleNamespace:
        """Fail like a read of a destroyed box.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("sandbox not found")

    handle.info = destroyed_info
    reservation.dispatched()
    with pytest.raises(RuntimeError):
        USAGE.confirm(reservation, handle)
    reservation.stopped()
    reservation.accept_sent_shape()
    USAGE.settle(reservation)
    operation = _operation(runtime, reservation)
    assert operation.state == "settled"
    assert operation.actual_cents == Decimal("2.5")


def test_settle_falls_back_to_measured_time_when_the_history_fails(
    database: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Charge the measured wall time plus its margin when the usage history cannot be read.

    Args:
        database: Engine of the funded ledger.
        monkeypatch: The pytest monkeypatch fixture.
    """
    _clock(monkeypatch, 0.0, 1800.0 - mosaic_usage._WALL_MARGIN_MS / 1000)
    runtime = _runtime(database)
    reservation = USAGE.reserve(runtime, REQUEST, operation_key="box")
    _run_box(reservation, FakeHandle(history=RuntimeError("billing:read scope missing")))
    USAGE.settle(reservation)
    operation = _operation(runtime, reservation)
    assert operation.state == "settled"
    assert operation.actual_cents == Decimal("2.5")


def test_settle_clamps_to_the_reservation(database: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Charge no more than the hold when the box ran past its funded lifetime.

    Args:
        database: Engine of the funded ledger.
        monkeypatch: The pytest monkeypatch fixture.
    """
    _clock(monkeypatch, 0.0, 7200.0)
    runtime = _runtime(database)
    reservation = USAGE.reserve(runtime, REQUEST, operation_key="box")
    history = _page(_record(unit_seconds=9000, running_seconds=9000))
    _run_box(reservation, FakeHandle(history=history))
    USAGE.settle(reservation)
    operation = _operation(runtime, reservation)
    assert operation.state == "settled"
    assert operation.actual_cents == operation.max_cents == Decimal(5)


def test_a_box_without_a_confirmed_destroy_is_charged_the_whole_hold(database: Engine) -> None:
    """Settle a known box whose destroy is not confirmed at the whole hold, because it may still run.

    Args:
        database: Engine of the funded ledger.
    """
    runtime = _runtime(database)
    reservation = USAGE.reserve(runtime, REQUEST, operation_key="box")
    reservation.dispatched()
    USAGE.confirm(reservation, FakeHandle())
    USAGE.settle(reservation)
    operation = _operation(runtime, reservation)
    assert operation.state == "settled"
    assert operation.actual_cents == operation.max_cents == Decimal(5)


def test_settle_without_a_known_box_keeps_the_hold(database: Engine) -> None:
    """Keep the hold pending when no box id is known.

    Args:
        database: Engine of the funded ledger.
    """
    runtime = _runtime(database)
    reservation = USAGE.reserve(runtime, REQUEST, operation_key="box")
    reservation.dispatched()
    with pytest.raises(UsagePendingError):
        USAGE.settle(reservation)
    assert _operation(runtime, reservation).state == "pending"


def test_a_lost_box_bound_by_its_id_is_charged_the_whole_hold(
    database: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Charge the whole hold for a box found only by its tag, because nobody confirmed its shape.

    Args:
        database: Engine of the funded ledger.
        monkeypatch: The pytest monkeypatch fixture.
    """
    _clock(monkeypatch, 0.0, 1800.0)
    runtime = _runtime(database)
    reservation = USAGE.reserve(runtime, REQUEST, operation_key="box")
    reservation.dispatched()
    reservation.bind("sbx-lost")
    reservation.stopped()
    USAGE.settle(reservation)
    operation = _operation(runtime, reservation)
    assert operation.state == "settled"
    assert operation.actual_cents == operation.max_cents == Decimal(5)


def test_a_box_with_another_shape_is_not_confirmed(database: Engine) -> None:
    """Refuse a created box whose shape differs from the funded one.

    Args:
        database: Engine of the funded ledger.
    """
    runtime = _runtime(database)
    reservation = USAGE.reserve(runtime, REQUEST, operation_key="box")
    with pytest.raises(UsagePendingError):
        USAGE.confirm(reservation, FakeHandle(vcpu=4, memory_mb=8192))


@pytest.mark.parametrize("shape", [{"vcpu": 4, "memory_mb": 8192}, {"vcpu": 0, "memory_mb": 0}])
def test_a_box_with_an_unconfirmed_shape_is_charged_the_whole_hold(
    database: Engine, monkeypatch: pytest.MonkeyPatch, shape: dict[str, int]
) -> None:
    """Charge the whole hold, not the measured time, when Mosaic reports another shape or the SDK default zeros.

    Args:
        database: Engine of the funded ledger.
        monkeypatch: The pytest monkeypatch fixture.
        shape: The shape that ``info()`` reports.
    """
    _clock(monkeypatch, 0.0, 60.0)
    runtime = _runtime(database)
    reservation = USAGE.reserve(runtime, REQUEST, operation_key="box")
    reservation.dispatched()
    with pytest.raises(UsagePendingError):
        USAGE.confirm(reservation, FakeHandle(**shape))
    reservation.stopped()
    USAGE.settle(reservation)
    operation = _operation(runtime, reservation)
    assert operation.state == "settled"
    assert operation.actual_cents == operation.max_cents == Decimal(5)


def test_release_returns_the_hold_of_a_refused_create(database: Engine) -> None:
    """Release the hold when Mosaic refused the create, so no box exists.

    Args:
        database: Engine of the funded ledger.
    """
    runtime = _runtime(database)
    reservation = USAGE.reserve(runtime, REQUEST, operation_key="box")
    reservation.release("SandboxRejectedError")
    operation = _operation(runtime, reservation)
    assert operation.state != "pending"
    assert operation.actual_cents == 0


@pytest.mark.parametrize(
    "history",
    [
        _page(),
        _page(_record(), billing_final=False),
        {"records": [_record()], "next_cursor": None},
        _page(_record(), next_cursor="more"),
        _page(_record(ended_at=None)),
        _page(_record(accounting_status="estimated")),
        _page(_record(pricing_status="unavailable")),
        _page(_record(billing_units=2)),
        _page(_record(unit_seconds=None)),
        _page(_record(running_seconds=None)),
        _page(_record(unit_seconds=3600)),
        _page(_record(billing_snapshot={"rate": None})),
        _page(_record(billing_snapshot={"rate": {"currency": "USD", "micros_per_unit_hour": 0}})),
        _page(_record(billing_snapshot={"rate": {"currency": "EUR", "micros_per_unit_hour": 1}})),
    ],
)
def test_an_incomplete_history_is_not_used(history: dict[str, Any]) -> None:
    """Use the history only when it is final and every record is closed, metered, priced in USD and the funded shape.

    Args:
        history: A history page that must not settle the box.
    """
    assert history_usd(history, units=1) is None


def test_a_two_unit_box_is_priced_from_its_shape_weighted_seconds() -> None:
    """Charge a two-unit box's unit-seconds, which are twice its awake seconds."""
    record = _record(vcpu=4, memory_mb=8192, billing_units=2, unit_seconds=3600, running_seconds=1800)
    assert history_usd(_page(record), units=2) == Decimal("0.05")


def test_wall_time_ignores_float_noise_of_a_large_clock(database: Engine) -> None:
    """A long-running host's large monotonic clock must not add a millisecond to the bill.

    Args:
        database: Engine of the funded ledger.
    """
    reservation = USAGE.reserve(_runtime(database), REQUEST, operation_key="box")
    reservation._dispatched_at = 1e9
    reservation._stopped_at = 1e9 + 10.2
    assert reservation._wall_ms() == 10_200 + mosaic_usage._WALL_MARGIN_MS
    reservation._stopped_at = 1e9 + 10.2004
    assert reservation._wall_ms() == 10_201 + mosaic_usage._WALL_MARGIN_MS
