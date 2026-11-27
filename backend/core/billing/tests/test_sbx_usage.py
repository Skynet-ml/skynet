"""Verify sbx sandbox admission and measured-runtime settlement against a local ledger."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

from core.billing import sbx_usage
from core.billing.budgets import BudgetConflictError, BudgetError, BudgetService
from core.billing.operation_pricing import ChargePolicy, UnpricedOperationError
from core.billing.runtime import BudgetRuntime, UsagePendingError
from core.billing.sbx_usage import (
    BILLING_GRACE_MS,
    LIFETIME_CEILING_MS,
    SbxUsageReservation,
    quote_sbx_sandbox,
)
from core.config import settings
from core.sandbox.metering import usage_adapter
from core.sandbox.providers import sbx
from core.storage.models import Base, BillingCustomerModel, ExecutionUsageEvidenceModel

REQUEST = {"cpus": 2, "memory_mib": 4096, "lifetime_ms": 120_000}
BODY = {"resources": {"cpus": 2, "memoryMib": 4096}, "features": {"timeouts": {"timeout": "120s"}}}


class Clock:
    """Return monotonic seconds that a test sets by hand."""

    def __init__(self) -> None:
        """Start at zero."""
        self.now = 0.0

    def __call__(self) -> float:
        """Return the current time.

        Returns:
            The seconds the test set.
        """
        return self.now


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
    engine = create_engine(f"sqlite:///{tmp_path / 'sbx.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username="alice", stripe_customer_id="fixture", balance_cents=100, grant_remaining=0)
        )
        session.commit()
    yield engine
    engine.dispose()


def _runtime(database: Engine) -> BudgetRuntime:
    """Bind a budget runtime to the local ledger.

    Args:
        database: Engine of the funded ledger.

    Returns:
        A runtime for a fresh 50-cent budget owned by ``alice``.
    """
    service = BudgetService(engine=database)
    budget = service.create("alice", 50, idempotency_key="draft")
    return BudgetRuntime(service, username="alice", budget_id=budget.id, generation=0, phase="setup", wait_timeout=0)


def _cents(usd: Decimal) -> Decimal:
    """Convert a provider charge to charged cents at the test markup.

    Args:
        usd: The provider charge.

    Returns:
        The charged cents.
    """
    return ChargePolicy("sandbox").convert(usd).total


def _state(runtime: BudgetRuntime, reservation: SbxUsageReservation) -> str:
    """Return the ledger state of the reservation's operation.

    Args:
        runtime: The budget runtime.
        reservation: The reservation.

    Returns:
        The operation state.
    """
    return runtime.service.get_operation(reservation.operation.id, "alice").state


@pytest.mark.parametrize(
    ("cpus", "memory", "rate"),
    [(1, 2048, "0.07"), (2, 4096, "0.14"), (4, 8192, "0.28"), (8, 16384, "0.56"), (16, 32768, "1.12")],
)
def test_quote_covers_the_lifetime_and_grace_at_the_shape_rate(cpus: int, memory: int, rate: str) -> None:
    """Fund each published shape for its lifetime plus the create and delete grace."""
    quote = quote_sbx_sandbox({"cpus": cpus, "memory_mib": memory, "lifetime_ms": 3_600_000})
    hours = Decimal(3_600_000 + BILLING_GRACE_MS) / Decimal(3_600_000)
    assert quote.maximum.total == _cents(Decimal(rate) * hours)
    assert quote.price_snapshot["usd_per_hour"] == rate
    assert quote.price_snapshot["maximum_billable_ms"] == 3_600_000 + BILLING_GRACE_MS


@pytest.mark.parametrize(
    "change",
    [
        {"cpus": 3},
        {"memory_mib": 8192},
        {"cpus": True, "memory_mib": 2048},
        {"cpus": 2.0},
        {"cpus": [2]},
        {"cpus": None},
        {"memory_mib": 4096.0},
        {"lifetime_ms": 0},
        {"lifetime_ms": 43_200_001},
        {"lifetime_ms": 1.5},
        {"lifetime_ms": True},
    ],
)
def test_unpublished_shape_or_unbounded_lifetime_is_not_quoted(change: dict[str, Any]) -> None:
    """Refuse a shape sbx does not sell and a lifetime above the 12-hour cap."""
    with pytest.raises(UnpricedOperationError):
        quote_sbx_sandbox({**REQUEST, **change})


def test_reserve_then_settle_charges_the_measured_runtime(database: Engine) -> None:
    """Charge the seconds from the create call to the confirmed delete at the shape rate."""
    runtime = _runtime(database)
    clock = Clock()
    reservation = SbxUsageReservation(runtime, REQUEST, operation_key="box", clock=clock)
    assert reservation.operation.id in runtime._pinned
    clock.now = 10.0
    reservation.start_clock()
    reservation.confirm_created("uid-1", BODY)
    clock.now = 70.0
    reservation.stop_clock()
    reservation.settle()
    assert _state(runtime, reservation) == "settled"
    assert runtime.service.get(runtime.budget_id, "alice").setup_spent_cents == _cents(Decimal("0.14") * 60 / 3600)
    assert reservation.operation.id not in runtime._pinned


def test_runtime_is_billed_in_whole_seconds(database: Engine) -> None:
    """Round a part second up, as sbx meters by the second."""
    runtime = _runtime(database)
    clock = Clock()
    reservation = SbxUsageReservation(runtime, REQUEST, operation_key="box", clock=clock)
    reservation.start_clock()
    clock.now = 0.2
    reservation.stop_clock()
    reservation.settle()
    assert runtime.service.get(runtime.budget_id, "alice").setup_spent_cents == _cents(Decimal("0.14") / 3600)


def test_settle_clamps_a_long_runtime_to_the_reservation(database: Engine) -> None:
    """Never charge more than the hold, even when the measured runtime is longer."""
    runtime = _runtime(database)
    clock = Clock()
    reservation = SbxUsageReservation(runtime, REQUEST, operation_key="box", clock=clock)
    reservation.start_clock()
    clock.now = 20 * 3600.0
    reservation.stop_clock()
    reservation.settle()
    assert runtime.service.get(runtime.budget_id, "alice").setup_spent_cents == reservation.quote.maximum.total


def test_unconfirmed_delete_settles_at_the_whole_funded_lifetime(database: Engine) -> None:
    """Charge the full hold for a sandbox that runs on until its sbx timeout."""
    runtime = _runtime(database)
    reservation = SbxUsageReservation(runtime, REQUEST, operation_key="box", clock=Clock())
    reservation.start_clock()
    reservation.settle()
    assert _state(runtime, reservation) == "settled"
    assert runtime.service.get(runtime.budget_id, "alice").setup_spent_cents == reservation.quote.maximum.total


def test_settle_without_a_create_keeps_the_hold_pending(database: Engine) -> None:
    """Keep the hold, not a made-up charge, when the create never went out."""
    runtime = _runtime(database)
    reservation = SbxUsageReservation(runtime, REQUEST, operation_key="box")
    with pytest.raises(UsagePendingError):
        reservation.settle()
    assert _state(runtime, reservation) == "pending"
    assert runtime.service.get(runtime.budget_id, "alice").billed_cents == 0


def test_lost_create_keeps_the_hold_and_refuses_a_replay(database: Engine) -> None:
    """Keep a hold pending when no one knows if a sandbox exists, and refuse a second dispatch."""
    runtime = _runtime(database)
    reservation = SbxUsageReservation(runtime, REQUEST, operation_key="box")
    reservation.start_clock()
    reservation.pending()
    assert _state(runtime, reservation) == "pending"
    with pytest.raises(UsagePendingError, match="already dispatched"):
        SbxUsageReservation(runtime, REQUEST, operation_key="box")


def test_refused_create_releases_the_hold(database: Engine) -> None:
    """Return the coverage of a create that sbx refused outright."""
    runtime = _runtime(database)
    reservation = SbxUsageReservation(runtime, REQUEST, operation_key="box")
    reservation.start_clock()
    reservation.release()
    assert _state(runtime, reservation) == "released"
    assert runtime._pinned == {}
    snapshot = runtime.service.get(runtime.budget_id, "alice")
    assert snapshot.reserved_cents == 0
    assert snapshot.billed_cents == 0


@pytest.mark.parametrize(
    "body",
    [
        {**BODY, "resources": {"cpus": 4, "memoryMib": 8192}},
        {**BODY, "features": {"timeouts": {"timeout": "121s"}}},
        {**BODY, "features": {"timeouts": {"timeout": "two minutes"}}},
        {**BODY, "features": {"timeouts": {"timeout": 120}}},
        {**BODY, "features": {"timeouts": {}}},
    ],
)
def test_confirm_refuses_a_create_body_outside_the_funded_envelope(database: Engine, body: dict[str, Any]) -> None:
    """Refuse a create body with another shape or a longer timeout than the hold funds."""
    reservation = SbxUsageReservation(_runtime(database), REQUEST, operation_key="box")
    with pytest.raises(UsagePendingError):
        reservation.confirm_created("uid-1", body)


def test_extend_grows_the_hold_to_the_longer_lifetime(database: Engine) -> None:
    """Fund the whole extended lifetime, and refuse an extension that does not grow it."""
    runtime = _runtime(database)
    reservation = SbxUsageReservation(runtime, REQUEST, operation_key="box")
    with pytest.raises(ValueError):
        reservation.extend(REQUEST["lifetime_ms"], deadline=0.0)
    reservation.extend(600_000, deadline=0.0)
    grown = quote_sbx_sandbox({**REQUEST, "lifetime_ms": 600_000}).maximum.total
    assert runtime.service.get_operation(reservation.operation.id, "alice").max_cents == grown
    assert reservation.lifetime_ms == 600_000


def test_lease_slice_for_another_request_is_refused(database: Engine) -> None:
    """Refuse a lease slice whose fingerprint belongs to another sandbox request."""
    runtime = _runtime(database)
    other = SbxUsageReservation(runtime, {**REQUEST, "lifetime_ms": 60_000}, operation_key="other")
    with pytest.raises(BudgetConflictError):
        sbx_usage.USAGE.reserve(runtime, REQUEST, operation_key="box", reserved=other.operation)


def test_hosted_skynet_finds_the_sbx_adapter() -> None:
    """Expose the adapter where the provider list looks for it."""
    assert usage_adapter("sbx") is sbx_usage.USAGE
    assert sbx_usage.USAGE.name == "sbx"


@pytest.mark.parametrize("error", [BudgetError("refused"), RuntimeError("database down")])
def test_failed_release_keeps_the_hold_pending(
    database: Engine, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """Keep the hold pending, not lost, and raise, when the release fails for any reason."""
    runtime = _runtime(database)
    reservation = SbxUsageReservation(runtime, REQUEST, operation_key="box")

    def refuse(*args: Any, **kwargs: Any) -> None:
        """Fail the release.

        Args:
            *args: Ignored.
            **kwargs: Ignored.

        Raises:
            Exception: Always, the test's error.
        """
        raise error

    monkeypatch.setattr(runtime.service, "reject", refuse)
    with pytest.raises(type(error)):
        reservation.release()
    assert _state(runtime, reservation) == "pending"


def test_lifetime_ceiling_matches_the_provider_cap() -> None:
    """Keep the quote ceiling equal to the cap the sbx provider puts on every timeout."""
    assert LIFETIME_CEILING_MS == sbx._MAX_LIFETIME_SECONDS * 1000


def test_request_without_a_lifetime_is_not_reserved(database: Engine) -> None:
    """Refuse a request with no lifetime as unpriced, before any ledger write."""
    with pytest.raises(UnpricedOperationError):
        SbxUsageReservation(_runtime(database), {"cpus": 2, "memory_mib": 4096}, operation_key="box")


def test_measured_runtime_ignores_float_noise_of_a_large_clock(database: Engine) -> None:
    """A long-running host's large monotonic clock must not add a millisecond to the evidence.

    Args:
        database: The SQLite ledger.
    """
    runtime = _runtime(database)
    clock = Clock()
    reservation = SbxUsageReservation(runtime, REQUEST, operation_key="box", clock=clock)
    clock.now = 1e9
    reservation.start_clock()
    reservation.confirm_created("uid-1", BODY)
    clock.now = 1e9 + 10.2
    reservation.stop_clock()
    reservation.settle()
    with Session(database) as session:
        row = session.scalars(
            select(ExecutionUsageEvidenceModel).where(
                ExecutionUsageEvidenceModel.evidence_key == f"sbx-stop:{reservation.operation.id}"
            )
        ).one()
    assert row.evidence["measured_ms"] == 10_200
