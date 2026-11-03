"""Verify durable budget admission and reconciliation without paid provider calls."""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session

from core.billing.budget_amounts import cent_units
from core.billing.budgets import (
    AccountFundsError,
    BudgetBoundExceededError,
    BudgetConflictError,
    BudgetFencedError,
    BudgetFundingLostError,
    BudgetInFlightError,
    BudgetInsufficientError,
    BudgetNotFoundError,
    BudgetService,
    BudgetTotalConflictError,
    BudgetUnreconciledError,
)
from core.billing.operation_pricing import OperationCharge, OperationQuote
from core.billing.pricing import ModelUsage
from core.billing.runtime import BudgetRuntime, UsagePendingError, generation_operation_key
from core.billing.service import StripeBillingService, committed_spend_cents, legacy_job_committed_cents
from core.config import settings
from core.storage.models import (
    Base,
    BillingCustomerModel,
    ExecutionUsageEvidenceModel,
    JobModel,
    WalletLedgerModel,
)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Create a private transactional wallet database for each test."""
    database = create_engine(f"sqlite:///{tmp_path / 'budgets.db'}", connect_args={"timeout": 10})
    Base.metadata.create_all(database)
    with Session(database) as session:
        session.add_all(
            [
                BillingCustomerModel(
                    username=name, stripe_customer_id=f"local-{name}", balance_cents=50, grant_remaining=0
                )
                for name in ("alice", "bob")
            ]
        )
        session.commit()
    yield database
    database.dispose()


def _reserve(service: BudgetService, budget_id: str, key: str = "call", **kwargs: Any) -> Any:
    """Reserve a fully specified deterministic model request."""
    values = {
        "operation_key": key,
        "generation": 0,
        "phase": "setup",
        "cost_kind": "model",
        "request_fingerprint": "resolved-request",
        "price_snapshot": {"version": "fixture-v1"},
        "max_cents": 10,
    }
    values.update(kwargs)
    return service.reserve(budget_id, "alice", **values)


def _balance(engine: Engine) -> int:
    """Read actual debited wallet balance independently of outstanding holds."""
    with Session(engine) as session:
        return session.get(BillingCustomerModel, "alice").balance_cents


@pytest.mark.parametrize("reason", ["user_cancelled", "user_paused", "budget_reached"])
@pytest.mark.parametrize("phase", ["setup", "run"])
def test_closed_admission_prevents_reserved_dispatch_but_accepts_existing_receipts(
    engine: Engine, reason: str, phase: str
) -> None:
    """Reject an unstarted paid request after stop while reconciling already dispatched usage.

    Args:
        engine: Private funded ledger database.
        reason: Terminal or paused admission stop recorded after reservation.
        phase: Setup or attached execution whose admission is stopped.
    """
    service = BudgetService(engine=engine)
    budget = service.create("alice", 20, idempotency_key="stop-race")
    if phase == "run":
        service.attach_to_job(budget.id, "alice", "job", expected_revision=budget.revision)
    waiting = _reserve(service, budget.id, "waiting", phase=phase, max_cents=5)
    running = _reserve(service, budget.id, "running", phase=phase, max_cents=5)
    service.mark_dispatched(running.id, "alice")
    service.stop_admission(budget.id, "alice", reason=reason)
    with pytest.raises(BudgetConflictError, match="not admitting"):
        service.mark_dispatched(waiting.id, "alice", "must-not-start")
    assert service.get_operation(waiting.id, "alice").state == "reserved"
    assert service.get_operation(waiting.id, "alice").provider_request_id is None
    late = service.mark_dispatched(running.id, "alice", "already-dispatched")
    assert late.dispatch_claimed is False
    assert late.provider_request_id == "already-dispatched"
    service.settle(running.id, "alice", evidence_key="final", actual_cents=2, evidence={"actual": True})
    assert service.get_operation(running.id, "alice").state == "settled"
    assert service.get(budget.id, "alice").reserved_cents == 5


def test_legacy_commitments_protect_wallet_from_new_reservations(engine: Engine) -> None:
    """Keep active legacy ceilings covered when modern setup reserves the same wallet.

    Args:
        engine: Private funded ledger database.
    """
    service = BudgetService(engine=engine)
    budget = service.create("alice", 50, idempotency_key="draft")
    with Session(engine) as session:
        session.add(
            JobModel(
                optimization_id="legacy",
                username="alice",
                status="running",
                payload_overview={"max_cost_cents": 30, "token_source": "managed"},
            )
        )
        session.commit()
    assert StripeBillingService(engine=engine).spendable_cents("alice") == 20
    assert service.get(budget.id, "alice").account_available_cents == 20
    with pytest.raises(BudgetInFlightError):
        _reserve(service, budget.id, max_cents=21)
    _reserve(service, budget.id, max_cents=20)
    assert StripeBillingService(engine=engine).spendable_cents("alice") == 0


def test_legacy_debit_consumes_its_own_commitment_only(engine: Engine) -> None:
    """Protect other legacy jobs and modern holds while the original legacy run settles.

    Args:
        engine: Private funded ledger database.
    """
    service = BudgetService(engine=engine)
    budget = service.create("alice", 50, idempotency_key="draft")
    _reserve(service, budget.id, max_cents=20)
    with Session(engine) as session:
        session.add_all(
            [
                JobModel(
                    optimization_id=identity,
                    username="alice",
                    status="running",
                    payload_overview={"max_cost_cents": ceiling},
                )
                for identity, ceiling in [("finishing", 20), ("other", 10)]
            ]
        )
        session.commit()
    billing = StripeBillingService(engine=engine)
    usage = [ModelUsage(model="unpriced-fixture", input_tokens=1_000_000, output_tokens=0)]
    assert billing.debit_run("alice", usage, model=None, description="interactive") == 0
    assert billing.debit_run("alice", usage, model=None, description="legacy", optimization_id="finishing") == 20
    assert _balance(engine) == 30
    assert service.get(budget.id, "alice").reserved_cents == 20


def test_only_verified_budget_attachments_avoid_legacy_double_count(engine: Engine) -> None:
    """Exclude an actual matching root attachment and retain spoofed metadata commitments.

    Args:
        engine: Private funded ledger database.
    """
    service = BudgetService(engine=engine)
    budget = service.create("alice", 50, idempotency_key="root")
    service.attach_to_job(budget.id, "alice", "protected", expected_revision=1)
    with Session(engine) as session:
        session.add_all(
            [
                JobModel(
                    optimization_id="protected",
                    username="alice",
                    status="running",
                    execution_budget_id=budget.id,
                    execution_budget_generation=0,
                    payload_overview={"max_cost_cents": 50},
                ),
                JobModel(
                    optimization_id="spoofed",
                    username="alice",
                    status="pending",
                    execution_budget_id=budget.id,
                    execution_budget_generation=0,
                    payload_overview={"max_cost_cents": 7},
                ),
            ]
        )
        session.commit()
        assert legacy_job_committed_cents(session, "alice") == 7
    _reserve(service, budget.id, phase="run", max_cents=10)
    assert StripeBillingService(engine=engine).spendable_cents("alice") == 33
    with Session(engine) as session:
        session.get(JobModel, "protected").execution_budget_generation = 99
        session.commit()
        assert legacy_job_committed_cents(session, "alice") == 57


def test_legacy_commitments_count_active_roots_and_byok_budget(engine: Engine) -> None:
    """Ignore terminal jobs, child duplicates, and other owners while retaining paused promises.

    Args:
        engine: Private funded ledger database.
    """
    with Session(engine) as session:
        session.add_all(
            [
                JobModel(
                    optimization_id=status, username="alice", status=status, payload_overview={"max_cost_cents": 5}
                )
                for status in (
                    "pending",
                    "validating",
                    "running",
                    "paused",
                    "completed",
                    "failed",
                    "cancelled",
                    "budget_reached",
                )
            ]
        )
        session.add_all(
            [
                JobModel(
                    optimization_id="byok",
                    username="alice",
                    status="running",
                    payload_overview={"max_cost_cents": 100, "token_source": "byok"},
                ),
                JobModel(
                    optimization_id="child",
                    username="alice",
                    parent_optimization_id="running",
                    status="running",
                    payload_overview={"max_cost_cents": 500},
                ),
                JobModel(
                    optimization_id="foreign",
                    username="bob",
                    status="running",
                    payload_overview={"max_cost_cents": 500},
                ),
            ]
        )
        session.commit()
        assert legacy_job_committed_cents(session, "alice") == 20 + committed_spend_cents(100, "byok")


def test_budget_creation_revision_and_owner(engine: Engine) -> None:
    """Keep creation retries stable and reject stale edits or another owner."""
    service = BudgetService(engine=engine)
    budget = service.create("alice", 20, idempotency_key="draft")
    assert service.create("alice", 20, idempotency_key="draft").id == budget.id
    assert service.find_by_creation_key("alice", "draft") == budget
    assert service.find_by_creation_key("alice", "missing") is None
    assert service.find_by_creation_key("bob", "draft") is None
    with pytest.raises(BudgetConflictError):
        service.create("alice", 21, idempotency_key="draft")
    with pytest.raises(BudgetNotFoundError):
        service.get(budget.id, "bob")
    _reserve(service, budget.id, max_cents=12)
    with pytest.raises(BudgetTotalConflictError, match=r"minimum.*12") as rejected:
        service.update_total(budget.id, "alice", 11, expected_revision=1)
    assert rejected.value.current_total_cents == 20
    assert rejected.value.minimum_total_cents == 12
    changed = service.update_total(budget.id, "alice", 30, expected_revision=1)
    assert changed.revision == 2
    with pytest.raises(BudgetTotalConflictError) as stale:
        service.update_total(budget.id, "alice", 40, expected_revision=1)
    assert stale.value.current_total_cents == 30
    assert stale.value.minimum_total_cents == 12
    assert service.get(budget.id, "alice").total_cents == 30


def test_attachment_is_once_and_can_join_submission_transaction(engine: Engine) -> None:
    """Bind a single root job and roll attachment back with a failed submission."""
    service = BudgetService(engine=engine)
    budget = service.create("alice", 20, idempotency_key="draft")
    with Session(engine) as transaction:
        service.attach_to_job(budget.id, "alice", "job-first", expected_revision=1, session=transaction)
        transaction.rollback()
    assert service.get(budget.id, "alice").job_id is None
    attached = service.attach_to_job(budget.id, "alice", "job-first", expected_revision=1)
    assert attached.job_id == "job-first"
    assert service.attach_to_job(budget.id, "alice", "job-first", expected_revision=1) == attached
    with pytest.raises(BudgetConflictError):
        service.attach_to_job(budget.id, "alice", "job-second", expected_revision=attached.revision)


def test_submission_consumes_setup_authority_after_pending_tests_finish(engine: Engine) -> None:
    """Prevent stale draft tabs from admitting setup costs after successful submission."""
    service = BudgetService(engine=engine)
    budget = service.create("alice", 20, idempotency_key="draft")
    operation = _reserve(service, budget.id)
    with pytest.raises(BudgetInFlightError):
        service.attach_to_job(budget.id, "alice", "job", expected_revision=1)
    service.release(operation.id, "alice")
    service.attach_to_job(budget.id, "alice", "job", expected_revision=1)
    with pytest.raises(BudgetConflictError):
        _reserve(service, budget.id, "stale-tab")
    assert _reserve(service, budget.id, "run", phase="run").state == "reserved"


def test_partial_failure_and_replay_settle_once(engine: Engine) -> None:
    """Keep partial failure usage charged and unknown remaining usage covered."""
    service = BudgetService(engine=engine)
    budget = service.create("alice", 20, idempotency_key="draft")
    operation = _reserve(service, budget.id)
    assert service.mark_dispatched(operation.id, "alice", "provider-1").dispatch_claimed
    assert not service.mark_dispatched(operation.id, "alice", "provider-1").dispatch_claimed
    partial = service.settle(
        operation.id,
        "alice",
        evidence_key="partial",
        actual_cents="2.2",
        evidence={"model": "fixture", "failed": True},
        final=False,
    )
    assert partial.state == "pending"
    assert partial.budget.setup_spent_cents == Decimal("2.2")
    assert partial.budget.reserved_cents == Decimal("7.8")
    assert partial.budget.available_cents == 10
    assert partial.budget.billed_cents == 3
    assert partial.budget.wallet_reserved_cents == 7
    with pytest.raises(BudgetUnreconciledError):
        service.release(operation.id, "alice")
    complete = service.settle(
        operation.id, "alice", evidence_key="final", actual_cents="3.1", evidence={"failed": True, "reconciled": True}
    )
    assert complete.state == "settled"
    assert complete.budget.reserved_cents == 0
    assert complete.budget.available_cents == Decimal("16.9")
    assert complete.budget.billed_cents == 4
    assert (
        service.settle(
            operation.id,
            "alice",
            evidence_key="final",
            actual_cents="3.1",
            evidence={"failed": True, "reconciled": True},
        )
        == complete
    )
    assert _balance(engine) == 46
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(WalletLedgerModel)) == 2
        assert session.scalar(select(func.count()).select_from(ExecutionUsageEvidenceModel)) == 2


def test_aggregate_rounding_and_phase_cost_dimensions(engine: Engine) -> None:
    """Avoid a one-cent minimum for every subcall and retain phase/type attribution."""
    service = BudgetService(engine=engine)
    budget = service.create("alice", 10, idempotency_key="draft")
    for key, phase, kind, actual in [
        ("setup-model", "setup", "model", "0.1"),
        ("run-model", "run", "model", "0.2"),
        ("runtime", "run", "sandbox", "0.7"),
    ]:
        if key == "run-model":
            service.attach_to_job(budget.id, "alice", "job", expected_revision=1)
        operation = _reserve(service, budget.id, key, phase=phase, cost_kind=kind, max_cents=1)
        service.mark_dispatched(operation.id, "alice")
        service.settle(operation.id, "alice", evidence_key=key, actual_cents=actual, evidence={"cost_kind": kind})
    snapshot = service.get(budget.id, "alice")
    assert snapshot.setup_spent_cents == Decimal("0.1")
    assert snapshot.run_spent_cents == Decimal("0.9")
    assert snapshot.billed_cents == 1
    assert _balance(engine) == 49


def test_byok_model_scope_settles_at_zero(engine: Engine) -> None:
    """Keep provider-paid token cost outside both Total and the Skynet wallet."""
    service = BudgetService(engine=engine)
    budget = service.create("alice", 100, idempotency_key="draft")
    operation = _reserve(service, budget.id, max_cents=0, max_wallet_cents=0)
    assert operation.budget.reserved_cents == 0
    assert operation.budget.wallet_reserved_cents == 0
    service.mark_dispatched(operation.id, "alice")
    result = service.settle(
        operation.id,
        "alice",
        evidence_key="provider",
        actual_cents=0,
        actual_wallet_cents=0,
        evidence={"source": "byok", "provider_cost_cents": 27},
    )
    assert result.budget.external_spent_cents == 0
    assert result.budget.billed_cents == 0
    assert result.budget.available_cents == 100
    assert _balance(engine) == 50


def test_contention_differs_from_exhaustion(engine: Engine) -> None:
    """Report reversible in-flight contention separately from insufficient coverage."""
    service = BudgetService(engine=engine)
    budget = service.create("alice", 20, idempotency_key="draft")
    first = _reserve(service, budget.id, "first", max_cents=15)
    with pytest.raises(BudgetInFlightError):
        _reserve(service, budget.id, "second", max_cents=10)
    with pytest.raises(BudgetInsufficientError):
        _reserve(service, budget.id, "oversize", max_cents=21)
    service.release(first.id, "alice")
    assert _reserve(service, budget.id, "second", max_cents=10).state == "reserved"


def test_limit_refusal_states_its_amounts_in_dollars(engine: Engine) -> None:
    """The wizard shows the limit in dollars, so the refusal it relays does too."""
    service = BudgetService(engine=engine)
    budget = service.create("alice", 20, idempotency_key="draft")
    with pytest.raises(BudgetInsufficientError) as refused:
        _reserve(service, budget.id, "oversize", max_cents=21)
    assert str(refused.value) == "The next operation needs up to $0.21, but only $0.20 of the $0.20 limit remain."


def test_wallet_holds_span_budgets_and_protect_against_legacy_debits(engine: Engine) -> None:
    """Keep covered funding unavailable to other drafts and older interactive surfaces."""
    service = BudgetService(engine=engine)
    first = service.create("alice", 100, idempotency_key="first")
    second = service.create("alice", 100, idempotency_key="second")
    _reserve(service, first.id, max_cents=45)
    with pytest.raises(BudgetInFlightError):
        _reserve(service, second.id, max_cents=10)
    billing = StripeBillingService(engine=engine)
    assert billing.spendable_cents("alice") == 5
    billed = billing.debit_run(
        "alice", [ModelUsage("unpriced-fixture", 1_000_000, 0)], model="unpriced-fixture", description="legacy surface"
    )
    assert billed == 5
    assert _balance(engine) == 45
    assert service.get(first.id, "alice").wallet_reserved_cents == 45


def test_fencing_releases_only_undispatched_work(engine: Engine) -> None:
    """Fence old dispatch and wait for unresolved prior-generation costs before recovery."""
    service = BudgetService(engine=engine)
    budget = service.create("alice", 30, idempotency_key="draft")
    not_started = _reserve(service, budget.id, "not-started")
    dispatched = _reserve(service, budget.id, "started")
    service.mark_dispatched(dispatched.id, "alice")
    fenced = service.fence_generation(budget.id, "alice", expected_generation=0)
    assert fenced.generation == 1
    assert fenced.reserved_cents == 10
    assert service.get_operation(not_started.id, "alice").state == "released"
    with pytest.raises(BudgetFencedError):
        service.mark_dispatched(not_started.id, "alice")
    with pytest.raises(BudgetFencedError):
        _reserve(service, budget.id, "old-worker")
    with pytest.raises(BudgetUnreconciledError):
        _reserve(service, budget.id, "new-worker", generation=1)
    late = service.mark_dispatched(dispatched.id, "alice", provider_request_id="provider-late")
    assert late.provider_request_id == "provider-late"
    assert late.dispatch_claimed is False
    service.settle(dispatched.id, "alice", evidence_key="late-provider", actual_cents=2, evidence={"reconciled": True})
    assert _reserve(service, budget.id, "new-worker", generation=1).generation == 1


def test_evidence_conflicts_do_not_mutate_original_charge(engine: Engine) -> None:
    """Reject changed payloads under the same immutable usage event identity."""
    service = BudgetService(engine=engine)
    budget = service.create("alice", 20, idempotency_key="draft")
    operation = _reserve(service, budget.id)
    service.mark_dispatched(operation.id, "alice")
    service.settle(operation.id, "alice", evidence_key="usage", actual_cents=2, evidence={"cache_read": 10})
    with pytest.raises(BudgetConflictError):
        service.settle(operation.id, "alice", evidence_key="usage", actual_cents=4, evidence={"cache_read": 0})
    assert _balance(engine) == 48
    with Session(engine) as session:
        event = session.scalar(select(ExecutionUsageEvidenceModel))
        assert event.evidence == {"cache_read": 10}


def test_pending_evidence_is_immutable_without_inventing_a_charge(engine: Engine) -> None:
    """Preserve an incomplete receipt durably without claiming zero actual usage."""
    service = BudgetService(engine=engine)
    budget = service.create("alice", 20, idempotency_key="draft")
    operation = _reserve(service, budget.id)
    service.mark_dispatched(operation.id, "alice")
    for _ in range(2):
        service.mark_pending(operation.id, "alice", evidence_key="receipt", evidence={"cpu_ms": 100, "network": None})
    with pytest.raises(BudgetConflictError, match="immutable"):
        service.mark_pending(operation.id, "alice", evidence_key="receipt", evidence={"cpu_ms": 200})
    with Session(engine) as session:
        events = list(session.scalars(select(ExecutionUsageEvidenceModel)))
        assert len(events) == 1
        assert events[0].issue == "usage_pending"
        assert events[0].evidence == {"cpu_ms": 100, "network": None}
        assert events[0].billed_cents == 0
    assert service.get(budget.id, "alice").reserved_cents == 10
    assert _balance(engine) == 50


def test_explicit_resume_requires_reconciliation_and_is_not_triggered_by_funding(engine: Engine) -> None:
    """Keep stopped envelopes closed through top-ups until the lifecycle coordinator resumes."""
    service = BudgetService(engine=engine)
    budget = service.create("alice", 20, idempotency_key="draft")
    attached = service.attach_to_job(budget.id, "alice", "job-one", expected_revision=1)
    operation = _reserve(service, budget.id, phase="run")
    service.mark_dispatched(operation.id, "alice")
    service.stop_admission(budget.id, "alice", reason="paused")
    changed = service.update_total(budget.id, "alice", 30, expected_revision=attached.revision)
    assert changed.state == "closed"
    with pytest.raises(BudgetUnreconciledError):
        service.resume_admission(budget.id, "alice", expected_generation=0)
    service.settle(operation.id, "alice", evidence_key="receipt", actual_cents=2, evidence={"measured": True})
    with pytest.raises(BudgetFencedError):
        service.resume_admission(budget.id, "alice", expected_generation=1)
    assert service.resume_admission(budget.id, "alice", expected_generation=0).state == "attached"


def test_overrun_retains_evidence_and_coverage_without_unbounded_debit(engine: Engine) -> None:
    """Quarantine reported usage above a verified bound instead of charging beyond it."""
    service = BudgetService(engine=engine)
    budget = service.create("alice", 20, idempotency_key="draft")
    operation = _reserve(service, budget.id, max_cents=5)
    admitted = _reserve(service, budget.id, "already-admitted", max_cents=2)
    service.mark_dispatched(operation.id, "alice")
    with pytest.raises(BudgetBoundExceededError):
        service.settle(
            operation.id, "alice", evidence_key="overrun", actual_cents=6, evidence={"provider_request": "x"}
        )
    snapshot = service.get(budget.id, "alice")
    assert snapshot.state == "blocked"
    assert snapshot.reserved_cents == 7
    assert _balance(engine) == 50
    with Session(engine) as session:
        event = session.scalar(select(ExecutionUsageEvidenceModel))
        assert event.actual_units == cent_units(6)
        assert event.issue == "bound_exceeded"
    with pytest.raises(BudgetConflictError):
        _reserve(service, budget.id, "next")
    with pytest.raises(BudgetConflictError, match="quarantined"):
        service.mark_dispatched(admitted.id, "alice")
    assert service.stop_admission(budget.id, "alice", reason="paused").state == "blocked"
    with pytest.raises(BudgetConflictError):
        service.resume_admission(budget.id, "alice", expected_generation=0)


def test_removed_funding_keeps_usage_pending(engine: Engine) -> None:
    """Retain reconciliation evidence if an external wallet adjustment removes funding."""
    service = BudgetService(engine=engine)
    budget = service.create("alice", 20, idempotency_key="draft")
    operation = _reserve(service, budget.id, max_cents=5)
    service.mark_dispatched(operation.id, "alice")
    with Session(engine) as session:
        session.get(BillingCustomerModel, "alice").balance_cents = 0
        session.commit()
    with pytest.raises(BudgetFundingLostError):
        service.settle(operation.id, "alice", evidence_key="usage", actual_cents=3, evidence={"measured": True})
    assert service.get(budget.id, "alice").reserved_cents == 5


def test_no_charge_or_release_for_work_without_dispatch(engine: Engine) -> None:
    """Release unstarted reservations without manufacturing usage or a charge."""
    service = BudgetService(engine=engine)
    budget = service.create("alice", 20, idempotency_key="draft")
    operation = _reserve(service, budget.id)
    with pytest.raises(BudgetConflictError):
        service.settle(operation.id, "alice", evidence_key="invalid", actual_cents=1, evidence={})
    assert service.release(operation.id, "alice").state == "released"
    assert service.release(operation.id, "alice").state == "released"
    assert _balance(engine) == 50
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(ExecutionUsageEvidenceModel)) == 0


def test_refused_dispatch_is_rejected_only_without_any_provider_trace(engine: Engine) -> None:
    """Return coverage for a refusal, retained or not, never for work that may have started."""
    service = BudgetService(engine=engine)
    budget = service.create("alice", 20, idempotency_key="draft")
    refusal = {"evidence_key": "refusal", "evidence": {"status_code": 400}}
    operation = _reserve(service, budget.id)
    with pytest.raises(BudgetConflictError):
        service.reject(operation.id, "alice", **refusal)
    service.mark_dispatched(operation.id, "alice")
    assert service.reject(operation.id, "alice", **refusal).state == "released"
    assert service.reject(operation.id, "alice", **refusal).state == "released"
    assert service.get(budget.id, "alice").reserved_cents == 0
    assert _balance(engine) == 50
    with Session(engine) as session:
        evidence = session.scalar(select(ExecutionUsageEvidenceModel))
        assert (evidence.issue, evidence.final, evidence.billed_cents) == ("rejected", True, 0)
    started = _reserve(service, budget.id, key="started")
    service.mark_dispatched(started.id, "alice", "provider-session")
    with pytest.raises(BudgetUnreconciledError):
        service.reject(started.id, "alice", **refusal)
    retained = _reserve(service, budget.id, key="retained")
    service.mark_dispatched(retained.id, "alice")
    service.mark_pending(retained.id, "alice", evidence_key="answer", evidence={"status_code": 429})
    assert service.reject(retained.id, "alice", **refusal).state == "released"
    partial = _reserve(service, budget.id, key="partial")
    service.mark_dispatched(partial.id, "alice")
    service.settle(
        partial.id, "alice", evidence_key="partial", actual_cents="0.5", evidence={"seen": True}, final=False
    )
    with pytest.raises(BudgetUnreconciledError):
        service.reject(partial.id, "alice", **refusal)
    assert service.get(budget.id, "alice").pending_operations == 2


def test_attempt_identity_binds_configuration_and_retry_number(engine: Engine) -> None:
    """Reserve each paid retry separately while identical HTTP delivery reuses its hold."""
    service = BudgetService(engine=engine)
    budget = service.create("alice", 30, idempotency_key="draft")
    first = _reserve(service, budget.id)
    assert _reserve(service, budget.id).id == first.id
    with pytest.raises(BudgetConflictError):
        _reserve(service, budget.id, request_fingerprint="different")
    second = _reserve(service, budget.id, attempt=1)
    assert second.id != first.id
    assert second.budget.reserved_cents == 20


def test_concurrent_wallet_reservations_cannot_overcommit(engine: Engine) -> None:
    """Serialize separate budget admissions against the same prepaid account."""
    service = BudgetService(engine=engine)
    budgets = [service.create("alice", 100, idempotency_key=key).id for key in ("one", "two")]
    barrier = Barrier(2)

    def reserve(budget_id: str) -> str:
        """Race independent requests after both threads have reached admission."""
        barrier.wait(timeout=5)
        try:
            return _reserve(service, budget_id, max_cents=40).state
        except BudgetInFlightError:
            return "contended"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(reserve, budgets))
    assert sorted(outcomes) == ["contended", "reserved"]
    assert StripeBillingService(engine=engine).spendable_cents("alice") == 10


def test_concurrent_duplicate_dispatch_has_one_sender(engine: Engine) -> None:
    """Give only one competing delivery the authority to send a physical request."""
    service = BudgetService(engine=engine)
    budget = service.create("alice", 20, idempotency_key="draft")
    operation = _reserve(service, budget.id)
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(lambda _: service.mark_dispatched(operation.id, "alice").dispatch_claimed, range(2))
        )
    assert sorted(outcomes) == [False, True]
    assert service.get(budget.id, "alice").reserved_cents == 10


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-1", "0.0000000001"])
def test_amounts_reject_unrepresentable_values(amount: str) -> None:
    """Reject unknown or silently rounded charges at the accounting boundary."""
    with pytest.raises(ValueError):
        cent_units(amount)


def test_uncapped_budget_admits_past_its_total_until_the_account_runs_dry(engine: Engine) -> None:
    """Skip the total checks for an uncapped budget while the wallet still gates admission."""
    service = BudgetService(engine=engine)
    budget = service.create("alice", 5, idempotency_key="draft", uncapped=True)
    assert budget.uncapped is True
    assert budget.available_cents == 50
    with pytest.raises(BudgetConflictError):
        service.create("alice", 5, idempotency_key="draft")
    _reserve(service, budget.id, key="first", max_cents=30)
    _reserve(service, budget.id, key="second", max_cents=15)
    assert service.get(budget.id, "alice").available_cents == 5
    with pytest.raises(AccountFundsError, match="cannot fund"):
        _reserve(service, budget.id, key="third", max_cents=60)


def test_uncapped_flag_is_versioned_like_the_total(engine: Engine) -> None:
    """Bump the revision when only the mode changes and keep replays on the original fingerprint."""
    service = BudgetService(engine=engine)
    budget = service.create("alice", 20, idempotency_key="draft")
    assert budget.uncapped is False
    assert service.create("alice", 20, idempotency_key="draft").id == budget.id
    with pytest.raises(BudgetConflictError):
        service.create("alice", 20, idempotency_key="draft", uncapped=True)
    lifted = service.update_total(budget.id, "alice", 20, expected_revision=1, uncapped=True)
    assert lifted.uncapped is True
    assert lifted.revision == 2
    assert lifted.available_cents == 50
    _reserve(service, budget.id, max_cents=30)
    with pytest.raises(BudgetTotalConflictError):
        service.update_total(budget.id, "alice", 20, expected_revision=2)
    capped = service.update_total(budget.id, "alice", 30, expected_revision=2)
    assert capped.uncapped is False
    assert capped.revision == 3
    assert capped.available_cents == 0


def test_recovered_generation_can_repeat_a_logical_operation(engine: Engine) -> None:
    """Admit the same logical work again after recovery fences the interrupted generation."""
    service = BudgetService(engine=engine)
    budget = service.create("alice", 10, idempotency_key="recovery-repeat")
    service.attach_to_job(budget.id, "alice", "repeat-job", expected_revision=budget.revision)
    quote = OperationQuote(
        request_fingerprint="scorer-box",
        maximum=OperationCharge(total=Decimal(1), wallet=Decimal(1)),
        price_snapshot={"version": "fixture-v1"},
    )

    def runtime(generation: int) -> BudgetRuntime:
        """Bind a run-phase runtime to one execution generation."""
        return BudgetRuntime(service, username="alice", budget_id=budget.id, generation=generation, phase="run")

    first = runtime(0).reserve(quote, operation_key="repo-scorer:repeat-job", cost_kind="sandbox", role="runtime")
    service.release(first.id, "alice")
    service.fence_generation(budget.id, "alice", expected_generation=0)
    second = runtime(1).reserve(quote, operation_key="repo-scorer:repeat-job", cost_kind="sandbox", role="runtime")
    retried = runtime(1).reserve(quote, operation_key="repo-scorer:repeat-job", cost_kind="sandbox", role="runtime")

    assert second.id != first.id
    assert retried.id == second.id
    assert generation_operation_key("k" * 128, 3).endswith("#g3")
    assert len(generation_operation_key("k" * 128, 3)) <= 128


def test_sandbox_coverage_grows_only_through_admission(engine: Engine) -> None:
    """Admit each extra slice like new work, record it once, and keep settlement capped at the grown bound."""
    service = BudgetService(engine=engine)
    budget = service.create("alice", 30, idempotency_key="slices")
    box = _reserve(service, budget.id, "box", cost_kind="sandbox", max_cents=10)
    model = _reserve(service, budget.id, "model", max_cents=5)
    with pytest.raises(BudgetConflictError, match="running sandbox"):
        service.extend_coverage(
            box.id, "alice", evidence_key="early", max_cents=12, max_wallet_cents=12, evidence={"lifetime_ms": 2}
        )
    service.mark_dispatched(box.id, "alice", "session-one")
    service.mark_dispatched(model.id, "alice", "request-one")
    with pytest.raises(BudgetConflictError, match="running sandbox"):
        service.extend_coverage(
            model.id, "alice", evidence_key="model", max_cents=12, max_wallet_cents=12, evidence={"lifetime_ms": 2}
        )
    with pytest.raises(BudgetConflictError, match="grow"):
        service.extend_coverage(
            box.id, "alice", evidence_key="shrink", max_cents=10, max_wallet_cents=10, evidence={"lifetime_ms": 2}
        )
    with pytest.raises(BudgetInFlightError):
        service.extend_coverage(
            box.id, "alice", evidence_key="wait", max_cents=26, max_wallet_cents=26, evidence={"lifetime_ms": 2}
        )
    with pytest.raises(BudgetInsufficientError):
        service.extend_coverage(
            box.id, "alice", evidence_key="over", max_cents=41, max_wallet_cents=41, evidence={"lifetime_ms": 3}
        )
    for _ in range(2):
        service.extend_coverage(
            box.id, "alice", evidence_key="slice-2", max_cents=20, max_wallet_cents=20, evidence={"lifetime_ms": 2}
        )
        assert service.get(budget.id, "alice").reserved_cents == 25
    with pytest.raises(BudgetConflictError, match="different bound"):
        service.extend_coverage(
            box.id, "alice", evidence_key="slice-2", max_cents=22, max_wallet_cents=22, evidence={"lifetime_ms": 2}
        )
    with pytest.raises(BudgetBoundExceededError):
        service.settle(box.id, "alice", evidence_key="stop-over", actual_cents=21, actual_wallet_cents=21, evidence={})
    service.settle(box.id, "alice", evidence_key="stop", actual_cents=18, actual_wallet_cents=18, evidence={})
    assert service.get(budget.id, "alice").reserved_cents == 5


@pytest.mark.parametrize(("environment_name", "funded"), [("staging", False), ("skynet-pr-662", True)])
def test_new_account_without_wallet_row_funds_budget_only_in_preview(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, environment_name: str, funded: bool
) -> None:
    """A brand-new account's preview grant funds a budget; elsewhere it still has no funded wallet."""
    monkeypatch.setattr(settings, "preview_grant_cents", 1000)
    monkeypatch.setattr(settings, "app_environment", environment_name)
    service = BudgetService(engine=engine)
    if not funded:
        with pytest.raises(BudgetInsufficientError):
            service.create("carol", 100, idempotency_key="first")
        return
    budget = service.create("carol", 100, idempotency_key="first")
    assert budget.total_cents == 100
    with Session(engine) as session:
        assert session.get(BillingCustomerModel, "carol").grant_remaining == 1000


def _setup_runtime(service: BudgetService, budget_id: str, *, phase: str = "setup", wait: float = 600) -> BudgetRuntime:
    """Bind a runtime whose default wait would hang a test that wrongly waits."""
    return BudgetRuntime(service, username="alice", budget_id=budget_id, generation=0, phase=phase, wait_timeout=wait)


def _quote(cents: int) -> OperationQuote:
    """Bound one deterministic operation at ``cents``."""
    return OperationQuote(
        request_fingerprint=f"op-{cents}",
        maximum=OperationCharge(total=Decimal(cents), wallet=Decimal(cents)),
        price_snapshot={"version": "fixture-v1"},
    )


def test_in_flight_refusal_carries_the_limit_arithmetic(engine: Engine) -> None:
    """A wait-for-room refusal says how much the work needs and how much of the limit is unspent."""
    service = BudgetService(engine=engine)
    budget = service.create("alice", 40, idempotency_key="arithmetic")
    _reserve(service, budget.id, "box", cost_kind="sandbox", max_cents=10)
    with pytest.raises(BudgetInFlightError) as refused:
        _reserve(service, budget.id, "call", max_cents=35)

    assert refused.value.scope_units == cent_units(35)
    assert refused.value.room_units == cent_units(40)


def test_setup_check_refuses_at_once_when_only_its_own_boxes_hold_the_room(engine: Engine) -> None:
    """A check's open boxes never release while it waits, so it refuses instead of hanging."""
    service = BudgetService(engine=engine)
    budget = service.create("alice", 40, idempotency_key="own-boxes")
    runtime = _setup_runtime(service, budget.id)
    box = runtime.reserve(_quote(10), operation_key="box", cost_kind="sandbox", role="runtime")
    runtime.pin(box.id, cent_units(box.max_cents))

    started = time.monotonic()
    with pytest.raises(BudgetInsufficientError, match=r"needs up to \$0\.35, but this check's sandboxes hold \$0\.10"):
        runtime.reserve(_quote(35), operation_key="call", cost_kind="model")
    assert time.monotonic() - started < 5


def test_setup_check_waits_for_room_held_by_other_work(engine: Engine) -> None:
    """Coverage the check did not open may settle, so the check waits for it."""
    service = BudgetService(engine=engine)
    budget = service.create("alice", 40, idempotency_key="other-work")
    _reserve(service, budget.id, "other", cost_kind="sandbox", max_cents=10)
    runtime = _setup_runtime(service, budget.id, wait=0)

    with pytest.raises(UsagePendingError):
        runtime.reserve(_quote(35), operation_key="call", cost_kind="model")


def test_setup_check_waits_again_once_its_box_is_unpinned(engine: Engine) -> None:
    """A box that settled no longer counts against the room the check waits for."""
    service = BudgetService(engine=engine)
    budget = service.create("alice", 40, idempotency_key="unpinned")
    runtime = _setup_runtime(service, budget.id, wait=0)
    box = runtime.reserve(_quote(10), operation_key="box", cost_kind="sandbox", role="runtime")
    runtime.pin(box.id, cent_units(box.max_cents))
    runtime.unpin(box.id)

    with pytest.raises(UsagePendingError):
        runtime.reserve(_quote(35), operation_key="call", cost_kind="model")


def test_run_lanes_wait_for_each_other_even_with_pinned_boxes(engine: Engine) -> None:
    """A run's lanes release each other's coverage, so a run never refuses early."""
    service = BudgetService(engine=engine)
    budget = service.create("alice", 40, idempotency_key="run-lanes")
    service.attach_to_job(budget.id, "alice", "lanes-job", expected_revision=budget.revision)
    runtime = _setup_runtime(service, budget.id, phase="run", wait=0)
    box = runtime.reserve(_quote(10), operation_key="box", cost_kind="sandbox", role="runtime")
    runtime.pin(box.id, cent_units(box.max_cents))

    with pytest.raises(UsagePendingError):
        runtime.reserve(_quote(35), operation_key="call", cost_kind="model")


def _settled_run_call(service: BudgetService, budget_id: str, key: str, cents: int, generation: int = 0) -> None:
    """Reserve, dispatch, and settle one run-phase model call.

    Args:
        service: Budget service under test.
        budget_id: Attached run envelope.
        key: Operation identity.
        cents: Actual charge.
        generation: Execution generation of the call.
    """
    operation = _reserve(service, budget_id, key, phase="run", generation=generation, max_cents=cents)
    service.mark_dispatched(operation.id, "alice")
    service.settle(operation.id, "alice", evidence_key=f"{key}-usage", actual_cents=cents, evidence={"usage": key})


def _interrupted_run(engine: Engine, *, grant: int = 0) -> tuple[BudgetService, Any, datetime]:
    """Bill one call before a checkpoint and two after it, then fence the interrupted generation.

    Args:
        engine: Private funded ledger database.
        grant: Free-grant cents alice holds before the run.

    Returns:
        The service, the fenced budget, and the checkpoint time.
    """
    with Session(engine) as session:
        session.get(BillingCustomerModel, "alice").grant_remaining = grant
        session.commit()
    service = BudgetService(engine=engine)
    budget = service.create("alice", 40, idempotency_key=f"restart-{grant}")
    service.attach_to_job(budget.id, "alice", "restart-job", expected_revision=budget.revision)
    _settled_run_call(service, budget.id, "kept", 2)
    time.sleep(0.01)
    checkpoint = datetime.now(UTC)
    time.sleep(0.01)
    _settled_run_call(service, budget.id, "lost-a", 3)
    _settled_run_call(service, budget.id, "lost-b", 4)
    return service, service.fence_generation(budget.id, "alice", expected_generation=0), checkpoint


def test_restart_waiver_returns_only_work_after_the_checkpoint(engine: Engine) -> None:
    """Credit the repeated leg back once, and let the resumed run bill its redo in full."""
    service, budget, checkpoint = _interrupted_run(engine)
    assert _balance(engine) == 41

    waived = service.waive_interrupted_work(budget.id, "alice", generation=0, settled_after=checkpoint)
    again = service.waive_interrupted_work(budget.id, "alice", generation=0, settled_after=checkpoint)
    _settled_run_call(service, budget.id, "redo", 7, generation=1)

    assert waived.billed_cents == again.billed_cents == 2
    assert waived.available_cents == 38
    assert _balance(engine) == 50 - 2 - 7
    with Session(engine) as session:
        rows = session.scalars(
            select(WalletLedgerModel).where(WalletLedgerModel.settlement_key == f"restart:{budget.id}:g0")
        ).all()
    assert [(row.delta_cents, row.from_grant_cents) for row in rows] == [(7, 0)]


def test_restart_waiver_without_checkpoint_returns_the_whole_generation(engine: Engine) -> None:
    """A run that starts again repeats every call of the interrupted generation."""
    service, budget, _ = _interrupted_run(engine)

    waived = service.waive_interrupted_work(budget.id, "alice", generation=0, settled_after=None)

    assert waived.billed_cents == 0
    assert _balance(engine) == 50


def test_restart_waiver_keeps_the_setup_charge(engine: Engine) -> None:
    """A restart repeats run work only; the setup check before the run stays paid."""
    with Session(engine) as session:
        session.get(BillingCustomerModel, "alice").grant_remaining = 4
        session.commit()
    service = BudgetService(engine=engine)
    budget = service.create("alice", 40, idempotency_key="restart-setup")
    setup = _reserve(service, budget.id, "setup-check", max_cents=3)
    service.mark_dispatched(setup.id, "alice")
    service.settle(setup.id, "alice", evidence_key="setup-usage", actual_cents=3, evidence={"usage": "setup"})
    service.attach_to_job(budget.id, "alice", "restart-job", expected_revision=service.get(budget.id, "alice").revision)
    _settled_run_call(service, budget.id, "lost", 5)
    fenced = service.fence_generation(budget.id, "alice", expected_generation=0)

    waived = service.waive_interrupted_work(fenced.id, "alice", generation=0, settled_after=None)

    assert waived.billed_cents == 3
    with Session(engine) as session:
        wallet = session.get(BillingCustomerModel, "alice")
        assert (wallet.grant_remaining, wallet.balance_cents) == (1, 50)


def test_restart_waiver_returns_grant_cents_to_the_grant(engine: Engine) -> None:
    """A waiver never turns free-grant cents into paid balance."""
    service, budget, checkpoint = _interrupted_run(engine, grant=4)
    with Session(engine) as session:
        wallet = session.get(BillingCustomerModel, "alice")
        assert (wallet.grant_remaining, wallet.balance_cents) == (0, 45)

    service.waive_interrupted_work(budget.id, "alice", generation=0, settled_after=checkpoint)

    with Session(engine) as session:
        wallet = session.get(BillingCustomerModel, "alice")
        assert (wallet.grant_remaining, wallet.balance_cents) == (2, 50)
