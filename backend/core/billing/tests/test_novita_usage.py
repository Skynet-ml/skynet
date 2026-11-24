"""Verify Novita admission and measured-runtime settlement against a local ledger, with no network."""

from __future__ import annotations

import time
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from core.billing.budgets import BudgetService
from core.billing.novita_usage import USAGE, NovitaUsageReservation, quote_novita_sandbox
from core.billing.operation_pricing import UnpricedOperationError
from core.billing.runtime import BudgetRuntime, UsagePendingError
from core.config import settings
from core.sandbox.capabilities import SandboxSpecError
from core.sandbox.metering import usage_adapter
from core.storage.models import Base, BillingCustomerModel

# One hour at 2 vCPU and 2 GiB: 3600 * (2 * 0.0000098 + 2 * 0.0000032) USD = 9.36 cents.
REQUEST = {"lifetime_ms": 3_600_000, "vcpus": 2, "memory_mb": 2048, "template": "base"}


@pytest.fixture(autouse=True)
def _at_cost_markup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the usage markup to 1.0 so these tests check settlement arithmetic, not pricing policy.

    Args:
        monkeypatch: Restores the setting after the test.
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
    engine = create_engine(f"sqlite:///{tmp_path / 'novita.db'}")
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


def _info(cpu_count: int = 2, memory_mb: int = 2048) -> SimpleNamespace:
    """Return a stand-in for the SDK's ``SandboxInfo``.

    Args:
        cpu_count: The template's vCPU count.
        memory_mb: The template's memory.

    Returns:
        An object with the fields that ``confirm`` reads.
    """
    return SimpleNamespace(sandbox_id="sbx-1", cpu_count=cpu_count, memory_mb=memory_mb)


def _operation(reservation: NovitaUsageReservation) -> Any:
    """Read the reservation's current ledger row.

    Args:
        reservation: The reservation.

    Returns:
        The operation snapshot.
    """
    return reservation.runtime.service.get_operation(reservation.operation.id, "alice")


def _evidence(reservation: NovitaUsageReservation) -> list[dict[str, Any]]:
    """Read the evidence that the ledger keeps for the reservation.

    Args:
        reservation: The reservation.

    Returns:
        The evidence records, oldest first.
    """
    return reservation.runtime.service.get_reconciliation(reservation.operation.id, "alice").evidence


def test_novita_has_a_usage_adapter() -> None:
    """Find the Novita adapter by its provider key."""
    assert usage_adapter("novita") is USAGE


def test_quote_prices_the_whole_lifetime_at_the_published_rates() -> None:
    """Price CPU and memory per second for the funded lifetime, with the source recorded."""
    quote = quote_novita_sandbox(REQUEST)
    assert quote.maximum.total == Decimal("9.36")
    assert quote.price_snapshot["cpu_second_usd"] == "0.0000098"
    assert quote.price_snapshot["memory_gib_second_usd"] == "0.0000032"
    assert quote.price_snapshot["source"] == "https://docs.novita.ai/guides/sandbox-pricing"


def test_quote_rounds_a_partial_second_up() -> None:
    """Charge a started second in full, as Novita bills by the second."""
    one = quote_novita_sandbox({**REQUEST, "lifetime_ms": 1000}).maximum.total
    assert quote_novita_sandbox({**REQUEST, "lifetime_ms": 1001}).maximum.total == 2 * one


@pytest.mark.parametrize(
    "override",
    [
        {"lifetime_ms": 0},
        {"lifetime_ms": None},
        {"lifetime_ms": 24 * 3_600_000 + 1},
        {"vcpus": 9},
        {"vcpus": True},
        {"memory_mb": None},
        {"memory_mb": 8 * 1024 + 1},
    ],
)
def test_quote_refuses_an_unbounded_request(override: dict[str, object]) -> None:
    """Refuse a request with no finite, supported size or lifetime.

    Args:
        override: The request field to make invalid.
    """
    with pytest.raises(UnpricedOperationError):
        quote_novita_sandbox({**REQUEST, **override})


def test_reserve_to_settle_charges_the_measured_runtime(database: Engine) -> None:
    """Settle ten minutes of a one-hour reservation at the published rate."""
    reservation = USAGE.reserve(_runtime(database), REQUEST, operation_key="box")
    assert _operation(reservation).max_cents >= Decimal("9.36")
    reservation.start(100.0, "skynet-box")
    USAGE.confirm(reservation, _info())
    reservation.stopped(100.0 + 599.2, confirmed=True, box_seen=True)
    USAGE.settle(reservation)
    operation = _operation(reservation)
    assert operation.state == "settled"
    # 600 seconds * 0.000026 USD = 1.56 cents.
    assert operation.actual_cents == Decimal("1.56")
    assert reservation.operation.id not in reservation.runtime._pinned
    (settle,) = _evidence(reservation)
    assert settle["sandbox_id"] == "sbx-1"
    assert settle["billed_seconds"] == 600
    assert settle["clamped"] is False


def test_settle_clamps_to_the_hold_and_records_it(database: Engine, caplog: pytest.LogCaptureFixture) -> None:
    """Charge at most the hold when the box ran longer than funded, with a warning and evidence.

    Args:
        database: The funded ledger.
        caplog: Captures the clamp warning.
    """
    reservation = USAGE.reserve(_runtime(database), REQUEST, operation_key="box")
    reservation.start(0.0, "skynet-box")
    USAGE.confirm(reservation, _info())
    reservation.stopped(10 * 3600.0, confirmed=True, box_seen=True)
    USAGE.settle(reservation)
    operation = _operation(reservation)
    assert operation.actual_cents == operation.max_cents
    (settle,) = _evidence(reservation)
    assert settle["clamped"] is True
    assert Decimal(settle["unclamped_cents"]) == Decimal("93.6")
    assert "ran past its hold" in caplog.text


def test_late_stop_is_billed_only_to_the_novita_timeout(database: Engine) -> None:
    """Stop the bill where Novita's timeout ends the box, not at a late kill."""
    reservation = USAGE.reserve(_runtime(database), REQUEST, operation_key="box")
    reservation.start(0.0, "skynet-box")
    reservation.created(5.0, 600)
    USAGE.confirm(reservation, _info())
    reservation.stopped(900.0, confirmed=True, box_seen=True)
    assert reservation.billed_seconds() == 605
    USAGE.settle(reservation)
    # 605 seconds * 0.000026 USD = 1.573 cents.
    assert _operation(reservation).actual_cents == Decimal("1.573")


def test_failed_pending_keeps_the_settle_error(database: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Raise the settle error, not the error of the pending mark that followed it.

    Args:
        database: The funded ledger.
        monkeypatch: Breaks the pending mark.
    """
    reservation = USAGE.reserve(_runtime(database), REQUEST, operation_key="box")
    reservation.start(0.0, "skynet-box")
    reservation.stopped(5.0, confirmed=False, box_seen=False)

    def broken() -> None:
        """Fail like a lost ledger.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("ledger down")

    monkeypatch.setattr(reservation, "pending", broken)
    with pytest.raises(UsagePendingError):
        USAGE.settle(reservation)


def test_settle_prices_the_created_box_not_the_funded_size(database: Engine) -> None:
    """Charge the real box size when the template is smaller than the funding."""
    reservation = USAGE.reserve(_runtime(database), REQUEST, operation_key="box")
    reservation.start(0.0, "skynet-box")
    USAGE.confirm(reservation, _info(cpu_count=1, memory_mb=1024))
    reservation.stopped(3600.0, confirmed=True, box_seen=True)
    USAGE.settle(reservation)
    # One hour at 1 vCPU and 1 GiB: 3600 * (0.0000098 + 0.0000032) USD = 4.68 cents.
    assert _operation(reservation).actual_cents == Decimal("4.68")


def test_box_larger_than_its_funding_settles_clamped(database: Engine) -> None:
    """Refuse a larger template at confirm, then settle its real size inside the hold."""
    reservation = USAGE.reserve(_runtime(database), REQUEST, operation_key="box")
    reservation.start(0.0, "skynet-box")
    with pytest.raises(SandboxSpecError):
        USAGE.confirm(reservation, _info(cpu_count=4))
    reservation.stopped(3600.0, confirmed=True, box_seen=True)
    USAGE.settle(reservation)
    operation = _operation(reservation)
    assert operation.actual_cents == operation.max_cents
    (settle,) = _evidence(reservation)
    assert settle["vcpus"] == 4
    assert settle["clamped"] is True


@pytest.mark.parametrize("missing", ["lifetime_ms", "vcpus", "memory_mb"])
def test_reserve_refuses_a_request_with_a_missing_field(missing: str) -> None:
    """Refuse a request with a missing field as unpriced, before any ledger call.

    Args:
        missing: The request field to leave out.
    """
    request = {key: value for key, value in REQUEST.items() if key != missing}
    with pytest.raises(UnpricedOperationError):
        USAGE.reserve(None, request, operation_key="box")


def test_box_that_never_started_releases_the_hold(database: Engine) -> None:
    """Release the whole hold when no create call went out."""
    reservation = USAGE.reserve(_runtime(database), REQUEST, operation_key="box")
    reservation.stopped(5.0, confirmed=False, box_seen=False)
    USAGE.settle(reservation)
    operation = _operation(reservation)
    assert operation.state == "released"
    assert operation.actual_cents == 0
    assert reservation.operation.id not in reservation.runtime._pinned


def test_refused_create_with_no_box_releases_the_hold(database: Engine) -> None:
    """Release the hold when Novita refused the create and the confirmed sweep found no box."""
    reservation = USAGE.reserve(_runtime(database), REQUEST, operation_key="box")
    reservation.start(0.0, "skynet-box")
    reservation.refused()
    reservation.stopped(2.0, confirmed=True, box_seen=False)
    USAGE.settle(reservation)
    operation = _operation(reservation)
    assert operation.state == "released"
    assert operation.actual_cents == 0


def test_refused_create_with_a_box_seen_settles_its_runtime(database: Engine) -> None:
    """Charge the measured runtime when a sweep found a box after a refusal."""
    reservation = USAGE.reserve(_runtime(database), REQUEST, operation_key="box")
    reservation.start(0.0, "skynet-box")
    reservation.refused()
    reservation.stopped(100.0, confirmed=True, box_seen=True)
    USAGE.settle(reservation)
    operation = _operation(reservation)
    assert operation.state == "settled"
    # 100 seconds * 0.000026 USD = 0.26 cents.
    assert operation.actual_cents == Decimal("0.26")


def test_refused_create_with_a_failed_sweep_stays_pending(database: Engine) -> None:
    """Keep the hold pending when the sweep could not prove that no box runs."""
    reservation = USAGE.reserve(_runtime(database), REQUEST, operation_key="box")
    reservation.start(0.0, "skynet-box")
    reservation.refused()
    reservation.stopped(2.0, confirmed=False, box_seen=False)
    with pytest.raises(UsagePendingError):
        USAGE.settle(reservation)
    assert _operation(reservation).state == "pending"


def test_unconfirmed_stop_stays_pending_with_evidence(database: Engine) -> None:
    """Keep the hold pending, never charged at a guess, when the kill was not confirmed."""
    reservation = USAGE.reserve(_runtime(database), REQUEST, operation_key="box")
    reservation.start(0.0, "skynet-box")
    reservation.stopped(5.0, confirmed=False, box_seen=False)
    with pytest.raises(UsagePendingError):
        USAGE.settle(reservation)
    assert _operation(reservation).state == "pending"
    (evidence,) = _evidence(reservation)
    assert evidence["box_name"] == "skynet-box"
    assert evidence["stop_confirmed"] is False
    assert evidence["sandbox_id"] is None


def test_extend_is_refused(database: Engine) -> None:
    """Refuse an extension, because the hold covers the whole lifetime from the start."""
    reservation = USAGE.reserve(_runtime(database), REQUEST, operation_key="box")
    with pytest.raises(UnpricedOperationError):
        USAGE.extend(reservation, 7_200_000, deadline=time.monotonic())


def test_confirm_refuses_a_box_larger_than_its_funding(database: Engine) -> None:
    """Refuse a template with more CPU or memory than the reservation covers."""
    reservation = USAGE.reserve(_runtime(database), REQUEST, operation_key="box")
    with pytest.raises(SandboxSpecError):
        USAGE.confirm(reservation, _info(cpu_count=4))
    with pytest.raises(SandboxSpecError):
        USAGE.confirm(reservation, _info(memory_mb=4096))


def test_unusable_price_snapshot_keeps_the_hold_pending(database: Engine) -> None:
    """Keep the hold pending, never settled at a guess, when the recorded rates are unusable."""
    reservation = USAGE.reserve(_runtime(database), REQUEST, operation_key="box")
    reservation.start(0.0, "skynet-box")
    reservation.stopped(60.0, confirmed=True, box_seen=True)
    reservation.quote = SimpleNamespace(price_snapshot={**reservation.quote.price_snapshot, "cpu_second_usd": "-1"})
    with pytest.raises(UsagePendingError):
        USAGE.settle(reservation)
    assert _operation(reservation).state == "pending"


def test_settle_twice_charges_once(database: Engine) -> None:
    """Accept a repeated settle without a second charge."""
    runtime = _runtime(database)
    reservation = USAGE.reserve(runtime, REQUEST, operation_key="box")
    reservation.start(0.0, "skynet-box")
    reservation.stopped(60.0, confirmed=True, box_seen=True)
    USAGE.settle(reservation)
    billed = runtime.service.get(runtime.budget_id, "alice").billed_cents
    USAGE.settle(reservation)
    assert runtime.service.get(runtime.budget_id, "alice").billed_cents == billed
