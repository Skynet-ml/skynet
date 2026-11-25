"""Verify HF Sandbox admission and per-minute metering without HF calls."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

from core.billing import hf_sandbox_usage
from core.billing.budget_amounts import cent_units
from core.billing.budgets import BudgetService
from core.billing.hf_sandbox_usage import FUNDED_MS, HfSandboxUsageReservation, quote_hf_sandbox
from core.billing.operation_pricing import UnpricedOperationError
from core.billing.runtime import BudgetRuntime, UsagePendingError
from core.config import settings
from core.sandbox.metering import usage_adapter
from core.storage.models import Base, BillingCustomerModel, ExecutionOperationModel, ExecutionUsageEvidenceModel

CREATE = {"flavor": "cpu-basic", "lifetime_ms": 600_000, "exposed_ports": 1, "image": "python:3.12"}
START = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


class FakeClock:
    """A monotonic clock that the test moves by hand."""

    def __init__(self) -> None:
        """Start the clock at 1000 seconds."""
        self.now = 1000.0

    def __call__(self) -> float:
        """Return the current time.

        Returns:
            The current clock value in seconds.
        """
        return self.now


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
    engine = create_engine(f"sqlite:///{tmp_path / 'hf.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(
                username="alice", stripe_customer_id="fixture", balance_cents=10_000, grant_remaining=0
            )
        )
        session.commit()
    yield engine
    engine.dispose()


def _runtime(database: Engine) -> BudgetRuntime:
    """Bind a real budget runtime to the local ledger.

    Args:
        database: Engine of the funded ledger.

    Returns:
        A runtime for a fresh 50-dollar budget owned by ``alice``.
    """
    service = BudgetService(engine=database)
    budget = service.create("alice", 5000, idempotency_key="draft")
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
        The saved evidence.
    """
    with Session(database) as session:
        return (
            session.scalars(select(ExecutionUsageEvidenceModel).where(ExecutionUsageEvidenceModel.evidence_key == key))
            .one()
            .evidence
        )


def _reservation(database: Engine, request: dict[str, Any] = CREATE) -> tuple[HfSandboxUsageReservation, FakeClock]:
    """Reserve one box with a fake clock.

    Args:
        database: Engine of the ledger.
        request: The create parameters.

    Returns:
        The claimed reservation and its clock.
    """
    clock = FakeClock()
    runtime = _runtime(database)
    return HfSandboxUsageReservation(runtime, request, operation_key="box-one", clock=clock), clock


def _up(usd: Decimal) -> Decimal:
    """Round a cost up to the ten decimal places the meter keeps.

    Args:
        usd: The exact cost.

    Returns:
        The cost rounded up.
    """
    return usd.quantize(Decimal("0.0000000001"), rounding=ROUND_CEILING)


def _charge(reservation: HfSandboxUsageReservation, usd: Decimal) -> int:
    """Convert a provider cost to the ledger units of its charge.

    Args:
        reservation: The reservation whose price snapshot sets the policy.
        usd: The provider cost.

    Returns:
        The charge in ledger units.
    """
    charge = hf_sandbox_usage.vercel_charge_policy(reservation.quote.price_snapshot).convert(usd)
    return cent_units(charge.total)


def test_quote_funds_the_fixed_24_hour_timeout_plus_the_margin_at_the_published_rates() -> None:
    """Price the 24-hour SDK timeout plus the start margin at the flavor and exposed-port rates."""
    quote = quote_hf_sandbox(CREATE)
    assert FUNDED_MS == (24 * 60 + 30) * 60_000
    assert hf_sandbox_usage._usd(quote.price_snapshot, FUNDED_MS) == Decimal(1470) * Decimal("0.02") / 60
    assert quote.price_snapshot["source"] == "https://huggingface.co/docs/hub/jobs-pricing"
    assert quote.price_snapshot["read_date"] == "2026-11-24"
    gpu = quote_hf_sandbox({**CREATE, "flavor": "a10g-small", "exposed_ports": 0}).price_snapshot
    assert hf_sandbox_usage._usd(gpu, 60_000) == _up(Decimal("1.00") / 60)
    upgrade = quote_hf_sandbox({**CREATE, "flavor": "cpu-upgrade"}).price_snapshot
    assert hf_sandbox_usage._usd(upgrade, 60_000) == _up(Decimal("0.04") / 60)


def test_usd_rounds_up_to_whole_minutes() -> None:
    """Bill a partial minute as a whole minute, and a zero-length job as one minute."""
    snapshot = quote_hf_sandbox(CREATE).price_snapshot
    per_minute = Decimal("0.02") / 60
    assert hf_sandbox_usage._usd(snapshot, 0) == hf_sandbox_usage._usd(snapshot, 60_000)
    assert hf_sandbox_usage._usd(snapshot, 60_001) == _up(2 * per_minute)


@pytest.mark.parametrize(
    "change",
    [
        {"flavor": "t4-small"},
        {"flavor": None},
        {"lifetime_ms": 0},
        {"lifetime_ms": 24 * 60 * 60 * 1000 + 1},
        {"lifetime_ms": True},
        {"exposed_ports": None},
        {"exposed_ports": -1},
        {"exposed_ports": True},
    ],
)
def test_unpinned_or_unbounded_box_is_not_quoted(change: dict[str, Any]) -> None:
    """Refuse a flavor with no pinned rate, an unbounded lifetime, or an unknown port count.

    Args:
        change: The field that breaks the quote.
    """
    with pytest.raises(UnpricedOperationError):
        quote_hf_sandbox({**CREATE, **change})


def test_reserve_then_settle_charges_the_job_minutes(database: Engine) -> None:
    """Settle a stopped box at the job's own minutes, with the job id in the evidence."""
    reservation, clock = _reservation(database)
    reservation.sent("skynet-label")
    reservation.confirm_created(SimpleNamespace(id="job-1"))
    clock.now += 900
    reservation.stopped(START, START + timedelta(minutes=4, seconds=1))
    reservation.settle()
    row = _operation(database)
    assert row.state == "settled"
    assert row.provider_request_id == "job-1"
    assert row.actual_units == _charge(reservation, _up(5 * Decimal("0.02") / 60))
    assert row.actual_units < row.max_units
    assert reservation.runtime._pinned == {}
    evidence = _evidence(database, "hf-sandbox-stop:job-1")
    assert evidence["job_id"] == "job-1"
    assert evidence["time_source"] == "job"
    assert evidence["billed_minutes"] == 5
    assert evidence["over_funded"] is False


def test_settle_charges_from_created_at_and_keeps_both_times_in_the_evidence(database: Engine) -> None:
    """Charge from created_at, not started_at or the running time, and keep the times and durations for audits."""
    reservation, clock = _reservation(database)
    reservation.sent("skynet-label")
    reservation.confirm_created(SimpleNamespace(id="job-1"))
    clock.now += 900
    started = START + timedelta(minutes=2)
    durations = SimpleNamespace(scheduling_secs=120, running_secs=121, total_secs=241)
    reservation.stopped(START, START + timedelta(minutes=4, seconds=1), started_at=started, durations=durations)
    reservation.settle()
    evidence = _evidence(database, "hf-sandbox-stop:job-1")
    assert evidence["job_created_at"] == START.isoformat()
    assert evidence["job_started_at"] == started.isoformat()
    assert evidence["job_durations"] == {"scheduling_secs": 120, "running_secs": 121, "total_secs": 241}
    # The 2 scheduling minutes stay in the charge, because HF may bill Starting inside them.
    assert evidence["billed_minutes"] == 5


def test_settle_falls_back_to_the_measured_wall_time(database: Engine) -> None:
    """Settle from the create-to-stop wall time when HF reports no usable job times."""
    reservation, clock = _reservation(database)
    reservation.sent("skynet-label")
    reservation.confirm_created(SimpleNamespace(id="job-1"))
    clock.now += 125
    reservation.stopped(START, None)
    reservation.settle()
    row = _operation(database)
    assert row.state == "settled"
    assert row.actual_units == _charge(reservation, _up(3 * Decimal("0.02") / 60))
    assert _evidence(database, "hf-sandbox-stop:job-1")["time_source"] == "measured"


def test_settle_clamps_to_the_hold_with_a_warning(database: Engine, caplog: pytest.LogCaptureFixture) -> None:
    """Settle at the funded time, log a warning and flag the evidence when the job ran longer.

    Args:
        database: The SQLite ledger.
        caplog: The log capture.
    """
    reservation, _ = _reservation(database)
    reservation.sent("skynet-label")
    reservation.confirm_created(SimpleNamespace(id="job-1"))
    reservation.stopped(START, START + timedelta(hours=30))
    with caplog.at_level("WARNING", logger=hf_sandbox_usage.__name__):
        reservation.settle()
    row = _operation(database)
    evidence = _evidence(database, "hf-sandbox-stop:job-1")
    assert row.state == "settled"
    assert row.actual_units == row.max_units
    assert evidence["over_funded"] is True
    assert evidence["billed_ms"] == FUNDED_MS
    assert "more than its funded" in caplog.text


def test_settle_runs_only_once(database: Engine) -> None:
    """Ignore a second settle, so one hold is never charged twice."""
    reservation, _ = _reservation(database)
    reservation.sent("skynet-label")
    reservation.confirm_created(SimpleNamespace(id="job-1"))
    reservation.stopped(START, START + timedelta(minutes=1))
    reservation.settle()
    first = _operation(database).actual_units
    reservation.settle()
    assert _operation(database).actual_units == first


def test_settle_without_a_job_id_keeps_the_hold_pending(database: Engine) -> None:
    """Keep the hold pending when the box has no job id, because no usage can be tied to a job."""
    reservation, _ = _reservation(database)
    reservation.sent("skynet-label")
    reservation.stopped()
    with pytest.raises(UsagePendingError, match="no job id"):
        reservation.settle()
    assert _operation(database).state == "pending"


def test_confirm_refuses_a_job_with_no_id(database: Engine) -> None:
    """Refuse a created sandbox that carries no job id."""
    reservation, _ = _reservation(database)
    with pytest.raises(UsagePendingError, match="no job id"):
        reservation.confirm_created(SimpleNamespace(id=None))


def test_confirm_writes_the_job_id_into_the_pending_evidence(database: Engine) -> None:
    """Record the job id and the label at once, so a dead worker leaves enough to reconcile."""
    reservation, _ = _reservation(database)
    reservation.sent("skynet-label")
    reservation.confirm_created(SimpleNamespace(id="job-1"))
    with Session(database) as session:
        (evidence,) = session.scalars(select(ExecutionUsageEvidenceModel))
    assert _operation(database).state == "pending"
    assert evidence.evidence["job_id"] == "job-1"
    assert evidence.evidence["label"] == "skynet-label"


def test_unsent_create_releases_the_hold(database: Engine) -> None:
    """Release the hold when no create reached HF."""
    reservation, _ = _reservation(database)
    reservation.fail()
    assert _operation(database).state == "released"
    assert reservation.runtime._pinned == {}


def test_refused_create_with_no_job_releases_the_hold(database: Engine) -> None:
    """Release the hold when HF refused the create and the label lookup found no job."""
    reservation, _ = _reservation(database)
    reservation.sent("skynet-label")
    reservation.fail(absent=True)
    assert _operation(database).state == "released"
    assert reservation.runtime._pinned == {}


def test_unknown_create_keeps_the_hold_pending(database: Engine) -> None:
    """Keep the hold pending when a sent create failed in a way that does not prove no job exists."""
    reservation, _ = _reservation(database)
    reservation.sent("skynet-label")
    reservation.fail()
    assert _operation(database).state == "pending"
    with pytest.raises(UsagePendingError, match="already dispatched"):
        HfSandboxUsageReservation(reservation.runtime, CREATE, operation_key="box-one")


def test_absent_after_a_job_id_keeps_the_hold(database: Engine) -> None:
    """Keep the hold when a job id exists, even if a later lookup found no job."""
    reservation, _ = _reservation(database)
    reservation.sent("skynet-label")
    reservation.confirm_created(SimpleNamespace(id="job-1"))
    reservation.fail(absent=True)
    assert _operation(database).state == "pending"


def test_extend_is_refused(database: Engine) -> None:
    """Refuse an extension, because HF cannot lengthen a running job's timeout."""
    reservation, _ = _reservation(database)
    before = _operation(database).max_units
    with pytest.raises(UnpricedOperationError, match="cannot lengthen"):
        hf_sandbox_usage.USAGE.extend(reservation, FUNDED_MS * 2, deadline=0)
    assert _operation(database).max_units == before


def test_hf_sandbox_usage_adapter_is_found() -> None:
    """Find ``USAGE`` through the metering registry, so the bridge can fund an HF Sandbox."""
    adapter = usage_adapter("hf_sandbox")
    assert adapter is not None
    assert adapter is hf_sandbox_usage.USAGE
    assert adapter.name == "hf_sandbox"
