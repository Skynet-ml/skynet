"""Verify Daytona box quotes, reservations and settlement without network or paid calls."""

from __future__ import annotations

import ast
import time
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from core.billing import daytona_usage
from core.billing.budgets import BudgetService
from core.billing.daytona_usage import DaytonaUsageReservation, billed_usd, quote_daytona_sandbox
from core.billing.operation_pricing import ChargePolicy, UnpricedOperationError
from core.billing.runtime import BudgetRuntime, UsagePendingError
from core.config import settings
from core.sandbox.metering import usage_adapter
from core.storage.models import Base, BillingCustomerModel

CREATE = {"lifetime_ms": 240_000, "vcpus": 2, "memory_gib": 1, "disk_gib": 3, "image": "python:3.12.15-slim"}
HOURLY = Decimal("0.0504") * 2 + Decimal("0.0162") + Decimal("0.000108") * 3
START = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _at_cost_markup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the usage markup to 1.0 so these tests check settlement arithmetic, not pricing policy.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
    """
    monkeypatch.setattr(settings, "usage_markup", 1.0)


@pytest.fixture(autouse=True)
def _no_usage_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the analytics credentials, so no test reaches the network by accident.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
    """
    for name in ("DAYTONA_ORGANIZATION_ID", "DAYTONA_API_KEY", "DAYTONA_JWT_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace the adapter's wall clock with a settable one.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        A one-item list that holds the current epoch time.
    """
    now = [START]
    monkeypatch.setattr(daytona_usage.time, "time", lambda: now[0])
    return now


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Engine]:
    """Create a private funded wallet and ledger for real admission tests.

    Args:
        tmp_path: Per-test directory for the SQLite file.

    Yields:
        The engine bound to the funded ledger.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'daytona.db'}")
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


def _box(cpu: int = 2, memory: int = 1, disk: int = 3) -> SimpleNamespace:
    """Build a created box handle with the SDK's size fields.

    Args:
        cpu: CPU count.
        memory: Memory in GiB.
        disk: Disk in GiB.

    Returns:
        The box handle.
    """
    return SimpleNamespace(id="box-1", cpu=cpu, memory=memory, disk=disk)


def _cents(usd: Decimal) -> Decimal:
    """Convert USD to wallet cents with the sandbox charge policy.

    Args:
        usd: The provider cost.

    Returns:
        The charge in cents.
    """
    return ChargePolicy("sandbox").convert(usd).total


def _usage_api(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]) -> None:
    """Route the analytics API to a fake transport and set test credentials.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        handler: Answers each request.
    """
    monkeypatch.setenv("DAYTONA_ORGANIZATION_ID", "org-1")
    monkeypatch.setenv("DAYTONA_API_KEY", "test-key")
    monkeypatch.setattr(
        daytona_usage,
        "_http_client",
        lambda: httpx.Client(base_url="https://analytics.test", transport=httpx.MockTransport(handler)),
    )


def _period(**change: Any) -> dict[str, Any]:
    """Build one closed usage period for the funded box size.

    Args:
        **change: Fields to override.

    Returns:
        The period.
    """
    return {
        "cpu": 2,
        "ramGB": 1,
        "diskGB": 3,
        "gpu": 0,
        "price": 0.001,
        "startAt": "2027-01-15T08:00:00Z",
        "endAt": "2027-01-15T08:01:00Z",
        **change,
    }


def _started(database: Engine) -> DaytonaUsageReservation:
    """Reserve a box and confirm its creation.

    Args:
        database: Engine of the funded ledger.

    Returns:
        The confirmed reservation.
    """
    reservation = DaytonaUsageReservation(_runtime(database), CREATE, operation_key="box-op")
    reservation.confirm_created(_box())
    return reservation


def _state(reservation: DaytonaUsageReservation) -> Any:
    """Read the reservation's current operation.

    Args:
        reservation: The reservation.

    Returns:
        The operation snapshot.
    """
    return reservation.runtime.service.get_operation(reservation.operation.id, "alice")


def test_quote_prices_the_whole_lifetime_at_the_published_rates() -> None:
    """Price CPU, memory and full disk for every second of the funded lifetime."""
    quote = quote_daytona_sandbox(CREATE)
    assert quote.maximum.total == _cents(HOURLY * 240 / 3600)
    assert quote.price_snapshot["provider"] == "daytona"
    assert quote.price_snapshot["source"] == "https://www.daytona.io/pricing"
    assert quote.price_snapshot["rates"] == {
        "vcpu_hour_usd": "0.0504",
        "memory_gib_hour_usd": "0.0162",
        "disk_gib_hour_usd": "0.000108",
    }


@pytest.mark.parametrize(
    "change",
    [{"lifetime_ms": None}, {"vcpus": 0}, {"memory_gib": True}, {"disk_gib": 1.5}, {"lifetime_ms": -1}],
)
def test_quote_refuses_an_unbounded_or_unsized_box(change: dict[str, Any]) -> None:
    """Refuse to price a box without a bounded lifetime and whole sizes."""
    with pytest.raises(UnpricedOperationError):
        quote_daytona_sandbox({**CREATE, **change})


def test_adapter_is_registered_for_metering() -> None:
    """Expose the adapter where the sandbox metering registry looks for it."""
    adapter = usage_adapter("daytona")
    assert adapter is daytona_usage.USAGE
    assert adapter.name == "daytona"
    assert adapter.quote(CREATE).maximum == quote_daytona_sandbox(CREATE).maximum


def test_reserve_then_settle_at_measured_time(database: Engine, clock: list[float]) -> None:
    """Settle at measured seconds times the rates when no usage API is configured."""
    reservation = _started(database)
    clock[0] = START + 59.2
    reservation.settle()
    settled = _state(reservation)
    assert settled.state == "settled"
    assert settled.actual_cents == _cents(HOURLY * 60 / 3600)
    assert settled.provider_request_id == "box-1"


def test_settle_uses_the_billed_price_from_the_usage_api(
    database: Engine, clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settle at the billed sum of the closed periods when it is above the measured time."""
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        """Return two closed periods that cost more than the measured time.

        Args:
            request: The API request.

        Returns:
            The usage periods.
        """
        seen.append(request)
        return httpx.Response(200, json=[_period(price=0.003), _period(price=0.002)])

    _usage_api(monkeypatch, answer)
    reservation = _started(database)
    clock[0] = START + 120
    reservation.settle()
    assert Decimal("0.005") > HOURLY * 120 / 3600
    assert _state(reservation).actual_cents == _cents(Decimal("0.005"))
    (request,) = seen
    assert request.url.path == "/organization/org-1/sandbox/box-1/usage"
    assert request.headers["Authorization"] == "Bearer test-key"
    assert set(request.url.params) == {"from", "to"}


def test_settle_never_goes_below_the_measured_time(
    database: Engine, clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settle at measured time when the closed periods bill less, because late charges can still come."""
    _usage_api(monkeypatch, lambda _request: httpx.Response(200, json=[_period(price=0.001), _period(price=0.0005)]))
    reservation = _started(database)
    clock[0] = START + 120
    reservation.settle()
    assert Decimal("0.0015") < HOURLY * 120 / 3600
    assert _state(reservation).actual_cents == _cents(HOURLY * 120 / 3600)


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(500, text="boom"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json=[_period(endAt=None)]),
        httpx.Response(200, json=[_period(price="0.1")]),
        httpx.Response(200, json={"error": "shape"}),
        httpx.Response(200, text="not json"),
    ],
)
def test_usage_api_failure_falls_back_to_measured_time(
    database: Engine, clock: list[float], monkeypatch: pytest.MonkeyPatch, answer: httpx.Response
) -> None:
    """Fall back to measured time when the report fails, is empty, still open or malformed."""
    _usage_api(monkeypatch, lambda _request: answer)
    reservation = _started(database)
    clock[0] = START + 60
    reservation.settle()
    assert _state(reservation).actual_cents == _cents(HOURLY * 60 / 3600)


def test_usage_api_transport_error_falls_back_to_measured_time(
    database: Engine, clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fall back to measured time when the analytics API cannot be reached."""

    def broken(request: httpx.Request) -> httpx.Response:
        """Fail the connection.

        Args:
            request: The API request.

        Raises:
            httpx.ConnectError: Always.
        """
        raise httpx.ConnectError("down", request=request)

    _usage_api(monkeypatch, broken)
    reservation = _started(database)
    clock[0] = START + 60
    reservation.settle()
    assert _state(reservation).actual_cents == _cents(HOURLY * 60 / 3600)


def test_usage_api_with_another_size_keeps_the_hold_pending(
    database: Engine, clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the hold pending when the bill shows a size other than the funded one."""
    _usage_api(monkeypatch, lambda _request: httpx.Response(200, json=[_period(cpu=4)]))
    reservation = _started(database)
    with pytest.raises(UsagePendingError):
        reservation.settle()
    assert _state(reservation).state == "pending"


@pytest.mark.parametrize("source", ["api", "measured"])
def test_settle_clamps_to_the_reservation(
    database: Engine, clock: list[float], monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    """Never settle above the reserved maximum, from the API or from measured time."""
    if source == "api":
        _usage_api(monkeypatch, lambda _request: httpx.Response(200, json=[_period(price=5.0)]))
    reservation = _started(database)
    clock[0] = START + 10_000
    reservation.settle()
    settled = _state(reservation)
    assert settled.state == "settled"
    assert settled.actual_cents == reservation.quote.maximum.total == settled.max_cents


def test_settle_is_a_no_op_after_a_settle(database: Engine, clock: list[float]) -> None:
    """Leave an already settled operation as it is."""
    reservation = _started(database)
    clock[0] = START + 30
    reservation.settle()
    first = _state(reservation).actual_cents
    clock[0] = START + 200
    reservation.settle()
    assert _state(reservation).actual_cents == first


def test_settle_without_a_box_identity_keeps_the_hold_pending(database: Engine) -> None:
    """Refuse to settle a box whose create never returned an id."""
    reservation = DaytonaUsageReservation(_runtime(database), CREATE, operation_key="box-op")
    with pytest.raises(UsagePendingError):
        reservation.settle()
    assert _state(reservation).state == "pending"


def test_bind_records_the_box_without_a_size_check(database: Engine, clock: list[float]) -> None:
    """Bind a box that no confirm saw, so its hold can settle; a second bind changes nothing."""
    reservation = DaytonaUsageReservation(_runtime(database), CREATE, operation_key="box-op")
    box = SimpleNamespace(id="box-1", cpu=None, memory=None, disk=None)
    reservation.bind(box)
    reservation.bind(box)
    assert _state(reservation).provider_request_id == "box-1"
    clock[0] = START + 30
    reservation.settle()
    assert _state(reservation).state == "settled"


def test_confirm_refuses_a_box_of_another_size(database: Engine) -> None:
    """Refuse a created box whose sizes differ from the funded ones."""
    reservation = DaytonaUsageReservation(_runtime(database), CREATE, operation_key="box-op")
    with pytest.raises(UsagePendingError):
        reservation.confirm_created(_box(memory=2))


@pytest.mark.parametrize(("refused", "state"), [(True, "released"), (False, "pending")])
def test_fail_releases_only_a_refusal(database: Engine, refused: bool, state: str) -> None:
    """Release the hold when Daytona made no box, and keep it when a box can exist."""
    reservation = DaytonaUsageReservation(_runtime(database), CREATE, operation_key="box-op")
    reservation.fail(RuntimeError("create failed"), refused=refused)
    assert _state(reservation).state == state


def test_pending_evidence_names_the_box_label(database: Engine) -> None:
    """Put the box label into the pending evidence, so a box whose id never came back can be matched."""
    label = {"skynet-box": "abc123"}
    reservation = DaytonaUsageReservation(_runtime(database), CREATE, operation_key="box-op")
    reservation.box_label = label
    reservation.fail(RuntimeError("create lost"), refused=False)
    (evidence,) = reservation.runtime.service.get_reconciliation(reservation.operation.id, "alice").evidence
    assert evidence["box_label"] == label
    assert evidence["sandbox_id"] is None


def test_extend_funds_a_longer_lifetime(database: Engine) -> None:
    """Raise the reserved maximum before the box gets a longer lifetime."""
    reservation = _started(database)
    reservation.extend(480_000, deadline=time.monotonic() + 5)
    assert _state(reservation).max_cents == quote_daytona_sandbox({**CREATE, "lifetime_ms": 480_000}).maximum.total
    with pytest.raises(ValueError, match="lengthen"):
        reservation.extend(480_000, deadline=time.monotonic() + 5)


def test_billed_usd_sums_closed_periods() -> None:
    """Sum the exact decimal prices of closed periods."""
    assert billed_usd([_period(price=0.1), _period(price=0.2)], vcpus=2, memory_gib=1, disk_gib=3) == Decimal("0.3")


def test_pending_usage_messages_never_name_the_sandbox_vendor() -> None:
    """Keep the vendor name out of every pending-usage message, because failed runs show it to users."""
    tree = ast.parse(Path(daytona_usage.__file__).read_text())
    messages = [
        "".join(part.value for part in ast.walk(node.args[0]) if isinstance(part, ast.Constant))
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "UsagePendingError"
        and node.args
    ]
    assert len(messages) >= 4
    assert [message for message in messages if "daytona" in message.lower()] == []
