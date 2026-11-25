"""Verify Superserve sandbox admission and measured-runtime metering without Superserve calls."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

from core.billing import superserve_usage
from core.billing.budget_amounts import cent_units
from core.billing.budgets import BudgetService
from core.billing.operation_pricing import UnpricedOperationError
from core.billing.runtime import BudgetRuntime, UsagePendingError
from core.billing.superserve_usage import SuperserveUsageReservation, quote_superserve_sandbox
from core.config import settings
from core.sandbox.metering import usage_adapter
from core.storage.models import Base, BillingCustomerModel, ExecutionOperationModel, ExecutionUsageEvidenceModel

CREATE = {
    "image": "python:3.12-slim",
    "lifetime_ms": 600_000,
    "vcpu": 2,
    "memory_mib": 4096,
    "disk_mib": 10240,
    "gpu": False,
}
# The funded lifetime: the 600 s timeout plus the 60 s boot and kill margin.
FUNDED_MS = 660_000
HOURLY = 2 * Decimal("0.0504") + 4 * Decimal("0.0162") + 10 * Decimal("0.000108")


@pytest.fixture(autouse=True)
def _at_cost_markup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the usage markup to 1.0 so these tests check settlement arithmetic, not pricing policy.

    Args:
        monkeypatch: Pytest fixture that restores the setting after the test.
    """
    monkeypatch.setattr(settings, "usage_markup", 1.0)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace the runtime clock with a fake one that a test moves by hand.

    Args:
        monkeypatch: Pytest fixture that restores the clock after the test.

    Returns:
        A one-item list that holds the current fake time.
    """
    now = [1000.0]
    monkeypatch.setattr(superserve_usage, "_monotonic", lambda: now[0])
    return now


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Engine]:
    """Create a private funded wallet and ledger for real admission tests.

    Args:
        tmp_path: Per-test directory for the SQLite file.

    Yields:
        The engine bound to the funded ledger.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'superserve.db'}")
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


def _evidence(database: Engine, key: str) -> dict[str, Any]:
    """Return the evidence saved under one key.

    Args:
        database: Engine of the ledger.
        key: The evidence key.

    Returns:
        The evidence.
    """
    with Session(database) as session:
        row = session.scalars(
            select(ExecutionUsageEvidenceModel).where(ExecutionUsageEvidenceModel.evidence_key == key)
        ).one()
        return dict(row.evidence)


def _info(**overrides: Any) -> SimpleNamespace:
    """Return a ``SandboxInfo`` of the funded size.

    Args:
        **overrides: Fields to replace.

    Returns:
        The fake info.
    """
    return SimpleNamespace(
        **{"id": "sbx-1", "vcpu_count": 2, "memory_mib": 4096, "vcpu_max": None, "memory_max_mib": None, **overrides}
    )


def _run(reservation: SuperserveUsageReservation, clock: list[float], *, measured_sec: float | None) -> None:
    """Send, create, stop and settle one box with a fixed measured runtime.

    Args:
        reservation: The claimed reservation.
        clock: The fake clock.
        measured_sec: Seconds between the create and the confirmed kill, or
            ``None`` when the kill was not confirmed.
    """
    reservation.sent("box", "attempt-1")
    superserve_usage.USAGE.confirm(reservation, _info())
    if measured_sec is not None:
        clock[0] += measured_sec
        reservation.stopped()
    superserve_usage.USAGE.settle(reservation)


def _cents(reservation: SuperserveUsageReservation, usd: Decimal) -> int:
    """Convert a provider cost to the ledger units of its charge.

    Args:
        reservation: The reservation whose price snapshot converts the cost.
        usd: The provider cost.

    Returns:
        The charge in ledger units.
    """
    return cent_units(superserve_usage.vercel_charge_policy(reservation.quote.price_snapshot).convert(usd).total)


def test_quote_covers_the_padded_lifetime_at_the_published_rates() -> None:
    """Price the timeout plus the margin at the vCPU, memory and disk rates, with no egress."""
    quote = quote_superserve_sandbox(CREATE)
    assert superserve_usage._usd(quote.price_snapshot, FUNDED_MS) == 660 * HOURLY / 3600
    charge = superserve_usage.vercel_charge_policy(quote.price_snapshot).convert(660 * HOURLY / 3600)
    assert quote.maximum.total == charge.total
    snapshot = quote.price_snapshot
    assert snapshot["provider"] == "superserve"
    assert snapshot["source"] == "https://superserve.ai/pricing"
    assert snapshot["price_version"] == "superserve-public-2026-11-24"
    assert (snapshot["vcpu_hour_usd"], snapshot["memory_gib_hour_usd"], snapshot["storage_gib_hour_usd"]) == (
        "0.0504",
        "0.0162",
        "0.000108",
    )
    assert not any("egress" in key or "network" in key for key in snapshot)


def test_quote_rounds_a_part_second_up() -> None:
    """Bill a part second as a whole second, as Superserve does."""
    snapshot = quote_superserve_sandbox(CREATE).price_snapshot
    assert superserve_usage._usd(snapshot, 10_001) == 11 * HOURLY / 3600
    assert superserve_usage._usd(snapshot, 0) == HOURLY / 3600


@pytest.mark.parametrize(
    "change",
    [
        {"lifetime_ms": 0},
        {"lifetime_ms": 604_800_000 + 1},
        {"lifetime_ms": True},
        {"vcpu": 0},
        {"vcpu": None},
        {"memory_mib": None},
        {"disk_mib": 2.5},
        {"gpu": True},
    ],
)
def test_unbounded_or_gpu_box_is_not_quoted(change: dict[str, Any]) -> None:
    """Refuse a box with no bounded lifetime or size, and a box with a GPU.

    Args:
        change: The request field that makes the box unpriced.
    """
    with pytest.raises(UnpricedOperationError):
        quote_superserve_sandbox({**CREATE, **change})


def test_reserve_then_settle_charges_the_measured_runtime(database: Engine, clock: list[float]) -> None:
    """Settle a killed box at its measured seconds, below the hold."""
    runtime = _runtime(database)
    reservation = superserve_usage.USAGE.reserve(runtime, CREATE, operation_key="box-one")
    assert _operation(database).state == "dispatched"
    _run(reservation, clock, measured_sec=10.5)
    row = _operation(database)
    assert row.state == "settled"
    assert row.provider_request_id == "sbx-1"
    assert row.actual_units == _cents(reservation, 11 * HOURLY / 3600)
    assert row.actual_units < row.max_units
    assert runtime._pinned == {}
    evidence = _evidence(database, "superserve-stop:sbx-1")
    assert evidence["measured_ms"] == 10_500
    assert evidence["over_funded"] is False
    assert evidence["attempt"] == "attempt-1"


def test_settle_clamps_to_the_hold_and_warns(
    database: Engine, clock: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    """Bill at most the funded lifetime and log a warning when the box ran longer.

    Args:
        database: The SQLite ledger.
        clock: The fake clock.
        caplog: The log capture.
    """
    reservation = SuperserveUsageReservation(_runtime(database), CREATE, operation_key="long")
    with caplog.at_level("WARNING", logger=superserve_usage.__name__):
        _run(reservation, clock, measured_sec=10_000)
    row = _operation(database)
    assert row.state == "settled"
    assert row.actual_units == row.max_units
    assert _evidence(database, "superserve-stop:sbx-1")["over_funded"] is True
    assert "more than its funded" in caplog.text


def test_unconfirmed_kill_settles_at_the_hold(database: Engine, clock: list[float]) -> None:
    """Bill the funded lifetime when the kill failed, because Superserve pauses the box at its timeout."""
    reservation = SuperserveUsageReservation(_runtime(database), CREATE, operation_key="unknown")
    _run(reservation, clock, measured_sec=None)
    row = _operation(database)
    assert row.state == "settled"
    assert row.actual_units == row.max_units


def test_box_of_the_wrong_size_settles_at_the_hold(database: Engine, clock: list[float]) -> None:
    """Keep the hold pending when the box is larger than funded or can grow, then bill all of it on settle."""
    reservation = SuperserveUsageReservation(_runtime(database), CREATE, operation_key="big")
    reservation.sent("box", "attempt-1")
    reservation.bind("sbx-1")
    with pytest.raises(UsagePendingError, match="funded size"):
        reservation.check_size(_info(vcpu_max=8))
    assert _operation(database).state == "pending"
    clock[0] += 5
    reservation.stopped()
    reservation.settle()
    row = _operation(database)
    assert row.actual_units == row.max_units
    assert _evidence(database, "superserve-stop:sbx-1")["size_ok"] is False


@pytest.mark.parametrize(
    ("disk_mib", "ok"),
    [(None, True), (10240, True), (20480, False), (5120, False)],
)
def test_check_size_compares_a_reported_disk_with_the_funded_disk(
    database: Engine, disk_mib: int | None, ok: bool
) -> None:
    """Accept a box with no disk field or the funded disk, and refuse any other disk."""
    reservation = SuperserveUsageReservation(_runtime(database), CREATE, operation_key=f"disk-{disk_mib}")
    info = _info() if disk_mib is None else _info(disk_mib=disk_mib)
    if ok:
        reservation.check_size(info)
    else:
        with pytest.raises(UsagePendingError, match="funded size"):
            reservation.check_size(info)
    assert reservation.size_ok is ok


def test_charge_conversion_rounds_a_fraction_up() -> None:
    """Round a provider cost below the ledger precision up, never down to zero."""
    snapshot = quote_superserve_sandbox(CREATE).price_snapshot
    charge = superserve_usage.vercel_charge_policy(snapshot).convert(Decimal("1e-15"))
    assert charge.total > 0
    assert charge.wallet > 0


def test_bind_records_the_start_for_reconciliation(database: Engine, clock: list[float]) -> None:
    """Write the box id, name, attempt and wall-clock start into the pending evidence at once."""
    reservation = SuperserveUsageReservation(_runtime(database), CREATE, operation_key="box-one")
    reservation.sent("box", "attempt-1")
    reservation.bind("sbx-1")
    with Session(database) as session:
        (evidence,) = session.scalars(select(ExecutionUsageEvidenceModel))
    assert _operation(database).state == "pending"
    assert evidence.evidence["sandbox_id"] == "sbx-1"
    assert evidence.evidence["name"] == "box"
    assert evidence.evidence["attempt"] == "attempt-1"
    assert isinstance(evidence.evidence["sent_at"], float)
    assert evidence.evidence["funded_ms"] == FUNDED_MS


def test_bind_refuses_an_empty_id(database: Engine) -> None:
    """Refuse a created box that carries no sandbox id."""
    reservation = SuperserveUsageReservation(_runtime(database), CREATE, operation_key="box-one")
    reservation.sent("box", "attempt-1")
    with pytest.raises(UsagePendingError, match="sandbox id"):
        reservation.bind("")


def test_failure_before_the_create_releases_the_hold(database: Engine) -> None:
    """Release the hold when no create reached Superserve."""
    runtime = _runtime(database)
    reservation = SuperserveUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.fail()
    assert _operation(database).state == "released"
    assert runtime._pinned == {}


def test_refused_create_with_no_box_releases_the_hold(database: Engine, clock: list[float]) -> None:
    """Release the hold when Superserve refused the create and the lookup found no box."""
    runtime = _runtime(database)
    reservation = SuperserveUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.sent("box", "attempt-1")
    reservation.fail(absent=True)
    assert _operation(database).state == "released"
    assert runtime._pinned == {}


def test_unknown_create_keeps_the_hold_pending_with_evidence(database: Engine, clock: list[float]) -> None:
    """Keep the hold pending with the name and attempt when the create outcome is unknown."""
    runtime = _runtime(database)
    reservation = SuperserveUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.sent("box", "attempt-1")
    reservation.fail()
    assert _operation(database).state == "pending"
    with Session(database) as session:
        (evidence,) = session.scalars(select(ExecutionUsageEvidenceModel))
    assert evidence.evidence["name"] == "box"
    assert evidence.evidence["attempt"] == "attempt-1"


def test_absent_after_a_bound_box_keeps_the_hold(database: Engine, clock: list[float]) -> None:
    """Keep the hold when a box id exists, even if a later lookup found no box."""
    reservation = SuperserveUsageReservation(_runtime(database), CREATE, operation_key="box-one")
    reservation.sent("box", "attempt-1")
    reservation.bind("sbx-1")
    reservation.fail(absent=True)
    assert _operation(database).state == "pending"


def test_settle_without_an_id_or_a_kill_keeps_the_hold_pending(database: Engine, clock: list[float]) -> None:
    """Keep the hold pending when the box has no id and no confirmed kill."""
    reservation = SuperserveUsageReservation(_runtime(database), CREATE, operation_key="box-one")
    reservation.sent("box", "attempt-1")
    with pytest.raises(UsagePendingError, match="sandbox id"):
        reservation.settle()
    assert _operation(database).state == "pending"


def test_second_settle_and_late_fail_do_not_charge_again(database: Engine, clock: list[float]) -> None:
    """Charge one box once, even when a second owner settles or fails it later."""
    runtime = _runtime(database)
    reservation = SuperserveUsageReservation(runtime, CREATE, operation_key="box-one")
    _run(reservation, clock, measured_sec=5)
    charged = _operation(database).actual_units
    reservation.settle()
    reservation.fail(absent=True)
    row = _operation(database)
    assert row.state == "settled"
    assert row.actual_units == charged
    assert runtime._pinned == {}


def test_failure_after_the_create_refuses_a_replay(database: Engine, clock: list[float]) -> None:
    """Refuse a second dispatch of an operation whose create outcome is unknown."""
    runtime = _runtime(database)
    reservation = SuperserveUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.sent("box", "attempt-1")
    reservation.fail()
    with pytest.raises(UsagePendingError, match="already dispatched"):
        SuperserveUsageReservation(runtime, CREATE, operation_key="box-one")


def test_extend_is_refused(database: Engine) -> None:
    """Refuse an extension, because a running box's lifetime is not priced again."""
    reservation = SuperserveUsageReservation(_runtime(database), CREATE, operation_key="box-one")
    before = _operation(database).max_units
    with pytest.raises(UnpricedOperationError, match="does not lengthen"):
        superserve_usage.USAGE.extend(reservation, 1_200_000, deadline=0)
    assert _operation(database).max_units == before
    assert reservation.lifetime_ms == FUNDED_MS


def test_superserve_usage_adapter_is_found() -> None:
    """Find ``USAGE`` through the metering registry, so a protected run can use Superserve."""
    adapter = usage_adapter("superserve")
    assert adapter is not None
    assert adapter is superserve_usage.USAGE
    assert adapter.name == "superserve"
