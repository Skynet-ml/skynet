"""Tests for the platform tools of the remote MCP server (SKY-320).

The app mounts the real remote MCP server with the platform extension over a
few stand-in routes. The routes verify the backend session token themselves,
so each test checks what reaches a REST route through a platform tool.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ...config import settings
from ...storage.models import Base, UserModel
from ..auth import _verify_hs256
from ..errors import DomainError
from ..mcp_mount import AGENT_TAG
from ..remote_mcp import mount_remote_mcp
from ..remote_mcp_routes import register_route_tools, session_token
from .test_remote_mcp import (
    READ_TOKEN,
    RESOURCE,
    USER,
    WRITE_TOKEN,
    _call,
    _error_text,
    _payload,
    _rpc,
    _seed_token,
    _Store,
)

SECRET = "test" * 8


def _caller(authorization: str | None) -> str:
    """Return the account a stand-in route sees in its session token.

    Args:
        authorization: The ``Authorization`` header.

    Returns:
        The token's ``name`` claim.
    """
    payload = _verify_hs256((authorization or "").removeprefix("Bearer "), SECRET)
    assert payload["role"] == "user"
    return payload["name"]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Build an app with agent-tagged stand-in routes and the platform tools.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Yields:
        The test client.
    """
    monkeypatch.setattr(settings, "mcp_resource_url", RESOURCE)
    monkeypatch.setattr(settings, "backend_auth_secret", SecretStr(SECRET))
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(UserModel(email=USER, name="Alice Example", password_hash="x"))
        session.commit()
    _seed_token(engine, READ_TOKEN, "runs:read", "mg_read")
    _seed_token(engine, WRITE_TOKEN, "runs:read runs:write", "mg_write")
    app = FastAPI()

    @app.get("/folders", tags=[AGENT_TAG], operation_id="list_folders")
    def list_folders(authorization: str | None = Header(default=None)) -> dict[str, str]:
        """Stand in for an agent-tagged read route."""
        return {"caller": _caller(authorization)}

    @app.post("/folders", tags=[AGENT_TAG], operation_id="create_folder")
    def create_folder(authorization: str | None = Header(default=None)) -> dict[str, str]:
        """Stand in for an agent-tagged write route."""
        return {"caller": _caller(authorization)}

    @app.get("/internal", operation_id="internal_route")
    def internal_route() -> dict[str, str]:
        """Stand in for a route without the agent tag."""
        return {}

    @app.post("/optimize", tags=[AGENT_TAG], operation_id="optimize")
    def optimize() -> dict[str, str]:
        """Stand in for the paid run route that the projection leaves out."""
        return {}

    @app.get("/billing/wallet", tags=[AGENT_TAG], operation_id="billing_wallet")
    def billing_wallet() -> dict[str, str]:
        """Stand in for a billing read that stays a tool."""
        return {}

    @app.post("/billing/top-up", tags=[AGENT_TAG], operation_id="billing_top_up")
    def billing_top_up() -> dict[str, str]:
        """Stand in for a billing change that the projection leaves out."""
        return {}

    mount_remote_mcp(
        app, service="svc", job_store=_Store(engine), get_worker=object, extensions=[register_route_tools(app)]
    )
    with TestClient(app) as test_client:
        yield test_client


def _names(client: TestClient) -> set[str]:
    """Return the names of the listed tools."""
    resp = _rpc(client, "tools/list", {})
    assert resp.status_code == 200, resp.text
    return {tool["name"] for tool in resp.json()["result"]["tools"]}


def test_only_agent_routes_become_platform_tools(client: TestClient) -> None:
    """List agent-tagged routes as platform tools, without paid run, billing change or untagged routes."""
    names = _names(client)
    assert {"platform_list_folders", "platform_create_folder", "platform_billing_wallet"} <= names
    assert not {name for name in names if "optimize" in name or "top_up" in name or "internal" in name}
    assert "get_run" in names


def test_platform_tool_calls_the_route_as_the_principal(client: TestClient) -> None:
    """Sign the in-process call with a session token for the token's account."""
    assert _payload(_call(client, "platform_list_folders", {}, token=READ_TOKEN)) == {"caller": USER}
    assert _payload(_call(client, "platform_create_folder", {})) == {"caller": USER}


def test_read_token_cannot_call_a_write_route(client: TestClient) -> None:
    """Refuse a POST route to a token without runs:write."""
    assert "insufficient_scope" in _error_text(_call(client, "platform_create_folder", {}, token=READ_TOKEN))


def test_admin_allowlisted_account_is_refused(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep platform tools from accounts that every session makes an admin."""
    monkeypatch.setattr(settings, "admin_usernames", USER)
    assert "admin" in _error_text(_call(client, "platform_list_folders", {}))


def test_missing_secret_is_a_tool_error(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Report a server without a backend auth secret as a tool error."""
    monkeypatch.setattr(settings, "backend_auth_secret", None)
    assert "not configured" in _error_text(_call(client, "platform_list_folders", {}))


def test_session_token_expires_after_a_minute() -> None:
    """Sign a token that the backend verifies until it expires."""
    payload = _verify_hs256(session_token(USER, SECRET), SECRET)
    assert payload["name"] == USER
    assert payload["exp"] - payload["iat"] == 60
    with pytest.raises(DomainError, match="invalid or expired"):
        _verify_hs256(session_token(USER, SECRET, now=1), SECRET)
