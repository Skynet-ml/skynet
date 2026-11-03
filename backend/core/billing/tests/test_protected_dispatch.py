"""Prove final-request admission and exactly-once charging at the HTTP boundary."""

from __future__ import annotations

import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

from core.billing.budgets import BudgetInsufficientError, BudgetService
from core.billing.model_dispatch import (
    BYOK_FUNDS_EXHAUSTED,
    FUNDS_BUSY,
    MANAGED_FUNDS_EXHAUSTED,
    OpenRouterDispatcher,
    mark_prompt_cache,
    response_usage,
)
from core.billing.openrouter_quotes import price_text_request
from core.billing.operation_pricing import ChargePolicy, UnpricedOperationError, operation_quote
from core.billing.runtime import (
    BudgetRuntime,
    OperationCompletedError,
    PaidResult,
    ProviderFailedError,
    UsagePendingError,
)
from core.config import settings
from core.storage.models import Base, BillingCustomerModel, ExecutionOperationModel, ExecutionUsageEvidenceModel

CATALOG = {
    "id": "fixture/text",
    "endpoints": [
        {
            "tag": "fixture",
            "provider_name": "Fixture",
            "context_length": 1000,
            "max_completion_tokens": 1000,
            "pricing": {"prompt": "0.00001", "completion": "0.00002", "input_cache_write": "0.000015"},
        }
    ],
}
REQUEST = {"model": "fixture/text", "max_tokens": 100, "messages": [{"role": "user", "content": "hello"}]}


@pytest.fixture(autouse=True)
def _at_cost_markup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the usage markup to 1.0 so these tests check settlement arithmetic, not pricing policy."""
    monkeypatch.setattr(settings, "usage_markup", 1.0)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Engine]:
    """Keep wallet and operation evidence in a private test database."""
    engine = create_engine(f"sqlite:///{tmp_path / 'dispatch.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username="alice", stripe_customer_id="fixture", balance_cents=100, grant_remaining=0)
        )
        session.commit()
    yield engine
    engine.dispose()


def _runtime(database: Engine, total: int = 50) -> BudgetRuntime:
    """Create a setup authority with real durable admission and settlement."""
    service = BudgetService(engine=database)
    budget = service.create("alice", total, idempotency_key="setup")
    return BudgetRuntime(service, username="alice", budget_id=budget.id, generation=0, phase="setup", wait_timeout=0)


def test_byok_model_and_sandbox_are_metered_separately(database: Engine) -> None:
    """Charge sandbox work while a BYOK model call settles for zero."""
    runtime = _runtime(database)
    byok = ChargePolicy("byok_model")
    runtime.execute(
        operation_quote({}, Decimal(0), byok, {}),
        byok,
        lambda: PaidResult(None, None, {}),
        operation_key="model",
        cost_kind="model",
        role="task",
    )
    sandbox = ChargePolicy("sandbox")
    runtime.execute(
        operation_quote({}, Decimal("0.01"), sandbox, {}),
        sandbox,
        lambda: PaidResult(None, Decimal("0.01"), {}),
        operation_key="sandbox",
        cost_kind="sandbox",
        role="runtime",
    )
    snapshot = runtime.service.get(runtime.budget_id, "alice")
    assert snapshot.setup_spent_cents == sandbox.convert(Decimal("0.01")).total
    assert snapshot.reserved_cents == 0


def test_openrouter_byok_anthropic_prompt_is_cacheable(database: Engine) -> None:
    """Mark an Anthropic BYOK prompt before sending it to OpenRouter."""
    sent: list[dict] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """Capture the routed request and return a successful response."""
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": []})

    model = "anthropic/claude-sonnet-5"
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        dispatcher = OpenRouterDispatcher(
            _runtime(database),
            api_key="private",
            model=model,
            role="task",
            policy=ChargePolicy("byok_model"),
            client=client,
        )
        dispatcher.dispatch("/chat/completions", {"model": model, "messages": [{"role": "system", "content": "hello"}]})
    assert sent[0]["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.parametrize("usage", [None, {"cost": "bad"}, {"cost": -1}, {"cost": "NaN"}])
def test_byok_success_with_missing_or_invalid_usage_settles_zero(database: Engine, usage: object) -> None:
    """Settle a successful BYOK call even when usage is unavailable or malformed."""
    runtime = _runtime(database)

    def provider(request: httpx.Request) -> httpx.Response:
        """Return one success without valid provider-cost evidence."""
        body = {"id": "generation", "choices": []}
        if usage is not None:
            body["usage"] = usage
        return httpx.Response(200, json=body)

    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        dispatcher = OpenRouterDispatcher(
            runtime,
            api_key="private",
            model="fixture/text",
            role="task",
            policy=ChargePolicy("byok_model"),
            client=client,
        )
        assert dispatcher.dispatch("/chat/completions", REQUEST).status == 200
    snapshot = runtime.service.get(runtime.budget_id, "alice")
    assert snapshot.reserved_cents == 0
    assert snapshot.pending_operations == 0
    assert snapshot.setup_spent_cents == 0
    with Session(database) as session:
        assert session.scalar(select(ExecutionOperationModel)).state == "settled"


def test_final_request_caps_include_cache_and_output_overrides() -> None:
    """Bind quotes after overrides and cover cache writes without billing the hold."""
    original = json.loads(json.dumps(REQUEST))
    policy = ChargePolicy("managed_model")
    first = price_text_request(original, CATALOG, policy)
    changed = price_text_request({**original, "max_tokens": 500}, CATALOG, policy)
    assert first.quote.maximum.total == Decimal("1.785")
    assert changed.quote.maximum.total == Decimal("2.625")
    assert first.quote.request_fingerprint != changed.quote.request_fingerprint
    assert first.body["provider"]["max_price"]["completion"] == 20
    assert "provider" not in original


@pytest.mark.parametrize(
    "extra",
    [
        {"max_tokens": None},
        {"plugins": [{"id": "web"}]},
        {"n": 2},
        {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": "https://example.com"}]}]},
        {"model": "other/model"},
        {"service_tier": "priority"},
    ],
)
def test_unbounded_requests_never_authorize_work(extra: dict) -> None:
    """Reject absent bounds or uncovered billable categories before dispatch."""
    with pytest.raises(UnpricedOperationError):
        price_text_request({**REQUEST, **extra}, CATALOG, ChargePolicy("managed_model"))


def test_paid_provider_failure_settles_and_is_not_replayed(database: Engine) -> None:
    """Charge measured usage on a failed response and reject duplicate physical dispatch."""
    runtime = _runtime(database)
    calls = []

    def provider(request: httpx.Request) -> httpx.Response:
        """Assert the hold exists before the first byte reaches the fake provider."""
        if request.method == "GET":
            return httpx.Response(200, json={"data": CATALOG})
        calls.append(request)
        assert runtime.service.get(runtime.budget_id, "alice").reserved_cents == Decimal("1.785")
        return httpx.Response(
            500, json={"id": "gen-one", "error": "failed after inference", "usage": {"cost": "0.004"}}
        )

    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        dispatcher = OpenRouterDispatcher(
            runtime,
            api_key="private",
            model="fixture/text",
            role="judge",
            policy=ChargePolicy("managed_model"),
            client=client,
        )
        result = dispatcher.dispatch("/chat/completions", REQUEST)
    assert result.status == 500
    snapshot = runtime.service.get(runtime.budget_id, "alice")
    assert len(calls) == 1
    assert snapshot.setup_spent_cents == Decimal("0.4")
    assert snapshot.billed_cents == 1
    assert snapshot.reserved_cents == 0


def test_each_physical_retry_has_separate_coverage_and_delivery_replay_is_deduplicated(database: Engine) -> None:
    """Reserve and settle each real retry while refusing a replay of either attempt."""
    runtime = _runtime(database)
    posts: list[httpx.Request] = []
    held_before_dispatch: list[Decimal] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """Return measured usage for two separately admitted provider attempts."""
        if request.method == "GET":
            return httpx.Response(200, json={"data": CATALOG})
        posts.append(request)
        held_before_dispatch.append(runtime.service.get(runtime.budget_id, "alice").reserved_cents)
        number = len(posts)
        return httpx.Response(
            503 if number == 1 else 200,
            json={
                "id": f"gen-{number}",
                "usage": {"cost": "0.004" if number == 1 else "0.002"},
                "choices": [{"message": {"content": "retry" if number == 1 else "OK"}}],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        dispatcher = OpenRouterDispatcher(
            runtime,
            api_key="private",
            model="fixture/text",
            role="optimization",
            policy=ChargePolicy("managed_model"),
            client=client,
        )
        first = dispatcher.dispatch("/chat/completions", REQUEST, operation_key="logical-call", attempt=0)
        with pytest.raises(OperationCompletedError):
            dispatcher.dispatch("/chat/completions", REQUEST, operation_key="logical-call", attempt=0)
        second = dispatcher.dispatch("/chat/completions", REQUEST, operation_key="logical-call", attempt=1)
        with pytest.raises(OperationCompletedError):
            dispatcher.dispatch("/chat/completions", REQUEST, operation_key="logical-call", attempt=1)

    assert (first.status, second.status) == (503, 200)
    assert len(posts) == 2
    assert held_before_dispatch == [Decimal("1.785"), Decimal("1.785")]
    snapshot = runtime.service.get(runtime.budget_id, "alice")
    assert snapshot.setup_spent_cents == Decimal("0.6")
    assert snapshot.reserved_cents == 0
    with Session(database) as session:
        operations = session.scalars(select(ExecutionOperationModel).order_by(ExecutionOperationModel.attempt)).all()
    assert [(operation.attempt, operation.state) for operation in operations] == [(0, "settled"), (1, "settled")]


def test_insufficient_setup_never_sends_inference_and_remains_editable(database: Engine) -> None:
    """Leave the user able to increase setup allowance without silently truncating a request."""
    runtime = _runtime(database, total=1)

    def provider(request: httpx.Request) -> httpx.Response:
        """Permit only free catalog retrieval when the operation is not covered."""
        assert request.method == "GET"
        return httpx.Response(200, json={"data": CATALOG})

    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        dispatcher = OpenRouterDispatcher(
            runtime,
            api_key="private",
            model="fixture/text",
            role="judge",
            policy=ChargePolicy("managed_model"),
            client=client,
        )
        with pytest.raises(BudgetInsufficientError):
            dispatcher.dispatch("/chat/completions", REQUEST)
    assert runtime.service.get(runtime.budget_id, "alice").state == "open"


def test_missing_usage_remains_reserved_without_a_fake_zero_charge(database: Engine) -> None:
    """Keep a completed but unpriced response pending for later reconciliation."""
    runtime = _runtime(database)

    def provider(request: httpx.Request) -> httpx.Response:
        """Return a provider result whose charge is temporarily unavailable."""
        if request.url.path.endswith("/endpoints"):
            return httpx.Response(200, json={"data": CATALOG})
        if request.method == "GET":
            return httpx.Response(404)
        return httpx.Response(200, json={"id": "gen-pending", "choices": []})

    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        dispatcher = OpenRouterDispatcher(
            runtime,
            api_key="private",
            model="fixture/text",
            role="judge",
            policy=ChargePolicy("managed_model"),
            client=client,
        )
        with pytest.raises(UsagePendingError):
            dispatcher.dispatch("/chat/completions", REQUEST)
    snapshot = runtime.service.get(runtime.budget_id, "alice")
    assert snapshot.reserved_cents == Decimal("1.785")
    assert snapshot.setup_spent_cents == 0
    assert snapshot.pending_operations == 1
    with Session(database) as session:
        operation = session.scalar(select(ExecutionOperationModel))
        assert operation.provider_request_id == "gen-pending"
        assert operation.state == "pending"


def test_anthropic_stream_merges_final_cost_without_inventing_zero() -> None:
    """Read a generation identity from message-start and its cost from message-delta."""
    stream = b'data: {"type":"message_start","message":{"id":"gen-stream","usage":{"input_tokens":8}}}\n\ndata: {"type":"message_delta","usage":{"output_tokens":2,"cost":0.001}}\n\ndata: [DONE]\n'
    identity, usage = response_usage(stream, "text/event-stream")
    assert identity == "gen-stream"
    assert usage == {"input_tokens": 8, "output_tokens": 2, "cost": 0.001}


def test_usage_markup_applies_to_platform_paid_costs_but_not_byok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mark up sandbox and managed dollars but do not charge BYOK model calls."""
    monkeypatch.setattr(settings, "usage_markup", 1.15)
    assert ChargePolicy("sandbox").convert(Decimal("0.01")).total == Decimal("1.15")
    assert ChargePolicy("managed_model").convert(Decimal("0.01")).total == Decimal("1.15")
    byok = ChargePolicy("byok_model").convert(Decimal("0.01"))
    assert byok.total == byok.wallet == Decimal(0)


def test_protected_hold_includes_the_usage_markup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reserve the marked-up worst case, and freeze the markup into the policy snapshot."""
    monkeypatch.setattr(settings, "usage_markup", 1.15)
    policy = ChargePolicy("managed_model")
    priced = price_text_request(json.loads(json.dumps(REQUEST)), CATALOG, policy)
    assert priced.quote.maximum.total == Decimal("1.785") * Decimal("1.15")
    assert policy.snapshot()["model_markup"] == "1.15"


def test_responses_dispatch_reserves_the_actual_protocol_body(database: Engine) -> None:
    """Cover native Responses output before sending and settle its nested final usage."""
    runtime = _runtime(database)
    calls = []

    def provider(request: httpx.Request) -> httpx.Response:
        """Check the real dispatch body against the admitted Responses operation."""
        if request.method == "GET":
            return httpx.Response(200, json={"data": CATALOG})
        calls.append(request)
        assert request.url.path == "/api/v1/responses"
        sent = json.loads(request.content)
        assert sent["input"] == [{"role": "user", "content": "Reply OK"}]
        assert sent["max_output_tokens"] == 1000
        assert "messages" not in sent
        assert "max_tokens" not in sent
        assert sent["provider"]["allow_fallbacks"] is False
        assert runtime.service.get(runtime.budget_id, "alice").reserved_cents > 0
        return httpx.Response(
            200,
            json={
                "id": "gen-responses",
                "status": "completed",
                "output": [],
                "usage": {"cost": "0.002"},
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
        response = dispatcher.dispatch(
            "/responses",
            {
                "model": "fixture/text",
                "input": [{"role": "user", "content": "Reply OK"}],
                "store": False,
            },
        )
    assert response.status == 200
    assert len(calls) == 1
    snapshot = runtime.service.get(runtime.budget_id, "alice")
    assert snapshot.setup_spent_cents == Decimal("0.2")
    assert snapshot.reserved_cents == 0


def test_failed_responses_stream_is_retryable_but_keeps_its_coverage(database: Engine) -> None:
    """Report a provider-failed response as retryable while its unconfirmed bill stays held."""
    runtime = _runtime(database)
    failed = {
        "type": "response.failed",
        "response": {"id": "gen-failed", "status": "failed", "error": {"message": "Upstream provider error"}},
    }
    stream = (
        'data: {"type":"response.created","response":{"id":"gen-failed","status":"in_progress"}}\n\n'
        f"data: {json.dumps(failed)}\n\ndata: [DONE]\n\n"
    ).encode()

    posts = []

    def provider(request: httpx.Request) -> httpx.Response:
        """Serve the catalog, fail the first stream, and complete the retry."""
        if request.url.path.endswith("/endpoints"):
            return httpx.Response(200, json={"data": CATALOG})
        if request.method == "GET":
            return httpx.Response(404)
        posts.append(request)
        if len(posts) == 1:
            return httpx.Response(200, content=stream, headers={"content-type": "text/event-stream"})
        return httpx.Response(
            200, json={"id": "gen-retry", "status": "completed", "output": [], "usage": {"cost": "0.001"}}
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
        with pytest.raises(ProviderFailedError, match="Upstream provider error"):
            dispatcher.dispatch(
                "/responses", {"model": "fixture/text", "input": [{"role": "user", "content": "hi"}], "stream": True}
            )
        retried = dispatcher.dispatch(
            "/responses", {"model": "fixture/text", "input": [{"role": "user", "content": "hi"}]}
        )
    assert retried.status == 200
    snapshot = runtime.service.get(runtime.budget_id, "alice")
    assert snapshot.pending_operations == 1
    assert snapshot.reserved_cents > 0
    with Session(database) as session:
        operation = session.scalar(select(ExecutionOperationModel))
        assert operation.state == "pending"
        assert operation.provider_request_id == "gen-failed"


def _refusing_dispatcher(database: Engine, kind: str, headers: dict[str, str], calls: list) -> tuple:
    """Build a dispatcher whose fake OpenRouter answers every inference with 402."""
    runtime = _runtime(database)

    def provider(request: httpx.Request) -> httpx.Response:
        """Serve the price catalog and refuse inference for lack of credits."""
        if request.method == "GET":
            return httpx.Response(200, json={"data": CATALOG})
        calls.append(request)
        body = {"error": {"code": 402, "message": "Your account or API key has insufficient credits"}}
        return httpx.Response(402, json=body, headers=headers)

    client = httpx.Client(transport=httpx.MockTransport(provider))
    dispatcher = OpenRouterDispatcher(
        runtime, api_key="private", model="fixture/text", role="task", policy=ChargePolicy(kind), client=client
    )
    return runtime, dispatcher, client


@pytest.mark.parametrize(
    ("kind", "headers", "code"),
    [
        ("managed_model", {}, MANAGED_FUNDS_EXHAUSTED),
        ("byok_model", {}, BYOK_FUNDS_EXHAUSTED),
        ("managed_model", {"Retry-After": "5"}, FUNDS_BUSY),
    ],
)
def test_funds_refusal_is_named_uncharged_and_not_resent(
    database: Engine, monkeypatch: pytest.MonkeyPatch, kind: str, headers: dict, code: str
) -> None:
    """A 402 says which funds ran out, costs nothing, and later retries never reach OpenRouter."""
    alerts: list[str] = []
    monkeypatch.setattr("core.billing.model_dispatch.notify_managed_refusal", alerts.append)
    calls: list = []
    runtime, dispatcher, client = _refusing_dispatcher(database, kind, headers, calls)
    with client:
        first = dispatcher.dispatch("/chat/completions", REQUEST)
        second = dispatcher.dispatch("/chat/completions", REQUEST, attempt=1)
    assert first.status == second.status == 402
    assert json.loads(first.body)["error"]["type"] == code
    assert second.body == first.body
    assert len(calls) == 1
    snapshot = runtime.service.get(runtime.budget_id, "alice")
    assert snapshot.billed_cents == 0
    assert snapshot.reserved_cents == 0
    assert alerts == (["fixture/text"] if code == MANAGED_FUNDS_EXHAUSTED else [])


def test_funds_refusal_hold_expires(database: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Once the hold lapses the route asks OpenRouter again."""
    calls: list = []
    clock = [1000.0]
    monkeypatch.setattr("core.billing.model_dispatch.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("core.billing.model_dispatch.notify_managed_refusal", lambda model: None)
    _, dispatcher, client = _refusing_dispatcher(database, "managed_model", {"Retry-After": "5"}, calls)
    with client:
        dispatcher.dispatch("/chat/completions", REQUEST)
        clock[0] += 6
        dispatcher.dispatch("/chat/completions", REQUEST, attempt=1)
    assert len(calls) == 2


def test_anthropic_chat_prefix_is_marked_cacheable() -> None:
    """Break the cache after the system prompt and after the demos before the final turn."""
    body = {
        "model": "anthropic/claude-sonnet-5",
        "messages": [
            {"role": "system", "content": "instructions"},
            {"role": "user", "content": "demo question"},
            {"role": "assistant", "content": [{"type": "text", "text": "demo answer"}]},
            {"role": "user", "content": "real question"},
        ],
    }
    marked = mark_prompt_cache("/chat/completions", body)["messages"]
    assert marked[0]["content"] == [{"type": "text", "text": "instructions", "cache_control": {"type": "ephemeral"}}]
    assert marked[1]["content"] == "demo question"
    assert marked[2]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert marked[3]["content"] == "real question"
    assert body["messages"][0]["content"] == "instructions"


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/chat/completions", {"model": "openai/gpt-5", "messages": [{"role": "system", "content": "x"}]}),
        ("/messages", {"model": "anthropic/claude-sonnet-5", "messages": [{"role": "user", "content": "x"}]}),
        (
            "/chat/completions",
            {
                "model": "anthropic/claude-sonnet-5",
                "messages": [
                    {
                        "role": "system",
                        "content": [{"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}],
                    }
                ],
            },
        ),
    ],
)
def test_prompt_cache_leaves_other_requests_alone(path: str, body: dict) -> None:
    """Skip providers that cache on their own, other protocols, and callers that placed breakpoints."""
    assert mark_prompt_cache(path, body) is body


def test_dispatch_sends_and_reserves_the_marked_body(database: Engine) -> None:
    """Price and send the cacheable body so the reservation covers exactly what the provider saw."""
    runtime = _runtime(database)
    catalog = {**CATALOG, "id": "anthropic/claude-sonnet-5"}
    sent = []

    def provider(request: httpx.Request) -> httpx.Response:
        """Record the final body the relay sends."""
        if request.method == "GET":
            return httpx.Response(200, json={"data": catalog})
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "gen-cache", "usage": {"cost": "0.001"}})

    request = {
        "model": "anthropic/claude-sonnet-5",
        "max_tokens": 100,
        "messages": [{"role": "system", "content": "instructions"}, {"role": "user", "content": "hello"}],
    }
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        dispatcher = OpenRouterDispatcher(
            runtime,
            api_key="private",
            model="anthropic/claude-sonnet-5",
            role="task",
            policy=ChargePolicy("managed_model"),
            client=client,
        )
        result = dispatcher.dispatch("/chat/completions", request)
    assert result.status == 200
    assert sent[0]["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_dispatch_records_usage_tags_and_latency_without_forwarding_them(database: Engine) -> None:
    """Keep a call's attribution tags in its evidence and out of the provider request."""
    runtime = _runtime(database)
    sent: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """Answer with measured usage and remember what reached the provider."""
        if request.method == "GET":
            return httpx.Response(200, json={"data": CATALOG})
        sent.append(request)
        return httpx.Response(200, json={"id": "gen-tags", "usage": {"cost": "0.001"}, "choices": []})

    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        dispatcher = OpenRouterDispatcher(
            runtime,
            api_key="private",
            model="fixture/text",
            role="task",
            policy=ChargePolicy("managed_model"),
            client=client,
        )
        dispatcher.dispatch(
            "/chat/completions",
            REQUEST,
            protocol_headers={"x-skynet-usage-tags": '{"stage":"training","candidate":"3","role":"judge"}'},
        )
    assert "x-skynet-usage-tags" not in sent[0].headers
    with Session(database) as session:
        evidence = session.scalars(select(ExecutionUsageEvidenceModel)).one().evidence
    assert evidence["tags"] == {"stage": "training", "candidate": "3"}
    assert isinstance(evidence["latency_ms"], int)
