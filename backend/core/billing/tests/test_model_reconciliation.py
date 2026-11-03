"""Verify uncertain model usage settles once from the original provider receipt."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.billing.model_dispatch import OpenRouterDispatcher, generation_charge
from core.billing.model_reconciliation import OpenRouterUsageReconciler
from core.billing.openrouter_quotes import price_text_request, resolve_model_slug
from core.billing.operation_pricing import ChargePolicy, json_fingerprint
from core.billing.runtime import PaidResult, UsagePendingError
from core.billing.tests.test_protected_dispatch import CATALOG, REQUEST, _runtime
from core.billing.tests.test_protected_dispatch import database as _database_fixture
from core.config import settings
from core.storage.models import ExecutionOperationModel, ExecutionUsageEvidenceModel

database = _database_fixture


def test_historical_byok_snapshot_reconciles_at_five_percent(database) -> None:
    """Keep the fee stored in a historical operation when its receipt arrives."""
    runtime = _runtime(database)
    policy = ChargePolicy("byok_model", byok_fee_fraction=Decimal("0.05"))
    quote = price_text_request(REQUEST, CATALOG, policy).quote
    quote = replace(
        quote, price_snapshot={**quote.price_snapshot, "credential_fingerprint": json_fingerprint("private")}
    )
    with pytest.raises(UsagePendingError):
        runtime.execute(
            quote,
            policy,
            lambda: PaidResult(
                None, None, {"provider": "openrouter", "model": "fixture/text"}, provider_request_id="historical"
            ),
            operation_key="historical",
            cost_kind="model",
            role="task",
        )
    with Session(database) as session:
        operation_id = session.scalar(select(ExecutionOperationModel.id))

    def provider(request: httpx.Request) -> httpx.Response:
        """Return the original generation's final external cost."""
        return httpx.Response(
            200,
            json={
                "data": {
                    "id": "historical",
                    "model": "fixture/text",
                    "total_cost": "0",
                    "upstream_inference_cost": "0.004",
                    "is_byok": True,
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        result = OpenRouterUsageReconciler(runtime.service, lambda owner, digest: "private").reconcile(
            operation_id, "alice", client=client
        )
    assert result.state == "settled"
    snapshot = runtime.service.get(runtime.budget_id, "alice")
    assert snapshot.setup_spent_cents == Decimal("0.02")
    assert snapshot.reserved_cents == 0


@pytest.fixture(autouse=True)
def _at_cost_markup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the usage markup to 1.0 so these tests check settlement arithmetic, not pricing policy."""
    monkeypatch.setattr(settings, "usage_markup", 1.0)


def test_truncated_stream_retains_charge_until_original_generation_settles(database) -> None:
    """Reject an early stream cost and later settle the exact generation without another POST."""
    runtime = _runtime(database)
    calls = []
    confirmed = False

    def provider(request: httpx.Request) -> httpx.Response:
        """Return early usage followed by a delayed authoritative billing receipt."""
        calls.append(request)
        if request.url.path.endswith("/endpoints"):
            return httpx.Response(200, json={"data": CATALOG})
        if request.method == "POST":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b'data: {"id":"gen-held","usage":{"cost":0}}\n\n',
            )
        if not confirmed:
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                "data": {
                    "id": "gen-held",
                    "model": "fixture/text",
                    "total_cost": "0.004",
                    "is_byok": False,
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        dispatcher = OpenRouterDispatcher(
            runtime,
            api_key="private",
            model="fixture/text",
            role="task",
            policy=ChargePolicy("managed_model"),
            client=client,
        )
        with pytest.raises(UsagePendingError):
            dispatcher.dispatch("/chat/completions", {**REQUEST, "stream": True})
        with Session(database) as session:
            operation = session.scalar(select(ExecutionOperationModel))
            operation_id = operation.id
        snapshot = runtime.service.get(runtime.budget_id, "alice")
        assert snapshot.setup_spent_cents == 0
        assert snapshot.pending_operations == 1
        assert snapshot.reserved_cents > 0
        reconciler = OpenRouterUsageReconciler(runtime.service, lambda owner, digest: "private")
        confirmed = True
        assert reconciler.reconcile(operation_id, "alice", client=client).state == "settled"
        read_count = len(calls)
        reconciler.reconcile(operation_id, "alice", client=client)
        assert len(calls) == read_count
    snapshot = runtime.service.get(runtime.budget_id, "alice")
    assert snapshot.setup_spent_cents == Decimal("0.4")
    assert snapshot.reserved_cents == 0
    assert len([call for call in calls if call.method == "POST"]) == 1


def test_rotated_credential_does_not_query_or_release_original_hold(database) -> None:
    """Keep uncertain usage covered when its original provider credential is unavailable."""
    runtime = _runtime(database)

    def pending_provider(request: httpx.Request) -> httpx.Response:
        """Return one paid generation with no available final usage yet."""
        if request.url.path.endswith("/endpoints"):
            return httpx.Response(200, json={"data": CATALOG})
        if request.method == "POST":
            return httpx.Response(200, json={"id": "gen-original"})
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(pending_provider)) as client:
        dispatcher = OpenRouterDispatcher(
            runtime,
            api_key="original",
            model="fixture/text",
            role="judge",
            policy=ChargePolicy("managed_model"),
            client=client,
        )
        with pytest.raises(UsagePendingError):
            dispatcher.dispatch("/chat/completions", REQUEST)
    with Session(database) as session:
        operation_id = session.scalar(select(ExecutionOperationModel.id))

    def forbidden(request: httpx.Request) -> httpx.Response:
        """Fail if reconciliation attempts to use an unrelated provider credential."""
        pytest.fail("Rotated credentials must not be used for an old generation.")

    with httpx.Client(transport=httpx.MockTransport(forbidden)) as client:
        reconciler = OpenRouterUsageReconciler(runtime.service, lambda owner, digest: "rotated")
        with pytest.raises(UsagePendingError):
            reconciler.reconcile(operation_id, "alice", client=client)
    assert runtime.service.get(runtime.budget_id, "alice").pending_operations == 1


def test_refused_request_releases_its_hold_instead_of_waiting_for_a_receipt(database) -> None:
    """Return coverage when the provider's complete error answer names no generation."""
    runtime = _runtime(database)

    def refusing_provider(request: httpx.Request) -> httpx.Response:
        """Rate-limit the only POST and fail if anything tries to look a generation up."""
        if request.url.path.endswith("/endpoints"):
            return httpx.Response(200, json={"data": CATALOG})
        if request.method == "POST":
            return httpx.Response(429, json={"error": {"message": "Rate limit exceeded", "code": 429}})
        pytest.fail("A refusal without a generation must not be looked up.")

    with httpx.Client(transport=httpx.MockTransport(refusing_provider)) as client:
        dispatcher = OpenRouterDispatcher(
            runtime,
            api_key="private",
            model="fixture/text",
            role="task",
            policy=ChargePolicy("managed_model"),
            client=client,
        )
        response = dispatcher.dispatch("/chat/completions", REQUEST)
    assert response.status == 429
    snapshot = runtime.service.get(runtime.budget_id, "alice")
    assert (snapshot.pending_operations, snapshot.reserved_cents, snapshot.setup_spent_cents) == (0, 0, 0)
    with Session(database) as session:
        assert session.scalar(select(ExecutionOperationModel.state)) == "released"
        evidence = session.scalar(select(ExecutionUsageEvidenceModel))
        assert (evidence.issue, evidence.final, evidence.evidence["status"]) == ("rejected", True, 429)


def test_sweep_releases_a_refusal_that_earlier_runtimes_parked_as_pending(database) -> None:
    """Settle nothing and return coverage from a stored refusal without any provider credential."""
    runtime = _runtime(database)
    policy = ChargePolicy("managed_model")
    quote = price_text_request(REQUEST, CATALOG, policy).quote
    refusal = {
        "provider": "openrouter",
        "model": "fixture/text",
        "request_id": None,
        "status": 429,
        "usage": None,
        "interrupted": False,
    }
    with pytest.raises(UsagePendingError):
        runtime.execute(
            quote,
            policy,
            lambda: PaidResult(value=None, provider_usd=None, evidence=refusal),
            operation_key="parked",
            cost_kind="model",
            role="task",
        )
    assert runtime.service.get(runtime.budget_id, "alice").pending_operations == 1
    reconciler = OpenRouterUsageReconciler(runtime.service, lambda owner, digest: None)
    page = reconciler.sweep(limit=4)
    assert [result.state for result in page.results] == ["released"]
    snapshot = runtime.service.get(runtime.budget_id, "alice")
    assert (snapshot.pending_operations, snapshot.reserved_cents) == (0, 0)
    assert reconciler.sweep(limit=4).results == ()


@pytest.mark.parametrize(
    ("is_byok", "upstream", "expected"),
    [
        (True, "0.01", Decimal("0.0105")),
        (False, "0.01", Decimal("0.0005")),
        (None, "0.01", None),
        (True, None, None),
        (None, None, Decimal("0.0005")),
    ],
)
def test_provider_fee_and_external_cost_are_not_double_counted(is_byok, upstream, expected) -> None:
    """Use explicit provider attribution rather than guessing an external bill's scope."""
    assert (
        generation_charge({"total_cost": "0.0005", "is_byok": is_byok, "upstream_inference_cost": upstream}) == expected
    )


def test_legacy_model_alias_requires_a_unique_exact_catalog_match() -> None:
    """Preserve old short identifiers without choosing a different or ambiguous model."""
    rows = [{"id": "openai/gpt-4o-mini"}]
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": rows}))
    ) as client:
        assert resolve_model_slug("gpt-4o-mini", client=client) == "openai/gpt-4o-mini"
        rows.append({"id": "other/gpt-4o-mini"})
        with pytest.raises(ValueError, match="ambiguous"):
            resolve_model_slug("gpt-4o-mini", client=client)
