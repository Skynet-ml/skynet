"""Link the Mac app to one hosted Skynet account (SKY-124, addendum section 2).

The local backend runs the client side of the desktop link: it starts a PKCE
loopback flow, trades the returned code for a ``skyl_`` token pair, keeps the
pair encrypted in the connector vault under the reserved ``skynet_hosted``
slug, refreshes it, and revokes it on unlink. Tokens never leave this module
except as an ``Authorization`` header to the hosted API.
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
import threading
import time
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.fernet import InvalidToken
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from ..api.errors import DomainError
from ..config import settings
from ..connectors.vault import ConnectorVault, ConnectorView, vault_cipher
from ..storage.models import UserConnectorModel
from . import hosted_dns

logger = logging.getLogger(__name__)

PROVIDER = "skynet_hosted"
AUTH_METHOD = "desktop_link"
RELAY_AUTH_METHOD = "oauth_relay"
CLIENT_ID = "skynet-desktop"
SCOPES = "identity balance:read sync models connectors web"
CALLBACK_PATH = "/local/hosted-link/callback"
# The Mac app opens the consent page in the default browser, which hands the
# result back through this URL; the app then calls ``finish_link`` (SKY-228).
APP_CALLBACK_URI = "skynet://link/callback"
STATE_TTL_SECONDS = 600
REFRESH_MARGIN = timedelta(minutes=5)
_TIMEOUT = httpx.Timeout(15.0)
# Hosted (#782) gives an old refresh token a 30 s grace after a rotation, from
# when it has the whole first request. When a refresh answer is lost, the client
# sends the same token again, and the resend must reach hosted inside that grace.
# A resend starts at most 12 s after the first request body is sent. It then
# needs a TCP connect (5 s), a TLS handshake (httpcore applies the connect
# timeout again, 5 s) and a write (5 s), so it reaches hosted at most 27 s after
# the first send, plus network transit. The resend's read time does not count.
# After a system DNS failure, hosted_dns caches its answer, so the resend spends
# no time on DNS; a working system resolver answers from the OS cache.
_REFRESH_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_RESEND_TIMEOUT = httpx.Timeout(5.0)
REFRESH_RESEND_DEADLINE = 12.0
# Errors that can come after hosted received the request and rotated the pair.
# A connect error means the request never left, so it gets no resend.
_LOST_ANSWER_ERRORS = (httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError, httpx.WriteError)
_clock = time.monotonic
# Fixed key so the API and the worker take the same advisory lock (addendum 2.7).
_REFRESH_LOCK_KEY = 0x534B59_124
_ACCESS_PREFIX = "skyl_at_"
_REFRESH_PREFIX = "skyl_rt_"
# Hosted codes that mean this link is gone for good; the Mac clears its link data.
_GONE_CODES = frozenset({"desktop.link_revoked", "desktop.invalid_grant"})
# Hosted (#782) answers ``auth.invalid_token`` when it no longer knows the access
# token, for example after a grace retry replaced the pair. A refresh can still
# save the link, so the client refreshes once before it gives up.
_REFRESH_CODES = frozenset({"desktop.token_expired", "auth.invalid_token"})
# Purpose tag in the link state. Other flows seal their states with the same
# vault key, so the tag keeps their states out of this callback.
_STATE_PURPOSE = "hosted_link"
_STATE_FIELDS = ("v", "n", "r", "u")

# Tests swap in an ``httpx.MockTransport`` of the hosted routes.
_TRANSPORT: httpx.BaseTransport | None = None
_REFRESH_LOCK = threading.Lock()
_USED_STATES: dict[str, float] = {}
_USED_STATES_LOCK = threading.Lock()


def clear_used_states() -> None:
    """Forget the used link-state nonces (tests start each flow clean)."""
    with _USED_STATES_LOCK:
        _USED_STATES.clear()


def _client(timeout: httpx.Timeout = _TIMEOUT) -> httpx.Client:
    """Return an HTTP client for the hosted API.

    Args:
        timeout: Per-phase limits for each request.

    Returns:
        A client bound to ``settings.hosted_api_url``.

    Raises:
        DomainError: 503 ``hosted_link.not_configured`` when the build has no hosted API URL.
    """
    if not settings.hosted_api_url:
        raise DomainError("hosted_link.not_configured", status=503)
    transport = _TRANSPORT
    # An explicit transport turns off httpx's proxy lookup, so a proxied Mac keeps
    # the default. Each client gets its own transport: closing a client closes it.
    if transport is None and not hosted_dns.proxy_configured():
        transport = hosted_dns.FallbackDnsTransport()
    return httpx.Client(base_url=settings.hosted_api_url, timeout=timeout, transport=transport)


def callback_uri() -> str:
    """Return the exact loopback callback the hosted redirect rule accepts.

    Returns:
        ``http://127.0.0.1:<port>/local/hosted-link/callback`` or the ``[::1]`` form.
    """
    host = settings.api_host
    if host == "localhost":
        # The hosted rule takes only the IP literal forms (RFC 8252 section 7.3).
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{settings.port}{CALLBACK_PATH}"


def _challenge(verifier: str) -> str:
    """Return the PKCE S256 challenge of a verifier.

    Args:
        verifier: The PKCE code verifier.

    Returns:
        The unpadded urlsafe base64 SHA-256 digest.
    """
    return base64.urlsafe_b64encode(sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def start_link(username: str, *, device_name: str, app_callback: bool = False) -> str:
    """Begin a link and return the hosted consent URL to open in the browser.

    Args:
        username: The local user who will own the link.
        device_name: The Mac name shown on the hosted consent screen.
        app_callback: Send the result to the Mac app's ``skynet://`` URL
            instead of the loopback callback of this API.

    Returns:
        The hosted ``/desktop/link`` URL with PKCE and an encrypted state.

    Raises:
        DomainError: 503 when the build has no hosted URL or the vault has no key.
    """
    if not settings.hosted_api_url or not settings.hosted_app_url:
        raise DomainError("hosted_link.not_configured", status=503)
    verifier = secrets.token_urlsafe(64)
    redirect_uri = APP_CALLBACK_URI if app_callback else callback_uri()
    payload = {"t": _STATE_PURPOSE, "v": verifier, "n": secrets.token_urlsafe(16), "r": redirect_uri, "u": username}
    state = vault_cipher().encrypt(json.dumps(payload).encode("utf-8")).decode("ascii")
    query = urlencode(
        {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": redirect_uri,
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
            "state": state,
            "scope": SCOPES,
            "device_name": device_name,
        }
    )
    return f"{settings.hosted_app_url}/desktop/link?{query}"


def _open_state(state: str) -> dict[str, str]:
    """Decrypt a link state, check its age and use its nonce once.

    Args:
        state: The opaque state from the callback.

    Returns:
        The state payload (``t``, ``v``, ``n``, ``r``, ``u``).

    Raises:
        DomainError: 400 ``hosted_link.state_invalid`` when the state is forged,
            expired, used before, or made by another flow.
    """
    try:
        payload = json.loads(vault_cipher().decrypt(state.encode("ascii"), ttl=STATE_TTL_SECONDS))
    except (InvalidToken, UnicodeError, ValueError) as exc:
        raise DomainError("hosted_link.state_invalid", status=400) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("t") != _STATE_PURPOSE
        or not all(isinstance(payload.get(key), str) and payload[key] for key in _STATE_FIELDS)
    ):
        raise DomainError("hosted_link.state_invalid", status=400)
    now = time.time()
    with _USED_STATES_LOCK:
        for nonce, expiry in list(_USED_STATES.items()):
            if expiry < now:
                del _USED_STATES[nonce]
        if payload["n"] in _USED_STATES:
            raise DomainError("hosted_link.state_invalid", status=400)
        _USED_STATES[payload["n"]] = now + STATE_TTL_SECONDS
    return payload


def _hosted_code(response: httpx.Response) -> str:
    """Return the domain error code of a hosted error response.

    Args:
        response: A non-2xx hosted response.

    Returns:
        The ``code`` field, or an empty string when the body has none.
    """
    try:
        body = response.json()
    except ValueError:
        return ""
    return str(body.get("code", "")) if isinstance(body, dict) else ""


def _save_pair(engine: Engine, username: str, body: dict[str, Any]) -> ConnectorView:
    """Check a hosted token response and save the pair encrypted.

    Args:
        engine: Local database.
        username: Owner of the link.
        body: The hosted token response.

    Returns:
        The masked view of the saved link.

    Raises:
        DomainError: 502 ``hosted_link.failed`` when the response is not a desktop token pair.
    """
    access = body.get("access_token")
    refresh = body.get("refresh_token")
    expires_in = body.get("expires_in")
    if (
        not isinstance(access, str)
        or not access.startswith(_ACCESS_PREFIX)
        or not isinstance(refresh, str)
        or not refresh.startswith(_REFRESH_PREFIX)
        or not isinstance(expires_in, int)
    ):
        raise DomainError("hosted_link.failed", status=502)
    account = body.get("account")
    if not isinstance(account, dict):
        account = {}
    return ConnectorVault(engine).save(
        username,
        PROVIDER,
        access_token=access,
        refresh_token=refresh,
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        scopes=str(body.get("scope") or ""),
        auth_method=AUTH_METHOD,
        account_label=str(account.get("email") or "") or None,
    )


def finish_link(engine: Engine, *, code: str, state: str) -> ConnectorView:
    """Trade the code from the browser callback for a token pair and save it.

    Args:
        engine: Local database.
        code: The single-use link code from hosted.
        state: The opaque state this Mac made in :func:`start_link`.

    Returns:
        The masked view of the saved link.

    Raises:
        DomainError: 400 on a bad state, 400 ``hosted_link.failed`` when hosted
            refuses the code, 503 when hosted is unreachable or not configured.
    """
    payload = _open_state(state)
    request = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": payload["v"],
        "redirect_uri": payload["r"],
        "client_id": CLIENT_ID,
    }
    try:
        with _client() as client:
            response = client.post("/desktop/link/token", json=request)
    except httpx.HTTPError as exc:
        raise DomainError("hosted_link.unreachable", status=503) from exc
    if response.status_code != 200:
        logger.info("Hosted refused the link code (status %s, %s)", response.status_code, _hosted_code(response))
        raise DomainError("hosted_link.failed", status=400)
    view = _save_pair(engine, payload["u"], response.json())
    logger.info("Linked the Mac app to a hosted account")
    return view


def link_status(engine: Engine, username: str) -> ConnectorView | None:
    """Return the masked view of the hosted link, if the Mac is linked.

    Args:
        engine: Local database.
        username: The local user.

    Returns:
        The view, or ``None`` when not linked.
    """
    return ConnectorVault(engine).get(username, PROVIDER)


def clear_link(engine: Engine, username: str) -> None:
    """Delete the hosted link and every connector that came through the relay.

    Local runs, datasets and settings stay (addendum 2.8).

    Args:
        engine: Local database.
        username: The local user.
    """
    with Session(engine) as session:
        session.query(UserConnectorModel).filter(
            UserConnectorModel.username == username,
            (UserConnectorModel.provider == PROVIDER) | (UserConnectorModel.auth_method == RELAY_AUTH_METHOD),
        ).delete(synchronize_session=False)
        session.commit()


def _gone(engine: Engine, username: str) -> DomainError:
    """Clear the local link data after hosted said the link is gone.

    Args:
        engine: Local database.
        username: The local user.

    Returns:
        The 401 ``hosted_link.revoked`` error for the caller to raise.
    """
    clear_link(engine, username)
    logger.info("Hosted revoked the desktop link; cleared the local link data")
    return DomainError("hosted_link.revoked", status=401)


def _post_refresh(refresh_token: str) -> httpx.Response:
    """Send the refresh grant, and send it once more when the answer is lost.

    Hosted can rotate the pair and then lose the answer (timeout, broken
    connection). The client still holds only the old refresh token, and a later
    use of it after the 30 s grace revokes the link and emails the owner a
    false theft notice. So the client sends the same token once more while the
    grace is still open; hosted then answers with a new pair. The caller holds
    the refresh locks, so no other caller sends in between.

    Args:
        refresh_token: The stored refresh token.

    Returns:
        The hosted response.

    Raises:
        DomainError: 503 ``hosted_link.unreachable`` when no answer arrives.
    """
    body = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    sent_at: list[float] = []

    def mark_sent(event: str, _info: dict[str, Any]) -> None:
        """Record when the first request body has fully left the client.

        Args:
            event: The httpcore trace event name.
            _info: The event details (not used).
        """
        if event.endswith("send_request_body.complete"):
            sent_at.append(_clock())

    started = _clock()
    try:
        with _client(_REFRESH_TIMEOUT) as client:
            return client.post("/desktop/link/token", json=body, extensions={"trace": mark_sent})
    except _LOST_ANSWER_ERRORS as exc:
        # Hosted starts the grace only after it has the whole request, so a slow
        # connect must not use up the deadline. A body that never fully left
        # cannot have been committed, so the earlier start mark is safe there.
        if _clock() - (sent_at[0] if sent_at else started) > REFRESH_RESEND_DEADLINE:
            raise DomainError("hosted_link.unreachable", status=503) from exc
        logger.info("The hosted refresh answer was lost; sending the same refresh grant once more")
    except httpx.HTTPError as exc:
        raise DomainError("hosted_link.unreachable", status=503) from exc
    try:
        with _client(_RESEND_TIMEOUT) as client:
            return client.post("/desktop/link/token", json=body)
    except httpx.HTTPError as exc:
        raise DomainError("hosted_link.unreachable", status=503) from exc


def refresh_after(engine: Engine, username: str, *, stale_access_token: str) -> str:
    """Refresh the pair once, unless another caller did it while this one waited.

    The API and the worker share the pair, so the refresh runs under a
    process lock and, on Postgres, ``pg_advisory_xact_lock`` (addendum 2.7).

    Args:
        engine: Local database.
        username: The local user.
        stale_access_token: The access token the caller found too old.

    Returns:
        A current access token.

    Raises:
        DomainError: 409 when not linked, 401 when hosted revoked the link,
            502 on another hosted error, 503 when hosted is unreachable.
    """
    vault = ConnectorVault(engine)
    with _REFRESH_LOCK, engine.connect() as conn, conn.begin():
        if engine.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _REFRESH_LOCK_KEY})
        secret = vault.resolve(username, PROVIDER)
        if secret is None or not secret.refresh_token:
            raise DomainError("hosted_link.not_linked", status=409)
        if secret.access_token != stale_access_token:
            return secret.access_token
        response = _post_refresh(secret.refresh_token)
        if response.status_code != 200:
            hosted_code = _hosted_code(response)
            if hosted_code in _GONE_CODES:
                raise _gone(engine, username)
            logger.warning("Hosted refused the token refresh (status %s, %s)", response.status_code, hosted_code)
            raise DomainError("hosted_link.failed", status=502)
        body = response.json()
        _save_pair(engine, username, body)
        return str(body["access_token"])


def access_token(engine: Engine, username: str) -> str:
    """Return a desktop access token with at least 5 minutes left.

    Args:
        engine: Local database.
        username: The local user.

    Returns:
        The access token.

    Raises:
        DomainError: 409 ``hosted_link.not_linked`` when the Mac is not linked,
            plus the refresh errors of :func:`refresh_after`.
    """
    secret = ConnectorVault(engine).resolve(username, PROVIDER)
    if secret is None:
        raise DomainError("hosted_link.not_linked", status=409)
    if secret.expires_at is not None and secret.expires_at - datetime.now(UTC) > REFRESH_MARGIN:
        return secret.access_token
    return refresh_after(engine, username, stale_access_token=secret.access_token)


def _send(method: str, path: str, token: str, body: Any = None) -> httpx.Response:
    """Send one authenticated request to the hosted desktop API.

    Args:
        method: The HTTP verb.
        path: The ``/desktop/v1/...`` path.
        token: The desktop access token.
        body: The JSON body, or ``None`` for none.

    Returns:
        The hosted response.

    Raises:
        DomainError: 503 ``hosted_link.unreachable`` on a network error.
    """
    try:
        with _client() as client:
            return client.request(method, path, json=body, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise DomainError("hosted_link.unreachable", status=503) from exc


def hosted_request(engine: Engine, username: str, method: str, path: str, body: Any = None) -> Any:
    """Call a hosted ``/desktop/v1`` route with the linked account.

    A 401 ``desktop.token_expired`` or ``auth.invalid_token`` refreshes once
    and retries once. An ``auth.invalid_token`` after that refresh means hosted
    dropped the link, so the local link data is cleared.

    Args:
        engine: Local database.
        username: The local user.
        method: The HTTP verb.
        path: The hosted path, such as ``/desktop/v1/whoami``.
        body: The JSON body, or ``None`` for none.

    Returns:
        The JSON body, or ``None`` for an empty 2xx answer.

    Raises:
        DomainError: 409 when not linked, 401 when the link is revoked (local
            link data is then deleted), 502 on another hosted error, 503 when
            hosted is unreachable.
    """
    token = access_token(engine, username)
    response = _send(method, path, token, body)
    if response.status_code == 401 and _hosted_code(response) in _REFRESH_CODES:
        token = refresh_after(engine, username, stale_access_token=token)
        response = _send(method, path, token, body)
    if 200 <= response.status_code < 300:
        return response.json() if response.content else None
    hosted_code = _hosted_code(response)
    # Only reached after a refresh, so a fresh token that hosted does not know means a dead link.
    if response.status_code == 401 and (hosted_code in _GONE_CODES or hosted_code == "auth.invalid_token"):
        raise _gone(engine, username)
    logger.warning("Hosted call %s %s failed (status %s, %s)", method, path, response.status_code, hosted_code)
    raise DomainError("hosted_link.failed", status=502)


def hosted_get(engine: Engine, username: str, path: str) -> dict[str, Any]:
    """Call a hosted ``/desktop/v1`` read route that answers a JSON object.

    Args:
        engine: Local database.
        username: The local user.
        path: The hosted path, such as ``/desktop/v1/whoami``.

    Returns:
        The JSON body, or ``{}`` when it is not an object.

    Raises:
        DomainError: The errors of :func:`hosted_request`.
    """
    body = hosted_request(engine, username, "GET", path)
    return body if isinstance(body, dict) else {}


def unlink(engine: Engine, username: str) -> bool:
    """Revoke the link on hosted (best effort) and delete the local link data.

    Args:
        engine: Local database.
        username: The local user.

    Returns:
        Whether hosted confirmed the revoke. ``False`` means hosted was not told
        (offline or the stored link cannot be read), so the link stays on the
        account until the user removes it on the website or it idles out.
    """
    # The refresh locks keep a refresh in flight from saving a new pair after
    # the clear, which would show the Mac as linked again. The revoke call is
    # bounded by ``_TIMEOUT``, so a slow hosted holds the locks only that long.
    with _REFRESH_LOCK, engine.connect() as conn, conn.begin():
        if engine.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _REFRESH_LOCK_KEY})
        try:
            secret = ConnectorVault(engine).resolve(username, PROVIDER)
            revoked = True
        except DomainError as exc:
            # A changed or missing vault key must not trap the user in a link
            # they cannot remove; hosted is not told, as when offline.
            logger.warning("Cannot read the stored hosted link, so hosted is not told of the unlink (%s)", exc.code)
            secret, revoked = None, False
        if secret is not None:
            try:
                with _client() as client:
                    response = client.post(
                        "/desktop/link/revoke", json={"token": secret.refresh_token or secret.access_token}
                    )
                revoked = response.status_code == 200
            except (httpx.HTTPError, DomainError):
                revoked = False
        clear_link(engine, username)
    logger.info("Unlinked the Mac app (hosted revoke confirmed: %s)", revoked)
    return revoked
