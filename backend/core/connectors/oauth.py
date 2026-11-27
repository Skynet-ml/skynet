"""Provider-agnostic OAuth 2.0 authorization-code flow with PKCE.

Google, Microsoft, GitHub, Notion and Supabase share one flow: mint an authorize URL
whose encrypted ``state`` carries the initiating user and the PKCE verifier,
trade the code for tokens, and refresh when a token lapses. Each provider
describes itself with an :class:`OAuthApp`; the Hugging Face connector
predates this module and keeps its own copy of the flow.
"""

from __future__ import annotations

import base64
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import httpx
from cryptography.fernet import InvalidToken

from ..api.errors import DomainError
from ..config import settings
from ..local import connector_relay
from .transport import label
from .vault import vault_cipher

if TYPE_CHECKING:
    from .vault import ConnectorVault

STATE_TTL_SECONDS = 600
API_TIMEOUT = httpx.Timeout(30.0)
REFRESH_LEEWAY = timedelta(seconds=60)


@dataclass(frozen=True)
class OAuthApp:
    """Everything the flow needs to know about one provider's OAuth app."""

    provider: str
    authorize_url: str
    token_url: str
    scopes: str
    client_id: str | None
    client_secret: str | None
    extra_authorize_params: dict[str, str]
    # Notion's token endpoint only takes a JSON body with the client in HTTP Basic auth, and no PKCE verifier.
    basic_auth_json: bool = False
    # Supabase takes the usual form body, but only accepts the client in HTTP Basic auth.
    basic_auth_form: bool = False


@dataclass(frozen=True)
class TokenSet:
    """Tokens returned by a token endpoint."""

    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    scope: str | None


def oauth_available(app: OAuthApp) -> bool:
    """Report whether the "Continue with …" button can be offered.

    Args:
        app: The provider's OAuth app.

    Returns:
        ``True`` when a client id and the vault key are configured.
    """
    return bool(app.client_id) and settings.byok_vault_key is not None


def _require_client_id(app: OAuthApp) -> str:
    """Return the configured client id or refuse the flow.

    Args:
        app: The provider's OAuth app.

    Returns:
        The client id.

    Raises:
        DomainError: 503 when the OAuth app is not configured.
    """
    if not app.client_id:
        raise DomainError("connectors.oauth_not_configured", status=503, provider=label(app.provider))
    return app.client_id


def build_authorize_url(app: OAuthApp, username: str, redirect_uri: str, account_label: str | None = None) -> str:
    """Start the PKCE authorization-code flow for ``username``.

    Args:
        app: The provider's OAuth app.
        username: The app user linking their account.
        redirect_uri: Callback URL registered on the OAuth app.
        account_label: Label chosen before sign-in (the Azure storage account),
            carried through ``state`` so the callback stores it.

    Returns:
        The URL to send the browser to.

    Raises:
        DomainError: 503 when OAuth or the vault is not configured.
    """
    client_id = _require_client_id(app)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    payload = {"p": app.provider, "u": username, "v": verifier, "r": redirect_uri}
    if account_label:
        payload["a"] = account_label
    state_payload = json.dumps(payload).encode("utf-8")
    state = vault_cipher().encrypt(state_payload).decode("ascii")
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": app.scopes,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        **app.extra_authorize_params,
    }
    # Notion grants access per page at consent time and takes no scope parameter.
    if not app.scopes:
        del params["scope"]
    return f"{app.authorize_url}?{urlencode(params)}"


def parse_state(app: OAuthApp, state: str) -> dict[str, str]:
    """Decrypt and validate the ``state`` echoed back by the callback.

    Args:
        app: The provider whose callback received the state.
        state: The opaque state parameter.

    Returns:
        The ``{"u": username, "v": verifier, "r": redirect_uri}`` payload,
        plus ``"a"`` when an account label was chosen before sign-in.

    Raises:
        DomainError: 400 when the state is forged, tampered with, minted for
            another provider or older than :data:`STATE_TTL_SECONDS`.
    """
    try:
        raw = vault_cipher().decrypt(state.encode("ascii"), ttl=STATE_TTL_SECONDS)
        payload = json.loads(raw)
    except (InvalidToken, ValueError, UnicodeEncodeError) as exc:
        raise DomainError("connectors.oauth_state_invalid", status=400) from exc
    if not all(isinstance(payload.get(k), str) for k in ("p", "u", "v", "r")) or payload["p"] != app.provider:
        raise DomainError("connectors.oauth_state_invalid", status=400)
    if not isinstance(payload.get("a", ""), str):
        raise DomainError("connectors.oauth_state_invalid", status=400)
    return payload


def _token_request(app: OAuthApp, form: dict[str, str]) -> TokenSet:
    """POST to the token endpoint and normalise the response.

    Args:
        app: The provider's OAuth app.
        form: Grant parameters; the client id/secret are added here, or sent
            as HTTP Basic auth for :attr:`OAuthApp.basic_auth_json` and
            :attr:`OAuthApp.basic_auth_form` apps.

    Returns:
        The issued tokens.

    Raises:
        DomainError: 502 when unreachable; 400 ``oauth_failed`` when the grant
            is refused.
    """
    client_id = _require_client_id(app)
    try:
        if app.basic_auth_json:
            response = httpx.post(
                app.token_url,
                json={k: v for k, v in form.items() if k != "code_verifier"},
                auth=(client_id, app.client_secret or ""),
                headers={"Accept": "application/json"},
                timeout=API_TIMEOUT,
            )
        elif app.basic_auth_form:
            response = httpx.post(
                app.token_url,
                data=form,
                auth=(client_id, app.client_secret or ""),
                headers={"Accept": "application/json"},
                timeout=API_TIMEOUT,
            )
        else:
            form = {**form, "client_id": client_id}
            if app.client_secret:
                form["client_secret"] = app.client_secret
            response = httpx.post(app.token_url, data=form, headers={"Accept": "application/json"}, timeout=API_TIMEOUT)
    except httpx.HTTPError as exc:
        raise DomainError("connectors.unreachable", status=502, provider=label(app.provider)) from exc
    if response.status_code >= 400:
        raise DomainError("connectors.oauth_failed", status=400, provider=label(app.provider))
    body: dict[str, Any] = response.json()
    access = body.get("access_token")
    # GitHub answers 200 with an ``error`` field when the code is bad.
    if not isinstance(access, str) or not access or body.get("error"):
        raise DomainError("connectors.oauth_failed", status=400, provider=label(app.provider))
    expires_in = body.get("expires_in")
    expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in)) if isinstance(expires_in, int | float) else None
    return TokenSet(
        access_token=access,
        refresh_token=body.get("refresh_token") or None,
        expires_at=expires_at,
        scope=body.get("scope") or None,
    )


def exchange_code(app: OAuthApp, code: str, verifier: str, redirect_uri: str) -> TokenSet:
    """Trade an authorization code for tokens.

    Args:
        app: The provider's OAuth app.
        code: The code from the callback.
        verifier: The PKCE verifier minted in :func:`build_authorize_url`.
        redirect_uri: The same redirect URI the code was issued for.

    Returns:
        The issued tokens.
    """
    return _token_request(
        app,
        {"grant_type": "authorization_code", "code": code, "code_verifier": verifier, "redirect_uri": redirect_uri},
    )


def refresh_tokens(app: OAuthApp, refresh_token: str) -> TokenSet:
    """Obtain a fresh access token from a refresh token.

    Args:
        app: The provider's OAuth app.
        refresh_token: The stored refresh token.

    Returns:
        The issued tokens.
    """
    return _token_request(app, {"grant_type": "refresh_token", "refresh_token": refresh_token})


def current_oauth_token(app: OAuthApp, vault: ConnectorVault, username: str) -> str:
    """Return a usable OAuth access token for ``username``, refreshing when stale.

    Args:
        app: The provider's OAuth app.
        vault: Vault holding the user's connector.
        username: The app user.

    Returns:
        The bearer token.

    Raises:
        DomainError: 409 ``not_connected`` when nothing is linked; 409
            ``expired`` when the token lapsed and could not be refreshed, in
            which case the connector is marked invalid so the UI prompts a
            reconnect.
    """
    secret = vault.resolve(username, app.provider)
    if secret is None:
        raise DomainError("connectors.not_connected", status=409, provider=label(app.provider))
    if connector_relay.is_relay(secret):
        # The Mac app holds no refresh token; hosted refreshes and hands over a fresh copy.
        token = connector_relay.current_token(vault, username, app.provider, secret)
        if token is None:
            vault.mark_invalid(username, app.provider)
            raise DomainError("connectors.expired", status=409, provider=label(app.provider))
        return token
    if secret.expires_at is None or secret.expires_at - REFRESH_LEEWAY > datetime.now(UTC):
        return secret.access_token
    if not secret.refresh_token:
        vault.mark_invalid(username, app.provider)
        raise DomainError("connectors.expired", status=409, provider=label(app.provider))
    try:
        fresh = refresh_tokens(app, secret.refresh_token)
    except DomainError as exc:
        vault.mark_invalid(username, app.provider)
        raise DomainError("connectors.expired", status=409, provider=label(app.provider)) from exc
    view = vault.get(username, app.provider)
    vault.save(
        username,
        app.provider,
        access_token=fresh.access_token,
        auth_method="oauth",
        account_label=view.account_label if view else None,
        refresh_token=fresh.refresh_token or secret.refresh_token,
        expires_at=fresh.expires_at,
        scopes=fresh.scope or (view.scopes if view else None),
    )
    return fresh.access_token
