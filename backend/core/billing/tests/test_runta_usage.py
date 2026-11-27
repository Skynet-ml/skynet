"""Verify Runta runtime admission and measured-wall-time metering without Runta calls."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

from core.billing import runta_usage
from core.billing.budget_amounts import cent_units
from core.billing.budgets import BudgetConflictError, BudgetService
from core.billing.operation_pricing import UnpricedOperationError
from core.billing.runta_usage import RuntaUsageReservation, quote_runta_runtime
from core.billing.runtime import BudgetRuntime, UsagePendingError
from core.config import settings
from core.sandbox.metering import usage_adapter
from core.storage.models import Base, BillingCustomerModel, ExecutionOperationModel, ExecutionUsageEvidenceModel

CREATE = {"lifetime_ms": 60_000, "vcpus": 2, "memory_mib": 4096, "disk_gib": 16}
# The funded lifetime: the 60 s lifetime plus the 300 s boot, retention and delete margin.
FUNDED_MS = 360_000
# Read from https://runta.com/pricing on 2026-11-24.
HOURLY = 2 * Decimal("0.0504") + 4 * Decimal("0.0162") + 16 * Decimal("0.000108")


@pytest.fixture(autouse=True)
def _at_cost_markup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the usage markup to 1.0 so these tests check settlement arithmetic, not pricing policy.

    Args:
        monkeypatch: Pytest fixture that restores the setting after the test.
    """
    monkeypatch.setattr(settings, "usage_markup", 1.0)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Engine]:
    """Create a private funded wallet and ledger for real admission tests.

    Args:
        tmp_path: Per-test directory for the SQLite file.

    Yields:
        The engine bound to the funded ledger.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'runta.db'}")
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
        return session.scalars(select(ExecutionOperationModel)).one()


def _stop_evidence(database: Engine, key: str) -> ExecutionUsageEvidenceModel:
    """Return the settle evidence row with a key.

    Args:
        database: Engine of the ledger.
        key: The evidence key.

    Returns:
        The evidence row.
    """
    with Session(database) as session:
        return session.scalars(
            select(ExecutionUsageEvidenceModel).where(ExecutionUsageEvidenceModel.evidence_key == key)
        ).one()


def _run(reservation: RuntaUsageReservation, *, measured_sec: float) -> None:
    """Send, create, delete and settle one box with a fixed measured wall time.

    Args:
        reservation: The claimed reservation.
        measured_sec: Seconds between the create call and the confirmed delete.
    """
    reservation.sent("skynet-box")
    reservation.confirm_created(SimpleNamespace(id="rt-1"))
    assert reservation.sent_at is not None
    reservation.stopped()
    reservation.stopped_at = reservation.sent_at + measured_sec
    reservation.settle()


def _charge_units(reservation: RuntaUsageReservation, usd: Decimal) -> int:
    """Convert a provider cost to the ledger units the settle writes.

    Args:
        reservation: The reservation whose price snapshot converts the cost.
        usd: The provider cost.

    Returns:
        The charged ledger units.
    """
    return cent_units(runta_usage.vercel_charge_policy(reservation.quote.price_snapshot).convert(usd).total)


def test_quote_covers_the_padded_lifetime_at_the_published_rates_with_no_egress() -> None:
    """Price the lifetime plus the margin at the vCPU, memory and disk rates, with no egress."""
    quote = quote_runta_runtime(CREATE)
    expected = Decimal(FUNDED_MS // 1000) * HOURLY / Decimal(3600)
    assert runta_usage._usd(quote.price_snapshot, FUNDED_MS) == expected
    charge = runta_usage.vercel_charge_policy(quote.price_snapshot).convert(expected)
    assert quote.maximum.total == charge.total
    assert quote.price_snapshot["provider"] == "runta"
    assert quote.price_snapshot["source"] == "https://runta.com/pricing"
    assert quote.price_snapshot["vcpu_hour_usd"] == "0.0504"
    assert quote.price_snapshot["memory_gib_hour_usd"] == "0.0162"
    assert quote.price_snapshot["disk_gib_hour_usd"] == "0.000108"
    assert not any("egress" in key or "network" in key for key in quote.price_snapshot)


def test_cost_rounds_a_partial_second_up() -> None:
    """Bill a partial second as a whole second, so the charge never falls short."""
    snapshot = quote_runta_runtime(CREATE).price_snapshot
    assert runta_usage._usd(snapshot, 1_001) == 2 * HOURLY / Decimal(3600)
    assert runta_usage._usd(snapshot, 0) == HOURLY / Decimal(3600)


@pytest.mark.parametrize(
    "change",
    [
        {"lifetime_ms": 0},
        {"lifetime_ms": 24 * 60 * 60 * 1000 + 1},
        {"lifetime_ms": True},
        {"lifetime_ms": None},
        {"vcpus": 0},
        {"vcpus": 1.5},
        {"memory_mib": None},
        {"disk_gib": 15},
    ],
)
def test_unbounded_or_invalid_runtime_is_not_quoted(change: dict[str, Any]) -> None:
    """Refuse a runtime whose lifetime has no ceiling or whose size is not whole and positive.

    Args:
        change: The field that breaks the quote.
    """
    with pytest.raises(UnpricedOperationError):
        quote_runta_runtime({**CREATE, **change})


def test_reserve_then_settle_charges_the_measured_wall_time(database: Engine) -> None:
    """Hold the funded lifetime before the create, then settle the deleted box at its measured seconds."""
    runtime = _runtime(database)
    reservation = RuntaUsageReservation(runtime, CREATE, operation_key="box-one")
    assert _operation(database).state == "dispatched"
    assert reservation.lifetime_ms == FUNDED_MS
    _run(reservation, measured_sec=10.2)
    row = _operation(database)
    assert row.state == "settled"
    assert row.provider_request_id == "rt-1"
    assert row.actual_units == _charge_units(reservation, 11 * HOURLY / Decimal(3600))
    assert row.actual_units < row.max_units
    assert runtime._pinned == {}
    evidence = _stop_evidence(database, "runta-stop:rt-1").evidence
    assert evidence["over_funded"] is False
    assert evidence["billed_ms"] == 10_200
    assert evidence["name"] == "skynet-box"


def test_settle_clamps_to_the_hold_and_warns(database: Engine, caplog: pytest.LogCaptureFixture) -> None:
    """Settle at the funded time, log a warning and flag the evidence when the box outlived its hold.

    Args:
        database: The SQLite ledger.
        caplog: The log capture.
    """
    runtime = _runtime(database)
    reservation = RuntaUsageReservation(runtime, CREATE, operation_key="box-one")
    with caplog.at_level("WARNING", logger=runta_usage.__name__):
        _run(reservation, measured_sec=10_000)
    row = _operation(database)
    evidence = _stop_evidence(database, "runta-stop:rt-1").evidence
    assert row.state == "settled"
    assert row.actual_units == row.max_units
    assert evidence["over_funded"] is True
    assert evidence["billed_ms"] == FUNDED_MS
    assert "more than its funded" in caplog.text


def test_settle_without_a_confirmed_delete_keeps_the_hold_pending(database: Engine) -> None:
    """Keep the hold pending when the delete was not confirmed, and never settle at a guess."""
    runtime = _runtime(database)
    reservation = RuntaUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.sent("skynet-box")
    reservation.confirm_created(SimpleNamespace(id="rt-1"))
    with pytest.raises(UsagePendingError, match="confirmed delete"):
        reservation.settle()
    assert _operation(database).state == "pending"
    assert runtime._pinned != {}


def test_a_failed_pending_call_keeps_the_first_settle_error(
    database: Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Raise the ledger settle error, not the error of the pending call after it.

    Args:
        database: The SQLite ledger.
        monkeypatch: The pytest monkeypatch fixture.
        caplog: The log capture.
    """
    runtime = _runtime(database)
    reservation = RuntaUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.sent("skynet-box")
    reservation.confirm_created(SimpleNamespace(id="rt-1"))
    reservation.stopped()

    def broken(*args: Any, **kwargs: Any) -> None:
        """Fail like a ledger that is down.

        Args:
            *args: Ignored.
            **kwargs: Ignored.

        Raises:
            ConnectionError: Always.
        """
        raise ConnectionError("ledger down")

    def broken_pending() -> None:
        """Fail the pending call.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("pending down")

    monkeypatch.setattr(runtime.service, "settle", broken)
    monkeypatch.setattr(reservation, "pending", broken_pending)
    with caplog.at_level("WARNING", logger=runta_usage.__name__), pytest.raises(ConnectionError, match="ledger down"):
        reservation.settle()
    assert "could not mark the hold pending: RuntimeError" in caplog.text


def test_confirm_refuses_a_runtime_with_no_id(database: Engine) -> None:
    """Refuse a created runtime that carries no Runta id."""
    reservation = RuntaUsageReservation(_runtime(database), CREATE, operation_key="box-one")
    reservation.sent("skynet-box")
    with pytest.raises(UsagePendingError, match="runtime id"):
        reservation.confirm_created(SimpleNamespace(id=None))
    assert reservation.returned is True


def test_confirm_records_the_start_for_reconciliation(database: Engine) -> None:
    """Write the runtime id, the name and the wall-clock start into the pending evidence."""
    runtime = _runtime(database)
    reservation = RuntaUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.sent("skynet-box")
    reservation.confirm_created(SimpleNamespace(id="rt-1"))
    with Session(database) as session:
        (evidence,) = session.scalars(select(ExecutionUsageEvidenceModel))
    assert _operation(database).state == "pending"
    assert evidence.evidence["runtime_id"] == "rt-1"
    assert evidence.evidence["name"] == "skynet-box"
    assert isinstance(evidence.evidence["sent_at"], float)
    assert evidence.evidence["funded_ms"] == FUNDED_MS


def test_a_failed_confirm_leaves_no_runtime_id(database: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the runtime id empty when the ledger refuses it, so the provider confirms again.

    Args:
        database: The SQLite ledger.
        monkeypatch: The pytest monkeypatch fixture.
    """
    runtime = _runtime(database)
    reservation = RuntaUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.sent("skynet-box")

    def broken(*args: Any, **kwargs: Any) -> None:
        """Fail like a ledger that is down.

        Args:
            *args: Ignored.
            **kwargs: Ignored.

        Raises:
            ConnectionError: Always.
        """
        raise ConnectionError("ledger down")

    working = runtime.service.mark_dispatched
    monkeypatch.setattr(runtime.service, "mark_dispatched", broken)
    with pytest.raises(ConnectionError):
        reservation.confirm_created(SimpleNamespace(id="rt-1"))
    assert reservation.runtime_id is None
    monkeypatch.setattr(runtime.service, "mark_dispatched", working)
    reservation.confirm_created(SimpleNamespace(id="rt-1"))
    assert reservation.runtime_id == "rt-1"


def test_a_stray_duplicate_keeps_the_hold_pending_with_its_id(database: Engine) -> None:
    """Refuse to settle while a duplicate runtime did not delete, and keep its id in the evidence."""
    runtime = _runtime(database)
    reservation = RuntaUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.sent("skynet-box")
    reservation.keep_stray("rt-2")
    reservation.confirm_created(SimpleNamespace(id="rt-1"))
    reservation.stopped()
    with pytest.raises(UsagePendingError, match="duplicate"):
        reservation.settle()
    assert _operation(database).state == "pending"
    with Session(database) as session:
        rows = session.scalars(select(ExecutionUsageEvidenceModel)).all()
    assert rows
    assert all(row.evidence["stray_runtime_ids"] == ["rt-2"] for row in rows)


def test_a_stray_is_kept_once_and_a_gone_stray_lets_the_hold_settle(database: Engine) -> None:
    """Keep one entry for a repeated duplicate id, and settle once its later delete succeeded."""
    runtime = _runtime(database)
    reservation = RuntaUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.sent("skynet-box")
    reservation.keep_stray("rt-2")
    reservation.keep_stray("rt-2")
    assert reservation.strays == ["rt-2"]
    reservation.confirm_created(SimpleNamespace(id="rt-1"))
    reservation.stopped()
    reservation.stray_gone("rt-2")
    reservation.settle()
    assert _operation(database).state == "settled"


def test_failure_before_the_create_releases_the_hold(database: Engine) -> None:
    """Release the hold when no create reached Runta."""
    runtime = _runtime(database)
    reservation = RuntaUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.fail()
    assert _operation(database).state == "released"
    assert runtime._pinned == {}


def test_refused_create_with_no_box_releases_the_hold(database: Engine) -> None:
    """Release the hold when Runta refused the create and the name lookup found no runtime."""
    runtime = _runtime(database)
    reservation = RuntaUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.sent("skynet-box")
    reservation.fail(absent=True)
    assert _operation(database).state == "released"
    assert runtime._pinned == {}


def test_unknown_create_outcome_keeps_the_hold_and_refuses_a_replay(database: Engine) -> None:
    """Keep the hold pending with the name when a sent create has an unknown outcome."""
    runtime = _runtime(database)
    reservation = RuntaUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.sent("skynet-box")
    reservation.fail()
    assert _operation(database).state == "pending"
    with Session(database) as session:
        (evidence,) = session.scalars(select(ExecutionUsageEvidenceModel))
    assert evidence.evidence["name"] == "skynet-box"
    with pytest.raises(UsagePendingError, match="already dispatched"):
        RuntaUsageReservation(runtime, CREATE, operation_key="box-one")


def test_absent_after_a_confirmed_runtime_keeps_the_hold(database: Engine) -> None:
    """Keep the hold when a runtime id exists, even if a later lookup found no runtime."""
    runtime = _runtime(database)
    reservation = RuntaUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.sent("skynet-box")
    reservation.confirm_created(SimpleNamespace(id="rt-1"))
    reservation.fail(absent=True)
    assert _operation(database).state == "pending"


def test_absent_after_a_returned_handle_keeps_the_hold(database: Engine) -> None:
    """Keep the hold when a create returned a handle with no id, because a runtime surely existed."""
    runtime = _runtime(database)
    reservation = RuntaUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.sent("skynet-box")
    with pytest.raises(UsagePendingError):
        reservation.confirm_created(SimpleNamespace(id=None))
    reservation.fail(absent=True)
    assert _operation(database).state == "pending"


def test_confirmed_delete_settles_a_box_with_no_id(database: Engine) -> None:
    """Settle a box whose id was never confirmed once its delete is confirmed, keyed by the operation id."""
    runtime = _runtime(database)
    reservation = RuntaUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.sent("skynet-box")
    with pytest.raises(UsagePendingError):
        reservation.confirm_created(SimpleNamespace(id=None))
    reservation.stopped()
    reservation.settle()
    assert _operation(database).state == "settled"
    _stop_evidence(database, f"runta-stop:{reservation.operation.id}")
    reservation.fail()
    assert _operation(database).state == "settled"
    assert runtime._pinned == {}


def test_lease_slice_is_claimed_for_the_same_request(database: Engine) -> None:
    """Claim a pool lease slice reserved for the same request, and settle it like a fresh hold."""
    runtime = _runtime(database)
    slice_ = runtime.reserve(quote_runta_runtime(CREATE), operation_key="slice", cost_kind="sandbox", role="runtime")
    reservation = RuntaUsageReservation(runtime, CREATE, operation_key="slice", reserved=slice_)
    assert reservation.operation.id == slice_.id
    _run(reservation, measured_sec=5)
    assert _operation(database).state == "settled"
    assert runtime._pinned == {}


def test_lease_slice_for_another_request_is_refused(database: Engine) -> None:
    """Refuse a lease slice whose fingerprint belongs to a different runtime request."""
    runtime = _runtime(database)
    slice_ = runtime.reserve(quote_runta_runtime(CREATE), operation_key="slice", cost_kind="sandbox", role="runtime")
    with pytest.raises(BudgetConflictError, match="different runtime request"):
        RuntaUsageReservation(runtime, {**CREATE, "vcpus": 1}, operation_key="slice", reserved=slice_)
    assert _operation(database).state == "reserved"
    assert runtime._pinned == {}


def test_extend_is_refused(database: Engine) -> None:
    """Refuse an extension, because the provider deletes the box at its funded lifetime."""
    runtime = _runtime(database)
    reservation = RuntaUsageReservation(runtime, CREATE, operation_key="box-one")
    before = _operation(database).max_units
    with pytest.raises(UnpricedOperationError, match="cannot extend"):
        runta_usage.USAGE.extend(reservation, 240_000, deadline=0)
    assert _operation(database).max_units == before
    assert reservation.lifetime_ms == FUNDED_MS


def test_runta_usage_adapter_is_found() -> None:
    """Find ``USAGE`` through the metering registry, so hosted Skynet can meter Runta."""
    adapter = usage_adapter("runta")
    assert adapter is not None
    assert adapter is runta_usage.USAGE
    assert adapter.name == "runta"


def test_measured_wall_time_ignores_float_noise_of_a_large_clock(database: Engine) -> None:
    """A long-running host's large monotonic clock must not add a millisecond to the bill.

    Args:
        database: The SQLite ledger.
    """
    reservation = RuntaUsageReservation(_runtime(database), CREATE, operation_key="box-one")
    reservation.sent_at = 1e9
    reservation.stopped_at = 1e9 + 10.2
    assert reservation._evidence()["measured_ms"] == 10_200
    reservation.stopped_at = 1e9 + 10.2004
    assert reservation._evidence()["measured_ms"] == 10_201
