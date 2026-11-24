"""Verify Sail sandbox admission and spend-report settlement without network or paid calls."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator, Mapping
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

from core.billing import sail_usage
from core.billing.budgets import BudgetService
from core.billing.operation_pricing import UnpricedOperationError
from core.billing.runtime import BudgetRuntime, UsagePendingError
from core.billing.sail_usage import SailUsageReservation, quote_sail_sandbox, sail_actual_usd
from core.config import settings
from core.sandbox.metering import usage_adapter
from core.storage.models import Base, BillingCustomerModel, ExecutionOperationModel

CREATE = {
    "lifetime_ms": 3_600_000,
    "size": "s",
    "memory_limit_gib": None,
    "disk_limit_gib": None,
    "image": "python:3.12.15-slim",
    "network_disabled": True,
    "allowed_hosts": [],
}
# Ten minutes of one vCPU, 2 GiB of memory and 4 GiB of disk, with one creation.
SPEND = {
    "sailbox_id": "sb-1",
    "cost_usd_nanos": 12_000_000,
    "running_seconds": 600,
    "vcpu_seconds": 600,
    "memory_gib_seconds": 1_200,
    "state_disk_gib_seconds": 2_400,
    "creation_count": 1,
}


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
    engine = create_engine(f"sqlite:///{tmp_path / 'sail.db'}")
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


class FakeSpend:
    """Answer spend reads from a script and count the waits."""

    def __init__(self, *answers: Any) -> None:
        """Keep the answers in order; the last one repeats.

        Args:
            *answers: Spend lines, ``None`` for a report without the box, or an error to raise.
        """
        self.answers = list(answers)
        self.reads: list[tuple[str, dt.datetime]] = []
        self.now = 0.0

    def fetch(self, sailbox_id: str, since: dt.datetime) -> Mapping[str, Any]:
        """Return the next scripted spend report.

        Args:
            sailbox_id: The box to report.
            since: The start of the report window.

        Returns:
            A spend report with Sail's own prices set.

        Raises:
            BaseException: A scripted error.
        """
        self.reads.append((sailbox_id, since))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return {
            "pricing_configured": True,
            "creation_fees_waived": False,
            "sailboxes": [] if answer is None else [answer],
        }

    def sleep(self, seconds: float) -> None:
        """Move the fake clock instead of waiting.

        Args:
            seconds: The wait.
        """
        self.now += seconds

    def clock(self) -> float:
        """Return the fake monotonic time.

        Returns:
            Seconds since the fake start.
        """
        return self.now


def _reservation(database: Engine, spend: FakeSpend, request: Mapping[str, Any] = CREATE) -> SailUsageReservation:
    """Reserve one box against a fresh budget with a scripted spend report.

    Args:
        database: Engine of the funded ledger.
        spend: The scripted spend report.
        request: The create parameters.

    Returns:
        The claimed reservation.
    """
    return SailUsageReservation(
        _runtime(database),
        request,
        operation_key="box-1",
        fetch=spend.fetch,
        sleep=spend.sleep,
        clock=spend.clock,
    )


def _created(reservation: SailUsageReservation, *, memory_mib: int = 16 * 1024) -> None:
    """Send the fake create and confirm the box it made.

    Args:
        reservation: The box's reservation.
        memory_mib: The memory limit that Sail reports.
    """
    reservation.begin_create()
    reservation.confirm_created(
        SimpleNamespace(sailbox_id="sb-1", vcpu_count=1, memory_mib=memory_mib, state_disk_size_gib=32)
    )


def _state(database: Engine) -> str:
    """Return the ledger state of the only operation.

    Args:
        database: Engine of the funded ledger.

    Returns:
        The operation state.
    """
    with Session(database) as session:
        return session.scalar(select(ExecutionOperationModel)).state


def test_sail_has_a_usage_adapter() -> None:
    """Let hosted Skynet offer Sail: the metering registry finds its adapter."""
    adapter = usage_adapter("sail")
    assert adapter is sail_usage.USAGE
    assert adapter.name == "sail"


def test_quote_funds_the_creation_and_every_limit_for_the_whole_lifetime() -> None:
    """Price size s for one hour at 1 vCPU, 16 GiB memory and 32 GiB disk, plus the creation charge."""
    quote = quote_sail_sandbox(CREATE)
    # 0.01 + 1 * 0.015 + 16 * 0.008 + 32 * 0.0007 USD, in cents.
    assert quote.maximum.total == Decimal("17.54")
    assert quote.price_snapshot["envelope"] == {"vcpus": 1, "memory_gib": 16, "disk_gib": 32}
    assert quote.price_snapshot["source"] == "https://docs.sailresearch.com/pricing.md"


def test_quote_rounds_a_raised_limit_up_to_a_power_of_two_and_defaults_to_size_l() -> None:
    """Fund the limit that Sail applies, and the large default size when the request names none."""
    raised = quote_sail_sandbox({**CREATE, "memory_limit_gib": 20})
    assert raised.price_snapshot["envelope"]["memory_gib"] == 32
    unsized = quote_sail_sandbox({**CREATE, "size": None})
    assert unsized.price_snapshot["envelope"] == {"vcpus": 8, "memory_gib": 64, "disk_gib": 256}


@pytest.mark.parametrize(
    "change",
    [
        {"lifetime_ms": 0},
        {"lifetime_ms": 25 * 3_600_000},
        {"size": "ultra_experimental"},
        {"memory_limit_gib": 65},
        {"disk_limit_gib": 1.5},
    ],
)
def test_quote_refuses_an_unbounded_box(change: dict[str, Any]) -> None:
    """Refuse a lifetime, size or limit that Skynet cannot bound.

    Args:
        change: The request field that breaks the bound.
    """
    with pytest.raises(UnpricedOperationError):
        quote_sail_sandbox({**CREATE, **change})


def test_reserve_then_settle_charges_sails_reported_cost(database: Engine) -> None:
    """Settle at Sail's cost once two spend reads agree, and free the rest of the hold."""
    spend = FakeSpend(None, SPEND)
    reservation = _reservation(database, spend)
    _created(reservation)
    reservation.settle()
    runtime = reservation.runtime
    assert _state(database) == "settled"
    assert runtime.service.get_operation(reservation.operation.id, "alice").actual_cents == Decimal("1.20")
    assert reservation.operation.id not in runtime._pinned
    assert [read[0] for read in spend.reads] == ["sb-1", "sb-1", "sb-1"]
    assert spend.reads[0][1] < reservation.create_sent_at
    assert spend.now == sail_usage.SPEND_DELAY_SECONDS + 2 * sail_usage.SPEND_POLL_SECONDS


def test_settle_prices_measured_usage_when_sail_has_no_prices() -> None:
    """Price the measured counters at the admitted rates, and add the creation charge unless it is waived."""
    snapshot = quote_sail_sandbox(CREATE).price_snapshot
    line = {**SPEND, "pricing_configured": False, "creation_fees_waived": False}
    usage = (600 * Decimal("0.015") + 1200 * Decimal("0.008") + 2400 * Decimal("0.0007")) / 3600
    assert sail_actual_usd(line, snapshot) == usage + Decimal("0.01")
    assert sail_actual_usd({**line, "creation_fees_waived": True}, snapshot) == usage


def test_settle_clamps_to_the_reservation(database: Engine) -> None:
    """Charge no more than the hold when Sail reports more than the funded ceiling."""
    spend = FakeSpend({**SPEND, "cost_usd_nanos": 5_000_000_000})
    reservation = _reservation(database, spend)
    _created(reservation)
    reservation.settle()
    runtime = reservation.runtime
    assert _state(database) == "settled"
    settled = runtime.service.get_operation(reservation.operation.id, "alice")
    assert settled.actual_cents == settled.max_cents == Decimal("17.54")


def test_a_spend_api_failure_keeps_the_hold_pending(database: Engine) -> None:
    """Keep the hold pending and raise when the spend report cannot be read."""
    spend = FakeSpend(httpx.ConnectError("down"))
    reservation = _reservation(database, spend)
    _created(reservation)
    with pytest.raises(httpx.ConnectError):
        reservation.settle()
    assert _state(database) == "pending"
    assert reservation.operation.id in reservation.runtime._pinned


def test_a_spend_that_never_arrives_stays_pending(database: Engine) -> None:
    """Give up at the deadline when Sail never lists the box, and keep the hold."""
    spend = FakeSpend(None)
    reservation = _reservation(database, spend)
    _created(reservation)
    with pytest.raises(UsagePendingError, match="final spend"):
        reservation.settle()
    assert _state(database) == "pending"
    assert spend.now <= sail_usage.SPEND_DEADLINE_SECONDS


def test_a_box_over_its_funding_fails_the_confirm(database: Engine) -> None:
    """Refuse a created box whose memory is larger than the funded limit."""
    reservation = _reservation(database, FakeSpend(SPEND))
    with pytest.raises(UsagePendingError, match="inside its funding"):
        _created(reservation, memory_mib=32 * 1024)
    assert reservation.sailbox_id == "sb-1"


def test_a_failed_start_before_the_create_releases_the_hold(database: Engine) -> None:
    """Return the hold when the create call was never sent, so no box can exist."""
    reservation = _reservation(database, FakeSpend(SPEND))
    reservation.release()
    assert _state(database) == "released"
    assert reservation.runtime._pinned == {}


def test_a_cancelled_create_keeps_the_hold_pending(database: Engine) -> None:
    """Keep the hold when the create call went out but no box handle came back."""
    reservation = _reservation(database, FakeSpend(SPEND))
    reservation.begin_create()
    reservation.release()
    assert _state(database) == "pending"
    with pytest.raises(UsagePendingError, match="no box identity"):
        reservation.settle()
