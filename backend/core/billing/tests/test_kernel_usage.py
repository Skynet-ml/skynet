"""Verify Kernel browser admission and uptime metering on a local ledger, without paid calls."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

from core.billing.budgets import BudgetService
from core.billing.kernel_usage import USAGE, KernelUsageReservation, quote_kernel_browser
from core.billing.operation_pricing import UnpricedOperationError
from core.billing.runtime import BudgetRuntime, UsagePendingError
from core.config import settings
from core.sandbox.metering import usage_adapter
from core.storage.models import Base, BillingCustomerModel, ExecutionOperationModel

REQUEST = {"lifetime_ms": 300_000, "timeout_seconds": 300, "memory_gib": 8, "headless": False, "gpu": False}
RECEIPT = {
    "session_id": "ses-1",
    "deleted_at": "2026-11-24T00:05:00Z",
    "usage_status": "ready",
    "usage": {"uptime_ms": 60_000},
}


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
    engine = create_engine(f"sqlite:///{tmp_path / 'kernel.db'}")
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


def _browser(**overrides: Any) -> SimpleNamespace:
    """Build an SDK browser that matches the funded request.

    Args:
        **overrides: Fields to change.

    Returns:
        The fake browser.
    """
    fields = {"session_id": "ses-1", "headless": False, "gpu": False, "memory": "8GiB", "timeout_seconds": 300}
    return SimpleNamespace(**{**fields, **overrides})


def _state(database: Engine) -> str:
    """Return the state of the only operation in the ledger.

    Args:
        database: Engine of the ledger.

    Returns:
        The operation state.
    """
    with Session(database) as session:
        return session.scalar(select(ExecutionOperationModel)).state


def _actual_cents(runtime: BudgetRuntime, reservation: KernelUsageReservation) -> Decimal:
    """Return the settled charge of a reservation.

    Args:
        runtime: The budget runtime.
        reservation: The settled reservation.

    Returns:
        The actual charge in cents.
    """
    return runtime.service.get_operation(reservation.operation.id, "alice").actual_cents


def test_usage_adapter_finds_kernel() -> None:
    """Let hosted Skynet find the Kernel adapter by its provider name."""
    assert usage_adapter("kernel") is USAGE
    assert USAGE.name == "kernel"


def test_quote_covers_lifetime_idle_timeout_and_stop_margin() -> None:
    """Fund the lifetime, one more idle timeout and the stop margin at the published headful rate."""
    quote = quote_kernel_browser(REQUEST)
    # (300 s + 300 s + 120 s) x $0.0001333336/s = $0.0960001920 = 9.6 cents.
    assert quote.maximum.total == Decimal("9.60001920")
    assert quote.price_snapshot["headful_second_usd"] == "0.0001333336"


@pytest.mark.parametrize(
    "change",
    [
        {"lifetime_ms": 0},
        {"lifetime_ms": 259_200_001},
        {"timeout_seconds": True},
        {"memory_gib": 4},
        {"headless": True},
        {"gpu": True},
    ],
)
def test_quote_refuses_an_unbounded_or_unpriced_browser(change: dict[str, Any]) -> None:
    """Refuse a browser that the published headful rate does not bound.

    Args:
        change: The field that breaks the request.
    """
    with pytest.raises(UnpricedOperationError):
        quote_kernel_browser({**REQUEST, **change})


def test_sixteen_gib_browser_costs_twice_the_eight_gib_rate() -> None:
    """Price a 16 GiB headful browser per GiB-second, so it costs twice the 8 GiB browser."""
    quote = quote_kernel_browser({**REQUEST, "memory_gib": 16})
    assert quote.maximum.total == 2 * quote_kernel_browser(REQUEST).maximum.total
    assert quote.price_snapshot["headful_second_usd"] == "0.0002666672"


def test_reserve_then_settle_charges_the_reported_uptime(database: Engine) -> None:
    """Settle a deleted browser at Kernel's reported uptime and release the pin."""
    runtime = _runtime(database)
    reservation = USAGE.reserve(runtime, REQUEST, operation_key="box-one")
    USAGE.confirm(reservation, _browser())
    reservation.record(RECEIPT, measured_ms=90_000)
    USAGE.settle(reservation)
    assert _state(database) == "settled"
    # 60 s x $0.0001333336/s = $0.0080000160.
    assert _actual_cents(runtime, reservation) == Decimal("0.80000160")
    assert runtime._pinned == {}


def test_settle_clamps_to_the_reservation(database: Engine, caplog: pytest.LogCaptureFixture) -> None:
    """Never charge more than the reservation, and log a warning when the reported uptime is longer.

    Args:
        database: Engine of the funded ledger.
        caplog: The pytest log capture fixture.
    """
    runtime = _runtime(database)
    reservation = USAGE.reserve(runtime, REQUEST, operation_key="box-one")
    USAGE.confirm(reservation, _browser())
    reservation.record({**RECEIPT, "usage": {"uptime_ms": 10_000_000}}, measured_ms=None)
    with caplog.at_level(logging.WARNING, logger="core.billing.kernel_usage"):
        reservation.settle()
    assert _state(database) == "settled"
    assert _actual_cents(runtime, reservation) == reservation.operation.max_cents
    assert "clamped" in caplog.text


def test_unconfirmed_browser_keeps_the_hold(database: Engine) -> None:
    """Keep the hold of a browser that does not match its funded request, also with a usage report."""
    reservation = USAGE.reserve(_runtime(database), REQUEST, operation_key="box-one")
    with pytest.raises(UsagePendingError):
        USAGE.confirm(reservation, _browser(memory="16GiB"))
    reservation.record(RECEIPT, measured_ms=90_000)
    with pytest.raises(UsagePendingError, match="funded request"):
        reservation.settle()
    assert _state(database) == "pending"


def test_usage_report_not_ready_falls_back_to_measured_time(database: Engine) -> None:
    """Settle from the measured wall time when Kernel's report is still pending."""
    runtime = _runtime(database)
    reservation = USAGE.reserve(runtime, REQUEST, operation_key="box-one")
    USAGE.confirm(reservation, _browser())
    reservation.record({**RECEIPT, "usage_status": "pending"}, measured_ms=90_000)
    reservation.settle()
    assert _state(database) == "settled"
    # 90 s x $0.0001333336/s = $0.0120000240.
    assert _actual_cents(runtime, reservation) == Decimal("1.20000240")


def test_no_usage_report_and_no_confirmed_delete_keeps_the_hold(database: Engine) -> None:
    """Keep the hold pending when the read failed and the delete is not confirmed."""
    runtime = _runtime(database)
    reservation = USAGE.reserve(runtime, REQUEST, operation_key="box-one")
    USAGE.confirm(reservation, _browser())
    reservation.record({}, measured_ms=None)
    with pytest.raises(UsagePendingError):
        reservation.settle()
    assert _state(database) == "pending"


def test_a_report_for_another_session_is_not_used(database: Engine) -> None:
    """Ignore a report that names another session and use the measured time."""
    runtime = _runtime(database)
    reservation = USAGE.reserve(runtime, REQUEST, operation_key="box-one")
    USAGE.confirm(reservation, _browser())
    reservation.record({**RECEIPT, "session_id": "ses-2"}, measured_ms=30_000)
    reservation.settle()
    assert _actual_cents(runtime, reservation) == Decimal("0.40000080")


@pytest.mark.parametrize("change", [{"memory": "16GiB"}, {"headless": True}, {"gpu": True}, {"timeout_seconds": 301}])
def test_confirm_refuses_a_browser_outside_the_funded_request(database: Engine, change: dict[str, Any]) -> None:
    """Refuse a created browser that could cost more than its reservation.

    Args:
        database: Engine of the funded ledger.
        change: The field that differs from the funded request.
    """
    reservation = USAGE.reserve(_runtime(database), REQUEST, operation_key="box-one")
    with pytest.raises(UsagePendingError):
        USAGE.confirm(reservation, _browser(**change))
    assert reservation.session_id == "ses-1"


def _http_error(status: int) -> RuntimeError:
    """Return a create failure with an HTTP status.

    Args:
        status: The status code.

    Returns:
        The error.
    """
    error = RuntimeError("refused")
    error.status_code = status  # type: ignore[attr-defined]
    return error


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 429])
def test_refused_create_with_no_browser_releases_the_hold(database: Engine, status: int) -> None:
    """Release the hold when Kernel refused the create call and the sweep found no browser.

    Args:
        database: Engine of the funded ledger.
        status: A refusal status.
    """
    runtime = _runtime(database)
    reservation = USAGE.reserve(runtime, REQUEST, operation_key="box-one")
    reservation.fail(_http_error(status), no_browser=True)
    assert _state(database) == "released"
    assert runtime._pinned == {}


@pytest.mark.parametrize(("status", "no_browser"), [(429, False), (408, True), (409, True), (418, True)])
def test_uncertain_create_failure_keeps_the_hold(database: Engine, status: int, no_browser: bool) -> None:
    """Keep the hold when the sweep did not prove no browser, or the status can follow a real create.

    Args:
        database: Engine of the funded ledger.
        status: The create status.
        no_browser: Whether the sweep found no browser.
    """
    reservation = USAGE.reserve(_runtime(database), REQUEST, operation_key="box-one")
    reservation.fail(_http_error(status), no_browser=no_browser)
    assert _state(database) == "pending"


def test_release_before_the_create_call_frees_the_hold(database: Engine) -> None:
    """Release the hold when the create call never went out."""
    runtime = _runtime(database)
    reservation = USAGE.reserve(runtime, REQUEST, operation_key="box-one")
    reservation.release("cancelled before the create call")
    assert _state(database) == "released"
    assert runtime._pinned == {}


def test_lost_create_response_keeps_the_hold_and_refuses_a_replay(database: Engine) -> None:
    """Keep the hold after a create failure with no status, and refuse a second dispatch."""
    runtime = _runtime(database)
    reservation = USAGE.reserve(runtime, REQUEST, operation_key="box-one")
    reservation.fail(TimeoutError("create response lost"), no_browser=True)
    assert _state(database) == "pending"
    with pytest.raises(UsagePendingError, match="already dispatched"):
        USAGE.reserve(runtime, REQUEST, operation_key="box-one")


@pytest.mark.parametrize("status", [502, 503])
def test_server_error_on_create_keeps_the_hold(database: Engine, status: int) -> None:
    """Keep the hold when a 5xx answer may still have made a browser.

    Args:
        database: Engine of the funded ledger.
        status: The server error status.
    """
    reservation = USAGE.reserve(_runtime(database), REQUEST, operation_key="box-one")
    reservation.fail(_http_error(status), no_browser=True)
    assert _state(database) == "pending"


def test_ledger_error_on_reject_marks_the_hold_pending(database: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mark the hold pending and raise the error when the ledger fails to reject it.

    Args:
        database: Engine of the funded ledger.
        monkeypatch: Replaces the ledger reject call.
    """
    runtime = _runtime(database)
    reservation = USAGE.reserve(runtime, REQUEST, operation_key="box-one")

    def broken_reject(*args: Any, **kwargs: Any) -> None:
        """Fail as a database error would.

        Args:
            *args: Ignored.
            **kwargs: Ignored.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("database down")

    monkeypatch.setattr(runtime.service, "reject", broken_reject)
    with pytest.raises(RuntimeError, match="database down"):
        reservation.fail(_http_error(429), no_browser=True)
    assert _state(database) == "pending"


def test_extend_funds_the_longer_lifetime(database: Engine) -> None:
    """Grow the coverage to the quote of the longer lifetime."""
    runtime = _runtime(database)
    reservation = USAGE.reserve(runtime, REQUEST, operation_key="box-one")
    USAGE.confirm(reservation, _browser())
    USAGE.extend(reservation, 600_000, deadline=0.0)
    longer = quote_kernel_browser({**REQUEST, "lifetime_ms": 600_000})
    assert runtime.service.get_operation(reservation.operation.id, "alice").max_cents == longer.maximum.total
    assert reservation.quote.request_fingerprint == longer.request_fingerprint
    with pytest.raises(ValueError):
        USAGE.extend(reservation, 600_000, deadline=0.0)
