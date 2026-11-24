"""Prove the protected OpenRouter Decisions API dispatch reserves first and settles after (SKY-706)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

from core.billing.budgets import AccountFundsError, BudgetService
from core.billing.model_dispatch import OpenRouterDispatcher
from core.billing.openrouter_quotes import price_decision_request
from core.billing.operation_pricing import ChargePolicy, UnpricedOperationError
from core.billing.runtime import BudgetRuntime
from core.config import settings
from core.storage.models import Base, BillingCustomerModel, ExecutionOperationModel

MODEL = "typesafe/jev-1.14"
# Shaped like the live /models/typesafe/jev-1.14/endpoints answer.
CATALOG = {
    "id": MODEL,
    "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["decisions"]},
    "endpoints": [
        {
            "tag": "typesafe",
            "provider_name": "TypeSafe",
            "context_length": 262144,
            "pricing": {"prompt": "0.000000042", "completion": "0", "discount": 0},
        }
    ],
}
REQUEST = {
    "model": MODEL,
    "state": {"user_latest_request": "Rename my run", "tool_call": {"name": "rename", "args": {"id": "j1"}}},
    "questions": {"verdict": {"type": "choice", "instructions": "Decide.", "criteria": {"allow": None, "deny": None}}},
}
ANSWER = {
    "model": MODEL,
    "answers": {"verdict": {"type": "choice", "choice": "allow", "confidence": 0.9}},
    "usage": {"input_tokens": 120, "output_tokens": 0, "cost": "0.00002"},
    "id": "gen-dec-1791209504-cXRBLHSCBmsjQX93jkWe",
    "provider": "TypeSafe",
}


@pytest.fixture(autouse=True)
def _at_cost_markup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the usage markup to 1.0 so these tests check settlement arithmetic, not pricing policy.

    Args:
        monkeypatch: Sets the markup.
    """
    monkeypatch.setattr(settings, "usage_markup", 1.0)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Engine]:
    """Keep wallet and operation evidence in a private test database.

    Args:
        tmp_path: Private database directory.

    Yields:
        The engine, with 100 cents for ``alice``.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'decisions.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username="alice", stripe_customer_id="fixture", balance_cents=100, grant_remaining=0)
        )
        session.commit()
    yield engine
    engine.dispose()


def _runtime(database: Engine, total: int = 50) -> BudgetRuntime:
    """Create a setup authority with real durable admission and settlement.

    Args:
        database: The test database.
        total: The budget in cents.

    Returns:
        The runtime.
    """
    service = BudgetService(engine=database)
    budget = service.create("alice", total, idempotency_key="setup")
    return BudgetRuntime(service, username="alice", budget_id=budget.id, generation=0, phase="setup", wait_timeout=0)


def _maximum_usd(request: dict[str, Any]) -> Decimal:
    """Compute the expected reservation: UTF-8 bytes plus the wrapper allowance, at the prompt rate.

    Args:
        request: The Decisions API body.

    Returns:
        The maximum provider USD, with the 5% fee margin.
    """
    size = len(json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return (size + 2048) * Decimal("0.000000042") * Decimal("1.05")


def _dispatcher(
    runtime: BudgetRuntime, client: httpx.Client, policy: ChargePolicy | None = None
) -> OpenRouterDispatcher:
    """Build a dispatcher for the decision model.

    Args:
        runtime: The spending authority.
        client: The mock HTTP client.
        policy: The charge policy. The default is built here, after the
            markup fixture runs, because a policy reads the markup when it is made.

    Returns:
        The dispatcher, with the zero-retention data policy.
    """
    return OpenRouterDispatcher(
        runtime,
        api_key="private",
        model=MODEL,
        role="agent",
        policy=policy or ChargePolicy("managed_model"),
        client=client,
        data_policy="zdr",
    )


def test_the_pricer_bounds_the_prompt_by_utf8_bytes_and_caps_the_route() -> None:
    """The maximum uses the UTF-8 size of the body, so Hebrew text counts per byte, not per character."""
    request = {**REQUEST, "state": {"user_latest_request": "שנה את השם של הריצה"}}
    priced = price_decision_request(request, CATALOG, ChargePolicy("managed_model"))
    assert Decimal(priced.quote.price_snapshot["maximum_provider_usd"]) == _maximum_usd(request)
    assert priced.body["provider"]["only"] == ["typesafe"]
    assert priced.body["provider"]["allow_fallbacks"] is False
    assert priced.body["state"] == request["state"]


@pytest.mark.parametrize(
    ("catalog", "request_body"),
    [
        ({**CATALOG, "endpoints": [{**CATALOG["endpoints"][0], "pricing": {"completion": "0"}}]}, REQUEST),
        (
            {**CATALOG, "endpoints": [{**CATALOG["endpoints"][0], "pricing": {"prompt": "-1", "completion": "0"}}]},
            REQUEST,
        ),
        (
            {**CATALOG, "endpoints": [{**CATALOG["endpoints"][0], "pricing": {"prompt": "1", "completion": "1"}}]},
            REQUEST,
        ),
        ({**CATALOG, "endpoints": []}, REQUEST),
        ({**CATALOG, "architecture": {"output_modalities": ["text"]}}, REQUEST),
        (CATALOG, {**REQUEST, "model": "other/model"}),
        (CATALOG, {**REQUEST, "state": ["text", {"type": "image_url", "image_url": {"url": "https://x"}}]}),
        (CATALOG, {**REQUEST, "state": {"tool_call": {"args": [{"type": "image_url", "url": "https://x"}]}}}),
        (CATALOG, {**REQUEST, "state": {"tool_call": {"args": {"note": {"input_audio": {"data": "AAAA"}}}}}}),
        (CATALOG, {**REQUEST, "state": {"blob": b"raw"}}),
        (CATALOG, {**REQUEST, "assets": {"a": "https://x"}}),
        (CATALOG, {**REQUEST, "questions": {}}),
    ],
)
def test_the_pricer_refuses_what_it_cannot_bound(catalog: dict[str, Any], request_body: dict[str, Any]) -> None:
    """An unpriced, negative or output-priced model, a chat model, media and unknown fields never authorize work.

    Args:
        catalog: The endpoint catalog.
        request_body: The Decisions API body.
    """
    with pytest.raises(UnpricedOperationError):
        price_decision_request(request_body, catalog, ChargePolicy("managed_model"))


def test_the_pricer_accepts_nested_plain_json_state() -> None:
    """Nested dicts and lists of text, numbers, booleans and null are text state."""
    state = {"tool_call": {"name": "rename", "args": {"id": "j1", "tags": ["a", 1, 2.5, True, None], "type": "rename"}}}
    priced = price_decision_request({**REQUEST, "state": state}, CATALOG, ChargePolicy("managed_model"))
    assert priced.body["state"] == state


def test_dispatch_reserves_before_the_alpha_call_and_settles_the_reported_cost(database: Engine) -> None:
    """The hold exists when the provider sees the call, the URL is /api/alpha, and the cost settles."""
    runtime = _runtime(database)
    sent: list[httpx.Request] = []
    held: list[Decimal] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """Record the call and the hold it saw.

        Args:
            request: The outgoing request.

        Returns:
            Prices, or the decision answer.
        """
        if request.method == "GET":
            return httpx.Response(200, json={"data": CATALOG})
        sent.append(request)
        held.append(runtime.service.get(runtime.budget_id, "alice").reserved_cents)
        return httpx.Response(200, json=ANSWER)

    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        result = _dispatcher(runtime, client).dispatch("/decisions", REQUEST)
    assert result.status == 200
    assert str(sent[0].url) == "https://openrouter.ai/api/alpha/decisions"
    body = json.loads(sent[0].content)
    assert body["provider"]["zdr"] is True
    assert body["provider"]["data_collection"] == "deny"
    assert held == [_maximum_usd(REQUEST) * 100]
    snapshot = runtime.service.get(runtime.budget_id, "alice")
    assert snapshot.setup_spent_cents == Decimal("0.002")
    assert snapshot.reserved_cents == 0
    with Session(database) as session:
        assert session.scalars(select(ExecutionOperationModel.state)).all() == ["settled"]


def test_a_failed_decision_with_usage_still_settles_its_cost(database: Engine) -> None:
    """A provider error that reports a cost pays that cost and releases the rest of the hold."""
    runtime = _runtime(database)

    def provider(request: httpx.Request) -> httpx.Response:
        """Fail after the provider billed the call.

        Args:
            request: The outgoing request.

        Returns:
            Prices, or an error with usage.
        """
        if request.method == "GET":
            return httpx.Response(200, json={"data": CATALOG})
        return httpx.Response(502, json={"id": "gen-dec-1-x", "error": "upstream", "usage": {"cost": "0.00002"}})

    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        result = _dispatcher(runtime, client).dispatch("/decisions", REQUEST)
    assert result.status == 502
    snapshot = runtime.service.get(runtime.budget_id, "alice")
    assert snapshot.setup_spent_cents == Decimal("0.002")
    assert snapshot.reserved_cents == 0


def test_a_refused_decision_releases_its_hold(database: Engine) -> None:
    """A 400 with no generation and no usage charges nothing."""
    runtime = _runtime(database)

    def provider(request: httpx.Request) -> httpx.Response:
        """Refuse the call before inference.

        Args:
            request: The outgoing request.

        Returns:
            Prices, or a capability refusal.
        """
        if request.method == "GET":
            return httpx.Response(200, json={"data": CATALOG})
        return httpx.Response(400, json={"error": {"message": "capability mismatch"}})

    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        result = _dispatcher(runtime, client).dispatch("/decisions", REQUEST)
    assert result.status == 400
    snapshot = runtime.service.get(runtime.budget_id, "alice")
    assert snapshot.setup_spent_cents == 0
    assert snapshot.reserved_cents == 0
    with Session(database) as session:
        assert session.scalars(select(ExecutionOperationModel.state)).all() == ["released"]


def test_an_unfunded_budget_never_sends_the_decision(database: Engine) -> None:
    """A wallet too small for the hold refuses before the provider sees the call.

    A 300 KB state bills at most the 262,144-token context, about 1.16 cents,
    which a 1-cent balance cannot hold. Setup work is bounded by the wallet,
    not by the run's limit (SKY-925).
    """
    with Session(database) as session:
        session.get(BillingCustomerModel, "alice").balance_cents = 1
        session.commit()
    runtime = _runtime(database)
    posts: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """Answer prices and record any paid call.

        Args:
            request: The outgoing request.

        Returns:
            Prices, or the decision answer.
        """
        if request.method == "GET":
            return httpx.Response(200, json={"data": CATALOG})
        posts.append(request)
        return httpx.Response(200, json=ANSWER)

    with httpx.Client(transport=httpx.MockTransport(provider)) as client, pytest.raises(AccountFundsError):
        _dispatcher(runtime, client).dispatch("/decisions", {**REQUEST, "state": "x" * 300_000})
    assert posts == []
    assert runtime.service.get(runtime.budget_id, "alice").state == "open"


def test_byok_routes_cannot_dispatch_decisions(database: Engine) -> None:
    """Only the managed OpenRouter route serves /decisions, so a BYOK key never skips the reserve."""
    runtime = _runtime(database)
    with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(500))) as client:
        dispatcher = _dispatcher(runtime, client, policy=ChargePolicy("byok_model"))
        with pytest.raises(UnpricedOperationError):
            dispatcher.dispatch("/decisions", REQUEST)
