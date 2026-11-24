"""Verify Modal sandbox admission and measured-runtime metering without Modal calls."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

from core.billing import modal_usage
from core.billing.budget_amounts import cent_units
from core.billing.budgets import BudgetService
from core.billing.modal_usage import ModalUsageReservation, quote_modal_sandbox
from core.billing.operation_pricing import UnpricedOperationError
from core.billing.runtime import BudgetRuntime, UsagePendingError
from core.billing.vercel_usage import SANDBOX_NETWORK_BYTES_CAP
from core.config import settings
from core.sandbox.metering import usage_adapter
from core.storage.models import Base, BillingCustomerModel, ExecutionOperationModel

CREATE = {
    "image": "python:3.11-slim",
    "lifetime_ms": 60_000,
    "cpu_cores": 2,
    "memory_mib": 4096,
    "gpu": False,
    "network_bytes_cap": SANDBOX_NETWORK_BYTES_CAP,
}


@pytest.fixture(autouse=True)
def _at_cost_markup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the usage markup to 1.0 so these tests check settlement arithmetic, not pricing policy."""
    monkeypatch.setattr(settings, "usage_markup", 1.0)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Engine]:
    """Create a private funded wallet and ledger for real admission tests.

    Args:
        tmp_path: Per-test directory for the SQLite file.

    Yields:
        The engine bound to the funded ledger.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'modal.db'}")
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


def _operation(database: Engine) -> ExecutionOperationModel:
    """Return the only operation row.

    Args:
        database: Engine of the ledger.

    Returns:
        The operation row.
    """
    with Session(database) as session:
        return session.scalar(select(ExecutionOperationModel))


def _run(reservation: ModalUsageReservation, *, measured_sec: float | None) -> None:
    """Send, create, stop and settle one box with a fixed measured runtime.

    Args:
        reservation: The claimed reservation.
        measured_sec: Seconds between the create and the confirmed stop, or
            ``None`` when the terminate was not confirmed.
    """
    reservation.sent()
    reservation.confirm_created(SimpleNamespace(object_id="sb-1"))
    if measured_sec is not None:
        assert reservation.sent_at is not None
        reservation.stopped()
        reservation.stopped_at = reservation.sent_at + measured_sec
    reservation.settle()


def test_quote_covers_the_lifetime_at_the_published_rates_and_the_egress_cap() -> None:
    """Price the whole lifetime at the published CPU and memory rates, plus the whole egress cap."""
    quote = quote_modal_sandbox(CREATE)
    compute = 60 * (2 * Decimal("0.00003942") + 4 * Decimal("0.00000667"))
    egress = Decimal(SANDBOX_NETWORK_BYTES_CAP) / Decimal(1024**3) * Decimal("0.04")
    assert modal_usage._usd(quote.price_snapshot, 60_000, network=True) == compute + egress
    assert quote.price_snapshot["provider"] == "modal"
    assert quote.price_snapshot["source"] == "https://modal.com/pricing"
    assert quote.maximum.total > 0


def test_quote_applies_the_modal_minimum_size() -> None:
    """Bill a tiny box at Modal's 0.125-core and 128 MiB minimums."""
    snapshot = quote_modal_sandbox({**CREATE, "cpu_cores": 0.05, "memory_mib": 64}).price_snapshot
    expected = 10 * (Decimal("0.125") * Decimal("0.00003942") + Decimal("0.125") * Decimal("0.00000667"))
    assert modal_usage._usd(snapshot, 10_000, network=False) == expected


@pytest.mark.parametrize(
    "change",
    [
        {"lifetime_ms": 0},
        {"lifetime_ms": 24 * 60 * 60 * 1000 + 1},
        {"lifetime_ms": True},
        {"cpu_cores": 0},
        {"cpu_cores": None},
        {"memory_mib": None},
        {"network_bytes_cap": SANDBOX_NETWORK_BYTES_CAP + 1},
        {"gpu": True},
    ],
)
def test_unbounded_or_gpu_box_is_not_quoted(change: dict[str, Any]) -> None:
    """Refuse a box whose lifetime, size or egress has no ceiling, or that asks for a GPU.

    Args:
        change: The field that breaks the quote.
    """
    with pytest.raises(UnpricedOperationError):
        quote_modal_sandbox({**CREATE, **change})


def test_reserve_then_settle_charges_the_measured_runtime(database: Engine) -> None:
    """Settle a stopped box at its measured seconds, with no egress when the network never opened."""
    runtime = _runtime(database)
    reservation = ModalUsageReservation(runtime, CREATE, operation_key="box-one")
    _run(reservation, measured_sec=10.2)
    row = _operation(database)
    assert row.state == "settled"
    assert row.provider_request_id == "sb-1"
    expected = 11 * (2 * Decimal("0.00003942") + 4 * Decimal("0.00000667"))
    charge = modal_usage.vercel_charge_policy(reservation.quote.price_snapshot).convert(expected)
    assert row.actual_units == cent_units(charge.total)
    assert row.actual_units < row.max_units
    assert runtime._pinned == {}


def test_settle_clamps_to_the_funded_lifetime(database: Engine) -> None:
    """Bill at most the funded lifetime, even when the measured runtime is longer."""
    long_runtime = _runtime(database)
    long_box = ModalUsageReservation(long_runtime, CREATE, operation_key="long")
    long_box.opened_network()
    _run(long_box, measured_sec=10_000)
    unknown_runtime = _runtime(database)
    unknown_box = ModalUsageReservation(unknown_runtime, CREATE, operation_key="unknown")
    unknown_box.opened_network()
    _run(unknown_box, measured_sec=None)
    with Session(database) as session:
        rows = {row.operation_key: row for row in session.scalars(select(ExecutionOperationModel))}
    assert rows["long"].state == rows["unknown"].state == "settled"
    assert rows["long"].actual_units == rows["unknown"].actual_units == rows["long"].max_units


def test_settle_without_a_sandbox_id_keeps_the_hold_pending(database: Engine) -> None:
    """Keep the hold pending when the box has no sandbox id, because no usage can be measured."""
    runtime = _runtime(database)
    reservation = ModalUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.sent()
    with pytest.raises(UsagePendingError, match="sandbox id"):
        reservation.settle()
    assert _operation(database).state == "pending"


def test_confirm_refuses_a_sandbox_with_no_id(database: Engine) -> None:
    """Refuse a created sandbox that carries no Modal object id."""
    reservation = ModalUsageReservation(_runtime(database), CREATE, operation_key="box-one")
    with pytest.raises(UsagePendingError, match="sandbox id"):
        reservation.confirm_created(SimpleNamespace(object_id=None))


def test_failure_before_the_create_releases_the_hold(database: Engine) -> None:
    """Release the hold when no create reached Modal."""
    runtime = _runtime(database)
    reservation = ModalUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.fail()
    assert _operation(database).state == "released"
    assert runtime._pinned == {}


def test_failure_after_the_create_keeps_the_hold_and_refuses_a_replay(database: Engine) -> None:
    """Keep the hold pending when a sent create returned no sandbox, and refuse a second dispatch."""
    runtime = _runtime(database)
    reservation = ModalUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.sent()
    reservation.fail()
    assert _operation(database).state == "pending"
    with pytest.raises(UsagePendingError, match="already dispatched"):
        ModalUsageReservation(runtime, CREATE, operation_key="box-one")


def test_extend_funds_a_longer_lifetime(database: Engine) -> None:
    """Grow the hold to the quote for the longer lifetime."""
    runtime = _runtime(database)
    reservation = ModalUsageReservation(runtime, CREATE, operation_key="box-one")
    before = _operation(database).max_units
    reservation.extend(120_000, deadline=0)
    assert _operation(database).max_units > before
    assert reservation.lifetime_ms == 120_000
    with pytest.raises(ValueError, match="lengthen"):
        reservation.extend(120_000, deadline=0)


def test_modal_usage_adapter_is_found() -> None:
    """Find ``USAGE`` through the metering registry, so hosted Skynet can offer Modal."""
    adapter = usage_adapter("modal")
    assert adapter is modal_usage.USAGE
    assert adapter.name == "modal"
