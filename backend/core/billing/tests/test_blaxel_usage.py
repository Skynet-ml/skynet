"""Verify Blaxel sandbox admission and measured-runtime metering without Blaxel calls."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

from core.billing import blaxel_usage
from core.billing.blaxel_usage import BlaxelUsageReservation, quote_blaxel_sandbox
from core.billing.budget_amounts import cent_units
from core.billing.budgets import BudgetConflictError, BudgetService
from core.billing.operation_pricing import UnpricedOperationError
from core.billing.runtime import BudgetRuntime, UsagePendingError
from core.config import settings
from core.sandbox.metering import usage_adapter
from core.storage.models import Base, BillingCustomerModel, ExecutionOperationModel, ExecutionUsageEvidenceModel

CREATE = {"image": "blaxel/base-image:latest", "lifetime_ms": 300_000, "memory_mb": 4096}
# The funded lifetime: the 5-minute ttl plus the 60 s boot and delete margin.
FUNDED_MS = 360_000
RATE = Decimal("0.0000115")
NAME = "skynet-box"


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
    engine = create_engine(f"sqlite:///{tmp_path / 'blaxel.db'}")
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


def _box(name: str = NAME) -> SimpleNamespace:
    """Build a fake Blaxel sandbox with a metadata name.

    Args:
        name: The name that Blaxel returns.

    Returns:
        The fake sandbox.
    """
    return SimpleNamespace(metadata=SimpleNamespace(name=name))


def _run(reservation: BlaxelUsageReservation, *, measured_sec: float | None) -> None:
    """Send, create, stop and settle one box with a fixed measured runtime.

    Args:
        reservation: The claimed reservation.
        measured_sec: Seconds between the create and the confirmed delete, or
            ``None`` when the delete was not confirmed.
    """
    reservation.sent(NAME)
    reservation.confirm_created(_box())
    if measured_sec is not None:
        assert reservation.sent_at is not None
        reservation.stopped()
        reservation.stopped_at = reservation.sent_at + measured_sec
    reservation.settle()


def _evidence(database: Engine, key: str) -> dict[str, Any]:
    """Return the evidence stored under one key.

    Args:
        database: Engine of the ledger.
        key: The evidence key.

    Returns:
        The stored evidence.
    """
    with Session(database) as session:
        return (
            session.scalars(select(ExecutionUsageEvidenceModel).where(ExecutionUsageEvidenceModel.evidence_key == key))
            .one()
            .evidence
        )


def test_quote_covers_the_padded_ttl_at_the_published_rate_with_no_egress() -> None:
    """Price the ttl plus the margin at the GB-second rate of the memory size, with no egress."""
    quote = quote_blaxel_sandbox(CREATE)
    expected = 360 * 4 * RATE
    assert blaxel_usage._usd(quote.price_snapshot, FUNDED_MS) == expected
    charge = blaxel_usage.vercel_charge_policy(quote.price_snapshot).convert(expected)
    assert quote.maximum.total == charge.total
    assert quote.price_snapshot["provider"] == "blaxel"
    assert quote.price_snapshot["source"] == "https://blaxel.ai/pricing"
    assert quote.price_snapshot["gb_ram_second_usd"] == "0.0000115"
    assert not any("egress" in key or "network" in key for key in quote.price_snapshot)


def test_usd_rounds_a_partial_second_up() -> None:
    """Bill a partial second as a whole second, so the charge never falls below the use."""
    snapshot = quote_blaxel_sandbox(CREATE).price_snapshot
    assert blaxel_usage._usd(snapshot, 1) == 4 * RATE
    assert blaxel_usage._usd(snapshot, 10_001) == 11 * 4 * RATE


@pytest.mark.parametrize(
    "change",
    [
        {"lifetime_ms": 299_999},
        {"lifetime_ms": 30 * 24 * 60 * 60 * 1000 + 1},
        {"lifetime_ms": True},
        {"lifetime_ms": None},
        {"memory_mb": 0},
        {"memory_mb": None},
        {"memory_mb": True},
    ],
)
def test_unbounded_box_is_not_quoted(change: dict[str, Any]) -> None:
    """Refuse a box whose ttl is outside 5 minutes to 30 days, or that has no memory size.

    Args:
        change: The field that breaks the quote.
    """
    with pytest.raises(UnpricedOperationError, match="5 minutes to 30 days"):
        quote_blaxel_sandbox({**CREATE, **change})


def test_bad_snapshot_keeps_the_usage_pending() -> None:
    """Refuse to price a snapshot of another provider."""
    with pytest.raises(UsagePendingError, match="price snapshot"):
        blaxel_usage._usd({"provider": "modal"}, 1000)


def test_reserve_then_settle_charges_the_measured_runtime(database: Engine) -> None:
    """Reserve the funded lifetime, then settle a deleted box at its measured seconds."""
    runtime = _runtime(database)
    reservation = BlaxelUsageReservation(runtime, CREATE, operation_key="box-one")
    reserved = _operation(database)
    assert reserved.state == "dispatched"
    assert reservation.lifetime_ms == FUNDED_MS
    _run(reservation, measured_sec=10.2)
    row = _operation(database)
    assert row.state == "settled"
    assert row.provider_request_id == NAME
    expected = 11 * 4 * RATE
    charge = blaxel_usage.vercel_charge_policy(reservation.quote.price_snapshot).convert(expected)
    assert row.actual_units == cent_units(charge.total)
    assert row.actual_units < row.max_units
    assert runtime._pinned == {}
    evidence = _evidence(database, f"blaxel-stop:{NAME}")
    assert evidence["over_funded"] is False
    assert evidence["billed_ms"] == 10_200
    assert evidence["source"] == "https://blaxel.ai/pricing"


def test_settle_clamps_to_the_hold_and_warns(database: Engine, caplog: pytest.LogCaptureFixture) -> None:
    """Settle an over-run at the funded time, log a warning and flag the evidence.

    Args:
        database: The SQLite ledger.
        caplog: The log capture.
    """
    runtime = _runtime(database)
    reservation = BlaxelUsageReservation(runtime, CREATE, operation_key="box-one")
    with caplog.at_level("WARNING", logger=blaxel_usage.__name__):
        _run(reservation, measured_sec=10_000)
    row = _operation(database)
    evidence = _evidence(database, f"blaxel-stop:{NAME}")
    assert row.state == "settled"
    assert row.actual_units == row.max_units
    assert evidence["over_funded"] is True
    assert evidence["billed_ms"] == FUNDED_MS
    assert "more than its funded" in caplog.text
    assert runtime._pinned == {}


def test_unconfirmed_delete_of_a_confirmed_box_settles_at_the_funded_time(database: Engine) -> None:
    """Bill the whole funded lifetime when the delete was not confirmed, because the ttl ends the box."""
    reservation = BlaxelUsageReservation(_runtime(database), CREATE, operation_key="box-one")
    _run(reservation, measured_sec=None)
    row = _operation(database)
    assert row.state == "settled"
    assert row.actual_units == row.max_units


def test_settle_with_no_box_and_no_delete_keeps_the_hold_pending(database: Engine) -> None:
    """Keep the hold pending when no box was confirmed and no delete was confirmed."""
    runtime = _runtime(database)
    reservation = BlaxelUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.sent(NAME)
    with pytest.raises(UsagePendingError, match="no confirmed box"):
        reservation.settle()
    assert _operation(database).state == "pending"
    assert runtime._pinned != {}


@pytest.mark.parametrize("returned", [None, "skynet-other"])
def test_confirm_refuses_a_box_with_no_name_or_another_name(database: Engine, returned: str | None) -> None:
    """Refuse a created sandbox whose name is missing or is not the sent name.

    Args:
        database: The SQLite ledger.
        returned: The name that the fake Blaxel returns.
    """
    reservation = BlaxelUsageReservation(_runtime(database), CREATE, operation_key="box-one")
    reservation.sent(NAME)
    with pytest.raises(UsagePendingError, match="another name"):
        reservation.confirm_created(_box(returned))
    assert reservation.sandbox_id is None


def test_confirm_records_the_box_name_and_start_for_reconciliation(database: Engine) -> None:
    """Write the box name and the wall-clock start into the pending evidence once the box is confirmed."""
    reservation = BlaxelUsageReservation(_runtime(database), CREATE, operation_key="box-one")
    reservation.sent(NAME)
    reservation.confirm_created(_box())
    with Session(database) as session:
        (evidence,) = session.scalars(select(ExecutionUsageEvidenceModel))
    assert _operation(database).state == "pending"
    assert evidence.evidence["sandbox_name"] == NAME
    assert evidence.evidence["sandbox_confirmed"] is True
    assert evidence.evidence["sent_at"] == reservation._sent_wall
    assert evidence.evidence["funded_ms"] == FUNDED_MS


def test_failure_before_the_create_releases_the_hold(database: Engine) -> None:
    """Release the hold when no create reached Blaxel."""
    runtime = _runtime(database)
    reservation = BlaxelUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.fail()
    assert _operation(database).state == "released"
    assert runtime._pinned == {}
    assert _evidence(database, f"blaxel-unsent:{reservation.operation.id}")["provider"] == "blaxel"


def test_refused_and_absent_box_releases_the_hold(database: Engine) -> None:
    """Release the hold when Blaxel refused the create and a lookup by name found no box."""
    runtime = _runtime(database)
    reservation = BlaxelUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.sent(NAME)
    reservation.fail(absent=True)
    assert _operation(database).state == "released"
    assert runtime._pinned == {}
    assert _evidence(database, f"blaxel-absent:{reservation.operation.id}")["sandbox_name"] == NAME


def test_unknown_create_keeps_the_hold_pending_with_the_name(database: Engine) -> None:
    """Keep the hold pending with the box name when a sent create has no certain outcome."""
    runtime = _runtime(database)
    reservation = BlaxelUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.sent(NAME)
    reservation.fail()
    assert _operation(database).state == "pending"
    with Session(database) as session:
        (evidence,) = session.scalars(select(ExecutionUsageEvidenceModel))
    assert evidence.evidence["sandbox_name"] == NAME
    assert evidence.evidence["sandbox_confirmed"] is False
    with pytest.raises(UsagePendingError, match="already dispatched"):
        BlaxelUsageReservation(runtime, CREATE, operation_key="box-one")


def test_absent_after_a_confirmed_box_keeps_the_hold(database: Engine) -> None:
    """Keep the hold when a box was confirmed, even if a later lookup found no box."""
    reservation = BlaxelUsageReservation(_runtime(database), CREATE, operation_key="box-one")
    reservation.sent(NAME)
    reservation.confirm_created(_box())
    reservation.fail(absent=True)
    assert _operation(database).state == "pending"


def test_settle_twice_charges_once(database: Engine) -> None:
    """Settle a hold only once; a second settle or a late fail changes nothing."""
    runtime = _runtime(database)
    reservation = BlaxelUsageReservation(runtime, CREATE, operation_key="box-one")
    _run(reservation, measured_sec=10)
    first = _operation(database).actual_units
    reservation.settle()
    reservation.fail()
    row = _operation(database)
    assert row.state == "settled"
    assert row.actual_units == first
    with Session(database) as session:
        keys = list(session.scalars(select(ExecutionUsageEvidenceModel.evidence_key)))
    assert keys.count(f"blaxel-stop:{NAME}") == 1
    assert runtime._pinned == {}


def test_lease_slice_for_another_request_is_refused(database: Engine) -> None:
    """Refuse a lease slice whose fingerprint belongs to a different sandbox request."""
    runtime = _runtime(database)
    slice_ = runtime.reserve(quote_blaxel_sandbox(CREATE), operation_key="slice", cost_kind="sandbox", role="runtime")
    with pytest.raises(BudgetConflictError, match="different sandbox request"):
        BlaxelUsageReservation(runtime, {**CREATE, "memory_mb": 2048}, operation_key="slice", reserved=slice_)
    assert _operation(database).state == "reserved"
    assert runtime._pinned == {}


def test_extend_is_refused(database: Engine) -> None:
    """Refuse an extension, because the provider never lengthens a running box's ttl."""
    reservation = BlaxelUsageReservation(_runtime(database), CREATE, operation_key="box-one")
    before = _operation(database).max_units
    with pytest.raises(UnpricedOperationError, match="cannot lengthen"):
        blaxel_usage.USAGE.extend(reservation, 600_000, deadline=0)
    assert _operation(database).max_units == before
    assert reservation.lifetime_ms == FUNDED_MS


def test_blaxel_usage_adapter_is_found() -> None:
    """Find ``USAGE`` through the metering registry, so the factory can meter Blaxel."""
    adapter = usage_adapter("blaxel")
    assert adapter is not None
    assert adapter is blaxel_usage.USAGE
    assert adapter.name == "blaxel"
