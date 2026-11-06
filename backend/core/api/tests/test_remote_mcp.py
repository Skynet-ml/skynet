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
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ... import i18n
from ...config import Settings, settings
from ...models import BlackboxRunRequest
from ...models.optimizations import JobCancelResponse
from ...models.unified import UnifiedRunRequest
from ...storage.models import Base, McpGrantModel, McpTokenModel, UserModel
from .. import mcp_oauth, remote_mcp
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

    monkeypatch.setattr(remote_mcp, "cancel_optimization", forbidden)
    text = _error_text(_call(client, tool, arguments, token=READ_TOKEN))
    assert "insufficient_scope" in text
    assert "runs:write" in text


@pytest.mark.parametrize(
    ("tool", "arguments", "token"),
    [
        ("start_run", {"spec": unified_payload(), "max_cost_cents": 500}, WRITE_TOKEN),
        ("check_run", {"spec": unified_payload()}, READ_TOKEN),
    ],
)
def test_run_tools_refuse_dspy_program_runs(harness: Any, tool: str, arguments: dict[str, Any], token: str) -> None:
    """Refuse a DSPy program run with a 410 that says runs optimize repositories (SKY-316)."""
    client, _store, _calls = harness
    error = json.loads(_error_text(_call(client, tool, arguments, token=token)))
    assert error["code"] == "optimization.dspy_run_retired"
    assert error["status"] == 410
    assert "repositories" in error["detail"]


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
            optimizer={"choice": "dspy_gepa"},
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


LOCAL_URL = "http://127.0.0.1:8123/remote-mcp"


@pytest.fixture
def local_harness(harness: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Switch the harness to the Mac app's local mode and write its token file.

    Args:
        harness: The remote MCP harness.
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Temporary local state directory.

    Returns:
        A ``(client, token, tmp_path)`` tuple.
    """
    client, _store, _calls = harness
    monkeypatch.setattr(mcp_oauth, "_local_token", None)
    monkeypatch.setattr(settings, "mcp_resource_url", LOCAL_URL)
    monkeypatch.setattr(settings, "local_mode", True)
    monkeypatch.setattr(settings, "local_user_email", USER.upper())
    token = mcp_oauth.load_or_create_local_token(tmp_path, LOCAL_URL)
    return client, token, tmp_path


def _local_tools(client: TestClient, token: str) -> Any:
    """List the tools on the local MCP URL with ``token``.

    Args:
        client: Test client.
        token: Bearer token.

    Returns:
        The HTTP response.
    """
    return client.post(
        LOCAL_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )


def test_local_token_file_is_private_and_stable(local_harness: Any) -> None:
    """Write the URL and token for this user only, and keep the token across restarts."""
    _client, token, state_dir = local_harness
    path = state_dir / mcp_oauth.LOCAL_MCP_FILE
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text()) == {"url": LOCAL_URL, "token": token}
    assert token.startswith(mcp_oauth.LOCAL_TOKEN_PREFIX)
    other_url = "http://127.0.0.1:9000/remote-mcp"
    assert mcp_oauth.load_or_create_local_token(state_dir, other_url) == token
    assert json.loads(path.read_text())["url"] == other_url


def test_local_token_reaches_the_local_server(local_harness: Any) -> None:
    """Accept the local token on the loopback URL as the local user."""
    client, token, _state_dir = local_harness
    resp = _local_tools(client, token)
    assert resp.status_code == 200, resp.text
    assert "get_run" in {tool["name"] for tool in resp.json()["result"]["tools"]}


@pytest.mark.parametrize("case", ["hosted", "wrong_token", "no_user"])
def test_local_token_is_refused_outside_its_conditions(
    local_harness: Any, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Refuse the local token outside local mode, for another value, and without the local user."""
    client, token, _state_dir = local_harness
    if case == "hosted":
        monkeypatch.setattr(settings, "local_mode", False)
    elif case == "wrong_token":
        token = token[:-4] + "AAAA"
    else:
        monkeypatch.setattr(settings, "local_user_email", "nobody@example.test")
    assert _local_tools(client, token).status_code == 401


def test_local_mode_serves_the_mcp_on_the_app_port(tmp_path: Any) -> None:
    """Set the local MCP URL from the API host and port when none is configured."""
    local = Settings(
        _env_file=None,
        local_mode=True,
        local_state_dir=str(tmp_path),
        api_host="127.0.0.1",
        port=8123,
        mcp_resource_url="",
    )
    assert local.mcp_resource_url == LOCAL_URL
    explicit = Settings(
        _env_file=None,
        local_mode=True,
        local_state_dir=str(tmp_path),
        api_host="127.0.0.1",
        port=8123,
        mcp_resource_url="http://127.0.0.1:8123/other",
    )
    assert explicit.mcp_resource_url == "http://127.0.0.1:8123/other"
