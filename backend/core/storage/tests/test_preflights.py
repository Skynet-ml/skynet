"""Verify setup leases, fenced recovery, and evidence reuse against the real ledger."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from core.billing.budgets import BudgetConflictError, BudgetFencedError
from core.storage.models import Base, BillingCustomerModel
from core.storage.preflights import PreflightClaim, PreflightStore, WizardPreflightModel, verification_fingerprint

PAYLOAD = {"seed_candidate": "real seed", "scorer": {"kind": "python", "metric_code": "def score(c): return 1"}}
SUCCESS = {
    "checks": [{"key": "scorer", "status": "succeeded"}],
    "workflow_result": {"ok": True, "node_results": [{"id": "actual"}]},
}


@pytest.fixture
def store(tmp_path: Path) -> Iterator[PreflightStore]:
    """Provide a funded private ledger with the current setup schema.

    Args:
        tmp_path: Test-owned database directory.

    Yields:
        Setup store with no model, sandbox, or Stripe connection.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'preflight.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username="alice", stripe_customer_id="fixture", balance_cents=50, grant_remaining=0)
        )
        session.commit()
    yield PreflightStore(engine)
    engine.dispose()


def _budget(store: PreflightStore) -> str:
    """Create the same funded envelope used throughout a setup and its retries.

    Args:
        store: Private setup and ledger fixture.

    Returns:
        Shared budget ID.
    """
    return store.budgets.create("alice", 20, idempotency_key="draft").id


def _claim(store: PreflightStore, budget_id: str, payload: dict[str, Any] | None = None) -> PreflightClaim:
    """Claim one exact setup configuration.

    Args:
        store: Setup fixture.
        budget_id: Existing draft authority.
        payload: Optional changed executable inputs.

    Returns:
        Current evidence and exclusive execution token, if admitted.
    """
    return store.claim(
        username="alice",
        budget_id=budget_id,
        revision=1,
        workflow="anything",
        scope="execution",
        payload=payload or PAYLOAD,
    )


def _expire(store: PreflightStore, identity: str) -> None:
    """Simulate the owner disappearing beyond its lease without sleeping.

    Args:
        store: Setup fixture.
        identity: Existing running attempt.
    """
    with Session(store.engine) as session:
        session.get(WizardPreflightModel, identity).lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()


def _reserve(store: PreflightStore, budget_id: str, generation: int):
    """Admit a deterministic paid attempt without dispatching any provider request.

    Args:
        store: Setup fixture.
        budget_id: Shared draft budget.
        generation: Setup owner's execution generation.

    Returns:
        Authoritative covered attempt.
    """
    return store.budgets.reserve(
        budget_id,
        "alice",
        operation_key="model-check",
        generation=generation,
        phase="setup",
        cost_kind="model",
        request_fingerprint="fixed-request",
        price_snapshot={"version": "fixture"},
        max_cents=2,
    )


def test_concurrent_claim_has_one_current_owner(store: PreflightStore) -> None:
    """Give identical overlapping Continue requests one shared execution owner.

    Args:
        store: Private setup fixture.
    """
    budget_id = _budget(store)
    barrier = threading.Barrier(2)

    def claim(_: int) -> PreflightClaim:
        """Start both requests before either acquires the budget lock.

        Args:
            _: Unused executor input.

        Returns:
            Current claim outcome.
        """
        barrier.wait(timeout=5)
        return _claim(store, budget_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(claim, range(2)))
    assert len({claim.document["id"] for claim in claims}) == 1
    assert sum(claim.token is not None for claim in claims) == 1
    assert all("claim_token" not in claim.document for claim in claims)


def test_expired_owner_is_fenced_before_unstarted_work_is_replaced(store: PreflightStore) -> None:
    """Revoke old admission and release only never-dispatched work before replacement.

    Args:
        store: Private setup fixture.
    """
    budget_id = _budget(store)
    first = _claim(store, budget_id)
    operation = _reserve(store, budget_id, first.generation)
    _expire(store, first.document["id"])
    second = _claim(store, budget_id)
    assert second.token is not None
    assert second.token != first.token
    assert second.generation == first.generation + 1
    assert store.budgets.get_operation(operation.id, "alice").state == "released"
    with pytest.raises(BudgetFencedError):
        _reserve(store, budget_id, first.generation)
    with pytest.raises(BudgetConflictError):
        store.finish(first.document["id"], claim_token=first.token, status="succeeded", result=SUCCESS)


@pytest.mark.parametrize("changed", [False, True])
def test_dispatched_work_blocks_expired_owner_replay_until_settlement(store: PreflightStore, changed: bool) -> None:
    """Keep interrupted paid work covered before retrying the same or changed inputs.

    Args:
        store: Private setup fixture.
        changed: Whether the replacement also changes executable inputs.
    """
    budget_id = _budget(store)
    first = _claim(store, budget_id)
    operation = _reserve(store, budget_id, first.generation)
    store.budgets.mark_dispatched(operation.id, "alice", "provider-request")
    _expire(store, first.document["id"])
    payload = {**PAYLOAD, "seed_candidate": "changed seed"} if changed else PAYLOAD
    pending = _claim(store, budget_id, payload)
    assert pending.token is None
    assert pending.document["status"] == "pending"
    assert pending.document["may_advance"] is False
    assert pending.document["pending_reason"]["category"] == "usage_reconciliation"
    assert store.budgets.get(budget_id, "alice").reserved_cents == 2
    assert not store.renew(first.document["id"], claim_token=first.token)
    store.budgets.settle(operation.id, "alice", evidence_key="final", actual_cents="0.2", evidence={"actual": True})
    retry = _claim(store, budget_id, payload)
    assert retry.token is not None
    assert retry.generation == pending.generation
    assert retry.document["status"] == "pending"
    with pytest.raises(BudgetConflictError):
        store.require_current(
            username="alice",
            budget_id=budget_id,
            identity=retry.document["id"],
            fingerprint=retry.document["fingerprint"],
            workflow="anything",
            payload=payload,
        )
    finished = store.finish(retry.document["id"], claim_token=retry.token, status="succeeded", result=SUCCESS)
    assert finished["status"] == "succeeded"
    store.require_current(
        username="alice",
        budget_id=budget_id,
        identity=finished["id"],
        fingerprint=finished["fingerprint"],
        workflow="anything",
        payload=payload,
    )


def test_confirmed_usage_unblocks_only_genuine_completed_evidence(store: PreflightStore) -> None:
    """Reuse actual workflow/scorer success once its late usage settles without rerunning it.

    Args:
        store: Private setup fixture.
    """
    budget_id = _budget(store)
    claim = _claim(store, budget_id)
    operation = _reserve(store, budget_id, claim.generation)
    store.budgets.mark_dispatched(operation.id, "alice")
    result = {
        **SUCCESS,
        "scorer_result": {"ok": True, "score": 0.75},
        "checks": [*SUCCESS["checks"], {"key": "usage", "status": "pending"}],
    }
    assert (
        store.finish(claim.document["id"], claim_token=claim.token, status="pending", result=result)["status"]
        == "pending"
    )
    assert _claim(store, budget_id).token is None
    store.budgets.settle(operation.id, "alice", evidence_key="final", actual_cents="0.3", evidence={"actual": True})
    reused = _claim(store, budget_id)
    assert reused.token is None
    assert reused.document["status"] == "succeeded"
    assert reused.document["may_advance"] is True
    assert "pending_reason" not in reused.document
    assert reused.document["workflow_result"] == SUCCESS["workflow_result"]
    assert reused.document["scorer_result"]["score"] == 0.75
    assert all(check["status"] == "succeeded" for check in reused.document["checks"])


@pytest.mark.parametrize(
    "checks",
    [
        [{"key": "usage", "status": "pending"}],
        [{"key": "scorer", "status": "failed"}, {"key": "usage", "status": "pending"}],
    ],
)
def test_settlement_cannot_manufacture_success_for_unperformed_checks(
    store: PreflightStore, checks: list[dict[str, str]]
) -> None:
    """Require real execution when the old record contains only billing or a failed check.

    Args:
        store: Private setup fixture.
        checks: Incomplete or failed previous attempt.
    """
    budget_id = _budget(store)
    first = _claim(store, budget_id)
    store.finish(first.document["id"], claim_token=first.token, status="pending", result={"checks": checks})
    retry = _claim(store, budget_id)
    assert retry.token is not None
    assert retry.token != first.token
    assert retry.document["status"] == "pending"
    with pytest.raises(BudgetConflictError):
        store.finish(first.document["id"], claim_token=first.token, status="succeeded", result=SUCCESS)


def test_heartbeat_keeps_live_owner_and_stops_cleanly_after_finish(
    store: PreflightStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Renew through a real short lease and preserve a completed result inside the context.

    Args:
        store: Private setup fixture.
        monkeypatch: Wraps renew so the test waits on real renewals instead of fixed sleeps.
    """
    # SKY-404: a 0.3 s lease with fixed sleeps expired on slow CI before the first renewal.
    store.lease_seconds = 1.5
    renew = store.renew
    renewed_past_claim_lease = threading.Event()
    renewed_after_finish = threading.Event()
    claim_lease_ends = math.inf
    finished_at = math.inf

    def recording_renew(identity: str, *, claim_token: str) -> bool:
        """Delegate to the real renew and signal the renewals the test waits for.

        Args:
            identity: Claimed setup record.
            claim_token: Private current-owner token.

        Returns:
            The real renew result.
        """
        started = time.monotonic()
        renewed = renew(identity, claim_token=claim_token)
        if renewed and time.monotonic() > claim_lease_ends:
            renewed_past_claim_lease.set()
        # A renewal that started before finish returned can commit while the row is still running.
        if renewed and started > finished_at:
            renewed_after_finish.set()
        return renewed

    monkeypatch.setattr(store, "renew", recording_renew)
    budget_id = _budget(store)
    claim = _claim(store, budget_id)
    claim_lease_ends = time.monotonic() + store.lease_seconds
    with store.heartbeat(claim.document["id"], claim_token=claim.token):
        assert renewed_past_claim_lease.wait(timeout=4)
        assert _claim(store, budget_id).token is None
        finished = store.finish(claim.document["id"], claim_token=claim.token, status="succeeded", result=SUCCESS)
        finished_at = time.monotonic()
        assert renewed_after_finish.wait(timeout=4)
    assert finished["status"] == "succeeded"
    with Session(store.engine) as session:
        row = session.get(WizardPreflightModel, claim.document["id"])
        assert row.attempt == 1
        assert row.status == "succeeded"


def test_stale_or_foreign_evidence_does_not_authorize_attachment(store: PreflightStore) -> None:
    """Require the actual current execution fingerprint immediately before submission.

    Args:
        store: Private setup fixture.
    """
    budget_id = _budget(store)
    claim = _claim(store, budget_id)
    with pytest.raises(BudgetConflictError, match="verification evidence"):
        store.finish(claim.document["id"], claim_token=claim.token, status="succeeded", result={"checks": []})
    with pytest.raises(BudgetConflictError):
        store.finish(claim.document["id"], claim_token="wrong-owner", status="succeeded", result=SUCCESS)
    result = store.finish(claim.document["id"], claim_token=claim.token, status="succeeded", result=SUCCESS)
    for username, payload in [("alice", {**PAYLOAD, "seed_candidate": "changed"}), ("bob", PAYLOAD)]:
        with pytest.raises(BudgetConflictError):
            store.require_current(
                username=username,
                budget_id=budget_id,
                identity=result["id"],
                fingerprint=result["fingerprint"],
                workflow="anything",
                payload=payload,
            )


def test_unified_evaluation_fingerprint_tracks_code_and_sample_count(store: PreflightStore) -> None:
    """A unified contract check's evidence changes with its code and sample count, so a stale pass is never reused."""
    base = {
        "payload_kind": "unified_v1",
        "task": {"objective": "o"},
        "workspace": {"run_code": "a", "metric_code": "m"},
        "name": "cosmetic",
    }

    def digest(payload: dict[str, Any]) -> str:
        """Fingerprint a unified evaluation payload.

        Args:
            payload: Canonical unified inputs.

        Returns:
            The evidence fingerprint.
        """
        with Session(store.engine) as session:
            return verification_fingerprint(
                session, username="alice", workflow="unified", scope="evaluation", payload=payload
            )

    assert digest(base) == digest({**base, "name": "renamed"})
    assert digest(base) != digest({**base, "workspace": {"run_code": "b", "metric_code": "m"}})
    assert digest(base) != digest({**base, "contract_sample_count": 5})


def test_evaluation_pass_is_not_reused_after_the_limit_changes(store: PreflightStore) -> None:
    """A contract check that passed under one spending limit runs again under a new one.

    Args:
        store: Private setup fixture.
    """
    budget_id = _budget(store)

    def claim(revision: int) -> PreflightClaim:
        """Claim the same unified evaluation under the budget's current revision.

        Args:
            revision: Budget revision the caller holds.

        Returns:
            Current evidence and token, if admitted.
        """
        return store.claim(
            username="alice",
            budget_id=budget_id,
            revision=revision,
            workflow="unified",
            scope="evaluation",
            payload=PAYLOAD,
        )

    first = claim(1)
    store.finish(first.document["id"], claim_token=first.token, status="succeeded", result=SUCCESS)
    assert claim(1).token is None

    lowered = store.budgets.update_total(budget_id, "alice", 10, expected_revision=1)
    assert claim(lowered.revision).token is not None
