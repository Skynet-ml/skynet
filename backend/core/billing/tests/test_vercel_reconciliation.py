"""Verify interrupted Vercel billing recovers exact receipts without new paid work."""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from core.billing import vercel_usage
from core.billing.budgets import BudgetNotFoundError, BudgetService, OperationSnapshot
from core.billing.operation_pricing import ChargePolicy
from core.billing.runtime import UsagePendingError
from core.billing.vercel_reconciliation import SessionNotFoundError, VercelSessionUsageClient, VercelUsageReconciler
from core.billing.vercel_usage import quote_vercel_sandbox, vercel_actual_usd
from core.config import settings
from core.storage.models import Base, BillingCustomerModel

from .test_vercel_usage import CREATE, RECEIPT


@pytest.fixture(autouse=True)
def _at_cost_markup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the usage markup to 1.0 so these tests check settlement arithmetic, not pricing policy."""
    monkeypatch.setattr(settings, "usage_markup", 1.0)


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[BudgetService]:
    """Create a private funded wallet for reconciliation-only tests.

    Args:
        tmp_path: Test-owned directory.

    Yields:
        Ledger with no real provider or Stripe connection.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'reconciliation.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username="alice", stripe_customer_id="fixture", balance_cents=100, grant_remaining=0)
        )
        session.commit()
    yield BudgetService(engine=engine)
    engine.dispose()


def _admit(ledger: BudgetService, *, key: str = "draft", session_id: str | None = "session-one") -> OperationSnapshot:
    """Leave a realistically quoted sandbox attempt awaiting its final usage.

    Args:
        ledger: Test ledger.
        key: Distinct budget creation identity.
        session_id: Provider identity, absent when creation's response was lost.

    Returns:
        Dispatched covered attempt.
    """
    budget = ledger.create("alice", 20, idempotency_key=key)
    quote = quote_vercel_sandbox(CREATE)
    operation = ledger.reserve(
        budget.id,
        "alice",
        operation_key="sandbox",
        generation=0,
        phase="setup",
        cost_kind="sandbox",
        request_fingerprint=quote.request_fingerprint,
        price_snapshot=quote.price_snapshot,
        max_cents=quote.maximum.total,
    )
    return ledger.mark_dispatched(operation.id, "alice", session_id)


def _unavailable(session_id: str) -> Mapping[str, Any]:
    """Fail if a recovery path unexpectedly contacts an already-destroyed provider resource.

    Args:
        session_id: Unexpected provider identity.

    Raises:
        AssertionError: Always, since durable receipts should suffice.
    """
    raise AssertionError(f"Unexpected provider request for {session_id}")


def test_durable_receipt_settles_after_fencing_at_original_prices(
    ledger: BudgetService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recover once from saved evidence even after provider destruction and price changes.

    Args:
        ledger: Private ledger fixture.
        monkeypatch: Test-only current price override.
    """
    operation = _admit(ledger)
    ledger.mark_pending(
        operation.id, "alice", evidence_key="saved-stop", evidence={"sessions": {"session-one": RECEIPT}}
    )
    ledger.fence_generation(operation.budget_id, "alice", expected_generation=0)
    expected = ChargePolicy("sandbox").convert(vercel_actual_usd(RECEIPT, session_id="session-one", vcpus=2)).total
    monkeypatch.setattr(vercel_usage, "_REGIONAL_RATES", {"iad1": (Decimal(9), Decimal(9))})
    monkeypatch.setattr(settings, "usage_markup", 9.0)
    reconcile = VercelUsageReconciler(ledger, _unavailable)
    first = reconcile.reconcile(operation.id, "alice")
    replay = reconcile.reconcile(operation.id, "alice")
    assert first == replay
    assert first.state == "settled"
    assert first.actual_cents == expected
    assert first.budget.reserved_cents == 0
    assert first.budget.billed_cents == 1


def test_missing_final_metrics_can_arrive_after_stop(ledger: BudgetService) -> None:
    """Refresh incomplete durable stop evidence instead of retaining it forever.

    Args:
        ledger: Private ledger fixture.
    """
    operation = _admit(ledger)
    incomplete = {**RECEIPT, "activeCpuDurationMs": None}
    ledger.mark_pending(
        operation.id, "alice", evidence_key="incomplete", evidence={"sessions": {"session-one": incomplete}}
    )
    identities = []

    def fetch(session_id: str) -> Mapping[str, Any]:
        """Return final provider evidence for the same admitted session.

        Args:
            session_id: Captured identity.

        Returns:
            Complete stopped-session receipt.
        """
        identities.append(session_id)
        return RECEIPT

    assert VercelUsageReconciler(ledger, fetch).reconcile(operation.id, "alice").state == "settled"
    assert identities == ["session-one"]


def test_control_plane_transfer_settles_from_stored_stop_evidence(ledger: BudgetService) -> None:
    """Settle a deny-all sandbox whose stored stop receipt reports control-plane bytes without a provider call.

    Args:
        ledger: Private ledger fixture.
    """
    operation = _admit(ledger)
    receipt = {**RECEIPT, "networkTransfer": {"ingress": 21_124, "egress": 7_548}}
    ledger.mark_pending(operation.id, "alice", evidence_key="stop", evidence={"sessions": {"session-one": receipt}})
    settled = VercelUsageReconciler(ledger, _unavailable).reconcile(operation.id, "alice")
    assert settled.state == "settled"
    expected = ChargePolicy("sandbox").convert(vercel_actual_usd(RECEIPT, session_id="session-one", vcpus=2)).total
    assert settled.actual_cents == expected
    assert settled.budget.reserved_cents == 0
    assert settled.budget.billed_cents == 1


def test_unfinished_runtime_retains_full_coverage(ledger: BudgetService) -> None:
    """Never turn an unfinished runtime into guessed charges or released holds.

    Args:
        ledger: Private ledger fixture.
    """
    operation = _admit(ledger)
    receipt = {**RECEIPT, "status": "running"}
    with pytest.raises(UsagePendingError):
        VercelUsageReconciler(ledger, lambda session_id: receipt).reconcile(operation.id, "alice")
    current = ledger.get_operation(operation.id, "alice")
    assert current.state == "pending"
    assert current.actual_cents == 0
    assert current.budget.reserved_cents == operation.max_cents
    assert current.budget.billed_cents == 0
    assert ledger.get_reconciliation(operation.id, "alice").evidence


def test_missing_identity_and_wrong_owner_never_issue_provider_calls(ledger: BudgetService) -> None:
    """Keep lost creation identities covered and do not expose another owner's receipts.

    Args:
        ledger: Private ledger fixture.
    """
    operation = _admit(ledger, session_id=None)
    reconcile = VercelUsageReconciler(ledger, _unavailable)
    with pytest.raises(BudgetNotFoundError):
        reconcile.reconcile(operation.id, "bob")
    with pytest.raises(UsagePendingError, match="exact session identity"):
        reconcile.reconcile(operation.id, "alice")
    assert ledger.get(operation.budget_id, "alice").reserved_cents == operation.max_cents


def test_concurrent_reconciliation_settles_once(ledger: BudgetService) -> None:
    """Allow concurrent sweepers without duplicate wallet charges.

    Args:
        ledger: Private ledger fixture.
    """
    operation = _admit(ledger)
    barrier = threading.Barrier(2)

    def fetch(session_id: str) -> Mapping[str, Any]:
        """Make both reconcilers observe pending state before returning the same receipt.

        Args:
            session_id: Admitted identity.

        Returns:
            Final stopped receipt.
        """
        assert session_id == "session-one"
        barrier.wait(timeout=5)
        return RECEIPT

    reconcile = VercelUsageReconciler(ledger, fetch)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(reconcile.reconcile, operation.id, "alice") for _ in range(2)]
        assert all(future.result().state == "settled" for future in futures)
    assert ledger.get(operation.budget_id, "alice").billed_cents == 1


def test_sweep_pages_past_unrecoverable_creation(ledger: BudgetService) -> None:
    """Advance a bounded sweep so an unresolved old operation cannot starve later ones.

    Args:
        ledger: Private ledger fixture.
    """
    lost = _admit(ledger, key="lost", session_id=None)
    final = _admit(ledger, key="final")
    reconcile = VercelUsageReconciler(ledger, lambda session_id: RECEIPT)
    cursor = None
    results = []
    for _ in range(3):
        page = reconcile.sweep(limit=1, after_id=cursor)
        results.extend(page.results)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert {result.operation_id for result in results} == {lost.id, final.id}
    assert ledger.get_operation(final.id, "alice").state == "settled"
    assert ledger.get_operation(lost.id, "alice").state == "dispatched"


def test_provider_client_only_reads_exact_session_and_strips_secrets() -> None:
    """Use one authenticated metadata GET with no creation, resume, or retry path."""
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        """Return a raw provider receipt containing unrelated secret fields.

        Args:
            request: Captured authenticated metadata request.

        Returns:
            Raw provider response.
        """
        requests.append(request)
        return httpx.Response(200, json={"session": {**RECEIPT, "unrelated_secret": "never-persist"}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert VercelSessionUsageClient("parent-token", "team", client=client)("session-one") == {
            key: value for key, value in RECEIPT.items() if key in vercel_usage._SESSION_FIELDS
        }
    [request] = requests
    assert request.method == "GET"
    assert request.url.path == "/api/v2/sandboxes/sessions/session-one"
    assert request.url.params["teamId"] == "team"
    assert request.headers["Authorization"] == "Bearer parent-token"


@pytest.mark.parametrize(
    ("respond", "expected"),
    [
        (lambda request: httpx.Response(404, json={"error": "gone"}), "(HTTP 404)"),
        (lambda request: httpx.Response(401, json={"error": "denied"}), "(HTTP 401)"),
        (lambda request: (_ for _ in ()).throw(httpx.ConnectTimeout("timed out", request=request)), "(ConnectTimeout)"),
        (lambda request: httpx.Response(200, content=b"not json"), "(JSONDecodeError)"),
        (lambda request: httpx.Response(200, json={"session": {"id": "other"}}), "(ValueError)"),
    ],
)
def test_provider_client_pending_reason_names_failure_without_secrets(respond: Any, expected: str) -> None:
    """Name the failure kind in the pending reason, but never the URL, team, session or token.

    Args:
        respond: Fake provider behavior for one request.
        expected: Failure label the reason must carry.
    """
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        reader = VercelSessionUsageClient("secret-token-value", "team-secret-id", client=client)
        with pytest.raises(UsagePendingError) as caught:
            reader("session-secret-id")
    reason = str(caught.value)
    assert reason.startswith("Vercel final usage remains unavailable")
    assert expected in reason
    for secret in ("secret-token-value", "team-secret-id", "session-secret-id", "vercel.com"):
        assert secret not in reason


def _gone(request: httpx.Request) -> httpx.Response:
    """Answer like Vercel for a session of a destroyed sandbox.

    Args:
        request: The session read.

    Returns:
        A 404 answer.
    """
    return httpx.Response(404, json={"error": "gone"})


@pytest.mark.parametrize("status", [401, 500])
def test_provider_client_raises_session_not_found_only_for_a_404(status: int) -> None:
    """Give a 404 its own typed error, and keep every other failure a plain pending error.

    Args:
        status: Another failure status.
    """
    with httpx.Client(transport=httpx.MockTransport(_gone)) as client, pytest.raises(SessionNotFoundError):
        VercelSessionUsageClient("token", "team", client=client)("session-one")
    failing = httpx.MockTransport(lambda request: httpx.Response(status))
    with httpx.Client(transport=failing) as client, pytest.raises(UsagePendingError) as caught:
        VercelSessionUsageClient("token", "team", client=client)("session-one")
    assert not isinstance(caught.value, SessionNotFoundError)


def test_sweep_reports_a_missing_session_as_pending_with_the_same_reason(ledger: BudgetService) -> None:
    """Keep a 404 session pending in the sweep, with the usual reason text.

    Args:
        ledger: Private ledger fixture.
    """
    operation = _admit(ledger)
    with httpx.Client(transport=httpx.MockTransport(_gone)) as client:
        page = VercelUsageReconciler(ledger, VercelSessionUsageClient("token", "team", client=client)).sweep()
    [result] = page.results
    assert (result.operation_id, result.state) == (operation.id, "pending")
    assert result.reason == "Vercel final usage remains unavailable (HTTP 404); its reservation is retained."
    assert ledger.get_operation(operation.id, "alice").state == "dispatched"
