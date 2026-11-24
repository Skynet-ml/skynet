"""Verify Modal sandbox admission and measured-runtime metering without Modal calls."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

from core.billing import modal_usage
from core.billing.budget_amounts import cent_units
from core.billing.budgets import BudgetConflictError, BudgetService
from core.billing.modal_usage import ModalUsageReservation, quote_modal_sandbox
from core.billing.operation_pricing import UnpricedOperationError
from core.billing.runtime import BudgetRuntime, UsagePendingError
from core.config import settings
from core.sandbox.metering import usage_adapter
from core.storage.models import Base, BillingCustomerModel, ExecutionOperationModel, ExecutionUsageEvidenceModel

CREATE = {
    "image": "python:3.11-slim",
    "lifetime_ms": 60_000,
    "cpu_cores": 2,
    "memory_mib": 4096,
    "gpu": False,
}
# The funded lifetime: the 60 s timeout plus the 60 s boot and terminate margin.
FUNDED_MS = 120_000


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
    engine = create_engine(f"sqlite:///{tmp_path / 'modal.db'}")
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


def _run(reservation: ModalUsageReservation, *, measured_sec: float | None) -> None:
    """Send, create, stop and settle one box with a fixed measured runtime.

    Args:
        reservation: The claimed reservation.
        measured_sec: Seconds between the create and the confirmed stop, or
            ``None`` when the terminate was not confirmed.
    """
    reservation.sent()
    reservation.confirm_created(SimpleNamespace(object_id="sb-1"))
    if measured_sec is not None:
        assert reservation.sent_at is not None
        reservation.stopped()
        reservation.stopped_at = reservation.sent_at + measured_sec
    reservation.settle()


def test_quote_covers_the_padded_lifetime_at_the_published_rates_with_no_egress() -> None:
    """Price the timeout plus the boot and terminate margin at the CPU and memory rates, with no egress."""
    quote = quote_modal_sandbox(CREATE)
    per_second = 2 * Decimal("0.00003942") + 4 * Decimal("0.00000667")
    assert modal_usage._usd(quote.price_snapshot, FUNDED_MS) == 120 * per_second
    charge = modal_usage.vercel_charge_policy(quote.price_snapshot).convert(120 * per_second)
    assert quote.maximum.total == charge.total
    assert quote.price_snapshot["provider"] == "modal"
    assert quote.price_snapshot["source"] == "https://modal.com/pricing"
    assert not any("egress" in key or "network" in key for key in quote.price_snapshot)


def test_quote_applies_the_modal_minimum_size() -> None:
    """Bill a tiny box at Modal's 0.125-core and 128 MiB minimums."""
    snapshot = quote_modal_sandbox({**CREATE, "cpu_cores": 0.05, "memory_mib": 64}).price_snapshot
    expected = 10 * (Decimal("0.125") * Decimal("0.00003942") + Decimal("0.125") * Decimal("0.00000667"))
    assert modal_usage._usd(snapshot, 10_000) == expected


@pytest.mark.parametrize(
    "change",
    [
        {"lifetime_ms": 0},
        {"lifetime_ms": 24 * 60 * 60 * 1000 + 1},
        {"lifetime_ms": True},
        {"cpu_cores": 0},
        {"cpu_cores": None},
        {"memory_mib": None},
        {"gpu": True},
    ],
)
def test_unbounded_or_gpu_box_is_not_quoted(change: dict[str, Any]) -> None:
    """Refuse a box whose lifetime or size has no ceiling, or that asks for a GPU.

    Args:
        change: The field that breaks the quote.
    """
    with pytest.raises(UnpricedOperationError):
        quote_modal_sandbox({**CREATE, **change})


def test_reserve_then_settle_charges_the_measured_runtime(database: Engine) -> None:
    """Settle a stopped box at its measured seconds, with no egress charge."""
    runtime = _runtime(database)
    reservation = ModalUsageReservation(runtime, CREATE, operation_key="box-one")
    _run(reservation, measured_sec=10.2)
    row = _operation(database)
    assert row.state == "settled"
    assert row.provider_request_id == "sb-1"
    expected = 11 * (2 * Decimal("0.00003942") + 4 * Decimal("0.00000667"))
    charge = modal_usage.vercel_charge_policy(reservation.quote.price_snapshot).convert(expected)
    assert row.actual_units == cent_units(charge.total)
    assert row.actual_units < row.max_units
    assert runtime._pinned == {}


def test_settle_clamps_to_the_padded_lifetime(database: Engine) -> None:
    """Bill at most the timeout plus the margin, even when the measured runtime is longer."""
    long_runtime = _runtime(database)
    long_box = ModalUsageReservation(long_runtime, CREATE, operation_key="long")
    assert long_box.lifetime_ms == FUNDED_MS
    _run(long_box, measured_sec=10_000)
    unknown_runtime = _runtime(database)
    unknown_box = ModalUsageReservation(unknown_runtime, CREATE, operation_key="unknown")
    _run(unknown_box, measured_sec=None)
    with Session(database) as session:
        rows = {row.operation_key: row for row in session.scalars(select(ExecutionOperationModel))}
    assert rows["long"].state == rows["unknown"].state == "settled"
    assert rows["long"].actual_units == rows["unknown"].actual_units == rows["long"].max_units


def test_settle_without_a_sandbox_id_keeps_the_hold_pending(database: Engine) -> None:
    """Keep the hold pending when the box has no sandbox id, because no usage can be measured."""
    runtime = _runtime(database)
    reservation = ModalUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.sent()
    with pytest.raises(UsagePendingError, match="sandbox id"):
        reservation.settle()
    assert _operation(database).state == "pending"


def test_confirm_refuses_a_sandbox_with_no_id(database: Engine) -> None:
    """Refuse a created sandbox that carries no Modal object id."""
    reservation = ModalUsageReservation(_runtime(database), CREATE, operation_key="box-one")
    with pytest.raises(UsagePendingError, match="sandbox id"):
        reservation.confirm_created(SimpleNamespace(object_id=None))


def test_failure_before_the_create_releases_the_hold(database: Engine) -> None:
    """Release the hold when no create reached Modal."""
    runtime = _runtime(database)
    reservation = ModalUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.fail()
    assert _operation(database).state == "released"
    assert runtime._pinned == {}


def test_confirm_records_the_start_time_for_reconciliation(database: Engine) -> None:
    """Write the wall-clock start into the pending evidence once the sandbox is confirmed."""
    runtime = _runtime(database)
    reservation = ModalUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.sent()
    reservation.confirm_created(SimpleNamespace(object_id="sb-1"))
    with Session(database) as session:
        (evidence,) = session.scalars(select(ExecutionUsageEvidenceModel))
    assert _operation(database).state == "pending"
    assert evidence.evidence["sandbox_id"] == "sb-1"
    assert evidence.evidence["sent_at"] == reservation._sent_wall
    assert isinstance(evidence.evidence["sent_at"], float)
    assert evidence.evidence["funded_ms"] == FUNDED_MS


def test_absent_box_releases_the_hold(database: Engine) -> None:
    """Release the hold when a lookup by the box tag found no sandbox after a failed create."""
    runtime = _runtime(database)
    reservation = ModalUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.sent()
    reservation.fail(absent=True)
    assert _operation(database).state == "released"
    assert runtime._pinned == {}


def test_absent_after_a_confirmed_sandbox_keeps_the_hold(database: Engine) -> None:
    """Keep the hold when a sandbox id exists, even if a later lookup found no sandbox."""
    runtime = _runtime(database)
    reservation = ModalUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.sent()
    reservation.confirm_created(SimpleNamespace(object_id="sb-1"))
    reservation.fail(absent=True)
    assert _operation(database).state == "pending"


def test_absent_after_a_returned_handle_keeps_the_hold(database: Engine) -> None:
    """Keep the hold when a create returned a handle with no id, because a sandbox surely existed."""
    runtime = _runtime(database)
    reservation = ModalUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.sent()
    with pytest.raises(UsagePendingError):
        reservation.confirm_created(SimpleNamespace(object_id=None))
    assert reservation.returned is True
    reservation.fail(absent=True)
    assert _operation(database).state == "pending"


def test_confirmed_terminate_settles_a_box_with_no_id(database: Engine) -> None:
    """Settle a box whose id was never confirmed once its terminate is confirmed, keyed by the operation id."""
    runtime = _runtime(database)
    reservation = ModalUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.sent()
    with pytest.raises(UsagePendingError):
        reservation.confirm_created(SimpleNamespace(object_id=None))
    reservation.stopped()
    reservation.settle()
    assert _operation(database).state == "settled"
    with Session(database) as session:
        keys = set(session.scalars(select(ExecutionUsageEvidenceModel.evidence_key)))
    assert f"modal-stop:{reservation.operation.id}" in keys
    reservation.fail()
    assert _operation(database).state == "settled"
    assert runtime._pinned == {}


def test_lease_slice_is_claimed_for_the_same_request(database: Engine) -> None:
    """Claim a pool lease slice reserved for the same request, and settle it like a fresh hold."""
    runtime = _runtime(database)
    slice_ = runtime.reserve(quote_modal_sandbox(CREATE), operation_key="slice", cost_kind="sandbox", role="runtime")
    reservation = ModalUsageReservation(runtime, CREATE, operation_key="slice", reserved=slice_)
    assert reservation.operation.id == slice_.id
    _run(reservation, measured_sec=5)
    assert _operation(database).state == "settled"
    assert runtime._pinned == {}


def test_lease_slice_for_another_request_is_refused(database: Engine) -> None:
    """Refuse a lease slice whose fingerprint belongs to a different sandbox request."""
    runtime = _runtime(database)
    slice_ = runtime.reserve(quote_modal_sandbox(CREATE), operation_key="slice", cost_kind="sandbox", role="runtime")
    with pytest.raises(BudgetConflictError, match="different sandbox request"):
        ModalUsageReservation(runtime, {**CREATE, "cpu_cores": 1}, operation_key="slice", reserved=slice_)
    assert _operation(database).state == "reserved"
    assert runtime._pinned == {}


def test_failure_after_the_create_keeps_the_hold_and_refuses_a_replay(database: Engine) -> None:
    """Keep the hold pending when a sent create returned no sandbox, and refuse a second dispatch."""
    runtime = _runtime(database)
    reservation = ModalUsageReservation(runtime, CREATE, operation_key="box-one")
    reservation.sent()
    reservation.fail()
    assert _operation(database).state == "pending"
    with pytest.raises(UsagePendingError, match="already dispatched"):
        ModalUsageReservation(runtime, CREATE, operation_key="box-one")


def test_extend_is_refused(database: Engine) -> None:
    """Refuse an extension, because Modal cannot lengthen a running sandbox's timeout."""
    runtime = _runtime(database)
    reservation = ModalUsageReservation(runtime, CREATE, operation_key="box-one")
    before = _operation(database).max_units
    with pytest.raises(UnpricedOperationError, match="cannot lengthen"):
        modal_usage.USAGE.extend(reservation, 240_000, deadline=0)
    assert _operation(database).max_units == before
    assert reservation.lifetime_ms == FUNDED_MS


def test_modal_usage_adapter_is_found() -> None:
    """Find ``USAGE`` through the metering registry, so hosted Skynet can offer Modal."""
    adapter = usage_adapter("modal")
    assert adapter is not None
    assert adapter is modal_usage.USAGE
    assert adapter.name == "modal"
