"""Verify Hyperbrowser sandbox admission and measured-runtime metering without Hyperbrowser calls."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

from core.billing import hyperbrowser_usage
from core.billing.budget_amounts import cent_units
from core.billing.budgets import BudgetConflictError, BudgetService
from core.billing.hyperbrowser_usage import HyperbrowserUsageReservation, quote_hyperbrowser_sandbox
from core.billing.operation_pricing import UnpricedOperationError
from core.billing.runtime import BudgetRuntime, UsagePendingError
from core.config import settings
from core.sandbox.metering import usage_adapter
from core.storage.models import Base, BillingCustomerModel, ExecutionOperationModel, ExecutionUsageEvidenceModel

CREATE = {"image": "default", "timeout_minutes": 1, "vcpus": 2, "memory_mib": 4096}
# The funded lifetime: the 1 minute timeout plus the 120 s stop margin.
FUNDED_MS = 180_000
# 2 vCPU at $0.045 per hour plus 4 GiB at $0.015 per hour, per second.
PER_SECOND = (2 * Decimal("0.045") + 4 * Decimal("0.015")) / 3600


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
    engine = create_engine(f"sqlite:///{tmp_path / 'hyperbrowser.db'}")
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


def _evidence(database: Engine, key: str) -> ExecutionUsageEvidenceModel:
    """Return the evidence row with this key.

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


def _run(reservation: HyperbrowserUsageReservation, *, measured_sec: float | None) -> None:
    """Send, create, stop and settle one box with a fixed measured runtime on a fake clock.

    Args:
        reservation: The claimed reservation.
        measured_sec: Seconds between the create and the confirmed stop, or
            ``None`` when the stop was not confirmed.
    """
    reservation.sent()
    reservation.confirm_created(SimpleNamespace(id="hb-1"))
    if measured_sec is not None:
        assert reservation.sent_at is not None
        reservation.stopped()
        reservation.stopped_at = reservation.sent_at + measured_sec
    reservation.settle()


def test_quote_covers_the_padded_timeout_at_the_published_rates() -> None:
    """Price the timeout plus the stop margin at the CPU and memory rates, with no egress charge."""
    quote = quote_hyperbrowser_sandbox(CREATE)
    # 2 vCPU and 4 GiB cost $0.15 per hour, so 180 s cost exactly $0.0075.
    assert hyperbrowser_usage._usd(quote.price_snapshot, FUNDED_MS) == Decimal("0.0075")
    charge = hyperbrowser_usage.vercel_charge_policy(quote.price_snapshot).convert(Decimal("0.0075"))
    assert quote.maximum.total == charge.total
    assert quote.price_snapshot["provider"] == "hyperbrowser"
    assert quote.price_snapshot["source"] == "https://www.hyperbrowser.ai/docs/pricing"
    assert quote.price_snapshot["vcpu_hour_usd"] == "0.045"
    assert quote.price_snapshot["memory_gib_hour_usd"] == "0.015"
    assert not any("egress" in key or "network" in key for key in quote.price_snapshot)


def test_partial_seconds_round_up() -> None:
    """Bill a part of a second as a whole second."""
    snapshot = quote_hyperbrowser_sandbox(CREATE).price_snapshot
    assert hyperbrowser_usage._usd(snapshot, 10_001) == 11 * PER_SECOND
    assert hyperbrowser_usage._usd(snapshot, 0) == PER_SECOND


@pytest.mark.parametrize(
    "change",
    [
        {"timeout_minutes": 0},
        {"timeout_minutes": 24 * 60 + 1},
        {"timeout_minutes": True},
        {"timeout_minutes": None},
        {"timeout_minutes": 1.5},
        {"vcpus": 0},
        {"vcpus": None},
        {"memory_mib": None},
        {"memory_mib": -1},
    ],
)
def test_unbounded_box_is_not_quoted(change: dict[str, Any]) -> None:
    """Refuse a box whose timeout or size has no ceiling.

    Args:
        change: The field that breaks the quote.
    """
    with pytest.raises(UnpricedOperationError):
        quote_hyperbrowser_sandbox({**CREATE, **change})


def test_reserve_then_settle_charges_the_measured_runtime(database: Engine) -> None:
    """Settle a stopped box at its measured seconds and keep the vendor usage as evidence only.

    Args:
        database: The SQLite ledger.
    """
    runtime = _runtime(database)
    reservation = HyperbrowserUsageReservation(runtime, CREATE, operation_key="box-one")
    assert _operation(database).state == "dispatched"
    reservation.sent()
    reservation.confirm_created(SimpleNamespace(id="hb-1"))
    assert reservation.sent_at is not None
    reservation.stopped()
    reservation.stopped_at = reservation.sent_at + 10.2
    reservation.report({"credits_used": 999})
    reservation.settle()
    row = _operation(database)
    assert row.state == "settled"
    assert row.provider_request_id == "hb-1"
    charge = hyperbrowser_usage.vercel_charge_policy(reservation.quote.price_snapshot).convert(11 * PER_SECOND)
    assert row.actual_units == cent_units(charge.total)
    assert row.actual_units < row.max_units
    assert runtime._pinned == {}
    settled = _evidence(database, "hyperbrowser-stop:hb-1")
    assert settled.evidence["over_funded"] is False
    assert settled.evidence["billed_ms"] == 10_200
    assert settled.evidence["vendor"] == {"credits_used": 999}


def test_settle_clamps_to_the_hold_and_warns_on_an_over_run(database: Engine, caplog: pytest.LogCaptureFixture) -> None:
    """Bill at most the funded lifetime, flag the over-run and log a warning.

    Args:
        database: The SQLite ledger.
        caplog: The log capture.
    """
    runtime = _runtime(database)
    reservation = HyperbrowserUsageReservation(runtime, CREATE, operation_key="long")
    assert reservation.lifetime_ms == FUNDED_MS
    with caplog.at_level("WARNING", logger=hyperbrowser_usage.__name__):
        _run(reservation, measured_sec=10_000)
    row = _operation(database)
    assert row.state == "settled"
    assert row.actual_units == row.max_units
    settled = _evidence(database, "hyperbrowser-stop:hb-1")
    assert settled.evidence["over_funded"] is True
    assert settled.evidence["billed_ms"] == FUNDED_MS
    assert "more than its funded" in caplog.text


def test_unconfirmed_stop_settles_the_whole_funded_lifetime(database: Engine) -> None:
    """Bill the whole funded lifetime when the stop failed, because Hyperbrowser ends the box at its timeout.

    Args:
        database: The SQLite ledger.
    """
    runtime = _runtime(database)
    reservation = HyperbrowserUsageReservation(runtime, CREATE, operation_key="unknown")
    _run(reservation, measured_sec=None)
    row = _operation(database)
    assert row.state == "settled"
    assert row.actual_units == row.max_units
    assert _evidence(database, "hyperbrowser-stop:hb-1").evidence["billed_ms"] == FUNDED_MS


def test_settle_twice_charges_once(database: Engine) -> None:
    """Keep the first settle when a second owner settles the same hold.

    Args:
        database: The SQLite ledger.
    """
    runtime = _runtime(database)
    reservation = HyperbrowserUsageReservation(runtime, CREATE, operation_key="box-one")
    _run(reservation, measured_sec=5)
    first = _operation(database).actual_units
    assert reservation.stopped_at is not None
    reservation.stopped_at += 50
    reservation.settle()
    assert _operation(database).actual_units == first


def test_settle_without_a_sandbox_id_keeps_the_hold_pending(database: Engine) -> None:
    """Keep the hold pending when the box has no sandbox id and no confirmed stop.

    Args:
        database: The SQLite ledger.
    """
    runtime = _runtime(database)
    reservation = HyperbrowserUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.sent()
    with pytest.raises(UsagePendingError, match="sandbox id"):
        reservation.settle()
    assert _operation(database).state == "pending"


def test_a_failed_pending_call_keeps_the_settle_error(
    database: Engine, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raise the first settle error and log the error of the pending call that follows it.

    Args:
        database: The SQLite ledger.
        caplog: The log capture.
        monkeypatch: The pytest monkeypatch fixture.
    """
    reservation = HyperbrowserUsageReservation(_runtime(database), CREATE, operation_key="box-one")
    reservation.sent()

    def broken_pending() -> None:
        """Fail like a lost ledger connection.

        Raises:
            ConnectionError: Always.
        """
        raise ConnectionError("ledger down")

    monkeypatch.setattr(reservation, "pending", broken_pending)
    with (
        caplog.at_level("WARNING", logger=hyperbrowser_usage.__name__),
        pytest.raises(UsagePendingError, match="sandbox id"),
    ):
        reservation.settle()
    assert "could not mark the hold pending: ConnectionError" in caplog.text


def test_confirm_refuses_a_sandbox_with_no_id(database: Engine) -> None:
    """Refuse a created sandbox that carries no id, and remember that a box existed.

    Args:
        database: The SQLite ledger.
    """
    reservation = HyperbrowserUsageReservation(_runtime(database), CREATE, operation_key="box-one")
    reservation.sent()
    with pytest.raises(UsagePendingError, match="sandbox id"):
        reservation.confirm_created(SimpleNamespace(id=None))
    assert reservation.returned is True
    reservation.fail(absent=True)
    assert _operation(database).state == "pending"


def test_confirm_records_the_start_for_reconciliation(database: Engine) -> None:
    """Write the sandbox id and wall-clock start into the pending evidence once the sandbox exists.

    Args:
        database: The SQLite ledger.
    """
    runtime = _runtime(database)
    reservation = HyperbrowserUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.sent()
    reservation.confirm_created(SimpleNamespace(id="hb-1"))
    with Session(database) as session:
        (evidence,) = session.scalars(select(ExecutionUsageEvidenceModel))
    assert _operation(database).state == "pending"
    assert evidence.evidence["sandbox_id"] == "hb-1"
    assert evidence.evidence["sent_at"] == reservation._sent_wall
    assert evidence.evidence["funded_ms"] == FUNDED_MS


def test_unsent_create_releases_the_hold(database: Engine) -> None:
    """Release the hold when no create reached Hyperbrowser.

    Args:
        database: The SQLite ledger.
    """
    runtime = _runtime(database)
    reservation = HyperbrowserUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.fail()
    assert _operation(database).state == "released"
    assert runtime._pinned == {}


def test_refused_create_releases_the_hold(database: Engine) -> None:
    """Release the hold when Hyperbrowser refused the sent create, so no box exists.

    Args:
        database: The SQLite ledger.
    """
    runtime = _runtime(database)
    reservation = HyperbrowserUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.sent()
    reservation.fail(absent=True)
    assert _operation(database).state == "released"
    assert runtime._pinned == {}
    _evidence(database, f"hyperbrowser-refused:{reservation.operation.id}")


def test_unknown_create_keeps_the_hold_and_refuses_a_replay(database: Engine) -> None:
    """Keep the hold pending when a sent create had no definite answer, and refuse a second dispatch.

    Args:
        database: The SQLite ledger.
    """
    runtime = _runtime(database)
    reservation = HyperbrowserUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.sent()
    reservation.fail()
    assert _operation(database).state == "pending"
    with pytest.raises(UsagePendingError, match="already dispatched"):
        HyperbrowserUsageReservation(runtime, CREATE, operation_key="box-one")


def test_refusal_after_a_confirmed_sandbox_keeps_the_hold(database: Engine) -> None:
    """Keep the hold when a sandbox id exists, even if a later step reported a refusal.

    Args:
        database: The SQLite ledger.
    """
    runtime = _runtime(database)
    reservation = HyperbrowserUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.sent()
    reservation.confirm_created(SimpleNamespace(id="hb-1"))
    reservation.fail(absent=True)
    assert _operation(database).state == "pending"


def test_confirmed_stop_settles_a_box_with_no_id(database: Engine) -> None:
    """Settle a box whose id was never confirmed once its stop is confirmed, keyed by the operation id.

    Args:
        database: The SQLite ledger.
    """
    runtime = _runtime(database)
    reservation = HyperbrowserUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.sent()
    with pytest.raises(UsagePendingError):
        reservation.confirm_created(SimpleNamespace(id=None))
    reservation.stopped()
    reservation.settle()
    assert _operation(database).state == "settled"
    _evidence(database, f"hyperbrowser-stop:{reservation.operation.id}")
    reservation.fail()
    assert _operation(database).state == "settled"
    assert runtime._pinned == {}


def test_lease_slice_is_claimed_for_the_same_request(database: Engine) -> None:
    """Claim a pool lease slice reserved for the same request, and settle it like a fresh hold.

    Args:
        database: The SQLite ledger.
    """
    runtime = _runtime(database)
    slice_ = runtime.reserve(
        quote_hyperbrowser_sandbox(CREATE), operation_key="slice", cost_kind="sandbox", role="runtime"
    )
    reservation = HyperbrowserUsageReservation(runtime, CREATE, operation_key="slice", reserved=slice_)
    assert reservation.operation.id == slice_.id
    _run(reservation, measured_sec=5)
    assert _operation(database).state == "settled"
    assert runtime._pinned == {}


def test_lease_slice_for_another_request_is_refused(database: Engine) -> None:
    """Refuse a lease slice whose fingerprint belongs to a different sandbox request.

    Args:
        database: The SQLite ledger.
    """
    runtime = _runtime(database)
    slice_ = runtime.reserve(
        quote_hyperbrowser_sandbox(CREATE), operation_key="slice", cost_kind="sandbox", role="runtime"
    )
    with pytest.raises(BudgetConflictError, match="different sandbox request"):
        HyperbrowserUsageReservation(runtime, {**CREATE, "vcpus": 1}, operation_key="slice", reserved=slice_)
    assert _operation(database).state == "reserved"
    assert runtime._pinned == {}


def test_extend_is_refused(database: Engine) -> None:
    """Refuse an extension, because Hyperbrowser cannot lengthen a running sandbox's timeout.

    Args:
        database: The SQLite ledger.
    """
    runtime = _runtime(database)
    reservation = HyperbrowserUsageReservation(runtime, CREATE, operation_key="box-one")
    before = _operation(database).max_units
    with pytest.raises(UnpricedOperationError, match="cannot lengthen"):
        hyperbrowser_usage.USAGE.extend(reservation, 240_000, deadline=0)
    assert _operation(database).max_units == before
    assert reservation.lifetime_ms == FUNDED_MS


def test_hyperbrowser_usage_adapter_is_found() -> None:
    """Find ``USAGE`` through the metering registry, so a budget can fund a Hyperbrowser box."""
    adapter = usage_adapter("hyperbrowser")
    assert adapter is not None
    assert adapter is hyperbrowser_usage.USAGE
    assert adapter.name == "hyperbrowser"


def test_measured_runtime_ignores_float_noise_of_a_large_clock(database: Engine) -> None:
    """A long-running host's large monotonic clock must not add a millisecond to the bill.

    Args:
        database: The SQLite ledger.
    """
    reservation = HyperbrowserUsageReservation(_runtime(database), CREATE, operation_key="box-one")
    reservation.sent_at = 1e9
    reservation.stopped_at = 1e9 + 10.2
    assert reservation._evidence()["measured_ms"] == 10_200
    reservation.stopped_at = 1e9 + 10.2004
    assert reservation._evidence()["measured_ms"] == 10_201
