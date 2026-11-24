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


def _settled_box(database: Engine, clock: Clock, box: SimpleNamespace, seconds: float) -> Any:
    """Reserve, confirm, delete and settle one box, and return its ledger state and evidence.

    Args:
        database: Engine of the funded ledger.
        clock: The fake monotonic clock.
        box: What the created box reports.
        seconds: Time from the reservation to the confirmed delete.

    Returns:
        The reconciliation snapshot of the settled operation.
    """
    runtime = _runtime(database)
    reservation = LangSmithUsageReservation(runtime, CREATE, operation_key="box-1")
    reservation.confirm_created(box)
    clock.now = seconds
    reservation.mark_deleted()
    reservation.settle()
    settled = runtime.service.get_reconciliation(reservation.operation.id, "alice")
    assert settled.operation.state == "settled"
    return settled


def _settle_evidence(settled: Any) -> dict[str, Any]:
    """Return the evidence of the settle.

    Args:
        settled: The reconciliation snapshot of the settled operation.

    Returns:
        The evidence that has the priced sizes.
    """
    (evidence,) = [item for item in settled.evidence if "priced_sizes" in item]
    return evidence


def test_smaller_box_settles_at_the_funded_size(database: Engine, clock: Clock) -> None:
    """Price a box that reports less memory than funded at the funded size."""
    settled = _settled_box(database, clock, SimpleNamespace(id="sandbox-uuid", mem_bytes=GIB // 2), 29.2)
    assert settled.operation.actual_cents == _usd(30) * 100
    assert _settle_evidence(settled)["priced_sizes"]["mem_bytes"] == GIB


def test_larger_box_settles_at_the_reported_size(database: Engine, clock: Clock) -> None:
    """Price a box that reports more vCPUs than funded at the reported size, inside the coverage."""
    settled = _settled_box(database, clock, SimpleNamespace(id="sandbox-uuid", vcpus=2), 29.2)
    two_cpus = (Decimal("0.0576") * 2 + Decimal("0.01845") + Decimal("0.000123")) * 30 / 3600
    assert settled.operation.actual_cents == two_cpus * 100
    evidence = _settle_evidence(settled)
    assert evidence["priced_sizes"]["vcpus"] == 2
    assert evidence["reported_sizes"] == {"vcpus": 2}
    assert evidence["clamped"] is False


def test_larger_box_clamps_to_the_coverage(database: Engine, clock: Clock) -> None:
    """Charge no more than the coverage when the larger size passes it, and record the clamp."""
    settled = _settled_box(database, clock, SimpleNamespace(id="sandbox-uuid", vcpus=8), 10 * 3600.0)
    assert settled.operation.actual_cents == settled.operation.max_cents
    assert _settle_evidence(settled)["clamped"] is True


def test_ttl_difference_does_not_block_a_proven_delete(database: Engine, clock: Clock) -> None:
    """Settle at the funded size when only the reported TTLs differ and the delete is proven."""
    box = SimpleNamespace(id="sandbox-uuid", idle_ttl_seconds=600, delete_after_stop_seconds=600)
    settled = _settled_box(database, clock, box, 29.2)
    assert settled.operation.actual_cents == _usd(30) * 100


def test_failed_pending_mark_keeps_the_original_error(
    database: Engine, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raise the settle error, not the error of the pending mark that follows it."""
    reservation = LangSmithUsageReservation(_runtime(database), CREATE, operation_key="box-1")

    def broken_pending() -> None:
        """Fail like a lost database.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("database lost")

    monkeypatch.setattr(reservation, "pending", broken_pending)
    with pytest.raises(UsagePendingError, match="not confirmed"):
        reservation.settle()


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
