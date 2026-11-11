"""Tests for the generalist's settings tools and their approval rules.

A mini app mounts the settings router beside small stand-in routes for the
real settings endpoints, so forwarding, masking and validation run in process
with no database. The approval tests drive the generalist's gate directly.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import dspy
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from mcp.types import Tool as McpTool
from mcp.types import ToolAnnotations

from ...service_gateway.agents import react_serve
from ...service_gateway.agents.generalist import (
    _ALWAYS_CONFIRM_TOOLS,
    _ALWAYS_TOOLS,
    _DESTRUCTIVE_TOOLS,
    ApprovalRegistry,
    _needs_approval,
    _screen_needs_approval,
    _wrap_tool_with_approval,
)
from ...service_gateway.optimization.training_ground import run_react
from ..errors import DomainError
from ..routers import agent_settings as agent_settings_module
from ..routers.agent_settings import MASK, create_agent_settings_router, mask_secrets
from .conftest import bypass_auth

_SETTINGS_TOOLS = {"list_settings", "get_setting", "change_setting", "change_protected_setting", "open_page"}


def _client(job_store: Any = None) -> tuple[TestClient, list[dict[str, Any]]]:
    """Mount the settings router with stand-in settings routes.

    Args:
        job_store: Passed to the router factory.

    Returns:
        A test client and the list of calls the stand-in routes received.
    """
    app = FastAPI()
    app.include_router(create_agent_settings_router(job_store=job_store))
    bypass_auth(app)
    calls: list[dict[str, Any]] = []

    @app.exception_handler(DomainError)
    async def _domain_error_handler(_request: Any, exc: DomainError) -> JSONResponse:
        """Mirror the app envelope."""
        return JSONResponse(status_code=exc.status_code, content={"code": exc.code, "params": exc.params})

    @app.get("/billing/byok/keys")
    def _keys(request: Request) -> dict[str, Any]:
        """Return a key list that carries a leaked secret."""
        calls.append({"path": "/billing/byok/keys", "auth": request.headers.get("authorization")})
        return {"keys": [{"provider": "openai", "last4": "abcd", "api_key": "sk-live-123", "verified": True}]}

    @app.patch("/account/notification-preferences")
    async def _notifications(request: Request) -> dict[str, Any]:
        """Echo the patch."""
        body = await request.json()
        calls.append({"path": "/account/notification-preferences", "body": body})
        return {"hold_minutes": body.get("hold_minutes")}

    @app.put("/account/model-privacy")
    def _privacy() -> JSONResponse:
        """Reject the change, the way a validation error would."""
        return JSONResponse(status_code=422, content={"detail": "bad policy", "token": "tok_123456"})

    @app.delete("/auth/security/passkeys/{credential_id}")
    def _remove_passkey(credential_id: str) -> dict[str, Any]:
        """Record the removed passkey."""
        calls.append({"path": f"/auth/security/passkeys/{credential_id}"})
        return {"removed": True}

    @app.put("/auth/security/email-codes")
    async def _email_codes(request: Request) -> dict[str, Any]:
        """Echo the toggle."""
        body = await request.json()
        calls.append({"path": "/auth/security/email-codes", "body": body})
        return {"email_2fa_enabled": body["enabled"]}

    return TestClient(app), calls


def test_list_settings_names_every_area() -> None:
    """The catalog marks protected and page-only steps per area."""
    client, _ = _client()
    rows = {row["key"]: row for row in client.get("/agent/settings").json()["settings"]}
    assert rows["account_deletion"]["page_only"] == ["delete the account"]
    assert rows["account_deletion"]["change_setting"] is False
    assert "security.remove_passkey" in rows["security"]["protected_actions"]
    for key in ("payment_methods", "api_token", "provider_keys"):
        assert rows[key]["protected_actions"] == []
        assert rows[key]["change_setting"] is False
    assert rows["appearance"]["change_setting"] is True


class _LinkStore:
    """Stand-in job store; only its ``engine`` is read."""

    engine = object()


def _area_tabs(job_store: Any = None) -> set[str]:
    """List the tabs of the areas ``list_settings`` returns.

    Args:
        job_store: Passed to the router factory.

    Returns:
        The set of tabs.
    """
    app = FastAPI()
    app.include_router(create_agent_settings_router(job_store=job_store))
    bypass_auth(app)
    return {row["tab"] for row in TestClient(app).get("/agent/settings").json()["settings"]}


def test_list_settings_keeps_every_tab_on_the_hosted_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hosted service hides no tab, so it lists every settings area."""
    monkeypatch.setattr(agent_settings_module.settings, "local_mode", False)
    assert _area_tabs() == {spec.tab for spec in agent_settings_module._SETTINGS.values()}


@pytest.mark.parametrize("local_mode", [False, True])
def test_tools_know_no_agent_engine_setting(monkeypatch: pytest.MonkeyPatch, local_mode: bool) -> None:
    """No app has an Agent tab or an agent engine setting, so the tools reject both as unknown."""
    monkeypatch.setattr(agent_settings_module.settings, "local_mode", local_mode)
    client, calls = _client()
    rows = client.get("/agent/settings").json()["settings"]
    assert "agent_engine" not in {row["key"] for row in rows}
    assert client.get("/agent/settings/agent_engine").status_code == 422
    assert client.post("/agent/settings/agent_engine", json={"values": {"engine": "codex"}}).status_code == 422
    assert client.post("/agent/open-page", json={"tab": "agent"}).status_code == 422
    assert calls == []


def test_list_settings_hides_hosted_tabs_in_an_unlinked_mac_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unlinked Mac app leaves out the security and billing areas."""
    monkeypatch.setattr(agent_settings_module.settings, "local_mode", True)
    monkeypatch.setattr(agent_settings_module.hosted_link, "link_status", lambda _engine, _username: None)
    tabs = _area_tabs(_LinkStore())
    assert not tabs & {"security", "billing"}
    assert "account" in tabs
    assert not _area_tabs() & {"security", "billing"}


def test_list_settings_shows_billing_but_not_security_in_a_linked_mac_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """A linked Mac app lists the billing areas again, but never the security area."""
    monkeypatch.setattr(agent_settings_module.settings, "local_mode", True)
    monkeypatch.setattr(agent_settings_module.hosted_link, "link_status", lambda _engine, _username: object())
    tabs = _area_tabs(_LinkStore())
    assert "billing" in tabs
    assert "security" not in tabs


def _mac_client(monkeypatch: pytest.MonkeyPatch, *, linked: bool) -> tuple[TestClient, list[dict[str, Any]]]:
    """Build a Mac app client, linked or not.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        linked: Whether the Mac app has a hosted account linked.

    Returns:
        A test client and the list of calls the stand-in routes received.
    """
    monkeypatch.setattr(agent_settings_module.settings, "local_mode", True)
    status = object() if linked else None
    monkeypatch.setattr(agent_settings_module.hosted_link, "link_status", lambda _engine, _username: status)
    return _client(_LinkStore())


def _assert_hosted_only(resp: Any, tab: str) -> None:
    """Check that a tool call was refused because its tab is hidden.

    Args:
        resp: The tool response.
        tab: The hidden tab the response must name.
    """
    assert resp.status_code == 409
    assert resp.json() == {"code": "agent_settings.hosted_only", "params": {"tab": tab}}


def test_tools_refuse_hidden_tabs_in_an_unlinked_mac_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every tool refuses the security and billing areas in an unlinked Mac app."""
    client, calls = _mac_client(monkeypatch, linked=False)
    _assert_hosted_only(client.get("/agent/settings/security"), "security")
    _assert_hosted_only(client.get("/agent/settings/payment_methods"), "billing")
    _assert_hosted_only(client.post("/agent/settings/payment_methods", json={"values": {"a": 1}}), "billing")
    _assert_hosted_only(
        client.post("/agent/protected-settings", json={"action": "security.remove_passkey", "target": "c"}),
        "security",
    )
    _assert_hosted_only(client.post("/agent/open-page", json={"tab": "billing"}), "billing")
    _assert_hosted_only(client.post("/agent/open-page", json={"tab": "security"}), "security")
    assert calls == []
    assert client.get("/agent/settings/provider_keys").status_code == 200
    assert client.post("/agent/open-page", json={"tab": "account"}).status_code == 200


def test_tools_refuse_only_security_in_a_linked_mac_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """A linked Mac app opens billing again, but still refuses the security area."""
    client, _ = _mac_client(monkeypatch, linked=True)
    assert client.post("/agent/open-page", json={"tab": "billing"}).json() == {
        "navigate": {"kind": "settings", "tab": "billing"}
    }
    _assert_hosted_only(client.post("/agent/open-page", json={"tab": "security"}), "security")
    _assert_hosted_only(
        client.post("/agent/protected-settings", json={"action": "security.email_codes", "values": {"enabled": True}}),
        "security",
    )


def test_get_setting_forwards_auth_and_masks_secrets() -> None:
    """A read forwards the bearer token and masks credentials in the reply."""
    client, calls = _client()
    resp = client.get("/agent/settings/provider_keys", headers={"Authorization": "Bearer abc"})
    assert resp.status_code == 200
    key = resp.json()["result"]["keys"][0]
    assert key == {"provider": "openai", "last4": "abcd", "api_key": MASK, "verified": True}
    assert calls == [{"path": "/billing/byok/keys", "auth": "Bearer abc"}]


def test_get_setting_rejects_unknown_key() -> None:
    """An unknown settings key fails validation."""
    client, _ = _client()
    assert client.get("/agent/settings/billing_secrets").status_code == 422


def test_change_setting_forwards_partial_patch() -> None:
    """An ordinary change goes to the setting's own write route."""
    client, calls = _client()
    resp = client.post("/agent/settings/notifications", json={"values": {"hold_minutes": 15}})
    assert resp.json() == {"ok": True, "result": {"hold_minutes": 15}}
    assert calls == [{"path": "/account/notification-preferences", "body": {"hold_minutes": 15}}]


def test_change_setting_passes_route_errors_through_masked() -> None:
    """A rejected write keeps its status and masks secrets in the error body."""
    client, _ = _client()
    resp = client.post("/agent/settings/privacy", json={"values": {"data_policy": "x"}})
    assert resp.status_code == 422
    assert resp.json() == {"detail": "bad policy", "token": MASK}


@pytest.mark.parametrize("key", ["security", "payment_methods", "api_token", "provider_keys", "account_deletion"])
def test_change_setting_refuses_protected_and_page_only_areas(key: str) -> None:
    """Protected and page-only areas have no ordinary write."""
    client, _ = _client()
    resp = client.post(f"/agent/settings/{key}", json={"values": {"anything": 1}})
    assert resp.status_code == 409
    assert resp.json()["code"] == "agent_settings.not_writable"


def test_change_setting_appearance_returns_client_patch() -> None:
    """Theme and language changes go back to the browser to apply."""
    client, _ = _client()
    resp = client.post("/agent/settings/appearance", json={"values": {"theme": "dark", "locale": "he"}})
    assert resp.json() == {"ok": True, "client_patch": {"theme": "dark", "locale": "he"}}
    bad = client.post("/agent/settings/appearance", json={"values": {"theme": "neon"}})
    assert bad.status_code == 422


def test_protected_action_quotes_target_and_checks_inputs() -> None:
    """A protected action needs its target and sends it as one path segment."""
    client, calls = _client()
    resp = client.post("/agent/protected-settings", json={"action": "security.remove_passkey", "target": "cred-1_A.b"})
    assert resp.json() == {"ok": True, "result": {"removed": True}}
    assert calls[-1] == {"path": "/auth/security/passkeys/cred-1_A.b"}
    missing = client.post("/agent/protected-settings", json={"action": "security.remove_passkey"})
    assert missing.status_code == 422
    traversal = client.post(
        "/agent/protected-settings", json={"action": "security.remove_passkey", "target": "../../account"}
    )
    assert traversal.status_code == 422
    toggle = client.post(
        "/agent/protected-settings", json={"action": "security.email_codes", "values": {"enabled": True}}
    )
    assert toggle.json()["result"] == {"email_2fa_enabled": True}


def test_protected_action_rejects_unknown_action() -> None:
    """Only the listed protected actions run; account deletion is not one."""
    client, _ = _client()
    resp = client.post("/agent/protected-settings", json={"action": "account.delete"})
    assert resp.status_code == 422


def test_open_page_returns_navigation() -> None:
    """``open_page`` returns a settings tab or a safe in-app route only."""
    client, _ = _client()
    assert client.post("/agent/open-page", json={"tab": "billing"}).json() == {
        "navigate": {"kind": "settings", "tab": "billing"}
    }
    assert client.post("/agent/open-page", json={"path": "/optimizations"}).json() == {
        "navigate": {"kind": "route", "path": "/optimizations"}
    }
    for path in ("//evil.example", "https://evil.example", "/a?b=1", "javascript:alert(1)", ""):
        assert client.post("/agent/open-page", json={"path": path}).status_code == 422
    assert client.post("/agent/open-page", json={"tab": "secret"}).status_code == 422


def test_mask_secrets_keeps_flags_and_masks_shapes() -> None:
    """Booleans, numbers and empty values stay; credential strings are masked."""
    masked = mask_secrets(
        {
            "has_password": True,
            "token_count": 2,
            "password": None,
            "api_token": {"value": "x"},
            "note": "use sk-proj-abcdefghijklmnop now",
            "items": [{"secret": "s"}],
        }
    )
    assert masked == {
        "has_password": True,
        "token_count": 2,
        "password": None,
        "api_token": MASK,
        "note": f"use {MASK} now",
        "items": [{"secret": MASK}],
    }


def test_settings_tools_are_on_the_generalist_roster() -> None:
    """Every settings tool is offered, with the right approval class."""
    assert set(_ALWAYS_TOOLS) >= _SETTINGS_TOOLS
    assert "change_setting" in _DESTRUCTIVE_TOOLS
    assert {"change_protected_setting"} == _ALWAYS_CONFIRM_TOOLS


def test_router_tags_every_route_for_the_agent() -> None:
    """Each settings route is an ``agent`` tool whose name is its operationId."""
    app = FastAPI()
    app.include_router(create_agent_settings_router())
    ops = {
        op["operationId"]: op
        for path in app.openapi()["paths"].values()
        for op in path.values()
        if "agent" in op.get("tags", [])
    }
    assert set(ops) == _SETTINGS_TOOLS
    assert ops["change_protected_setting"]["x-mcp-annotations"] == {"destructiveHint": True}


@pytest.mark.parametrize("mode", ["ask", "auto_safe", "yolo"])
def test_protected_settings_always_need_approval(mode: str) -> None:
    """The protected tool asks in every trust mode and on screen chats."""
    assert _needs_approval("change_protected_setting", mode) is True
    assert _screen_needs_approval("change_protected_setting", mode) is True


def test_change_setting_follows_trust_mode() -> None:
    """Ordinary settings changes ask in Ask and Auto-safe, not in YOLO."""
    assert _needs_approval("change_setting", "ask") is True
    assert _needs_approval("change_setting", "auto_safe") is True
    assert _needs_approval("change_setting", "yolo") is False
    assert _needs_approval("open_page", "auto_safe") is False


def _fake_tool(name: str) -> dspy.Tool:
    """Build a ``dspy.Tool`` that returns ``"ran"``.

    Args:
        name: The tool name.

    Returns:
        The tool.
    """

    async def func(**_kwargs: Any) -> str:
        """Return a fixed value."""
        return "ran"

    return dspy.Tool(func=func, name=name, desc="test tool", args={}, arg_types={}, arg_desc={})


@pytest.mark.asyncio
async def test_injected_policy_cannot_skip_protected_approval() -> None:
    """A policy that approves everything still gets an approval card."""
    events: list[dict] = []
    registry = ApprovalRegistry()
    tool = _wrap_tool_with_approval(
        _fake_tool("change_protected_setting"),
        trust_mode="yolo",
        registry=registry,
        emit=events.append,
        outer_loop=asyncio.get_running_loop(),
        needs_approval=lambda _name, _mode: False,
    )
    call = asyncio.create_task(tool.func._async_body())
    for _ in range(20):
        await asyncio.sleep(0)
        if any(e["event"] == "pending_approval" for e in events):
            break
    pending = next((e for e in events if e["event"] == "pending_approval"), None)
    assert pending is not None
    registry.resolve(pending["data"]["id"], False)
    assert await call == "User declined"


@pytest.mark.parametrize(
    "action",
    [
        "payment_methods.update",
        "payment_methods.remove",
        "api_token.revoke",
        "provider_keys.verify",
        "provider_keys.remove",
    ],
)
def test_protected_tool_refuses_billing_and_key_actions(action: str) -> None:
    """The agent cannot change payment methods, the API token or provider keys."""
    client, calls = _client()
    resp = client.post("/agent/protected-settings", json={"action": action, "target": "c", "values": {"a": 1}})
    assert resp.status_code == 422
    assert calls == []


def _mcp_specs() -> list[McpTool]:
    """Build a live MCP listing with the protected tool and one ordinary tool.

    Returns:
        The MCP tool specs, as the ``/mcp`` mount lists them.
    """
    schema = {"type": "object", "properties": {}}
    return [
        McpTool(
            name="change_protected_setting",
            description="protected",
            inputSchema=schema,
            annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
        ),
        McpTool(name="list_settings", description="read", inputSchema=schema),
    ]


def test_training_ground_live_roster_drops_protected_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Optimization rollouts call tools with nobody to confirm, so the protected tool is never listed."""

    async def listing(_url: str, _headers: dict[str, str] | None) -> list[McpTool]:
        """Return the fake live listing without a network call."""
        return _mcp_specs()

    monkeypatch.delenv("SKYNET_TOOL_RELAY_TOKEN", raising=False)
    monkeypatch.delenv("APP_TOOL_RELAY_TOKEN", raising=False)
    monkeypatch.setattr(run_react, "_list_live_tool_specs", listing)
    tools = run_react._list_live_tools("http://mcp.local/mcp", None)
    assert [tool.name for tool in tools] == ["list_settings"]


class _RosterSeenError(Exception):
    """Stop the react-serve driver once it built its roster."""


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ask", "auto_safe", "yolo"])
async def test_react_serve_roster_drops_protected_tool(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """A served react chat can rename tools or run in YOLO, so it never gets the protected tool."""
    seen: list[str] = []

    @contextlib.asynccontextmanager
    async def session(_url: str, auth_header: str | None = None) -> AsyncIterator[Any]:
        """Yield a fake MCP session whose listing holds the protected tool."""

        async def list_tools() -> Any:
            """Return the fake listing."""
            return SimpleNamespace(tools=_mcp_specs())

        yield SimpleNamespace(list_tools=list_tools)

    def capture(_hashes: Any, tools: list[dspy.Tool], strict: bool) -> None:
        """Record the roster the driver built, then stop the turn."""
        seen.extend(tool.name for tool in tools)
        raise _RosterSeenError

    monkeypatch.setattr(react_serve, "_mcp_session", session)
    monkeypatch.setattr(react_serve, "_assert_tool_set_matches", capture)
    with pytest.raises(_RosterSeenError):
        await react_serve._drive_react_chat(
            signature_cls=object,
            program_state_json="{}",
            react_overlay=SimpleNamespace(tool_source={}, tool_schema_hashes={}),
            user_message="hi",
            trust_mode=mode,
            registry=ApprovalRegistry(),
            emit=lambda _event: None,
            lm=object(),
            mcp_url="http://mcp.local/mcp",
            auth_header=None,
        )
    assert seen == ["list_settings"]
