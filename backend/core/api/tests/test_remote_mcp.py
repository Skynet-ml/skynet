"""Tests for the hosted remote MCP server and its tools (SKY-229).

The app mounts the real stateless Streamable HTTP server over in-memory
SQLite. Grants and tokens are written to the database directly; the OAuth
flow that issues them is covered in ``test_mcp_oauth.py``. The service-layer
functions are replaced in the ``remote_mcp`` namespace, so each test checks
what a tool hands to the service layer.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ... import i18n
from ...billing.budgets import BudgetService
from ...config import settings
from ...models import BlackboxRunRequest
from ...models.optimizations import JobCancelResponse
from ...models.unified import UnifiedRunRequest
from ...storage.models import Base, BillingCustomerModel, McpGrantModel, McpTokenModel, UserModel
from .. import remote_mcp
from ..auth import hash_api_token
from ..errors import DomainError
from ..model_billing import enforce_local_mode_sources, normalize_model_token_sources, request_model_configs
from ..remote_mcp import mount_remote_mcp
from ..routers import _helpers as routers_helpers
from ..sharing_access import ShareRole
from .test_submissions_router import unified_payload

RESOURCE = "https://mcp.example.test/mcp"
MCP_URL = RESOURCE
USER = "alice@example.test"
READ_TOKEN = "skym_at_read-only-token"
WRITE_TOKEN = "skym_at_read-write-token"


class _Store:
    """Minimal store exposing the engine and the MAU admission hook."""

    def __init__(self, engine: Any) -> None:
        """Hold the engine the token checks open sessions on.

        Args:
            engine: SQLAlchemy engine with the schema created.
        """
        self.engine = engine

    def admit_monthly_active_user(self, username: str, month_start: Any, cap: int) -> bool:
        """Admit every identity; the MAU cap is not under test here.

        Args:
            username: Identity to admit.
            month_start: First day of the month.
            cap: Configured cap.

        Returns:
            Always True.
        """
        return True


def _seed_token(engine: Any, token: str, scopes: str, grant_id: str) -> None:
    """Write a live grant and its access token.

    Args:
        engine: SQLAlchemy engine.
        token: Plaintext access token.
        scopes: Space-separated granted scopes.
        grant_id: Grant ID.
    """
    now = datetime.now(UTC)
    with Session(engine) as session:
        session.add(
            McpGrantModel(
                id=grant_id,
                username=USER,
                client_id="mcpc_test",
                client_name="Test agent",
                scopes=scopes,
                resource=RESOURCE,
                created_at=now,
                absolute_expires_at=now + timedelta(days=1),
            )
        )
        session.add(
            McpTokenModel(
                token_hash=hash_api_token(token),
                grant_id=grant_id,
                kind="access",
                created_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )
        session.commit()


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Build an app with the remote MCP server over in-memory SQLite.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Yields:
        A ``(client, store, calls)`` tuple; ``calls`` records service-layer calls.
    """
    monkeypatch.setattr(settings, "mcp_resource_url", RESOURCE)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(UserModel(email=USER, name="Alice Example", password_hash="x"))
        session.commit()
    _seed_token(engine, READ_TOKEN, "runs:read", "mg_read")
    _seed_token(engine, WRITE_TOKEN, "runs:read runs:write", "mg_write")
    store = _Store(engine)
    calls: dict[str, Any] = {}
    worker = object()
    app = FastAPI()

    @app.get("/mcp")
    def api_mcp() -> dict[str, str]:
        """Stand in for the internal API ``/mcp`` route."""
        return {"served_by": "api"}

    mount_remote_mcp(app, service="svc", job_store=store, get_worker=lambda: worker)
    calls["worker"] = worker
    with TestClient(app) as client:
        yield client, store, calls


def _rpc(
    client: TestClient,
    method: str,
    params: dict[str, Any],
    token: str | None = WRITE_TOKEN,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    """Send one JSON-RPC request to the MCP endpoint.

    Args:
        client: Test client.
        method: JSON-RPC method.
        params: JSON-RPC params.
        token: Bearer token, or ``None`` for no header.
        extra_headers: More request headers, if any.

    Returns:
        The HTTP response.
    """
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    headers.update(extra_headers or {})
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(MCP_URL, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})


def _call(
    client: TestClient,
    tool: str,
    arguments: dict[str, Any],
    token: str = WRITE_TOKEN,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Call a tool and return its JSON-RPC result.

    Args:
        client: Test client.
        tool: Tool name.
        arguments: Tool arguments.
        token: Bearer token.
        extra_headers: More request headers, if any.

    Returns:
        The ``result`` member.
    """
    resp = _rpc(client, "tools/call", {"name": tool, "arguments": arguments}, token, extra_headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    """Return the structured payload of a successful tool result."""
    assert not result.get("isError"), result
    return result["structuredContent"]


def _error_text(result: dict[str, Any]) -> str:
    """Return the text of a failed tool result."""
    assert result.get("isError") is True, result
    return result["content"][0]["text"]


def test_missing_token_gets_the_rfc9728_challenge(harness: Any) -> None:
    """Point a client without a token at the protected resource metadata."""
    client, _store, _calls = harness
    resp = _rpc(client, "tools/list", {}, token=None)
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == (
        'Bearer resource_metadata="https://mcp.example.test/.well-known/oauth-protected-resource/mcp"'
    )


def test_unknown_token_gets_invalid_token(harness: Any) -> None:
    """Refuse a token that the server did not issue."""
    client, _store, _calls = harness
    resp = _rpc(client, "tools/list", {}, token="skym_at_forged")
    assert resp.status_code == 401
    assert 'error="invalid_token"' in resp.headers["www-authenticate"]
    assert resp.json() == {"error": "invalid_token"}


def test_token_for_another_resource_is_refused(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse a token whose audience is another resource URL on this host."""
    client, _store, _calls = harness
    monkeypatch.setattr(settings, "mcp_resource_url", "https://mcp.example.test/other")
    resp = client.post(
        "https://mcp.example.test/other",
        headers={"Authorization": f"Bearer {WRITE_TOKEN}", "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert resp.status_code == 401


def test_other_hosts_reach_the_api(harness: Any) -> None:
    """Leave ``/mcp`` on the API host to the API."""
    client, _store, _calls = harness
    resp = client.get("https://api.example.test/mcp")
    assert resp.json() == {"served_by": "api"}


def test_unconfigured_server_serves_nothing(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Send every request to the API when no resource URL is configured."""
    client, _store, _calls = harness
    monkeypatch.setattr(settings, "mcp_resource_url", "")
    assert client.get(MCP_URL).json() == {"served_by": "api"}


def test_tools_list_names_the_five_tools(harness: Any) -> None:
    """List check, start, get, list and stop, and no money tools."""
    client, _store, _calls = harness
    resp = _rpc(client, "tools/list", {})
    assert resp.status_code == 200, resp.text
    names = {tool["name"] for tool in resp.json()["result"]["tools"]}
    assert names == {"check_run", "start_run", "get_run", "list_runs", "stop_run"}


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("start_run", {"spec": unified_payload(), "max_cost_cents": 500}),
        ("stop_run", {"optimization_id": "opt-1"}),
    ],
)
def test_read_scope_cannot_start_or_stop(
    harness: Any, monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict[str, Any]
) -> None:
    """Refuse write tools to a ``runs:read`` token before any service call."""
    client, _store, _calls = harness

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        """Fail the test if a write reaches the service layer."""
        raise AssertionError("service layer called")

    monkeypatch.setattr(remote_mcp, "submit_unified_payload", forbidden)
    monkeypatch.setattr(remote_mcp, "_submit_within_proxy_timeout", forbidden)
    monkeypatch.setattr(remote_mcp, "cancel_optimization", forbidden)
    text = _error_text(_call(client, tool, arguments, token=READ_TOKEN))
    assert "insufficient_scope" in text
    assert "runs:write" in text


def test_start_run_creates_a_budget_with_the_agent_limit(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Submit through the service layer with max_cost_cents and no reused budget."""
    client, _store, calls = harness

    def submit(spec: Any, *, service: Any, job_store: Any, current_user: Any, idempotency_key: Any) -> Any:
        """Record what the tool submits."""
        calls["submit"] = (spec, service, current_user, idempotency_key)
        return SimpleNamespace(model_dump=lambda mode: {"optimization_id": "opt-1", "status": "pending"})

    def within_timeout(username: str, replay_key: str, work: Any) -> Any:
        """Run the submission like the proxy-timeout wrapper does."""
        calls["timeout"] = (username, replay_key)
        return work()

    monkeypatch.setattr(remote_mcp, "submit_unified_payload", submit)
    monkeypatch.setattr(remote_mcp, "_submit_within_proxy_timeout", within_timeout)
    spec = unified_payload(execution_budget_id="eb-someone-else", max_cost_cents=99_999)
    body = _payload(_call(client, "start_run", {"spec": spec, "max_cost_cents": 700, "idempotency_key": "k-1"}))
    assert body == {"optimization_id": "opt-1", "status": "pending"}
    submitted, service, user, key = calls["submit"]
    assert submitted.max_cost_cents == 700
    assert submitted.execution_budget_id is None
    assert service == "svc"
    assert (user.username, user.role) == (USER, "user")
    assert key == "k-1"
    assert calls["timeout"][0] == USER


def test_start_run_refuses_a_reused_key_with_a_lower_limit(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse a retry that lowers max_cost_cents; replay one that keeps or raises it."""
    client, store, _calls = harness
    spec = unified_payload()
    budget_key = remote_mcp.resolve_submission_replay_keys(
        UnifiedRunRequest.model_validate(spec), username=USER, workflow=remote_mcp.UNIFIED_WORKFLOW, supplied_key="k-1"
    ).budget
    with Session(store.engine) as session:
        session.add(BillingCustomerModel(username=USER, stripe_customer_id="cus_local", balance_cents=5_000))
        session.commit()
    # 700 stands for a first request of 900 that the balance cap lowered.
    BudgetService(engine=store.engine).create(USER, 700, idempotency_key=budget_key)
    submitted: list[int] = []
    monkeypatch.setattr(
        remote_mcp,
        "submit_unified_payload",
        lambda spec, **kwargs: submitted.append(spec.max_cost_cents) or SimpleNamespace(model_dump=lambda mode: {}),
    )

    error = _error_text(_call(client, "start_run", {"spec": spec, "max_cost_cents": 500, "idempotency_key": "k-1"}))
    assert json.loads(error)["code"] == "budget.conflict"
    assert "700 cents" in json.loads(error)["detail"]
    assert submitted == []
    for limit in (700, 900):
        _payload(_call(client, "start_run", {"spec": spec, "max_cost_cents": limit, "idempotency_key": "k-1"}))
    assert submitted == [700, 900]


def test_start_run_replays_a_first_limit_after_the_user_raised_it(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay a same-limit retry after an edit in the app; name the run on a lower one."""
    client, store, _calls = harness
    spec = unified_payload()
    budget_key = remote_mcp.resolve_submission_replay_keys(
        UnifiedRunRequest.model_validate(spec), username=USER, workflow=remote_mcp.UNIFIED_WORKFLOW, supplied_key="k-2"
    ).budget
    with Session(store.engine) as session:
        session.add(BillingCustomerModel(username=USER, stripe_customer_id="cus_local", balance_cents=5_000))
        session.commit()
    service = BudgetService(engine=store.engine)
    first = service.create(USER, 700, idempotency_key=budget_key, uncapped=settings.local_mode)
    first = service.attach_to_job(first.id, USER, "run-k2", expected_revision=first.revision)
    service.update_total(first.id, USER, 1_500, expected_revision=first.revision, uncapped=settings.local_mode)
    submitted: list[int] = []
    monkeypatch.setattr(
        remote_mcp,
        "submit_unified_payload",
        lambda spec, **kwargs: submitted.append(spec.max_cost_cents) or SimpleNamespace(model_dump=lambda mode: {}),
    )

    _payload(_call(client, "start_run", {"spec": spec, "max_cost_cents": 700, "idempotency_key": "k-2"}))
    assert submitted == [700]
    error = json.loads(
        _error_text(_call(client, "start_run", {"spec": spec, "max_cost_cents": 600, "idempotency_key": "k-2"}))
    )
    assert error["code"] == "budget.conflict"
    assert "1500 cents" in error["detail"]
    assert error["optimization_id"] == "run-k2"
    assert submitted == [700]


def test_start_run_reports_a_slow_setup_check(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tell the agent to retry while the setup check still runs."""
    client, _store, _calls = harness
    monkeypatch.setattr(
        remote_mcp,
        "_submit_within_proxy_timeout",
        lambda username, key, work: JSONResponse(status_code=202, content={}, headers={"Retry-After": "7"}),
    )
    body = _payload(_call(client, "start_run", {"spec": unified_payload(), "max_cost_cents": 500}))
    assert body["status"] == "in_progress"
    assert body["retry_after_seconds"] == 7


def test_start_run_reports_unsettled_usage_as_in_progress(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tell the agent to retry while earlier usage settles, not to give up."""
    client, _store, _calls = harness

    def pending(username: str, key: str, work: Any) -> Any:
        """Refuse like a preflight that waits for usage reconciliation."""
        raise DomainError("budget.pending", status=409)

    monkeypatch.setattr(remote_mcp, "_submit_within_proxy_timeout", pending)
    result = _call(client, "start_run", {"spec": unified_payload(), "max_cost_cents": 500})
    assert result.get("isError") is not True
    body = _payload(result)
    assert body["status"] == "in_progress"
    assert body["retry_after_seconds"] == 5
    assert body["detail"].startswith("Other usage on this account must settle first.")


def test_start_run_refuses_a_zero_budget(harness: Any) -> None:
    """Require a positive spending limit."""
    client, _store, _calls = harness
    result = _call(client, "start_run", {"spec": unified_payload(), "max_cost_cents": 0})
    assert result.get("isError") is True


def test_start_run_reports_service_errors(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pass a service-layer refusal, like a low balance, to the agent."""
    client, _store, _calls = harness

    def refuse(username: str, key: str, work: Any) -> Any:
        """Refuse like the budget preflight."""
        raise DomainError("billing.insufficient_balance", status=402)

    monkeypatch.setattr(remote_mcp, "_submit_within_proxy_timeout", refuse)
    text = _error_text(_call(client, "start_run", {"spec": unified_payload(), "max_cost_cents": 500}))
    assert json.loads(text)["code"] == "billing.insufficient_balance"


@pytest.mark.parametrize(
    ("limit", "named", "key"), [(1, True, None), (500, False, None), (1, True, "k1"), (500, False, "k1")]
)
def test_start_run_names_the_minimum_of_a_too_low_limit(
    harness: Any, monkeypatch: pytest.MonkeyPatch, limit: int, named: bool, key: str | None
) -> None:
    """Give the agent the floor check_run reports, and a new key to retry with, when the limit is too low."""
    client, _store, _calls = harness
    monkeypatch.setattr(remote_mcp, "box_hold_cents", lambda: Decimal("18.2"))
    monkeypatch.setattr(remote_mcp, "call_hold_cents", lambda name, source, *, client: Decimal("100.5"))

    def refuse(username: str, key: str, work: Any) -> Any:
        """Refuse like a setup check the limit stopped."""
        raise DomainError("budget.below_minimum", status=409)

    monkeypatch.setattr(remote_mcp, "_submit_within_proxy_timeout", refuse)
    arguments: dict[str, Any] = {"spec": unified_payload(), "max_cost_cents": limit}
    if key:
        arguments["idempotency_key"] = key
    error = json.loads(_error_text(_call(client, "start_run", arguments)))
    assert error["code"] == "budget.below_minimum"
    assert error["min_max_cost_cents"] == 19 + 101
    assert ("minimum of 120 cents" in error["detail"]) is named
    assert error["detail"].startswith("max_cost_cents" if named else "The spending limit is below")
    # A same-key retry reuses the first budget and its low limit, so it can never pass.
    assert ("new idempotency_key" in error["detail"]) is bool(key)


def test_check_run_estimates_the_floor(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Return the checks, the wizard's floor, a suggested limit and the balance."""
    client, _store, calls = harness
    monkeypatch.setattr(remote_mcp, "enforce_submission_rate", lambda username: calls.setdefault("rate", username))
    monkeypatch.setattr(remote_mcp, "box_hold_cents", lambda: Decimal("40.2"))
    monkeypatch.setattr(remote_mcp, "call_hold_cents", lambda name, source, *, client: Decimal("150.5"))

    class _Billing:
        """Stand in for the billing service."""

        def __init__(self, engine: Any) -> None:
            """Accept the engine."""

        def spendable_cents(self, username: str) -> int:
            """Return a fixed balance."""
            return 2500

    monkeypatch.setattr(remote_mcp, "StripeBillingService", _Billing)
    body = _payload(_call(client, "check_run", {"spec": unified_payload()}, token=READ_TOKEN))
    assert body["min_max_cost_cents"] == 41 + 151
    assert body["suggested_max_cost_cents"] == 500
    assert body["spendable_cents"] == 2500
    assert "checks" in body
    assert calls["rate"] == USER


def test_effective_sources_match_submit_for_legacy_subscription() -> None:
    """Resolve the metric source after inheriting the job-level source."""
    payload = unified_payload(
        token_source="subscription",
        models={"reflection_model": {"name": "openai/reflection"}},
        workspace={"run_code": "pass", "metric_code": "from skynet import llm\nllm('x', model='anthropic/claude-x')"},
    )
    floor_spec = UnifiedRunRequest.model_validate(payload)
    submit_spec = UnifiedRunRequest.model_validate(payload)
    assert (
        dict(remote_mcp._effective_sources(floor_spec))["anthropic/claude-x"]
        == {config.name: config.token_source for config in request_model_configs(submit_spec)}["anthropic/claude-x"]
    )


def test_floor_includes_declared_metric_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Price the code-declared model even when the client omitted the metric role."""
    spec = remote_mcp.UnifiedRunRequest.model_validate(
        unified_payload(
            workspace={
                "run_code": "pass",
                "metric_code": "from skynet import llm\nllm('x', model='anthropic/expensive')",
            }
        )
    )
    monkeypatch.setattr(remote_mcp, "box_hold_cents", lambda: Decimal(10))
    monkeypatch.setattr(
        remote_mcp,
        "call_hold_cents",
        lambda name, source, *, client: Decimal(250) if name == "anthropic/expensive" else Decimal(1),
    )
    assert remote_mcp._floor_cents(spec, remote_mcp.httpx.Client) == (260, [])
    monkeypatch.setattr(
        remote_mcp,
        "call_hold_cents",
        lambda name, source, *, client: None if name == "anthropic/expensive" else Decimal(1),
    )
    assert remote_mcp._floor_cents(spec, remote_mcp.httpx.Client) == (110, ["anthropic/expensive"])


def test_declared_task_model_normalizes_with_task_source() -> None:
    """A metric naming the task model must not conflict with the task's billing source."""
    spec = UnifiedRunRequest.model_validate(
        unified_payload(
            models={
                "task_model": {"name": "openai/task", "token_source": "byok", "byok_provider": "openai"},
                "reflection_model": {"name": "openai/reflection", "token_source": "managed"},
            },
            workspace={"run_code": "pass", "metric_code": "from skynet import llm\nllm(model='openai/task')"},
        )
    )
    _, sources = normalize_model_token_sources(spec)
    assert sources["openai/task"] == "byok"
    assert spec.models.metric_model == spec.models.task_model


def test_declared_extra_reflection_model_normalizes_with_its_source() -> None:
    """A metric naming an extra reflection model uses that role's source."""
    spec = UnifiedRunRequest.model_validate(
        unified_payload(
            optimizer={"choice": "shinka_evolve"},
            models={
                "reflection_model": {"name": "openai/gpt-4o-mini", "token_source": "managed"},
                "extra_reflection_models": [
                    {"name": "anthropic/claude-x", "token_source": "byok", "byok_provider": "anthropic"}
                ],
            },
            workspace={"run_code": "pass", "metric_code": "from skynet import llm\nllm(model='anthropic/claude-x')"},
        )
    )
    _, sources = normalize_model_token_sources(spec)
    assert sources["anthropic/claude-x"] == "byok"
    assert spec.models.metric_model == spec.models.extra_reflection_models[0]


def test_submission_keeps_client_metric_model_without_declaration() -> None:
    """Submissions preserve an old picker value when no model is declared."""
    spec = UnifiedRunRequest.model_validate(
        unified_payload(
            models={
                "reflection_model": {"name": "openai/reflection", "token_source": "managed"},
                "metric_model": {"name": "anthropic/old", "token_source": "byok"},
            },
            workspace={"run_code": "pass", "metric_code": "from skynet import llm\nllm('x')"},
        )
    )
    normalize_model_token_sources(spec)
    assert spec.models.metric_model.name == "anthropic/old"
    assert spec.models.metric_model.token_source == "byok"


def test_repo_submission_rejects_conflicting_stored_scorer_model() -> None:
    """Reject a legacy repository scorer model that conflicts with its code."""
    spec = BlackboxRunRequest.model_validate(
        {
            "objective": "optimize",
            "reflection_model_config": {"name": "openai/reflection"},
            "scorer": {
                "kind": "python",
                "model": {"name": "anthropic/old"},
                "metric_code": "llm(model='anthropic/new')",
            },
        }
    )
    with pytest.raises(DomainError) as exc:
        request_model_configs(spec)
    assert exc.value.code == "contract.metric_model_conflict"


@pytest.mark.parametrize(
    ("code", "expected"),
    [("llm(model='anthropic/judge')", "anthropic/judge"), ("llm('x')", "openai/reflection")],
)
def test_blackbox_submission_derives_scorer_model(code: str, expected: str) -> None:
    """Resolve an unconfigured scorer before billing."""
    spec = BlackboxRunRequest.model_validate(
        {
            "objective": "optimize",
            "reflection_model_config": {"name": "openai/reflection"},
            "scorer": {"kind": "python", "metric_code": code},
        }
    )
    assert request_model_configs(spec)[-1].name == expected
    assert spec.scorer.model is not None
    assert spec.scorer.model.name == expected


@pytest.mark.parametrize(
    ("name", "provider"),
    [("openai/reflection", "openrouter"), ("openai/task", "custom"), ("openai/judge", "openrouter")],
)
def test_blackbox_submission_uses_byok_role_connection(name: str, provider: str) -> None:
    """Derive the scorer connection from the matching or compatible role."""
    spec = BlackboxRunRequest.model_validate(
        {
            "objective": "optimize",
            "reflection_model_config": {
                "name": "openai/reflection",
                "token_source": "byok",
                "byok_provider": "openrouter",
            },
            "task_model_config": {"name": "openai/task", "token_source": "byok", "byok_provider": "custom"},
            "target": {"kind": "agent", "harness": "custom", "model": "openai/task", "run_command": "run-agent"},
            "scorer": {"kind": "python", "metric_code": f"llm(model='{name}')"},
        }
    )
    request_model_configs(spec)
    assert spec.scorer.model is not None
    assert spec.scorer.model.byok_provider == provider


def test_blackbox_submission_rejects_unsafe_scorer_without_stored_model() -> None:
    """Reject ambiguous scorer declarations before billing."""
    spec = BlackboxRunRequest.model_validate(
        {
            "objective": "optimize",
            "reflection_model_config": {"name": "openai/reflection"},
            "scorer": {"kind": "python", "metric_code": "llm(model=x)"},
        }
    )
    with pytest.raises(DomainError) as exc:
        request_model_configs(spec)
    assert exc.value.code == "contract.metric_model_declaration"


def test_blackbox_submission_rejects_mixed_model_declarations() -> None:
    """Report ambiguous model use as a declaration error."""
    spec = BlackboxRunRequest.model_validate(
        {
            "objective": "optimize",
            "reflection_model_config": {"name": "openai/reflection"},
            "scorer": {"kind": "python", "metric_code": "llm('x'); llm('y', model='anthropic/judge')"},
        }
    )
    with pytest.raises(DomainError) as exc:
        request_model_configs(spec)
    assert exc.value.code == "contract.metric_model_declaration"


def test_submission_rejects_conflicting_stored_metric_model() -> None:
    """Reject a conflicting metric model before billing."""
    spec = UnifiedRunRequest.model_validate(
        unified_payload(
            models={"reflection_model": {"name": "openai/reflection"}, "metric_model": {"name": "anthropic/old"}},
            workspace={"run_code": "pass", "metric_code": "llm(model='anthropic/new')"},
        )
    )
    with pytest.raises(DomainError) as exc:
        request_model_configs(spec)
    assert exc.value.code == "contract.metric_model_conflict"


def test_unmatched_metric_model_is_managed_in_subscription_local_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject a managed metric in local mode rather than assigning it a CLI plan."""
    monkeypatch.setattr(settings, "local_mode", True)
    spec = UnifiedRunRequest.model_validate(
        unified_payload(
            token_source="subscription",
            models={
                "reflection_model": {
                    "name": "subscription/codex/model",
                    "token_source": "subscription",
                    "cli_provider": "codex",
                }
            },
            workspace={"run_code": "pass", "metric_code": "from skynet import llm\nllm(model='openai/judge')"},
        )
    )
    configs, _ = normalize_model_token_sources(spec)
    assert spec.models.metric_model.token_source == "managed"
    assert spec.models.metric_model.cli_provider is None
    with pytest.raises(DomainError) as exc:
        enforce_local_mode_sources(spec, "subscription", configs)
    assert exc.value.code == "models.managed_unavailable_local"


def test_get_run_is_owner_scoped(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Load the run through the owner check and return its budget."""
    client, _store, calls = harness

    def load(job_store: Any, optimization_id: str, user: Any) -> dict[str, Any]:
        """Record the owner check."""
        calls["load"] = (optimization_id, user.username)
        return {"optimization_id": optimization_id, "payload": {"username": USER}, "result": {"best": 1}}

    monkeypatch.setattr(remote_mcp, "load_job_for_user", load)
    monkeypatch.setattr(
        remote_mcp,
        "build_summary",
        lambda job: SimpleNamespace(status="success", model_dump=lambda mode: {"status": "success"}),
    )
    monkeypatch.setattr(remote_mcp, "execution_budget_view", lambda store, job: {"limit_cents": 500})
    body = _payload(_call(client, "get_run", {"optimization_id": "opt-1"}, token=READ_TOKEN))
    assert calls["load"] == ("opt-1", USER)
    assert body["terminal"] is True
    assert body["result"] == {"best": 1}
    assert body["execution_budget"] == {"limit_cents": 500}


def test_list_runs_lists_only_the_callers_runs(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Filter the list by the token's account and clamp the page size."""
    client, store, calls = harness
    store.count_jobs = lambda **kwargs: calls.setdefault("count", kwargs) and 1
    store.list_jobs = lambda **kwargs: calls.setdefault("list", kwargs) and [{"optimization_id": "opt-1"}]
    monkeypatch.setattr(
        remote_mcp, "build_summary", lambda job: SimpleNamespace(model_dump=lambda mode: {"id": job["optimization_id"]})
    )
    body = _payload(_call(client, "list_runs", {"limit": 5, "status": "running"}, token=READ_TOKEN))
    assert calls["count"]["username"] == USER
    assert calls["list"] == {"status": "running", "username": USER, "limit": 5, "offset": 0}
    assert body["runs"] == [{"id": "opt-1", "stop_reason": None}]
    assert "Unknown status" in _error_text(_call(client, "list_runs", {"status": "bogus"}, token=READ_TOKEN))


def test_stored_stop_reason_is_english_unless_accept_language_asks(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Render the Hebrew-base cancel reason in English, or in the asked locale."""
    client, store, _calls = harness
    hebrew = i18n.term("cancellationReason")
    job = {"optimization_id": "opt-1", "payload": {"username": USER}}
    monkeypatch.setattr(remote_mcp, "load_job_for_user", lambda store, oid, user: job)
    monkeypatch.setattr(remote_mcp, "execution_budget_view", lambda store, job: None)
    monkeypatch.setattr(
        remote_mcp,
        "build_summary",
        lambda job: SimpleNamespace(model_dump=lambda mode: {"status": "cancelled", "message": hebrew}),
    )
    store.count_jobs = lambda **kwargs: 1
    store.list_jobs = lambda **kwargs: [job]

    run = _payload(_call(client, "get_run", {"optimization_id": "opt-1"}, token=READ_TOKEN))["run"]
    assert run["message"] == "Cancelled by the user"
    listed = _payload(_call(client, "list_runs", {}, token=READ_TOKEN))["runs"][0]
    assert listed["message"] == "Cancelled by the user"
    asked = _call(client, "get_run", {"optimization_id": "opt-1"}, READ_TOKEN, {"Accept-Language": "he"})
    assert _payload(asked)["run"]["message"] == hebrew


def test_get_run_returns_the_stop_reason(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Return why a stopped run stopped, so the agent can tell a spent budget apart."""
    client, _store, _calls = harness
    job = {
        "optimization_id": "opt-1",
        "status": "stopped",
        "stop_reason": "budget_reached",
        "created_at": "2026-11-01T00:00:00+00:00",
        "payload": {"username": USER},
    }
    monkeypatch.setattr(remote_mcp, "load_job_for_user", lambda store, oid, user: job)
    monkeypatch.setattr(remote_mcp, "execution_budget_view", lambda store, job: None)
    run = _payload(_call(client, "get_run", {"optimization_id": "opt-1"}, token=READ_TOKEN))["run"]
    assert run["status"] == "stopped"
    assert run["stop_reason"] == "budget_reached"


def test_stop_run_cancels_through_the_service_layer(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancel like the Stop button, with the background worker."""
    client, _store, calls = harness

    def cancel(job_store: Any, optimization_id: str, user: Any, worker: Any) -> JobCancelResponse:
        """Record the cancellation."""
        calls["cancel"] = (optimization_id, user.username, worker)
        return JobCancelResponse(optimization_id=optimization_id, status="cancelled")

    monkeypatch.setattr(remote_mcp, "cancel_optimization", cancel)
    monkeypatch.setattr(remote_mcp, "load_job_for_user", lambda store, oid, user: {"payload": {"username": USER}})
    body = _payload(_call(client, "stop_run", {"optimization_id": "opt-1"}))
    assert body == {"optimization_id": "opt-1", "status": "cancelled"}
    assert calls["cancel"] == ("opt-1", USER, calls["worker"])


def _other_users_run(store: Any, monkeypatch: pytest.MonkeyPatch, calls: dict[str, Any]) -> None:
    """Serve one run owned by another account and record any cancellation.

    Args:
        store: Harness store; gets a ``get_job`` that serves the run.
        monkeypatch: Pytest monkeypatch fixture.
        calls: Harness call log.
    """
    job = {"optimization_id": "opt-bob", "payload": {"username": "bob@example.test"}, "status": "running"}

    def get_job(optimization_id: str) -> dict[str, Any]:
        """Return the run, or raise for an unknown ID."""
        if optimization_id != "opt-bob":
            raise KeyError(optimization_id)
        return dict(job)

    store.get_job = get_job
    monkeypatch.setattr(remote_mcp, "cancel_optimization", lambda *args: calls.setdefault("cancel", args))


def test_admin_allowlist_gives_no_access_to_other_runs(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse another account's run even when the token's account is on the admin allowlist."""
    client, store, calls = harness
    _other_users_run(store, monkeypatch, calls)
    monkeypatch.setattr(settings, "admin_usernames", USER)
    for tool in ("get_run", "stop_run"):
        error = json.loads(_error_text(_call(client, tool, {"optimization_id": "opt-bob"})))
        assert error["code"] == "optimization.not_found"
        assert error["status"] == 404
    assert "cancel" not in calls


def test_viewer_grant_can_read_but_not_stop(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Let a viewer grant read a shared run, and refuse it the Stop action."""
    client, store, calls = harness
    _other_users_run(store, monkeypatch, calls)
    for module in (remote_mcp, routers_helpers):
        monkeypatch.setattr(module, "_grant_role", lambda job_store, optimization_id, username: ShareRole.viewer)
    monkeypatch.setattr(
        remote_mcp,
        "build_summary",
        lambda job: SimpleNamespace(status="running", model_dump=lambda mode: {"status": "running"}),
    )
    monkeypatch.setattr(remote_mcp, "execution_budget_view", lambda store, job: {})
    body = _payload(_call(client, "get_run", {"optimization_id": "opt-bob"}))
    assert body["run"] == {"status": "running", "stop_reason": None}
    error = json.loads(_error_text(_call(client, "stop_run", {"optimization_id": "opt-bob"})))
    assert error["code"] == "optimization.insufficient_role"
    assert "cancel" not in calls
