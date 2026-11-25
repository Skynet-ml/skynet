"""Verify Sail sandbox admission and spend-report settlement without network or paid calls."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import threading
import time
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
from core.storage.models import Base, BillingCustomerModel, ExecutionOperationModel, ExecutionUsageEvidenceModel

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

# Sail's account rates in the spend report, in billionths of a dollar.
RATES = {
    "vcpu_second_usd_nanos": 10_000,
    "memory_gib_second_usd_nanos": 5_000,
    "state_disk_gib_second_usd_nanos": 1_000,
    "s_creation_usd_nanos": 20_000_000,
    "l_creation_usd_nanos": 50_000_000,
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

    def __init__(self, *answers: Any, rates: Mapping[str, int] | None = None, configured: bool = True) -> None:
        """Keep the answers in order; the last one repeats.

        Args:
            *answers: Spend lines, ``None`` for a report without the box, or an error to raise.
            rates: The ``rates`` object of the report, or ``None`` for none.
            configured: Whether the account has its own Sail prices.
        """
        self.answers = list(answers)
        self.rates = rates
        self.configured = configured
        self.waits: list[float] = []
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
            "pricing_configured": self.configured,
            "creation_fees_waived": False,
            "rates": self.rates,
            "sailboxes": [] if answer is None else [answer],
        }

    async def sleep(self, seconds: float) -> None:
        """Move the fake clock instead of waiting.

        Args:
            seconds: The wait.
        """
        self.waits.append(seconds)
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
    reservation.begin_create("skynet-box-1")
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
        return session.scalars(select(ExecutionOperationModel)).one().state


def test_sail_has_a_usage_adapter() -> None:
    """Let hosted Skynet offer Sail: the metering registry finds its adapter."""
    adapter = usage_adapter("sail")
    assert adapter is not None
    assert adapter is sail_usage.USAGE
    assert adapter.name == "sail"


def test_quote_funds_the_creation_and_every_limit_for_the_whole_lifetime() -> None:
    """Price size s for one hour at 1 vCPU, 16 GiB memory and 32 GiB disk, plus the creation charge."""
    quote = quote_sail_sandbox(CREATE)
    # 0.01 + 1 * 0.015 + 16 * 0.008 + 32 * 0.0007 USD, in cents.
    assert quote.maximum.total == Decimal("17.54")
    assert quote.price_snapshot["envelope"] == {"vcpus": 1, "memory_gib": 16, "disk_gib": 32}
    assert quote.price_snapshot["source"] == "https://docs.sailresearch.com/pricing.md"


def test_quote_funds_whole_minutes_of_the_lifetime() -> None:
    """Quote a 90 s lifetime the same as 120 s, because Sail bills whole minutes."""
    ninety = quote_sail_sandbox({**CREATE, "lifetime_ms": 90_000})
    assert ninety.maximum == quote_sail_sandbox({**CREATE, "lifetime_ms": 120_000}).maximum
    assert ninety.maximum.total > quote_sail_sandbox({**CREATE, "lifetime_ms": 60_000}).maximum.total


def test_a_box_at_its_limits_for_its_whole_lifetime_settles_in_full(database: Engine) -> None:
    """Settle a 90 s box at its limits at the full billed minutes, with no clamp.

    Args:
        database: Engine of the funded ledger.
    """
    request = {**CREATE, "lifetime_ms": 90_000}
    line = {
        **SPEND,
        "cost_usd_nanos": 0,
        "running_seconds": 90,
        "vcpu_seconds": 90,
        "memory_gib_seconds": 16 * 90,
        "state_disk_gib_seconds": 32 * 90,
    }
    reservation = _reservation(database, FakeSpend(line, configured=False), request)
    _created(reservation)
    reservation.settle()
    settled = reservation.runtime.service.get_operation(reservation.operation.id, "alice")
    assert settled.actual_cents == settled.max_cents
    with Session(database) as session:
        evidence = session.scalars(select(ExecutionUsageEvidenceModel)).one().evidence
    assert "clamped" not in evidence


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
        {"memory_limit_gib": 20.5},
    ],
)
def test_quote_refuses_an_unbounded_box(change: dict[str, Any]) -> None:
    """Refuse a lifetime, size or limit that Skynet cannot bound.

    Args:
        change: The request field that breaks the bound.
    """
    with pytest.raises(UnpricedOperationError):
        quote_sail_sandbox({**CREATE, **change})


def test_quote_takes_a_whole_float_limit() -> None:
    """Fund a whole float limit, such as 20.0 GiB, like the same whole number."""
    raised = quote_sail_sandbox({**CREATE, "memory_limit_gib": 20.0, "disk_limit_gib": 64.0})
    assert raised.price_snapshot["envelope"]["memory_gib"] == 32
    assert raised.price_snapshot["envelope"]["disk_gib"] == 64


class FakeConfig:
    """Stand in for ``sail.Config`` with the attribute names of SDK 0.17.2."""

    error: Exception | None = None

    def __init__(self) -> None:
        """Set the endpoint and the key."""
        self.sailbox_api_url = "https://sailbox.example/"
        self.api_key = "test-key"

    @classmethod
    def from_env(cls) -> FakeConfig:
        """Return a config, or fail as the test sets.

        Returns:
            The fake config.

        Raises:
            Exception: The error that the test sets.
        """
        if cls.error is not None:
            raise cls.error
        return cls()


def test_fetch_sail_spend_reads_the_spend_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Send the box id, the window start and the key to Sail's spend endpoint.

    Args:
        monkeypatch: Replaces the SDK config and the HTTP transport.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Record the request and answer a report.

        Args:
            request: The spend request.

        Returns:
            A JSON spend report.
        """
        seen.append(request)
        return httpx.Response(200, json={"sailboxes": [SPEND]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(sail_usage, "Config", FakeConfig)
    monkeypatch.setattr(sail_usage.httpx, "get", client.get)
    since = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)
    assert sail_usage.fetch_sail_spend("sb-1", since) == {"sailboxes": [SPEND]}
    (request,) = seen
    assert request.url.copy_with(query=None) == "https://sailbox.example/v1/sailboxes/spend"
    assert dict(request.url.params) == {"sailbox_id": "sb-1", "from": "2026-01-02T03:04:05Z"}
    assert request.headers["Authorization"] == "Bearer test-key"


def test_fetch_sail_spend_without_credentials_is_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the usage pending when the SDK finds no API key.

    Args:
        monkeypatch: Replaces the SDK config.
    """
    monkeypatch.setattr(FakeConfig, "error", ValueError("Set SAIL_API_KEY"))
    monkeypatch.setattr(sail_usage, "Config", FakeConfig)
    with pytest.raises(UsagePendingError, match="credentials"):
        sail_usage.fetch_sail_spend("sb-1", dt.datetime.now(dt.UTC))


def test_an_unexpected_read_error_keeps_the_hold_pending(database: Engine) -> None:
    """Stop at once and keep the hold, with its evidence, when the read has a code error.

    Args:
        database: Engine of the funded ledger.
    """
    spend = FakeSpend(AttributeError("no sailbox_api_url"))
    reservation = _reservation(database, spend)
    _created(reservation)
    with pytest.raises(UsagePendingError, match="could not be read") as raised:
        reservation.settle()
    assert isinstance(raised.value.__cause__, AttributeError)
    assert len(spend.reads) == 1
    assert _state(database) == "pending"


def test_a_body_that_does_not_decode_is_read_again(database: Engine) -> None:
    """Read again when httpx cannot decode the spend body, then settle.

    Args:
        database: Engine of the funded ledger.
    """
    spend = FakeSpend(httpx.DecodingError("bad gzip"), SPEND)
    reservation = _reservation(database, spend)
    _created(reservation)
    reservation.settle()
    assert _state(database) == "settled"
    assert len(spend.reads) > 1


def _status_error(status: int, retry_after: str | None = None) -> httpx.HTTPStatusError:
    """Build the error that ``raise_for_status`` raises for a refused spend read.

    Args:
        status: The HTTP status.
        retry_after: The ``Retry-After`` header, or ``None`` for none.

    Returns:
        The status error.
    """
    request = httpx.Request("GET", "https://api.sailresearch.com/v1/sailboxes/spend")
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    response = httpx.Response(status, headers=headers, request=request)
    return httpx.HTTPStatusError("refused", request=request, response=response)


def test_reserve_then_settle_charges_sails_reported_cost(database: Engine) -> None:
    """Settle at Sail's rates plus one creation charge once the line stays the same for a minute.

    Args:
        database: Engine of the funded ledger.
    """
    spend = FakeSpend(None, SPEND, rates=RATES)
    reservation = _reservation(database, spend)
    _created(reservation)
    reservation.settle()
    runtime = reservation.runtime
    assert _state(database) == "settled"
    # 1.44 cents of usage at Sail's rates and its 2-cent creation rate.
    assert runtime.service.get_operation(reservation.operation.id, "alice").actual_cents == Decimal("3.44")
    assert reservation.operation.id not in runtime._pinned
    assert {read[0] for read in spend.reads} == {"sb-1"}
    assert reservation.create_sent_at is not None
    assert spend.reads[0][1] < reservation.create_sent_at
    # The line first shows at the second read; equal reads 15 and 30 s
    # apart are not enough, so the settle waits until it held for 60 s.
    assert len(spend.reads) == 6
    assert spend.now == sail_usage.SPEND_DELAY_SECONDS + 5 * sail_usage.SPEND_POLL_SECONDS


def test_a_changed_line_restarts_the_agreement_wait(database: Engine) -> None:
    """Wait a new minute when a late usage batch changes the box's line.

    Args:
        database: Engine of the funded ledger.
    """
    late = {**SPEND, "vcpu_seconds": 700, "cost_usd_nanos": 13_000_000}
    spend = FakeSpend(SPEND, SPEND, late, rates=RATES)
    reservation = _reservation(database, spend)
    _created(reservation)
    reservation.settle()
    settled = reservation.runtime.service.get_operation(reservation.operation.id, "alice")
    assert settled.actual_cents == Decimal("3.54")
    assert spend.now == sail_usage.SPEND_DELAY_SECONDS + 6 * sail_usage.SPEND_POLL_SECONDS


def test_settle_prices_measured_usage_when_sail_has_no_prices() -> None:
    """Price the measured counters at the admitted rates, and add the creation charge unless it is waived."""
    snapshot = quote_sail_sandbox(CREATE).price_snapshot
    line = {**SPEND, "pricing_configured": False, "creation_fees_waived": False}
    usage = (600 * Decimal("0.015") + 1200 * Decimal("0.008") + 2400 * Decimal("0.0007")) / 3600
    assert sail_actual_usd(line, snapshot) == usage + Decimal("0.01")
    assert sail_actual_usd({**line, "creation_fees_waived": True}, snapshot) == usage


def test_settle_rounds_usage_up_to_whole_minutes_when_sail_has_no_prices() -> None:
    """Price 61 seconds of each counter as 2 minutes, because Sail bills in one-minute increments."""
    snapshot = quote_sail_sandbox(CREATE).price_snapshot
    line = {
        **SPEND,
        "running_seconds": 61,
        "vcpu_seconds": 61,
        "memory_gib_seconds": 61,
        "state_disk_gib_seconds": 61,
        "pricing_configured": False,
        "creation_fees_waived": True,
    }
    two_minutes = {
        **line,
        "running_seconds": 120,
        "vcpu_seconds": 120,
        "memory_gib_seconds": 120,
        "state_disk_gib_seconds": 120,
    }
    assert sail_actual_usd(line, snapshot) == sail_actual_usd(two_minutes, snapshot)
    expected = 120 * (Decimal("0.015") + Decimal("0.008") + Decimal("0.0007")) / 3600
    assert abs(sail_actual_usd(line, snapshot) - expected) < Decimal("1e-20")
    assert sail_actual_usd({**line, "vcpu_seconds": 60}, snapshot) < sail_actual_usd(line, snapshot)


def test_settle_bills_whole_running_minutes_of_a_two_vcpu_box() -> None:
    """Price 2 vCPU for 61 s the same as 2 vCPU for 120 s, because Sail bills whole running minutes."""
    snapshot = quote_sail_sandbox(CREATE).price_snapshot
    line = {
        **SPEND,
        "running_seconds": 61,
        "vcpu_seconds": 122,
        "memory_gib_seconds": 244,
        "state_disk_gib_seconds": 61,
        "pricing_configured": False,
        "creation_fees_waived": True,
    }
    two_minutes = {
        **line,
        "running_seconds": 120,
        "vcpu_seconds": 240,
        "memory_gib_seconds": 480,
        "state_disk_gib_seconds": 120,
    }
    assert sail_actual_usd(line, snapshot) == sail_actual_usd(two_minutes, snapshot)
    # Usage with no running time is a broken line, so the hold stays pending.
    idle = {**line, "running_seconds": 0}
    with pytest.raises(UsagePendingError, match="no running time"):
        sail_actual_usd(idle, snapshot)
    del idle["running_seconds"]
    with pytest.raises(UsagePendingError, match="no running time"):
        sail_actual_usd(idle, snapshot)
    empty = {**idle, "vcpu_seconds": 0, "memory_gib_seconds": 0, "state_disk_gib_seconds": 0}
    assert sail_actual_usd(empty, snapshot) == 0


def test_settle_never_bills_disk_below_its_own_whole_minutes() -> None:
    """Round disk-seconds up alone when they pass the scaled running time."""
    snapshot = quote_sail_sandbox(CREATE).price_snapshot
    # 1 GiB of disk for 119 s of running time, but Sail counts 121
    # disk-seconds. Scaled to 120 s they are about 122, and alone they round to 180.
    line = {
        **SPEND,
        "running_seconds": 119,
        "vcpu_seconds": 0,
        "memory_gib_seconds": 0,
        "state_disk_gib_seconds": 121,
        "pricing_configured": False,
        "creation_fees_waived": True,
    }
    rounded = {**line, "running_seconds": 120, "state_disk_gib_seconds": 180}
    assert sail_actual_usd(line, snapshot) == sail_actual_usd(rounded, snapshot)


def test_settle_recomputes_sails_cost_with_one_creation_charge() -> None:
    """Price the counters at Sail's rates plus one creation charge, with Sail's cost as the floor."""
    snapshot = quote_sail_sandbox(CREATE).price_snapshot
    line = {**SPEND, "pricing_configured": True, "creation_fees_waived": False, "rates": RATES}
    usage = 600 * Decimal("0.00001") + 1200 * Decimal("0.000005") + 2400 * Decimal("0.000001")
    assert sail_actual_usd(line, snapshot) == usage + Decimal("0.02")
    # A cost that already holds the creation charge does not get it twice.
    assert sail_actual_usd({**line, "cost_usd_nanos": 34_400_000}, snapshot) == Decimal("0.0344")
    # A rate that this code does not know shows only in Sail's own cost.
    assert sail_actual_usd({**line, "cost_usd_nanos": 50_000_000}, snapshot) == Decimal("0.05")
    assert sail_actual_usd({**line, "creation_fees_waived": True}, snapshot) == usage
    # With account prices, a zero creation rate is the account's price.
    free = {**line, "rates": {**RATES, "s_creation_usd_nanos": 0}}
    assert sail_actual_usd(free, snapshot) == max(usage, Decimal("0.012"))
    published = (600 * Decimal("0.015") + 1200 * Decimal("0.008") + 2400 * Decimal("0.0007")) / 3600
    assert sail_actual_usd({**line, "rates": None}, snapshot) == published + Decimal("0.01")


def test_settle_ignores_zero_creation_rates_when_sail_has_no_prices() -> None:
    """Charge the published creation fee when the account has no prices and the reported rates are zero."""
    snapshot = quote_sail_sandbox(CREATE).price_snapshot
    line = {
        **SPEND,
        "pricing_configured": False,
        "creation_fees_waived": False,
        "rates": {"s_creation_usd_nanos": 0, "m_creation_usd_nanos": 0, "l_creation_usd_nanos": 0},
    }
    usage = (600 * Decimal("0.015") + 1200 * Decimal("0.008") + 2400 * Decimal("0.0007")) / 3600
    assert sail_actual_usd(line, snapshot) == usage + Decimal("0.01")


def test_settle_clamps_to_the_reservation(database: Engine, caplog: pytest.LogCaptureFixture) -> None:
    """Charge no more than the hold when Sail reports more, and record the unclamped charge.

    Args:
        database: Engine of the funded ledger.
        caplog: Captures the clamp warning.
    """
    spend = FakeSpend({**SPEND, "cost_usd_nanos": 5_000_000_000})
    reservation = _reservation(database, spend)
    _created(reservation)
    with caplog.at_level(logging.WARNING, logger=sail_usage.__name__):
        reservation.settle()
    runtime = reservation.runtime
    assert _state(database) == "settled"
    settled = runtime.service.get_operation(reservation.operation.id, "alice")
    assert settled.actual_cents == settled.max_cents == Decimal("17.54")
    with Session(database) as session:
        evidence = session.scalars(select(ExecutionUsageEvidenceModel)).one().evidence
    assert evidence["clamped"] is True
    # Sail's own cost is above the recomputed cost, so it is the charge.
    assert Decimal(evidence["unclamped_cents"]) == Decimal(500)
    assert "clamped" in caplog.text


def test_a_spend_api_failure_keeps_polling_until_the_deadline(database: Engine) -> None:
    """Read again after a transport error, and keep the hold pending when the reads never work.

    Args:
        database: Engine of the funded ledger.
    """
    spend = FakeSpend(httpx.ConnectError("down"))
    reservation = _reservation(database, spend)
    _created(reservation)
    with pytest.raises(UsagePendingError, match="final spend"):
        reservation.settle()
    assert len(spend.reads) > 1
    assert _state(database) == "pending"
    assert reservation.operation.id in reservation.runtime._pinned


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_a_rate_limited_or_unavailable_read_is_tried_again(database: Engine, status: int) -> None:
    """Wait at least one poll after a short Retry-After, poll again after a 5xx answer, then settle.

    Args:
        database: Engine of the funded ledger.
        status: The server error that Sail answers once.
    """
    spend = FakeSpend(_status_error(429, "5"), _status_error(status), SPEND)
    reservation = _reservation(database, spend)
    _created(reservation)
    reservation.settle()
    assert _state(database) == "settled"
    poll = sail_usage.SPEND_POLL_SECONDS
    assert spend.waits[:3] == [sail_usage.SPEND_DELAY_SECONDS, poll, poll]


@pytest.mark.parametrize(
    ("header", "wait"),
    [
        ("0", sail_usage.SPEND_POLL_SECONDS),
        ("Wed, 21 Oct 2015 07:28:00 GMT", sail_usage.SPEND_POLL_SECONDS),
        ("40", 40.0),
    ],
)
def test_retry_after_never_waits_less_than_one_poll(database: Engine, header: str, wait: float) -> None:
    """Honor a long Retry-After, but never read again sooner than one poll.

    Args:
        database: Engine of the funded ledger.
        header: The Retry-After value of the 429 answer.
        wait: The wait expected before the next read.
    """
    spend = FakeSpend(_status_error(429, header), SPEND)
    reservation = _reservation(database, spend)
    _created(reservation)
    with contextlib.suppress(UsagePendingError):
        reservation.settle()
    assert spend.waits[1] == wait


def test_a_body_that_is_not_json_is_read_again(database: Engine) -> None:
    """Read again when the spend body is not JSON, then settle.

    Args:
        database: Engine of the funded ledger.
    """
    spend = FakeSpend(ValueError("not JSON"), SPEND)
    reservation = _reservation(database, spend)
    _created(reservation)
    reservation.settle()
    assert _state(database) == "settled"
    assert len(spend.reads) > 1


def test_a_report_that_is_not_an_object_is_read_again(database: Engine) -> None:
    """Read again when the spend JSON is not an object, then settle.

    Args:
        database: Engine of the funded ledger.
    """
    spend = FakeSpend(SPEND)
    answers: list[Any] = [["not", "an", "object"]]

    def fetch(sailbox_id: str, since: dt.datetime) -> Any:
        """Answer a JSON list once, then the scripted report.

        Args:
            sailbox_id: The box to report.
            since: The start of the report window.

        Returns:
            The decoded JSON body.
        """
        return answers.pop() if answers else spend.fetch(sailbox_id, since)

    reservation = _reservation(database, spend)
    reservation._fetch = fetch
    _created(reservation)
    reservation.settle()
    assert _state(database) == "settled"
    assert answers == []


def test_a_refused_spend_read_keeps_the_hold_pending(database: Engine) -> None:
    """Stop at once and keep the hold when Sail refuses the read for good, as with a wrong key.

    Args:
        database: Engine of the funded ledger.
    """
    spend = FakeSpend(_status_error(403))
    reservation = _reservation(database, spend)
    _created(reservation)
    with pytest.raises(httpx.HTTPStatusError):
        reservation.settle()
    assert len(spend.reads) == 1
    assert _state(database) == "pending"


def test_a_spend_that_never_arrives_stays_pending(database: Engine) -> None:
    """Give up at the deadline when Sail never lists the box, and keep the hold.

    Args:
        database: Engine of the funded ledger.
    """
    spend = FakeSpend(None)
    reservation = _reservation(database, spend)
    _created(reservation)
    with pytest.raises(UsagePendingError, match="final spend"):
        reservation.settle()
    assert _state(database) == "pending"
    assert spend.now <= sail_usage.SPEND_DEADLINE_SECONDS


def test_a_spend_that_arrives_after_minutes_settles(database: Engine) -> None:
    """Settle a box whose line keeps changing for four minutes, as Sail's batches allow.

    Args:
        database: Engine of the funded ledger.
    """
    # Reads come at 60 s and then every 15 s, so 13 changing lines last until 240 s.
    lines = [{**SPEND, "cost_usd_nanos": 12_000_000 + step} for step in range(12)]
    spend = FakeSpend(*lines, SPEND)
    reservation = _reservation(database, spend)
    _created(reservation)
    reservation.settle()
    assert _state(database) == "settled"
    assert 240.0 <= spend.now <= sail_usage.SPEND_DEADLINE_SECONDS


def test_a_read_slower_than_the_time_left_ends_inside_the_deadline(
    database: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop waiting for a spend read at the end of the deadline, and keep the hold.

    Args:
        database: Engine of the funded ledger.
        monkeypatch: Shortens the read timeout and the deadline.
    """
    monkeypatch.setattr(sail_usage, "_SPEND_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(sail_usage, "SPEND_DEADLINE_SECONDS", sail_usage.SPEND_DELAY_SECONDS + 0.05)
    gate = threading.Event()
    spend = FakeSpend(SPEND)
    reservation = _reservation(database, spend)
    _created(reservation)

    def slow(sailbox_id: str, since: dt.datetime) -> Mapping[str, Any]:
        """Trickle the answer until the test opens the gate.

        Args:
            sailbox_id: The box to report.
            since: The start of the report window.

        Returns:
            The scripted spend report.
        """
        gate.wait(5)
        return spend.fetch(sailbox_id, since)

    reservation._fetch = slow

    async def settle() -> float:
        """Run the spend wait and time it.

        Returns:
            The seconds that the wait took.
        """
        started = time.monotonic()
        try:
            with pytest.raises(UsagePendingError, match="final spend"):
                await reservation.final_spend()
            return time.monotonic() - started
        finally:
            gate.set()

    assert asyncio.run(settle()) < 1.0


def test_a_line_that_never_settles_stays_pending_with_its_last_read(database: Engine) -> None:
    """Keep the hold at the deadline when the line keeps changing, with the last line as evidence.

    Args:
        database: Engine of the funded ledger.
    """
    # More lines than reads before the deadline, so the line never settles.
    lines = [{**SPEND, "cost_usd_nanos": 12_000_000 + step} for step in range(60)]
    spend = FakeSpend(*lines)
    reservation = _reservation(database, spend)
    _created(reservation)
    with pytest.raises(UsagePendingError, match="final spend"):
        reservation.settle()
    assert _state(database) == "pending"
    assert spend.now <= sail_usage.SPEND_DEADLINE_SECONDS
    with Session(database) as session:
        evidence = session.scalars(select(ExecutionUsageEvidenceModel)).one().evidence
    assert reservation.spend is not None
    assert evidence["spend"]["cost_usd_nanos"] == reservation.spend["cost_usd_nanos"] > 12_000_000


def test_a_box_over_its_funding_fails_the_confirm(database: Engine) -> None:
    """Refuse a created box whose memory is larger than the funded limit.

    Args:
        database: Engine of the funded ledger.
    """
    reservation = _reservation(database, FakeSpend(SPEND))
    with pytest.raises(UsagePendingError, match="inside its funding"):
        _created(reservation, memory_mib=32 * 1024)
    assert reservation.sailbox_id == "sb-1"


def test_a_failed_start_before_the_create_releases_the_hold(database: Engine) -> None:
    """Return the hold when the create call was never sent, so no box can exist.

    Args:
        database: Engine of the funded ledger.
    """
    reservation = _reservation(database, FakeSpend(SPEND))
    reservation.release()
    assert _state(database) == "released"
    assert reservation.runtime._pinned == {}


def test_a_cancelled_create_keeps_the_hold_pending(database: Engine) -> None:
    """Keep the hold when the create call went out but no box handle came back.

    Args:
        database: Engine of the funded ledger.
    """
    reservation = _reservation(database, FakeSpend(SPEND))
    reservation.begin_create("skynet-box-1")
    reservation.release()
    assert _state(database) == "pending"
    with pytest.raises(UsagePendingError, match="no box identity"):
        reservation.settle()


def test_a_pending_hold_without_a_box_id_keeps_the_box_name(database: Engine) -> None:
    """Keep the box name in the pending evidence, as the only link to a box with no id.

    Args:
        database: Engine of the funded ledger.
    """
    reservation = _reservation(database, FakeSpend(SPEND))
    reservation.begin_create("skynet-box-1")
    reservation.release()
    assert _state(database) == "pending"
    with Session(database) as session:
        evidence = session.scalars(select(ExecutionUsageEvidenceModel)).one().evidence
    assert evidence["sailbox_id"] is None
    assert evidence["sailbox_name"] == "skynet-box-1"


def test_a_refused_create_releases_the_hold(database: Engine) -> None:
    """Return the hold when Sail clearly refused the create, so no box can exist.

    Args:
        database: Engine of the funded ledger.
    """
    reservation = _reservation(database, FakeSpend(SPEND))
    reservation.begin_create("skynet-box-1")
    reservation.release(refused=True)
    assert _state(database) == "released"
    assert reservation.runtime._pinned == {}
    with Session(database) as session:
        evidence = session.scalars(select(ExecutionUsageEvidenceModel)).one().evidence
    assert evidence["sailbox_name"] == "skynet-box-1"


def test_a_pending_hold_still_settles(database: Engine) -> None:
    """Settle a hold that a cancelled teardown already marked pending.

    Args:
        database: Engine of the funded ledger.
    """
    reservation = _reservation(database, FakeSpend(SPEND))
    _created(reservation)
    reservation.pending()
    reservation.pending()
    assert _state(database) == "pending"
    reservation.settle_spend(SPEND)
    assert _state(database) == "settled"


def test_a_failed_settle_of_a_pending_hold_raises_its_own_error(database: Engine) -> None:
    """Raise the settle error, not an evidence conflict, when a pending hold fails to settle.

    Args:
        database: Engine of the funded ledger.
    """
    reservation = _reservation(database, FakeSpend(SPEND))
    reservation.begin_create("skynet-box-1")
    reservation.release()
    assert _state(database) == "pending"
    reservation.sailbox_id = "sb-1"
    with pytest.raises(UsagePendingError, match="spend"):
        reservation.settle_spend({})
    assert _state(database) == "pending"


@pytest.mark.parametrize("step", ["settle_spend", "settle"])
def test_a_failed_pending_mark_keeps_the_first_error(
    database: Engine, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    """Raise the settle error, not the error of the pending mark that follows it.

    Args:
        database: Engine of the funded ledger.
        monkeypatch: Makes the pending mark and the spend read fail.
        step: The settle call that fails.
    """
    reservation = _reservation(database, FakeSpend(SPEND))
    _created(reservation)

    def fail_mark(*args: Any, **kwargs: Any) -> None:
        """Fail like a ledger that cannot write.

        Args:
            *args: Ignored.
            **kwargs: Ignored.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("ledger down")

    async def no_spend(budget: float | None = None) -> dict[str, Any]:
        """Fail like a spend that never arrives.

        Args:
            budget: Ignored.

        Raises:
            UsagePendingError: Always.
        """
        raise UsagePendingError("no spend")

    monkeypatch.setattr(reservation.runtime.service, "mark_pending", fail_mark)
    monkeypatch.setattr(reservation, "final_spend", no_spend)
    with pytest.raises(UsagePendingError):
        if step == "settle":
            reservation.settle()
        else:
            reservation.settle_spend({})


def test_a_failed_reject_still_unpins_the_hold(database: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unpin the hold of a box that was never created, even when the reject fails.

    Args:
        database: Engine of the funded ledger.
        monkeypatch: Makes the reject fail.
    """
    reservation = _reservation(database, FakeSpend(SPEND))

    def fail_reject(*args: Any, **kwargs: Any) -> None:
        """Fail like a ledger that cannot write.

        Args:
            *args: Ignored.
            **kwargs: Ignored.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("ledger down")

    monkeypatch.setattr(reservation.runtime.service, "reject", fail_reject)
    with pytest.raises(RuntimeError):
        reservation.release()
    assert reservation.runtime._pinned == {}
