"""Verify LangSmith sandbox quotes and measured-time settlement against a real local ledger, without paid calls."""

from __future__ import annotations

import time
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from core.billing import langsmith_usage
from core.billing.budgets import BudgetService
from core.billing.langsmith_usage import LangSmithUsageReservation, quote_langsmith_sandbox
from core.billing.operation_pricing import UnpricedOperationError
from core.billing.runtime import BudgetRuntime, UsagePendingError
from core.config import settings
from core.sandbox.metering import usage_adapter
from core.storage.models import Base, BillingCustomerModel

GIB = 1024**3
CREATE = {
    "name": "skynet-box-1",
    "snapshot": None,
    "lifetime_ms": 60_000,
    "startup_ms": 60_000,
    "idle_ttl_seconds": 60,
    "delete_after_stop_seconds": 60,
    "vcpus": 1,
    "mem_bytes": GIB,
    "fs_capacity_bytes": GIB,
    "network_disabled": True,
    "allowed_hosts": [],
    "environment_fingerprint": "env",
}


class Clock:
    """Stand in for the monotonic clock that times the box."""

    def __init__(self) -> None:
        """Start at zero."""
        self.now = 0.0

    def monotonic(self) -> float:
        """Return the current fake time.

        Returns:
            Seconds since the start.
        """
        return self.now


@pytest.fixture(autouse=True)
def _at_cost_markup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the usage markup to 1.0 so these tests check the settlement arithmetic, not the pricing policy."""
    monkeypatch.setattr(settings, "usage_markup", 1.0)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """Replace the adapter's clock with a fake one.

    Returns:
        The fake clock.
    """
    fake = Clock()
    monkeypatch.setattr(langsmith_usage, "time", SimpleNamespace(monotonic=fake.monotonic))
    return fake


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Engine]:
    """Create a private funded wallet and ledger.

    Args:
        tmp_path: Per-test directory for the SQLite file.

    Yields:
        The engine bound to the funded ledger.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'langsmith.db'}")
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


def _usd(seconds: int) -> Decimal:
    """Price the test box for a number of seconds at the published rates.

    Args:
        seconds: Running and stored time.

    Returns:
        The provider cost in USD.
    """
    per_hour = Decimal("0.0576") + Decimal("0.01845") + Decimal("0.000123")
    return per_hour * seconds / 3600


def test_usage_adapter_finds_the_langsmith_adapter() -> None:
    """Let hosted Skynet find the LangSmith metering adapter by its provider key."""
    adapter = usage_adapter("langsmith")
    assert adapter is not None
    assert adapter.name == "langsmith"


def test_quote_covers_start_lifetime_idle_stop_and_retention() -> None:
    """Price compute for start, lifetime and idle stop, and storage for that plus the delete delay."""
    quote = quote_langsmith_sandbox(CREATE)
    running_ms = 60_000 + 60_000 + 60_000
    expected = (
        (Decimal("0.0576") + Decimal("0.01845")) * running_ms + Decimal("0.000123") * (running_ms + 60_000)
    ) / 3_600_000
    assert quote.maximum.total == quote.maximum.wallet == expected * 100
    assert quote.price_snapshot["policy"]["kind"] == "sandbox"
    assert quote.price_snapshot["source"] == "https://www.langchain.com/pricing"


@pytest.mark.parametrize(
    "change",
    [
        {"vcpus": None},
        {"mem_bytes": None},
        {"fs_capacity_bytes": 0},
        {"lifetime_ms": 0},
        {"idle_ttl_seconds": 0},
        {"delete_after_stop_seconds": 0},
        {"vcpus": True},
    ],
)
def test_unbounded_sandbox_is_not_quoted(change: dict[str, Any]) -> None:
    """Refuse a box whose size is a server default or whose server stop or delete is off."""
    with pytest.raises(UnpricedOperationError):
        quote_langsmith_sandbox({**CREATE, **change})


def test_reserve_then_settle_charges_the_measured_seconds(database: Engine, clock: Clock) -> None:
    """Settle a deleted box at its measured seconds, rounded up, and record the sandbox id."""
    runtime = _runtime(database)
    reservation = langsmith_usage.USAGE.reserve(runtime, CREATE, operation_key="box-1")
    clock.now = 1.0
    langsmith_usage.USAGE.confirm(reservation, SimpleNamespace(id="sandbox-uuid", vcpus=1, mem_bytes=GIB))
    clock.now = 29.2
    reservation.mark_deleted()
    langsmith_usage.USAGE.settle(reservation)
    operation = runtime.service.get_operation(reservation.operation.id, "alice")
    assert operation.state == "settled"
    assert operation.actual_cents == _usd(30) * 100
    assert operation.actual_cents < operation.max_cents
    assert operation.provider_request_id == "sandbox-uuid"
    assert reservation.operation.id not in runtime._pinned
    langsmith_usage.USAGE.settle(reservation)
    assert runtime.service.get_operation(reservation.operation.id, "alice").actual_cents == operation.actual_cents


def test_settle_clamps_a_slow_delete_to_the_reservation(database: Engine, clock: Clock) -> None:
    """Charge no more than the reservation when the delete comes after the funded time."""
    runtime = _runtime(database)
    reservation = LangSmithUsageReservation(runtime, CREATE, operation_key="box-1")
    clock.now = 10 * 3600.0
    reservation.mark_deleted()
    reservation.settle()
    operation = runtime.service.get_operation(reservation.operation.id, "alice")
    assert operation.state == "settled"
    assert operation.actual_cents == operation.max_cents
    assert operation.actual_wallet_cents == operation.max_wallet_cents


def test_unconfirmed_delete_keeps_the_reservation_pending(database: Engine, clock: Clock) -> None:
    """Keep the coverage held, and refuse a second dispatch, when the delete is not confirmed."""
    runtime = _runtime(database)
    reservation = LangSmithUsageReservation(runtime, CREATE, operation_key="box-1")
    with pytest.raises(UsagePendingError, match="not confirmed"):
        reservation.settle()
    assert runtime.service.get_operation(reservation.operation.id, "alice").state == "pending"
    assert runtime.service.get(runtime.budget_id, "alice").reserved_cents > 0
    with pytest.raises(UsagePendingError, match="already dispatched"):
        LangSmithUsageReservation(runtime, CREATE, operation_key="box-1")


def test_confirm_refuses_a_box_larger_than_its_reservation(database: Engine, clock: Clock) -> None:
    """Raise when the created box reports a size that the reservation did not fund."""
    runtime = _runtime(database)
    reservation = LangSmithUsageReservation(runtime, CREATE, operation_key="box-1")
    with pytest.raises(UsagePendingError, match="vcpus"):
        reservation.confirm_created(SimpleNamespace(id="sandbox-uuid", vcpus=4))


def test_extend_funds_a_longer_lifetime(database: Engine, clock: Clock) -> None:
    """Grow the coverage for a longer lifetime and refuse an extension that does not grow it."""
    runtime = _runtime(database)
    reservation = LangSmithUsageReservation(runtime, CREATE, operation_key="box-1")
    before = runtime.service.get_operation(reservation.operation.id, "alice").max_cents
    langsmith_usage.USAGE.extend(reservation, 120_000, deadline=time.monotonic() + 1)
    after = runtime.service.get_operation(reservation.operation.id, "alice").max_cents
    assert after == quote_langsmith_sandbox({**CREATE, "lifetime_ms": 120_000}).maximum.total
    assert after > before
    with pytest.raises(ValueError, match="lengthen"):
        reservation.extend(120_000, deadline=time.monotonic() + 1)
