"""Verify E2B admission and measured-runtime settlement against a local ledger, with no network."""

from __future__ import annotations

import time
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from core.billing.budgets import BudgetService
from core.billing.e2b_usage import USAGE, E2BUsageReservation, quote_e2b_sandbox
from core.billing.operation_pricing import UnpricedOperationError
from core.billing.runtime import BudgetRuntime, UsagePendingError
from core.config import settings
from core.sandbox.capabilities import SandboxSpecError
from core.sandbox.metering import usage_adapter
from core.storage.models import Base, BillingCustomerModel

# One hour at 2 vCPU and 2 GiB: 3600 * (2 * 0.000014 + 2 * 0.0000045) USD = 13.32 cents.
REQUEST = {"lifetime_ms": 3_600_000, "vcpus": 2, "memory_mb": 2048, "template": "base"}


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
    engine = create_engine(f"sqlite:///{tmp_path / 'e2b.db'}")
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


def _info(cpu_count: int = 2, memory_mb: int = 2048) -> SimpleNamespace:
    """Return a stand-in for the SDK's ``SandboxInfo``.

    Args:
        cpu_count: The template's vCPU count.
        memory_mb: The template's memory.

    Returns:
        An object with the fields that ``confirm`` reads.
    """
    return SimpleNamespace(sandbox_id="sbx-1", cpu_count=cpu_count, memory_mb=memory_mb)


def _operation(reservation: E2BUsageReservation) -> object:
    """Read the reservation's current ledger row.

    Args:
        reservation: The reservation.

    Returns:
        The operation snapshot.
    """
    return reservation.runtime.service.get_operation(reservation.operation.id, "alice")


def test_e2b_has_a_usage_adapter() -> None:
    """Find the E2B adapter by its provider key."""
    assert usage_adapter("e2b") is USAGE


def test_quote_prices_the_whole_lifetime_at_the_published_rates() -> None:
    """Price CPU and memory per second for the funded lifetime, with the source recorded."""
    quote = quote_e2b_sandbox(REQUEST)
    assert quote.maximum.total == Decimal("13.32")
    assert quote.price_snapshot["cpu_second_usd"] == "0.000014"
    assert quote.price_snapshot["memory_gib_second_usd"] == "0.0000045"
    assert quote.price_snapshot["source"] == "https://e2b.dev/pricing"


def test_quote_rounds_a_partial_second_up() -> None:
    """Charge a started second in full, as E2B bills by the second."""
    one = quote_e2b_sandbox({**REQUEST, "lifetime_ms": 1000}).maximum.total
    assert quote_e2b_sandbox({**REQUEST, "lifetime_ms": 1001}).maximum.total == 2 * one


@pytest.mark.parametrize(
    "override",
    [
        {"lifetime_ms": 0},
        {"lifetime_ms": 24 * 3_600_000 + 1},
        {"vcpus": 9},
        {"vcpus": True},
        {"memory_mb": None},
        {"memory_mb": 8 * 1024 + 1},
    ],
)
def test_quote_refuses_an_unbounded_request(override: dict[str, object]) -> None:
    """Refuse a request with no finite, supported size or lifetime.

    Args:
        override: The request field to make invalid.
    """
    with pytest.raises(UnpricedOperationError):
        quote_e2b_sandbox({**REQUEST, **override})


def test_reserve_to_settle_charges_the_measured_runtime(database: Engine) -> None:
    """Settle ten minutes of a one-hour reservation at the published rate."""
    reservation = USAGE.reserve(_runtime(database), REQUEST, operation_key="box")
    assert _operation(reservation).max_cents >= Decimal("13.32")
    reservation.start(100.0)
    USAGE.confirm(reservation, _info())
    reservation.stopped(100.0 + 599.2, confirmed=True)
    USAGE.settle(reservation)
    operation = _operation(reservation)
    assert operation.state == "settled"
    assert operation.actual_cents == Decimal("2.22")
    assert reservation.operation.id not in reservation.runtime._pinned


def test_settle_never_passes_the_reservation(database: Engine) -> None:
    """Charge at most the funded lifetime when the box ran longer than funded."""
    reservation = USAGE.reserve(_runtime(database), REQUEST, operation_key="box")
    reservation.start(0.0)
    reservation.stopped(10 * 3600.0, confirmed=True)
    USAGE.settle(reservation)
    operation = _operation(reservation)
    assert operation.actual_cents == Decimal("13.32")
    assert operation.actual_cents <= operation.max_cents


def test_unconfirmed_kill_charges_the_whole_funded_lifetime(database: Engine) -> None:
    """Charge the full funded lifetime when the box can still run until its E2B timeout."""
    reservation = USAGE.reserve(_runtime(database), REQUEST, operation_key="box")
    reservation.start(0.0)
    reservation.stopped(5.0, confirmed=False)
    USAGE.settle(reservation)
    assert _operation(reservation).actual_cents == Decimal("13.32")


def test_settle_prices_the_created_box_not_the_funded_size(database: Engine) -> None:
    """Charge the real box size when the template is smaller than the funding."""
    reservation = USAGE.reserve(_runtime(database), REQUEST, operation_key="box")
    reservation.start(0.0)
    USAGE.confirm(reservation, _info(cpu_count=1, memory_mb=1024))
    reservation.stopped(3600.0, confirmed=True)
    USAGE.settle(reservation)
    # One hour at 1 vCPU and 1 GiB: 3600 * (0.000014 + 0.0000045) USD = 6.66 cents.
    assert _operation(reservation).actual_cents == Decimal("6.66")


@pytest.mark.parametrize("missing", ["lifetime_ms", "vcpus", "memory_mb"])
def test_reserve_refuses_a_request_with_a_missing_field(missing: str) -> None:
    """Refuse a request with a missing field as unpriced, before any ledger call.

    Args:
        missing: The request field to leave out.
    """
    request = {key: value for key, value in REQUEST.items() if key != missing}
    with pytest.raises(UnpricedOperationError):
        USAGE.reserve(None, request, operation_key="box")


def test_box_that_never_started_settles_at_zero(database: Engine) -> None:
    """Release the whole hold when no create call went out."""
    reservation = USAGE.reserve(_runtime(database), REQUEST, operation_key="box")
    reservation.stopped(5.0, confirmed=True)
    USAGE.settle(reservation)
    operation = _operation(reservation)
    assert operation.state == "settled"
    assert operation.actual_cents == 0


def test_extend_grows_the_coverage_and_the_charge_cap(database: Engine) -> None:
    """Cover a longer lifetime after an extension, and refuse a shrink."""
    reservation = USAGE.reserve(_runtime(database), REQUEST, operation_key="box")
    reservation.start(0.0)
    USAGE.extend(reservation, 7_200_000, deadline=time.monotonic())
    assert reservation.lifetime_ms == 7_200_000
    with pytest.raises(ValueError):
        USAGE.extend(reservation, 7_200_000, deadline=time.monotonic())
    reservation.stopped(7200.0, confirmed=True)
    USAGE.settle(reservation)
    assert _operation(reservation).actual_cents == Decimal("26.64")


def test_confirm_refuses_a_box_larger_than_its_funding(database: Engine) -> None:
    """Refuse a template with more CPU or memory than the reservation covers."""
    reservation = USAGE.reserve(_runtime(database), REQUEST, operation_key="box")
    with pytest.raises(SandboxSpecError):
        USAGE.confirm(reservation, _info(cpu_count=4))
    with pytest.raises(SandboxSpecError):
        USAGE.confirm(reservation, _info(memory_mb=4096))


def test_unusable_price_snapshot_keeps_the_hold_pending(database: Engine) -> None:
    """Keep the hold pending, never settled at a guess, when the recorded rates are unusable."""
    reservation = USAGE.reserve(_runtime(database), REQUEST, operation_key="box")
    reservation.start(0.0)
    reservation.stopped(60.0, confirmed=True)
    reservation.quote = SimpleNamespace(price_snapshot={**reservation.quote.price_snapshot, "cpu_second_usd": "-1"})
    with pytest.raises(UsagePendingError):
        USAGE.settle(reservation)
    assert _operation(reservation).state == "pending"


def test_settle_twice_charges_once(database: Engine) -> None:
    """Accept a repeated settle without a second charge."""
    runtime = _runtime(database)
    reservation = USAGE.reserve(runtime, REQUEST, operation_key="box")
    reservation.start(0.0)
    reservation.stopped(60.0, confirmed=True)
    USAGE.settle(reservation)
    billed = runtime.service.get(runtime.budget_id, "alice").billed_cents
    USAGE.settle(reservation)
    assert runtime.service.get(runtime.budget_id, "alice").billed_cents == billed
