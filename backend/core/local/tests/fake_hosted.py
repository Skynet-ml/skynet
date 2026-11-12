"""A fake of the hosted desktop-link routes, for local link-client tests.

It follows the SKY-124 addendum contract (section 2.3) and the error codes
and refresh rules of the hosted server in PR #782 (head ``88d1234b``): the
link code with PKCE S256, the token pair with ``skyl_`` prefixes, refresh
rotation with reuse detection (a 30 s grace retry, then the whole link is
revoked and the owner gets a notice), revoke, ``whoami`` and ``balance``, and
the RFC 9457 error bodies with a ``code``. It is a copy of those rules, not
the real hosted routes.
"""

from __future__ import annotations

import base64
import json
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

API_ORIGIN = "https://api.hosted.test"
APP_ORIGIN = "https://app.hosted.test"
# Same window as REFRESH_REUSE_GRACE in the #782 desktop_link router.
REFRESH_REUSE_GRACE_SECONDS = 30.0


@dataclass
class _Code:
    """One issued link code and what it is bound to."""

    challenge: str
    redirect_uri: str
    scope: str
    used: bool = False


@dataclass
class _Refresh:
    """One issued refresh token and its rotation history."""

    used_at: float | None = None
    superseded: bool = False
    children: list[str] = field(default_factory=list)


@dataclass
class FakeHosted:
    """In-memory hosted link server reached through an ``httpx.MockTransport``."""

    email: str = "dana@example.test"
    scopes: str = "identity balance:read sync models connectors web"
    access_ttl: int = 3600
    balance_cents: int = 1234
    reachable: bool = True
    revoked: bool = False
    reject_all_access: bool = False
    refresh_delay: float = 0.0
    lose_refresh_responses: int = 0
    clock: Callable[[], float] = time.monotonic
    codes: dict[str, _Code] = field(default_factory=dict)
    access: set[str] = field(default_factory=set)
    refresh: dict[str, _Refresh] = field(default_factory=dict)
    expired_access: set[str] = field(default_factory=set)
    calls: list[tuple[str, str]] = field(default_factory=list)
    refresh_count: int = 0
    reuse_notices: int = 0
    revoke_bodies: list[dict[str, Any]] = field(default_factory=list)
    refresh_timeouts: list[dict[str, float | None]] = field(default_factory=list)
    echo_bodies: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def transport(self) -> httpx.MockTransport:
        """Return a transport that routes requests to this fake.

        Returns:
            The mock transport.
        """
        return httpx.MockTransport(self._handle)

    def authorize(self, authorize_url: str) -> tuple[str, str, str]:
        """Do what the hosted consent page does after the user clicks Allow.

        Args:
            authorize_url: The URL the Mac opened in the system browser.

        Returns:
            ``(redirect_uri, code, state)``, the values the browser sends back.
        """
        query = {k: v[0] for k, v in parse_qs(urlsplit(authorize_url).query).items()}
        assert query["code_challenge_method"] == "S256"
        assert query["client_id"] == "skynet-desktop"
        code = secrets.token_urlsafe(32)
        self.codes[code] = _Code(query["code_challenge"], query["redirect_uri"], query["scope"])
        return query["redirect_uri"], code, query["state"]

    def _pair(self, parent: str | None = None) -> dict[str, Any]:
        """Issue a new token pair.

        Args:
            parent: The refresh token this pair replaces, if any.

        Returns:
            The token response body.
        """
        access = "skyl_at_" + secrets.token_urlsafe(32)
        refresh = "skyl_rt_" + secrets.token_urlsafe(32)
        self.access.add(access)
        self.refresh[refresh] = _Refresh()
        if parent is not None:
            self.refresh[parent].children.extend([access, refresh])
        return {
            "token_type": "Bearer",
            "access_token": access,
            "expires_in": self.access_ttl,
            "refresh_token": refresh,
            "refresh_expires_in": 2592000,
            "scope": self.scopes,
            "link_id": "dl_00000000-0000-0000-0000-000000000001",
            "account": {"email": self.email, "name": "Dana"},
        }

    @staticmethod
    def _problem(status: int, code: str) -> httpx.Response:
        """Build an RFC 9457 error like the hosted API sends.

        Args:
            status: HTTP status.
            code: Domain error code.

        Returns:
            The response.
        """
        return httpx.Response(status, json={"status": status, "code": code, "detail": code, "params": {}})

    def _token(self, body: dict[str, Any]) -> httpx.Response:
        """Answer ``POST /desktop/link/token``.

        Args:
            body: The JSON request body.

        Returns:
            The response.
        """
        if body.get("grant_type") == "authorization_code":
            entry = self.codes.get(body.get("code", ""))
            verifier = body.get("code_verifier", "")
            challenge = base64.urlsafe_b64encode(sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            if (
                entry is None
                or entry.used
                or entry.challenge != challenge
                or entry.redirect_uri != body.get("redirect_uri")
                or body.get("client_id") != "skynet-desktop"
            ):
                return self._problem(400, "desktop.invalid_grant")
            entry.used = True
            return httpx.Response(200, json=self._pair())
        if body.get("grant_type") == "refresh_token":
            return self._rotate(body.get("refresh_token", ""))
        return self._problem(400, "desktop.invalid_grant")

    def _revoke_for_reuse(self) -> httpx.Response:
        """Revoke the whole link and count the owner notice, as #782 does on reuse.

        Returns:
            The 401 ``desktop.link_revoked`` response.
        """
        self.revoked = True
        self.reuse_notices += 1
        return self._problem(401, "desktop.link_revoked")

    def _rotate(self, token: str) -> httpx.Response:
        """Rotate a refresh token with the #782 reuse rules.

        A reuse within the grace window, before the new refresh token is used,
        gets a new pair and kills the pair it replaces. A later reuse, or any
        use of a superseded token, revokes the link.

        Args:
            token: The refresh token sent by the client.

        Returns:
            The response.
        """
        row = self.refresh.get(token)
        if row is None:
            return self._problem(400, "desktop.invalid_grant")
        if self.revoked:
            return self._problem(401, "desktop.link_revoked")
        if row.superseded:
            return self._revoke_for_reuse()
        now = self.clock()
        if row.used_at is not None:
            successor_used = any(
                self.refresh[child].used_at is not None for child in row.children if child in self.refresh
            )
            if successor_used or now - row.used_at > REFRESH_REUSE_GRACE_SECONDS:
                return self._revoke_for_reuse()
            for child in row.children:
                if child in self.refresh:
                    self.refresh[child].superseded = True
                else:
                    self.access.discard(child)
            row.children = []
        else:
            row.used_at = now
        self.refresh_count += 1
        return httpx.Response(200, json=self._pair(parent=token))

    def _bearer(self, request: httpx.Request) -> httpx.Response | None:
        """Check the desktop access token on a ``/desktop/v1`` call.

        Args:
            request: The incoming request.

        Returns:
            An error response, or ``None`` when the token is good.
        """
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not token:
            return self._problem(401, "auth.missing_token")
        if self.reject_all_access or token not in self.access:
            return self._problem(401, "auth.invalid_token")
        if self.revoked:
            return self._problem(401, "desktop.link_revoked")
        if token in self.expired_access:
            return self._problem(401, "desktop.token_expired")
        return None

    def _handle(self, request: httpx.Request) -> httpx.Response:
        """Route one request.

        Args:
            request: The incoming request.

        Returns:
            The response.

        Raises:
            httpx.ConnectError: When the fake is set unreachable.
            httpx.ReadTimeout: When the fake commits a refresh but loses its response.
        """
        if not self.reachable:
            raise httpx.ConnectError("offline", request=request)
        path = request.url.path
        is_token_post = request.method == "POST" and path == "/desktop/link/token"
        if is_token_post:
            self.refresh_timeouts.append(dict(request.extensions.get("timeout", {})))
        if is_token_post and self.refresh_delay:
            # Holds the request in flight, so a second client caller can race this one.
            time.sleep(self.refresh_delay)
        # The real server serializes a token row with SELECT ... FOR UPDATE.
        with self._lock:
            response = self._route(request, path)
        if is_token_post and self.lose_refresh_responses and response.status_code == 200:
            # The server committed the rotation, but the answer never reaches the Mac.
            self.lose_refresh_responses -= 1
            raise httpx.ReadTimeout("response lost", request=request)
        return response

    def _route(self, request: httpx.Request, path: str) -> httpx.Response:
        """Answer one request under the fake's lock.

        Args:
            request: The incoming request.
            path: The URL path.

        Returns:
            The response.
        """
        self.calls.append((request.method, path))
        if request.method == "POST" and path == "/desktop/link/token":
            return self._token(json.loads(request.content))
        if request.method == "POST" and path == "/desktop/link/revoke":
            self.revoke_bodies.append(json.loads(request.content))
            self.revoked = True
            return httpx.Response(200, json={})
        if request.method == "GET" and path == "/desktop/v1/whoami":
            return self._bearer(request) or httpx.Response(
                200,
                json={
                    "email": self.email,
                    "name": "Dana",
                    "image": "https://lh3.googleusercontent.com/a/dana",
                    "link_id": "dl_00000000-0000-0000-0000-000000000001",
                    "scopes": self.scopes.split(),
                    "device_name": "Test Mac",
                },
            )
        if request.method == "GET" and path == "/desktop/v1/balance":
            return self._bearer(request) or httpx.Response(
                200, json={"available_cents": self.balance_cents, "currency": "usd", "low_balance": False}
            )
        if request.method == "POST" and path == "/desktop/v1/echo":
            denied = self._bearer(request)
            if denied is not None:
                return denied
            self.echo_bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        if request.method == "DELETE" and path.startswith("/desktop/v1/echo/"):
            return self._bearer(request) or httpx.Response(204)
        return httpx.Response(404, json={"code": "not_found"})
