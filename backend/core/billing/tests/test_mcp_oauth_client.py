"""Connect, call, refresh and disconnect a local OAuth MCP server end to end.

The server below runs on 127.0.0.1 in a thread. It serves the MCP OAuth
discovery documents, dynamic registration, a PKCE authorization endpoint, a
token endpoint, RFC 7009 revocation and a JSON-RPC MCP endpoint that checks
the bearer token. The tests drive the real HTTP stack, so no mock transport
hides a wrong header or URL.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import uvicorn
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from core.billing import mcp_oauth_client as client
from core.billing.budgets import BudgetService
from core.billing.mcp_broker import McpToolsBroker
from core.billing.model_gateway import ModelGateway
from core.billing.runtime import BudgetRuntime
from core.config import settings
from core.storage.models import Base, BillingCustomerModel, McpServerConnectionModel

REDIRECT_URI = "https://app.example/api/mcp/connections/callback"


@dataclass
class OAuthServerState:
    """Mutable behavior and records of the local OAuth MCP server."""

    base: str = ""
    registration: bool = True
    pkce: bool = True
    expires_in: int = 3600
    rotate_refresh: bool = True
    refresh_status: int = 200
    issuer: str | None = None
    clients: dict[str, dict[str, Any]] = field(default_factory=dict)
    codes: dict[str, dict[str, Any]] = field(default_factory=dict)
    access: set[str] = field(default_factory=set)
    refresh: set[str] = field(default_factory=set)
    revoked: list[str] = field(default_factory=list)
    token_forms: list[dict[str, str]] = field(default_factory=list)
    mcp_methods: list[str] = field(default_factory=list)
    mcp_auth: list[str] = field(default_factory=list)


def _form(body: bytes) -> dict[str, str]:
    """Parse a form body into single values.

    Args:
        body: ``application/x-www-form-urlencoded`` bytes.

    Returns:
        The fields.
    """
    return {key: values[0] for key, values in parse_qs(body.decode()).items()}


def _app(state: OAuthServerState) -> Starlette:
    """Build the OAuth MCP test server.

    Args:
        state: Behavior switches and records shared with the test.

    Returns:
        The ASGI app.
    """

    def issue() -> dict[str, Any]:
        """Mint a new token pair."""
        access, refresh = f"at-{secrets.token_hex(12)}", f"rt-{secrets.token_hex(12)}"
        state.access.add(access)
        state.refresh.add(refresh)
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": state.expires_in,
            "refresh_token": refresh,
        }

    async def prm(_request: Request) -> Response:
        """Serve RFC 9728 protected resource metadata."""
        return JSONResponse(
            {"resource": f"{state.base}/mcp", "authorization_servers": [state.base], "scopes_supported": ["tools"]}
        )

    async def as_metadata(_request: Request) -> Response:
        """Serve RFC 8414 authorization server metadata."""
        document = {
            "issuer": state.issuer or state.base,
            "authorization_endpoint": f"{state.base}/authorize",
            "token_endpoint": f"{state.base}/token",
            "revocation_endpoint": f"{state.base}/revoke",
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"] if state.pkce else ["plain"],
        }
        if state.registration:
            document["registration_endpoint"] = f"{state.base}/register"
        return JSONResponse(document)

    async def register(request: Request) -> Response:
        """Register a public client."""
        body = await request.json()
        client_id = f"client-{secrets.token_hex(4)}"
        state.clients[client_id] = body
        return JSONResponse({**body, "client_id": client_id}, status_code=201)

    async def authorize(request: Request) -> Response:
        """Approve at once, as a user who clicks Allow."""
        query = request.query_params
        if query["client_id"] not in state.clients or query["code_challenge_method"] != "S256":
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        code = secrets.token_hex(8)
        state.codes[code] = dict(query)
        return RedirectResponse(f"{query['redirect_uri']}?code={code}&state={query['state']}", status_code=302)

    async def token(request: Request) -> Response:
        """Trade a code or a refresh token for tokens."""
        form = _form(await request.body())
        state.token_forms.append(form)
        if form["grant_type"] == "authorization_code":
            grant = state.codes.pop(form["code"], None)
            digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
            challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
            if grant is None or grant["code_challenge"] != challenge or grant["redirect_uri"] != form["redirect_uri"]:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            return JSONResponse(issue())
        if state.refresh_status != 200:
            return JSONResponse({"error": "invalid_grant"}, status_code=state.refresh_status)
        if form["refresh_token"] not in state.refresh:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        pair = issue()
        if state.rotate_refresh:
            state.refresh.discard(form["refresh_token"])
        else:
            pair.pop("refresh_token")
        return JSONResponse(pair)

    async def revoke(request: Request) -> Response:
        """Revoke one token as RFC 7009 describes."""
        value = _form(await request.body())["token"]
        state.revoked.append(value)
        state.access.discard(value)
        state.refresh.discard(value)
        return Response(status_code=200)

    async def mcp(request: Request) -> Response:
        """Answer JSON-RPC MCP calls for a valid bearer token only."""
        if request.method != "POST":
            return Response(status_code=405)
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer ") or header[7:] not in state.access:
            challenge = f'Bearer resource_metadata="{state.base}/.well-known/oauth-protected-resource/mcp"'
            return JSONResponse({"error": "invalid_token"}, status_code=401, headers={"WWW-Authenticate": challenge})
        body = await request.json()
        state.mcp_methods.append(body["method"])
        state.mcp_auth.append(header)
        if "id" not in body:
            return Response(status_code=202)
        if body["method"] == "initialize":
            result = {
                "protocolVersion": body["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "oauth-fixture", "version": "1"},
            }
        elif body["method"] == "tools/list":
            schema = {"type": "object"}
            result = {
                "tools": [
                    {"name": "add", "description": "Add two numbers.", "inputSchema": schema},
                    {"name": "whoami", "description": "Echo the caller's credential.", "inputSchema": schema},
                ]
            }
        elif body["method"] == "tools/call":
            name, arguments = body["params"]["name"], body["params"].get("arguments", {})
            text = str(arguments["a"] + arguments["b"]) if name == "add" else header
            result = {"content": [{"type": "text", "text": text}], "isError": False}
        else:
            return JSONResponse({"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601, "message": "no"}})
        return JSONResponse({"jsonrpc": "2.0", "id": body["id"], "result": result})

    return Starlette(
        routes=[
            Route("/.well-known/oauth-protected-resource/mcp", prm),
            Route("/.well-known/oauth-authorization-server", as_metadata),
            Route("/register", register, methods=["POST"]),
            Route("/authorize", authorize),
            Route("/token", token, methods=["POST"]),
            Route("/revoke", revoke, methods=["POST"]),
            Route("/mcp", mcp, methods=["GET", "POST", "DELETE"]),
        ]
    )


@pytest.fixture
def oauth_server() -> Iterator[OAuthServerState]:
    """Run the OAuth MCP test server on a free loopback port.

    Yields:
        The server's state; ``base`` is its origin.
    """
    state = OAuthServerState()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    state.base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(_app(state), log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "The OAuth test server did not start."
        time.sleep(0.01)
    yield state
    server.should_exit = True
    thread.join(timeout=10)
    sock.close()


@pytest.fixture
def connections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Bind a fresh SQLite store and a vault key, and allow loopback servers.

    Yields:
        The engine.
    """
    monkeypatch.setattr(settings, "discover_allow_private", True)
    monkeypatch.setattr(settings, "byok_vault_key", SecretStr(Fernet.generate_key().decode()))
    engine = create_engine(f"sqlite:///{tmp_path / 'mcp.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    client.configure_mcp_connections(engine)
    yield engine
    client.configure_mcp_connections(None)
    engine.dispose()


def _sign_in(state: OAuthServerState, name: str = "tracker") -> str:
    """Start a connection, approve it on the server and finish it.

    Args:
        state: The running test server.
        name: Connector name.

    Returns:
        The access token the server issued.
    """
    authorize_url = client.start_connection("alice", name, f"{state.base}/mcp", REDIRECT_URI)
    answer = httpx.get(authorize_url, follow_redirects=False, trust_env=False)
    assert answer.status_code == 302
    callback = parse_qs(urlsplit(answer.headers["location"]).query)
    assert client.finish_connection(callback["state"][0], callback["code"][0]) == ("alice", name)
    return next(iter(state.access))


def _rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build one guest JSON-RPC request.

    Args:
        method: MCP method.
        params: Method parameters.

    Returns:
        The request body.
    """
    return {"jsonrpc": "2.0", "id": 3, "method": method, "params": params or {}}


def _row(engine: Any, name: str = "tracker") -> McpServerConnectionModel | None:
    """Read one stored connection of alice.

    Args:
        engine: The test engine.
        name: Connector name.

    Returns:
        The row, or ``None``.
    """
    with Session(engine) as session:
        return session.query(McpServerConnectionModel).filter_by(username="alice", name=name).first()


def test_sign_in_uses_pkce_resource_and_dynamic_registration(oauth_server: OAuthServerState, connections: Any) -> None:
    """Register once, send PKCE S256 and the RFC 8707 resource, and store only ciphertext."""
    token = _sign_in(oauth_server)

    [registered] = oauth_server.clients.values()
    assert registered["redirect_uris"] == [REDIRECT_URI]
    assert registered["token_endpoint_auth_method"] == "none"
    [exchange] = oauth_server.token_forms
    assert exchange["grant_type"] == "authorization_code"
    assert exchange["resource"] == f"{oauth_server.base}/mcp"
    assert exchange["code_verifier"]
    row = _row(connections)
    assert row is not None
    assert row.status == client.STATUS_CONNECTED
    assert row.pending_state_hash is None
    assert row.pending_encrypted is None
    assert token not in (row.credentials_encrypted or "")
    [view] = client.list_connections("alice")
    assert (view.name, view.server_url) == ("tracker", f"{oauth_server.base}/mcp")
    assert client.list_connections("bob") == []


def test_state_works_once_and_expires(oauth_server: OAuthServerState, connections: Any) -> None:
    """Refuse a reused or old state before any token request."""
    authorize_url = client.start_connection("alice", "tracker", f"{oauth_server.base}/mcp", REDIRECT_URI)
    callback = parse_qs(urlsplit(httpx.get(authorize_url, trust_env=False).headers["location"]).query)
    state, code = callback["state"][0], callback["code"][0]
    client.finish_connection(state, code)
    with pytest.raises(client.McpOAuthError) as reused:
        client.finish_connection(state, code)
    assert reused.value.code == "mcp_connection.state_invalid"

    authorize_url = client.start_connection("alice", "late", f"{oauth_server.base}/mcp", REDIRECT_URI)
    late = parse_qs(urlsplit(httpx.get(authorize_url, trust_env=False).headers["location"]).query)
    with Session(connections) as session:
        row = session.query(McpServerConnectionModel).filter_by(name="late").one()
        row.pending_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    with pytest.raises(client.McpOAuthError, match="too old"):
        client.finish_connection(late["state"][0], late["code"][0])
    assert len(oauth_server.token_forms) == 1


def test_server_without_registration_gives_a_clear_error(oauth_server: OAuthServerState, connections: Any) -> None:
    """Say that the server needs a pre-registered client, and store nothing."""
    oauth_server.registration = False
    with pytest.raises(client.McpOAuthError) as refused:
        client.start_connection("alice", "tracker", f"{oauth_server.base}/mcp", REDIRECT_URI)
    assert (refused.value.code, refused.value.status) == ("mcp_connection.registration_required", 409)
    assert "register first" in refused.value.message
    assert _row(connections) is None


def test_server_without_pkce_s256_is_refused(oauth_server: OAuthServerState, connections: Any) -> None:
    """Refuse an authorization server that cannot do PKCE S256."""
    oauth_server.pkce = False
    with pytest.raises(client.McpOAuthError) as refused:
        client.start_connection("alice", "tracker", f"{oauth_server.base}/mcp", REDIRECT_URI)
    assert refused.value.code == "mcp_connection.pkce_unsupported"


@pytest.mark.parametrize(
    ("name", "url", "code"),
    [
        ("maps", "https://mcp.example/mcp", "mcp_connection.name_reserved"),
        ("Bad Name", "https://mcp.example/mcp", "mcp_connection.invalid_name"),
        ("tracker", "ftp://mcp.example/mcp", "mcp_connection.invalid_url"),
        ("tracker", "https://mcp.example/mcp?token=x", "mcp_connection.invalid_url"),
        ("tracker", "https://user:pw@mcp.example/mcp", "mcp_connection.invalid_url"),
    ],
)
def test_bad_name_or_url_is_refused_before_any_request(connections: Any, name: str, url: str, code: str) -> None:
    """Refuse catalog names, malformed names and unsafe URLs."""
    with pytest.raises(client.McpOAuthError) as refused:
        client.start_connection("alice", name, url, REDIRECT_URI)
    assert refused.value.code == code


def test_private_server_is_refused_without_the_deployment_opt_in(
    oauth_server: OAuthServerState, connections: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the SSRF policy of the broker for every OAuth fetch."""
    monkeypatch.setattr(settings, "discover_allow_private", False)
    with pytest.raises(client.McpOAuthError) as plain_http:
        client.start_connection("alice", "tracker", f"{oauth_server.base}/mcp", REDIRECT_URI)
    assert plain_http.value.code == "mcp_connection.invalid_url"
    loopback = oauth_server.base.replace("http://", "https://")
    with pytest.raises(client.McpOAuthError) as private:
        client.start_connection("alice", "tracker", f"{loopback}/mcp", REDIRECT_URI)
    assert private.value.code == "mcp_connection.blocked_address"
    assert oauth_server.clients == {}
    assert oauth_server.mcp_methods == []


def test_authorize_url_carries_resource_and_the_state_stays_hashed(
    oauth_server: OAuthServerState, connections: Any
) -> None:
    """Send the RFC 8707 resource on authorize, and store only a state hash and an encrypted verifier."""
    authorize_url = client.start_connection("alice", "tracker", f"{oauth_server.base}/mcp", REDIRECT_URI)
    query = {key: values[0] for key, values in parse_qs(urlsplit(authorize_url).query).items()}

    assert query["resource"] == f"{oauth_server.base}/mcp"
    assert query["code_challenge_method"] == "S256"
    assert query["redirect_uri"] == REDIRECT_URI
    row = _row(connections)
    assert row is not None
    assert row.pending_state_hash not in (None, query["state"])
    pending = row.pending_encrypted or ""
    callback = parse_qs(urlsplit(httpx.get(authorize_url, trust_env=False).headers["location"]).query)
    client.finish_connection(callback["state"][0], callback["code"][0])
    assert oauth_server.token_forms[0]["code_verifier"] not in pending


def test_an_unknown_state_is_refused(oauth_server: OAuthServerState, connections: Any) -> None:
    """Refuse a state that no sign-in started, before any token request."""
    with pytest.raises(client.McpOAuthError) as unknown:
        client.finish_connection("never-issued", "some-code")
    assert (unknown.value.code, unknown.value.status) == ("mcp_connection.state_invalid", 400)
    with pytest.raises(client.McpOAuthError):
        client.finish_connection("", "some-code")
    assert oauth_server.token_forms == []


def test_an_issuer_mismatch_is_refused(oauth_server: OAuthServerState, connections: Any) -> None:
    """Refuse AS metadata whose issuer is not the discovered server, to block a mix-up."""
    oauth_server.issuer = "https://other-issuer.example"
    with pytest.raises(client.McpOAuthError) as refused:
        client.start_connection("alice", "tracker", f"{oauth_server.base}/mcp", REDIRECT_URI)
    assert refused.value.code == "mcp_connection.issuer_mismatch"
    assert oauth_server.clients == {}
    assert _row(connections) is None


def test_broker_calls_allowed_tools_and_redacts_the_token(
    oauth_server: OAuthServerState, connections: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Add the bearer on the parent, refuse other methods, and redact echoed tokens."""
    caplog.set_level(logging.DEBUG)
    token = _sign_in(oauth_server)
    broker = McpToolsBroker(
        f"{oauth_server.base}/mcp",
        token_source=lambda: client.access_token("alice", "tracker"),
        allow_private=True,
        check_admission=lambda: None,
    )

    listing = json.loads(broker.dispatch(_rpc("tools/list")).body)
    added = json.loads(broker.dispatch(_rpc("tools/call", {"name": "add", "arguments": {"a": 2, "b": 3}})).body)
    echoed = json.loads(broker.dispatch(_rpc("tools/call", {"name": "whoami", "arguments": {}})).body)

    assert [tool["name"] for tool in listing["result"]["tools"]] == ["add", "whoami"]
    assert added["result"]["content"] == [{"type": "text", "text": "5"}]
    assert token not in json.dumps(echoed)
    assert set(oauth_server.mcp_auth) == {f"Bearer {token}"}
    count = len(oauth_server.mcp_methods)
    with pytest.raises(ValueError, match="outside the selected roster"):
        broker.dispatch(_rpc("tools/call", {"name": "drop_tables", "arguments": {}}))
    with pytest.raises(ValueError, match="does not permit"):
        broker.dispatch(_rpc("resources/read", {"uri": "file:///etc/passwd"}))
    assert len(oauth_server.mcp_methods) == count
    assert token not in caplog.text


def test_refresh_replaces_a_token_near_expiry(oauth_server: OAuthServerState, connections: Any) -> None:
    """Refresh within the margin, send the resource, and rotate the stored refresh token."""
    oauth_server.expires_in = 30
    first = _sign_in(oauth_server)

    second = client.access_token("alice", "tracker")

    assert second != first
    assert second in oauth_server.access
    refresh_form = oauth_server.token_forms[-1]
    assert refresh_form["grant_type"] == "refresh_token"
    assert refresh_form["resource"] == f"{oauth_server.base}/mcp"
    oauth_server.rotate_refresh = False
    third = client.access_token("alice", "tracker")
    fourth = client.access_token("alice", "tracker")
    assert len({second, third, fourth}) == 3
    assert oauth_server.token_forms[-1]["refresh_token"] == oauth_server.token_forms[-2]["refresh_token"]


def test_a_refreshed_token_is_reused_until_it_nears_expiry(oauth_server: OAuthServerState, connections: Any) -> None:
    """Read the row again under the lock, so a fresh token is not refreshed twice."""
    oauth_server.expires_in = 30
    _sign_in(oauth_server)
    oauth_server.expires_in = 3600

    refreshed = client.access_token("alice", "tracker")
    forms = len(oauth_server.token_forms)

    assert client.access_token("alice", "tracker") == refreshed
    assert len(oauth_server.token_forms) == forms


def test_a_refused_refresh_asks_for_a_new_sign_in(oauth_server: OAuthServerState, connections: Any) -> None:
    """Mark the connection for reauth on a 4xx refresh, and keep it on a 5xx."""
    oauth_server.expires_in = 30
    _sign_in(oauth_server)
    oauth_server.refresh_status = 503
    with pytest.raises(client.McpOAuthError) as outage:
        client.access_token("alice", "tracker")
    assert outage.value.code == "mcp_connection.exchange_failed"
    assert _row(connections).status == client.STATUS_CONNECTED

    oauth_server.refresh_status = 400
    with pytest.raises(client.McpOAuthError) as refused:
        client.access_token("alice", "tracker")
    assert refused.value.code == "mcp_connection.reauth_required"
    assert _row(connections).status == client.STATUS_REAUTH
    with pytest.raises(client.McpOAuthError, match="again"):
        client.connected_server_url("alice", "tracker")


def test_a_revoked_sign_in_gives_a_clear_run_error_without_the_token(
    oauth_server: OAuthServerState, connections: Any
) -> None:
    """Stop the broker's tool call with reauth_required, and keep every token out of the error."""
    oauth_server.expires_in = 30
    _sign_in(oauth_server)
    broker = McpToolsBroker(
        f"{oauth_server.base}/mcp",
        token_source=lambda: client.access_token("alice", "tracker"),
        allow_private=True,
        check_admission=lambda: None,
    )
    oauth_server.refresh_status = 400
    calls = len(oauth_server.mcp_methods)

    with pytest.raises(client.McpOAuthError) as refused:
        broker.dispatch(_rpc("tools/call", {"name": "add", "arguments": {"a": 1, "b": 1}}))

    assert refused.value.code == "mcp_connection.reauth_required"
    assert str(refused.value) == "Sign in to the MCP server tracker again."
    for token in oauth_server.access | oauth_server.refresh:
        assert token not in str(refused.value)
        assert token not in json.dumps(refused.value.params)
    assert len(oauth_server.mcp_methods) == calls


def test_disconnect_revokes_and_deletes_the_token(oauth_server: OAuthServerState, connections: Any) -> None:
    """Revoke the tokens on the server, delete the row, and stop later calls."""
    token = _sign_in(oauth_server)

    assert client.disconnect("alice", "tracker") is True

    assert token in oauth_server.revoked
    assert _row(connections) is None
    assert client.connected_server_url("alice", "tracker") is None
    with pytest.raises(client.McpOAuthError) as gone:
        client.access_token("alice", "tracker")
    assert gone.value.code == "mcp_connection.not_connected"
    assert client.disconnect("alice", "tracker") is False


@pytest.fixture
def gateway(tmp_path: Path, connections: Any) -> Iterator[ModelGateway]:
    """Start a real model gateway for alice on the same store.

    Yields:
        The gateway.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'budget.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(BillingCustomerModel(username="alice", stripe_customer_id="fixture", balance_cents=50))
        session.commit()
    ledger = BudgetService(engine=engine)
    budget = ledger.create("alice", 20, idempotency_key="fixture")
    gateway = ModelGateway(BudgetRuntime(ledger, username="alice", budget_id=budget.id, generation=0, phase="setup"))
    yield gateway
    gateway.close()
    engine.dispose()


def test_gateway_routes_a_service_name_to_the_oauth_broker_without_the_token(
    oauth_server: OAuthServerState, gateway: ModelGateway
) -> None:
    """Give the box only a scoped route, and serve its calls with the owner's token."""
    token = _sign_in(oauth_server)

    protected = gateway.protect_payload(
        {"env_connectors": ["tracker"]}, managed_key="upstream-secret", allow_private_tools=True
    )

    route = protected["_skynet_connector_routes"]["tracker"]
    assert set(route) == {"url", "token"}
    assert token not in json.dumps(protected)
    assert oauth_server.base not in json.dumps(protected)
    answer = gateway.dispatch_guest(
        route["token"], "/v1/_mcp", _rpc("tools/call", {"name": "add", "arguments": {"a": 1, "b": 1}}), {}
    )
    assert answer.status == 200
    assert json.loads(answer.body)["result"]["content"] == [{"type": "text", "text": "2"}]
    assert token not in answer.body.decode()
    assert gateway.runtime.service.get(gateway.runtime.budget_id, "alice").setup_spent_cents == 0


def test_gateway_refuses_an_unknown_service_name(gateway: ModelGateway) -> None:
    """Name the unknown connector, as for catalog names."""
    with pytest.raises(ValueError, match=r"Unknown connector: nowhere\. Connect it"):
        gateway.protect_payload({"env_connectors": ["nowhere"]}, managed_key="upstream-secret")
