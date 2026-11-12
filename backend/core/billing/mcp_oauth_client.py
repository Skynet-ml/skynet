"""Sign a user in to a remote MCP server with OAuth and keep the tokens on the parent.

This is the client side of the MCP authorization spec: discovery from the
server's 401 answer (RFC 9728 protected resource metadata, RFC 8414 server
metadata), dynamic client registration (RFC 7591), the authorization code
grant with PKCE S256 and the RFC 8707 ``resource`` parameter, refresh, and
RFC 7009 revocation on disconnect. Tokens are Fernet-encrypted under
``BYOK_VAULT_KEY``; only the broker on the parent reads them, so no token
reaches the box. Every fetch pins the resolved address with the same SSRF
policy as the MCP broker and never follows a redirect.

The user's own account at the MCP server pays for its tools, so these
connections carry no Skynet per-call meter: a run pays only for its sandbox.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.fernet import InvalidToken
from mcp.client.auth.oauth2 import PKCEParameters
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    extract_field_from_www_auth,
    extract_scope_from_www_auth,
    get_client_metadata_scopes,
    issuers_match,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata, OAuthToken, ProtectedResourceMetadata
from mcp.shared.auth_utils import check_resource_allowed, resource_url_from_server_url
from mcp.types import LATEST_PROTOCOL_VERSION
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from ..config import settings
from ..connectors.vault import vault_cipher
from ..i18n_en import t_en
from ..storage.models import McpServerConnectionModel
from .mcp_broker import _pinned_address, _PinnedTransport

STATUS_PENDING = "pending"
STATUS_CONNECTED = "connected"
STATUS_REAUTH = "reauth_required"

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
_STATE_TTL = timedelta(minutes=10)
# Refresh a little early so a token does not expire between the check and the tool call.
_REFRESH_MARGIN = timedelta(seconds=60)
_TIMEOUT = httpx.Timeout(15.0)
_REFRESH_REFUSED_ERRORS = frozenset({"invalid_grant", "invalid_client", "unauthorized_client"})

_engine: Engine | None = None


class McpOAuthError(ValueError):
    """Report one sign-in failure with an i18n key and a token-free English message.

    It is a ``ValueError`` so the model gateway passes it through like its other
    connector setup errors. The params never hold a token, secret or response body.
    """

    def __init__(self, code: str, status: int, **params: str) -> None:
        """Keep the i18n key, its params and the HTTP status for the router.

        Args:
            code: The i18n key, such as ``mcp_connection.registration_required``.
            status: HTTP status the router returns.
            **params: Safe substitution values, such as the connection name.
        """
        message = t_en(code, **params)
        super().__init__(message)
        self.code = code
        self.params = params
        self.message = message
        self.status = status


@dataclass(frozen=True)
class ConnectionView:
    """Token-free description of one MCP server connection, safe to return to clients."""

    name: str
    server_url: str
    status: str
    connected_at: datetime


def configure_mcp_connections(engine: Engine | None) -> None:
    """Bind the database that keeps MCP server connections.

    Args:
        engine: The application engine, or ``None`` to unbind it in tests.
    """
    global _engine
    _engine = engine


def _db() -> Engine:
    """Return the bound engine.

    Returns:
        The engine from :func:`configure_mcp_connections`.

    Raises:
        RuntimeError: When the process did not bind one.
    """
    if _engine is None:
        raise RuntimeError("MCP server connections are not configured in this process.")
    return _engine


# The catalog is keyed by host since SKY-381, and NAME_PATTERN refuses dots, so
# no host can clash. The old short names stay reserved, so a saved connection
# cannot take the name that older runs used for a Skynet-hosted connector.
_RETIRED_CONNECTOR_NAMES = frozenset({"maps", "web_search", "browser"})


def validate_name(name: str) -> str:
    """Check a connector name that a run payload lists in ``env_connectors``.

    Args:
        name: Proposed connector name.

    Returns:
        The same name.

    Raises:
        McpOAuthError: When the name is malformed or belongs to a hosted connector.
    """
    if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
        raise McpOAuthError("mcp_connection.invalid_name", 422)
    if name in _RETIRED_CONNECTOR_NAMES:
        raise McpOAuthError("mcp_connection.name_reserved", 422, name=name)
    return name


def _validate_server_url(server_url: str) -> str:
    """Check that an MCP server URL is HTTPS without credentials, query or fragment.

    Args:
        server_url: URL the user picked.

    Returns:
        The same URL.

    Raises:
        McpOAuthError: When the URL is not a plain HTTPS address.
    """
    parsed = urlsplit(server_url) if isinstance(server_url, str) else None
    plain_http_allowed = settings.discover_allow_private and parsed is not None and parsed.scheme == "http"
    if (
        parsed is None
        or (parsed.scheme != "https" and not plain_http_allowed)
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or len(server_url) > 2048
    ):
        raise McpOAuthError("mcp_connection.invalid_url", 422)
    return server_url


def _require_https(url: str) -> None:
    """Refuse a URL from discovery that is not HTTPS.

    Plain HTTP is allowed only with ``discover_allow_private``, for local
    test servers, because tokens and codes must not cross the network in clear.

    Args:
        url: Endpoint URL from discovery or the user.

    Raises:
        McpOAuthError: ``blocked_address`` for a non-HTTPS URL.
    """
    scheme = urlsplit(url).scheme
    if scheme != "https" and not (scheme == "http" and settings.discover_allow_private):
        raise McpOAuthError("mcp_connection.blocked_address", 502)


def _fetch(method: str, url: str, *, code: str, label: str, **kwargs: Any) -> httpx.Response:
    """Send one OAuth request to a pinned address with redirects off.

    Args:
        method: HTTP method.
        url: Target URL from discovery or the user.
        code: Error code to raise when the request fails.
        label: Endpoint name for safe messages.
        **kwargs: Extra ``httpx`` request arguments.

    Returns:
        The full response.

    Raises:
        McpOAuthError: When the URL breaks the SSRF policy or the request fails.
    """
    _require_https(url)
    try:
        address = _pinned_address(url, settings.discover_allow_private, label=label)
    except ValueError as error:
        raise McpOAuthError("mcp_connection.blocked_address", 502) from error
    try:
        with httpx.Client(
            transport=_PinnedTransport(url, address), follow_redirects=False, trust_env=False, timeout=_TIMEOUT
        ) as client:
            return client.request(method, url, **kwargs)
    except (httpx.HTTPError, ValueError) as error:
        raise McpOAuthError(code, 502) from error


def _first_metadata(urls: list[str], model: type[Any], *, label: str) -> Any | None:
    """Return the first metadata document that parses, in discovery order.

    Args:
        urls: Candidate well-known URLs.
        model: Pydantic model of the document.
        label: Endpoint name for safe messages.

    Returns:
        The parsed document, or ``None`` when no candidate serves one.
    """
    for url in urls:
        response = _fetch(
            "GET",
            url,
            code="mcp_connection.discovery_failed",
            label=label,
            headers={"MCP-Protocol-Version": LATEST_PROTOCOL_VERSION},
        )
        if response.status_code == 200:
            try:
                return model.model_validate_json(response.content)
            except ValidationError:
                continue
        if response.status_code >= 500:
            break
    return None


def _discover(server_url: str) -> tuple[OAuthMetadata, str, str | None]:
    """Find the authorization server, the resource and the scope of an MCP server.

    Args:
        server_url: The MCP endpoint.

    Returns:
        The server metadata, the RFC 8707 resource and the scope to ask for.

    Raises:
        McpOAuthError: When discovery fails or the server cannot do PKCE S256.
    """
    probe = _fetch(
        "POST",
        server_url,
        code="mcp_connection.discovery_failed",
        label="MCP server",
        headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "Skynet", "version": "1"},
            },
        },
    )
    if probe.status_code != 401:
        raise McpOAuthError("mcp_connection.oauth_unsupported", 502)
    prm = _first_metadata(
        build_protected_resource_metadata_discovery_urls(
            extract_field_from_www_auth(probe, "resource_metadata"), server_url
        ),
        ProtectedResourceMetadata,
        label="resource metadata endpoint",
    )
    resource = resource_url_from_server_url(server_url)
    issuer = None
    if prm is not None:
        if not check_resource_allowed(resource, str(prm.resource)):
            raise McpOAuthError("mcp_connection.discovery_failed", 502)
        resource = str(prm.resource)
        issuer = str(prm.authorization_servers[0])
    metadata = _first_metadata(
        build_oauth_authorization_server_metadata_discovery_urls(issuer, server_url),
        OAuthMetadata,
        label="sign-in metadata endpoint",
    )
    if metadata is None:
        raise McpOAuthError("mcp_connection.discovery_failed", 502)
    parsed = urlsplit(server_url)
    expected = issuer or f"{parsed.scheme}://{parsed.netloc}"
    if not issuers_match(str(metadata.issuer), expected):
        raise McpOAuthError("mcp_connection.issuer_mismatch", 502)
    if "S256" not in (metadata.code_challenge_methods_supported or []):
        raise McpOAuthError("mcp_connection.pkce_unsupported", 502)
    scope = get_client_metadata_scopes(extract_scope_from_www_auth(probe), prm, metadata)
    return metadata, resource, scope


def _register(metadata: OAuthMetadata, redirect_uri: str, scope: str | None) -> OAuthClientInformationFull:
    """Register Skynet as a public OAuth client of the authorization server.

    Args:
        metadata: Authorization server metadata.
        redirect_uri: Skynet's callback URL.
        scope: Scope to ask for.

    Returns:
        The client registration.

    Raises:
        McpOAuthError: ``registration_required`` when the server has no dynamic
            registration or refuses it.
    """
    refusal = McpOAuthError("mcp_connection.registration_required", 409)
    if metadata.registration_endpoint is None:
        raise refusal
    body = {
        "redirect_uris": [redirect_uri],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "client_name": "Skynet",
        **({"scope": scope} if scope else {}),
    }
    response = _fetch(
        "POST",
        str(metadata.registration_endpoint),
        code="mcp_connection.registration_required",
        label="registration endpoint",
        json=body,
    )
    if response.status_code not in (200, 201):
        raise refusal
    try:
        client = OAuthClientInformationFull.model_validate_json(response.content)
    except ValidationError as error:
        raise refusal from error
    if not client.client_id:
        raise refusal
    return client


def _client_auth(client: dict[str, Any], form: dict[str, str]) -> dict[str, str]:
    """Add the client credentials to a token request the way the client registered.

    Args:
        client: Stored client registration.
        form: Form body; this function adds ``client_id`` or the secret to it.

    Returns:
        Extra headers, such as HTTP Basic auth.
    """
    method = client.get("token_endpoint_auth_method") or "none"
    secret = client.get("client_secret")
    if method == "client_secret_basic" and secret:
        raw = f"{client['client_id']}:{secret}".encode()
        return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}
    form["client_id"] = client["client_id"]
    if method == "client_secret_post" and secret:
        form["client_secret"] = secret
    return {}


def _token_request(credentials: dict[str, Any], form: dict[str, str]) -> httpx.Response:
    """Post one form to the token endpoint with the stored client authentication.

    Args:
        credentials: Decrypted connection credentials.
        form: Grant parameters.

    Returns:
        The token endpoint response.
    """
    headers = {"Accept": "application/json", **_client_auth(credentials["client_info"], form)}
    return _fetch(
        "POST",
        credentials["token_endpoint"],
        code="mcp_connection.exchange_failed",
        label="token endpoint",
        data=form,
        headers=headers,
    )


def _parse_token(response: httpx.Response) -> OAuthToken:
    """Parse a successful token answer.

    Args:
        response: Token endpoint response with status 200.

    Returns:
        The token set.

    Raises:
        McpOAuthError: When the body is not a bearer token set.
    """
    try:
        return OAuthToken.model_validate_json(response.content)
    except ValidationError as error:
        raise McpOAuthError("mcp_connection.exchange_failed", 502) from error


def _expiry(token: OAuthToken) -> datetime | None:
    """Return when a token stops working.

    Args:
        token: Token set from the server.

    Returns:
        The UTC expiry time, or ``None`` when the server gave none.
    """
    return datetime.now(UTC) + timedelta(seconds=token.expires_in) if token.expires_in is not None else None


def _encrypt(payload: dict[str, Any]) -> str:
    """Encrypt a JSON payload with the vault key.

    Args:
        payload: JSON-compatible data.

    Returns:
        The Fernet token text.
    """
    return vault_cipher().encrypt(json.dumps(payload).encode()).decode()


def _decrypt(blob: str) -> dict[str, Any]:
    """Decrypt a JSON payload written by :func:`_encrypt`.

    Args:
        blob: Fernet token text.

    Returns:
        The payload.

    Raises:
        McpOAuthError: ``vault_key_changed`` when the vault key changed.
    """
    try:
        return json.loads(vault_cipher().decrypt(blob.encode()))
    except InvalidToken as error:
        raise McpOAuthError("mcp_connection.vault_key_changed", 409) from error


def _aware(moment: datetime | None) -> datetime | None:
    """Treat a naive time from SQLite as the UTC time that was written.

    Args:
        moment: Stored time.

    Returns:
        The time with a UTC zone, or ``None``.
    """
    return moment.replace(tzinfo=UTC) if moment is not None and moment.tzinfo is None else moment


def _state_hash(state: str) -> str:
    """Hash an OAuth state value for storage.

    Args:
        state: The random state sent to the browser.

    Returns:
        The sha256 hex digest.
    """
    return hashlib.sha256(state.encode()).hexdigest()


def _row(session: Session, username: str, name: str) -> McpServerConnectionModel | None:
    """Fetch one user's connection by name.

    Args:
        session: Open session.
        username: Owner.
        name: Connector name.

    Returns:
        The row, or ``None``.
    """
    return (
        session.query(McpServerConnectionModel)
        .filter(McpServerConnectionModel.username == username, McpServerConnectionModel.name == name)
        .first()
    )


def start_connection(username: str, name: str, server_url: str, redirect_uri: str) -> str:
    """Discover the server, register Skynet and return the URL where the user signs in.

    Args:
        username: Owner of the connection.
        name: Connector name for ``env_connectors``.
        server_url: Remote MCP endpoint.
        redirect_uri: Skynet's OAuth callback URL.

    Returns:
        The authorization URL for the browser.

    Raises:
        McpOAuthError: When the name or URL is invalid, or discovery or registration fails.
    """
    validate_name(name)
    _validate_server_url(server_url)
    vault_cipher()
    metadata, resource, scope = _discover(server_url)
    _require_https(str(metadata.authorization_endpoint))
    client = _register(metadata, redirect_uri, scope)
    pkce = PKCEParameters.generate()
    state = secrets.token_urlsafe(32)
    pending = {
        "server_url": server_url,
        "redirect_uri": redirect_uri,
        "code_verifier": pkce.code_verifier,
        "resource": resource,
        "scope": scope,
        "issuer": str(metadata.issuer),
        "token_endpoint": str(metadata.token_endpoint),
        "revocation_endpoint": str(metadata.revocation_endpoint) if metadata.revocation_endpoint else None,
        "client_info": client.model_dump(mode="json", exclude_none=True),
    }
    now = datetime.now(UTC)
    with Session(_db()) as session:
        row = _row(session, username, name)
        if row is None:
            row = McpServerConnectionModel(username=username, name=name, server_url=server_url, status=STATUS_PENDING)
            session.add(row)
        row.pending_state_hash = _state_hash(state)
        row.pending_encrypted = _encrypt(pending)
        row.pending_expires_at = now + _STATE_TTL
        row.updated_at = now
        session.commit()
    params = {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": pkce.code_challenge,
        "code_challenge_method": "S256",
        "resource": resource,
        **({"scope": scope} if scope else {}),
    }
    endpoint = str(metadata.authorization_endpoint)
    return f"{endpoint}{'&' if urlsplit(endpoint).query else '?'}{urlencode(params)}"


def finish_connection(username: str, state: str, code: str) -> str:
    """Trade the callback code for tokens and store them encrypted.

    Each state works once and only for ten minutes. Only the user who started
    the sign-in can finish it, so a sign-in link that someone else started
    cannot attach this user's MCP account to theirs.

    Args:
        username: The signed-in caller.
        state: The ``state`` value from the callback.
        code: The authorization code from the callback.

    Returns:
        The connector name.

    Raises:
        McpOAuthError: ``state_invalid`` for an unknown or used state, ``state_expired`` for an old one,
            ``wrong_account`` (403) when another user started the sign-in;
            ``exchange_failed`` when the token endpoint refuses the code.
    """
    if not state or not code:
        raise McpOAuthError("mcp_connection.state_invalid", 400)
    with Session(_db()) as session:
        row = (
            session.query(McpServerConnectionModel)
            .filter(McpServerConnectionModel.pending_state_hash == _state_hash(state))
            .with_for_update()
            .first()
        )
        if row is None or row.pending_encrypted is None:
            raise McpOAuthError("mcp_connection.state_invalid", 400)
        pending = _decrypt(row.pending_encrypted)
        expired = (_aware(row.pending_expires_at) or datetime.min.replace(tzinfo=UTC)) < datetime.now(UTC)
        row.pending_state_hash = None
        row.pending_encrypted = None
        row.pending_expires_at = None
        session.commit()
        if row.username != username:
            raise McpOAuthError("mcp_connection.wrong_account", 403)
        if expired:
            raise McpOAuthError("mcp_connection.state_expired", 400)
        name = row.name
    credentials = {key: pending[key] for key in pending if key != "code_verifier"}
    response = _token_request(
        credentials,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": pending["redirect_uri"],
            "code_verifier": pending["code_verifier"],
            "resource": pending["resource"],
        },
    )
    if response.status_code != 200:
        raise McpOAuthError("mcp_connection.exchange_failed", 502)
    token = _parse_token(response)
    credentials["access_token"] = token.access_token
    credentials["refresh_token"] = token.refresh_token
    now = datetime.now(UTC)
    with Session(_db()) as session:
        row = _row(session, username, name)
        if row is None:
            raise McpOAuthError("mcp_connection.state_invalid", 400)
        row.server_url = credentials["server_url"]
        row.credentials_encrypted = _encrypt(credentials)
        row.expires_at = _expiry(token)
        row.status = STATUS_CONNECTED
        row.created_at = now
        row.updated_at = now
        session.commit()
    return name


def connected_server_url(username: str, name: str) -> str | None:
    """Return the server URL of a usable connection, for the model gateway.

    Args:
        username: Owner of the run.
        name: Connector name from ``env_connectors``.

    Returns:
        The MCP endpoint, or ``None`` when the user has no such connection.

    Raises:
        McpOAuthError: ``reauth_required`` when the user must sign in again.
    """
    # A process that never bound the table (a test or a one-off script) has no
    # connections, so an unknown name stays an "Unknown connector" error.
    if _engine is None:
        return None
    with Session(_db()) as session:
        row = _row(session, username, name)
        if row is None or row.credentials_encrypted is None:
            return None
        if row.status == STATUS_REAUTH:
            raise McpOAuthError("mcp_connection.reauth_required", 409, name=name)
        return row.server_url


def _refresh_refused(response: httpx.Response) -> bool:
    """Tell whether the token endpoint refused the refresh token for good.

    Only these RFC 6749 section 5.2 errors mean the user must sign in again.
    A 408, a 429 or another 4xx answer can pass, so the row keeps its status.

    Args:
        response: The token endpoint's answer to the refresh.

    Returns:
        True when the user must sign in again.
    """
    if response.status_code not in (400, 401):
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("error") in _REFRESH_REFUSED_ERRORS


def access_token(username: str, name: str) -> str:
    """Return a live access token, and refresh it first when it is about to expire.

    The row lock stops two broker calls from spending the same refresh token.

    Args:
        username: Owner of the connection.
        name: Connector name.

    Returns:
        The bearer token for the broker. It never goes to the box.

    Raises:
        McpOAuthError: ``not_connected``, ``reauth_required`` when the server
            refuses the refresh, or ``exchange_failed`` when it cannot be reached.
    """
    with Session(_db()) as session:
        row = (
            session.query(McpServerConnectionModel)
            .filter(McpServerConnectionModel.username == username, McpServerConnectionModel.name == name)
            .with_for_update()
            .first()
        )
        if row is None or row.credentials_encrypted is None:
            raise McpOAuthError("mcp_connection.not_connected", 404, name=name)
        if row.status == STATUS_REAUTH:
            raise McpOAuthError("mcp_connection.reauth_required", 409, name=name)
        credentials = _decrypt(row.credentials_encrypted)
        expires_at = _aware(row.expires_at)
        if expires_at is None or expires_at - _REFRESH_MARGIN > datetime.now(UTC):
            return credentials["access_token"]
        if not credentials.get("refresh_token"):
            row.status = STATUS_REAUTH
            row.updated_at = datetime.now(UTC)
            session.commit()
            raise McpOAuthError("mcp_connection.reauth_required", 409, name=name)
        response = _token_request(
            credentials,
            {
                "grant_type": "refresh_token",
                "refresh_token": credentials["refresh_token"],
                "resource": credentials["resource"],
            },
        )
        if _refresh_refused(response):
            row.status = STATUS_REAUTH
            row.updated_at = datetime.now(UTC)
            session.commit()
            raise McpOAuthError("mcp_connection.reauth_required", 409, name=name)
        if response.status_code != 200:
            raise McpOAuthError("mcp_connection.exchange_failed", 502)
        token = _parse_token(response)
        credentials["access_token"] = token.access_token
        # RFC 6749 section 6: a server may keep the old refresh token valid and send no new one.
        credentials["refresh_token"] = token.refresh_token or credentials["refresh_token"]
        row.credentials_encrypted = _encrypt(credentials)
        row.expires_at = _expiry(token)
        row.updated_at = datetime.now(UTC)
        session.commit()
        return token.access_token


def list_connections(username: str) -> list[ConnectionView]:
    """List the user's finished connections, without tokens.

    Args:
        username: Owner.

    Returns:
        One view for each connection with stored credentials, by name.
    """
    with Session(_db()) as session:
        rows = (
            session.query(McpServerConnectionModel)
            .filter(
                McpServerConnectionModel.username == username,
                McpServerConnectionModel.credentials_encrypted.is_not(None),
            )
            .order_by(McpServerConnectionModel.name)
            .all()
        )
        return [
            ConnectionView(name=row.name, server_url=row.server_url, status=row.status, connected_at=row.created_at)
            for row in rows
        ]


def disconnect(username: str, name: str) -> bool:
    """Revoke the tokens when the server allows it, then delete the connection.

    Revocation is best effort: the row and its tokens go away even when the
    server cannot be reached.

    Args:
        username: Owner.
        name: Connector name.

    Returns:
        ``True`` when a connection existed.
    """
    with Session(_db()) as session:
        row = _row(session, username, name)
        if row is None:
            return False
        blob = row.credentials_encrypted
        session.delete(row)
        session.commit()
    if blob is None:
        return True
    try:
        credentials = _decrypt(blob)
    except McpOAuthError:
        return True
    endpoint = credentials.get("revocation_endpoint")
    if not endpoint:
        return True
    for kind in ("refresh_token", "access_token"):
        token = credentials.get(kind)
        if not token:
            continue
        form = {"token": token, "token_type_hint": kind}
        headers = _client_auth(credentials["client_info"], form)
        try:
            _fetch(
                "POST",
                endpoint,
                code="mcp_connection.exchange_failed",
                label="revocation endpoint",
                data=form,
                headers=headers,
            )
        except McpOAuthError:
            continue
    return True
