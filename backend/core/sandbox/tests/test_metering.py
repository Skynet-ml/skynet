"""Tests for provider usage adapters and pool budget leases. Local SQLite ledger only; no paid calls."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

from core.billing.budget_amounts import CENT_SCALE
from core.billing.budgets import MAX_LEASE_SLICES, BudgetConflictError, BudgetInsufficientError, BudgetService
from core.billing.runtime import BudgetRuntime
from core.billing.vercel_usage import VercelSnapshotReservation, VercelUsage
from core.config import settings
from core.sandbox import metering
from core.sandbox.factory import PROVIDERS, ProductionNotAllowedError, ProviderEntry, UnknownProviderError
from core.sandbox.metering import (
    USAGE_ADAPTERS,
    LeaseExhaustedError,
    MissingUsageAdapterError,
    PoolLease,
    usage_for,
)
from core.storage.models import Base, BillingCustomerModel, ExecutionBudgetModel, ExecutionOperationModel

IMAGE = "vcr.example/skynet/optimizer@sha256:" + "a" * 64
CREATE = {
    "image": IMAGE,
    "lifetime_ms": 120_000,
    "vcpus": 2,
    "network_disabled": True,
    "ports": [],
    "persistent": False,
}


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    """Create a private funded wallet and ledger with the markup pinned to 1.0.

    Args:
        tmp_path: The test directory.
        monkeypatch: Pins the usage markup.

    Yields:
        The SQLite engine.
    """
    monkeypatch.setattr(settings, "usage_markup", 1.0)
    engine = create_engine(f"sqlite:///{tmp_path / 'metering.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username="alice", stripe_customer_id="fixture", balance_cents=5000, grant_remaining=0)
        )
        session.commit()
    yield engine
    engine.dispose()


def _runtime(database: Engine, limit: int = 50) -> BudgetRuntime:
    """Bind real admission to the local database.

    Args:
        database: The SQLite engine.
        limit: The budget limit in cents.

    Returns:
        The budget runtime.
    """
    service = BudgetService(engine=database)
    budget = service.create("alice", limit, idempotency_key=f"draft-{limit}")
    return BudgetRuntime(service, username="alice", budget_id=budget.id, generation=0, phase="setup", wait_timeout=0)


def _reserved_units(runtime: BudgetRuntime) -> int:
    """Read the budget's reserved coverage.

    Args:
        runtime: The budget runtime.

    Returns:
        Reserved cent units.
    """
    with Session(runtime.service.engine) as session:
        return session.get(ExecutionBudgetModel, runtime.budget_id).reserved_units


def _settled_units(runtime: BudgetRuntime) -> int:
    """Read the budget's settled usage.

    Args:
        runtime: The budget runtime.

    Returns:
        Settled cent units.
    """
    with Session(runtime.service.engine) as session:
        return session.get(ExecutionBudgetModel, runtime.budget_id).settled_units


def _states(database: Engine) -> list[str]:
    """Read every operation state in key order.

    Args:
        database: The SQLite engine.

    Returns:
        The states.
    """
    with Session(database) as session:
        return list(
            session.scalars(select(ExecutionOperationModel.state).order_by(ExecutionOperationModel.operation_key))
        )


def test_every_production_provider_has_a_usage_adapter() -> None:
    """Check that no provider may run in production without a meter."""
    for name, entry in PROVIDERS.items():
        if entry.production_allowed:
            assert name in USAGE_ADAPTERS
            assert usage_for(name).name == name


def test_production_refuses_a_provider_with_no_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that production refuses an allowed provider whose usage Skynet cannot meter."""
    monkeypatch.setitem(PROVIDERS, "unmetered", ProviderEntry("core.sandbox.providers.x", "X", None, True))
    monkeypatch.setattr(metering.settings, "app_environment", "production")
    with pytest.raises(ProductionNotAllowedError):
        usage_for("unmetered")
    with pytest.raises(ProductionNotAllowedError):
        usage_for("local")
    assert isinstance(usage_for("vercel"), VercelUsage)


def test_usage_for_reports_unknown_and_unmetered_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check the errors outside production."""
    monkeypatch.setattr(metering.settings, "app_environment", "")
    with pytest.raises(UnknownProviderError):
        usage_for("nope")
    with pytest.raises(MissingUsageAdapterError):
        usage_for("local")


def test_lease_reserves_all_slices_and_returns_the_untaken_ones(database: Engine) -> None:
    """Check that a lease holds N slices, hands each out once and returns the rest at close."""
    runtime = _runtime(database)
    lease = PoolLease(runtime, VercelUsage(), CREATE, lease_key="pool-a", boxes=3)
    per_slice = lease.slices[0].max_cents
    assert len({operation.id for operation in lease.slices}) == 3
    assert _reserved_units(runtime) == int(per_slice * 3 * CENT_SCALE)
    taken = [lease.take(), lease.take(), lease.take()]
    assert [operation.id for operation in taken] == [operation.id for operation in lease.slices]
    with pytest.raises(LeaseExhaustedError):
        lease.take()
    lease.close()
    lease.close()
    assert _reserved_units(runtime) == 0
    assert _states(database) == ["released"] * 3
    with pytest.raises(LeaseExhaustedError):
        lease.take()


def test_claimed_slices_settle_once_and_survive_close(database: Engine) -> None:
    """Check that each box settles its own slice, with no double charge, and close keeps dispatched slices."""
    runtime = _runtime(database)
    with PoolLease(runtime, VercelUsage(), CREATE, lease_key="pool-b", boxes=3) as lease:
        first = lease.usage_factory(runtime, CREATE, operation_key="box-0")
        second = lease.usage_factory(runtime, CREATE, operation_key="box-1")
        assert first.operation.id != second.operation.id
        costs = [Decimal("0.4"), Decimal("0.7")]
        for reservation, cost in zip((first, second), costs, strict=True):
            for _ in range(2):
                runtime.service.settle(
                    reservation.operation.id,
                    "alice",
                    evidence_key=f"stop:{reservation.operation.id}",
                    actual_cents=cost,
                    evidence={"provider": "vercel"},
                )
    assert _reserved_units(runtime) == 0
    assert _settled_units(runtime) == int(sum(costs) * CENT_SCALE)
    assert _states(database) == ["settled", "settled", "released"]


def test_closed_lease_stops_a_box_that_has_not_dispatched(database: Engine) -> None:
    """Check that a slice taken before close cannot dispatch after close returned it."""
    runtime = _runtime(database)
    lease = PoolLease(runtime, VercelUsage(), CREATE, lease_key="pool-c", boxes=1)
    taken = lease.take()
    lease.close()
    with pytest.raises(BudgetConflictError):
        VercelUsage().reserve(runtime, CREATE, operation_key="box-0", reserved=taken)
    assert _reserved_units(runtime) == 0


def test_failed_slice_claim_drops_its_pin(database: Engine) -> None:
    """Check that a box whose slice claim fails no longer counts as held coverage."""
    runtime = _runtime(database)
    lease = PoolLease(runtime, VercelUsage(), CREATE, lease_key="pool-p", boxes=2)
    taken = lease.take()
    lease.close()
    with pytest.raises(BudgetConflictError):
        VercelUsage().reserve(runtime, CREATE, operation_key="box-0", reserved=taken)
    assert runtime._pinned == {}


def test_failed_snapshot_claim_drops_its_pin(database: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a snapshot whose dispatch claim fails no longer counts as held coverage."""
    runtime = _runtime(database)

    def refuse(*args: object, **kwargs: object) -> None:
        """Fail the dispatch claim like a concurrent release does."""
        raise BudgetConflictError("released")

    monkeypatch.setattr(runtime.service, "mark_dispatched", refuse)
    with pytest.raises(BudgetConflictError):
        VercelSnapshotReservation(runtime, {"session_id": "sbx", "retention_ms": 60_000}, operation_key="snap")
    assert runtime._pinned == {}


def test_lease_box_must_match_the_lease(database: Engine) -> None:
    """Check that a box with another authority or another request cannot claim a slice."""
    runtime = _runtime(database)
    other = BudgetRuntime(
        runtime.service, username="alice", budget_id=runtime.budget_id, generation=0, phase="setup", wait_timeout=0
    )
    with PoolLease(runtime, VercelUsage(), CREATE, lease_key="pool-d", boxes=2) as lease:
        with pytest.raises(ValueError):
            lease.usage_factory(other, CREATE, operation_key="box-0")
        with pytest.raises(BudgetConflictError):
            lease.usage_factory(runtime, {**CREATE, "vcpus": 4}, operation_key="box-0")
    assert _reserved_units(runtime) == 0


def test_lease_replay_is_idempotent_and_a_different_count_conflicts(database: Engine) -> None:
    """Check that a retried lease returns the same slices and a resized one is refused."""
    runtime = _runtime(database)
    first = PoolLease(runtime, VercelUsage(), CREATE, lease_key="pool-e", boxes=2)
    again = PoolLease(runtime, VercelUsage(), CREATE, lease_key="pool-e", boxes=2)
    assert [operation.id for operation in again.slices] == [operation.id for operation in first.slices]
    assert _reserved_units(runtime) == int(first.slices[0].max_cents * 2 * CENT_SCALE)
    with pytest.raises(BudgetConflictError):
        PoolLease(runtime, VercelUsage(), CREATE, lease_key="pool-e", boxes=3)
    with pytest.raises(BudgetConflictError):
        PoolLease(runtime, VercelUsage(), {**CREATE, "vcpus": 4}, lease_key="pool-e", boxes=2)
    first.close()
    assert _reserved_units(runtime) == 0


def test_lease_keys_match_exactly(database: Engine) -> None:
    """Check that lease replay ignores other leases whose keys share a prefix or differ in case."""
    runtime = _runtime(database)
    PoolLease(runtime, VercelUsage(), CREATE, lease_key="a:slice-1", boxes=1)
    assert len(PoolLease(runtime, VercelUsage(), CREATE, lease_key="a", boxes=2).slices) == 2
    PoolLease(runtime, VercelUsage(), CREATE, lease_key="Pool", boxes=1)
    assert len(PoolLease(runtime, VercelUsage(), CREATE, lease_key="pool", boxes=1).slices) == 1


def test_smaller_replay_of_a_lease_conflicts(database: Engine) -> None:
    """Check that a replay asking for fewer slices than the lease holds is refused."""
    runtime = _runtime(database)
    PoolLease(runtime, VercelUsage(), CREATE, lease_key="pool-h", boxes=3)
    with pytest.raises(BudgetConflictError):
        PoolLease(runtime, VercelUsage(), CREATE, lease_key="pool-h", boxes=2)


def test_lease_exit_keeps_the_block_error(database: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a failed release on exit does not replace the error that ended the block."""
    runtime = _runtime(database)

    def broken(*args: object, **kwargs: object) -> None:
        """Fail the release like a lost database connection."""
        raise RuntimeError("release failed")

    lease = PoolLease(runtime, VercelUsage(), CREATE, lease_key="pool-i", boxes=1)
    monkeypatch.setattr(runtime.service, "release_slices", broken)
    with pytest.raises(KeyError), lease:
        raise KeyError("pool failed")
    with pytest.raises(RuntimeError), PoolLease(runtime, VercelUsage(), CREATE, lease_key="pool-j", boxes=1):
        pass


def test_lease_that_does_not_fit_reserves_nothing(database: Engine) -> None:
    """Check that admission refuses the whole lease, not a part of it."""
    runtime = _runtime(database, limit=1)
    per_box = VercelUsage().quote(CREATE).maximum.total
    boxes = int(Decimal(1) / per_box) + 1
    with pytest.raises(BudgetInsufficientError):
        PoolLease(runtime, VercelUsage(), CREATE, lease_key="pool-f", boxes=boxes)
    assert _reserved_units(runtime) == 0
    assert _states(database) == []


def test_lease_refuses_an_invalid_slice_count(database: Engine) -> None:
    """Check the slice count bounds."""
    runtime = _runtime(database)
    for boxes in (0, MAX_LEASE_SLICES + 1):
        with pytest.raises(ValueError):
            PoolLease(runtime, VercelUsage(), CREATE, lease_key="pool-g", boxes=boxes)
