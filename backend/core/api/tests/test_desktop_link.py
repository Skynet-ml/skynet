"""Tests for the hosted desktop link server (SKY-124 PR 1).

The app runs over a shared in-memory SQLite engine (StaticPool), so the real
``get_authenticated_user`` and ``get_desktop_principal`` paths hash and look
up every token. Only the session JWT is bypassed, with a dependency override
that acts as a signed-in browser user.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ...registry.core import ServiceRegistry
from ...storage.models import (
    Base,
    BillingCustomerModel,
    DesktopLinkCodeModel,
    DesktopLinkModel,
    DesktopTokenModel,
    UserModel,
)
from .. import auth as auth_mod
from .. import desktop_auth
from ..account_data_service import delete_account, export_account
from ..app import create_app
from ..auth import AuthenticatedUser, get_authenticated_user, hash_api_token, is_admin
from ..desktop_auth import DesktopPrincipal, get_desktop_principal
from ..errors import DomainError
from ..routers import desktop_link
from ..routers.api_tokens import create_api_tokens_router
from ..routers.desktop_link import create_desktop_link_router

_SessionUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]
_SyncDep = Annotated[DesktopPrincipal, Depends(get_desktop_principal("sync"))]

ALL_SCOPES = "identity balance:read sync models connectors web"
# One ``(to, subject, body, html)`` row per sent security notice.
Mailbox = list[tuple[str, str, str, str]]
REDIRECT = "http://127.0.0.1:54321/local/hosted-link/callback"


class _Store:
    """Minimal store exposing the engine and the MAU admission hook."""

    def __init__(self, engine: Any) -> None:
        """Hold the engine the routes open sessions on.

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


async def _domain_error_json(request: Request, exc: Exception) -> JSONResponse:
    """Render a DomainError with its semantic code, like the app's handler.

    Args:
        request: Incoming request.
        exc: The raised DomainError.

    Returns:
        A JSON body with ``code`` and ``params``.
    """
    assert isinstance(exc, DomainError)
    return JSONResponse(status_code=exc.status_code, content={"code": exc.code, "params": exc.params})


class _Session:
    """Mutable holder for the browser user the session override returns."""

    username = "alice@example.test"


@pytest.fixture
def harness() -> Any:
    """Build an app with the desktop link router over in-memory SQLite.

    Returns:
        A ``(client, engine, session_user)`` tuple. Set
        ``session_user.username`` to act as another browser user.
    """
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(UserModel(email="alice@example.test", name="Alice Example", password_hash="x"))
        session.commit()
    store = _Store(engine)
    app = FastAPI()
    app.state.job_store = store
    app.add_exception_handler(DomainError, _domain_error_json)
    app.include_router(create_desktop_link_router(job_store=store))
    app.include_router(create_api_tokens_router(job_store=store))

    @app.get("/session-only")
    def session_only(user: _SessionUserDep) -> dict[str, str]:
        """Stand in for any normal session route.

        Args:
            user: Authenticated caller.

        Returns:
            The caller's username.
        """
        return {"username": user.username}

    @app.get("/optimizations")
    def optimizations(user: _SessionUserDep) -> dict[str, Any]:
        """Stand in for an account-data route on the desktop sync allowlist.

        Args:
            user: Authenticated caller.

        Returns:
            The caller's username and admin status.
        """
        return {"username": user.username, "admin": is_admin(user)}

    @app.get("/account/export")
    def account_export(user: _SessionUserDep) -> dict[str, str]:
        """Stand in for a sensitive route that the allowlist excludes.

        Args:
            user: Authenticated caller.

        Returns:
            The caller's username.
        """
        return {"username": user.username}

    @app.get("/needs-sync")
    def needs_sync(principal: _SyncDep) -> dict[str, str]:
        """Stand in for a desktop route that needs the ``sync`` scope.

        Args:
            principal: Desktop principal.

        Returns:
            The link id.
        """
        return {"link_id": principal.link_id}

    session_user = _Session()
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser(
        username=session_user.username, role="user", groups=()
    )
    return TestClient(app), engine, session_user


def _pkce() -> tuple[str, str]:
    """Return a PKCE ``(verifier, S256 challenge)`` pair."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _authorize(client: TestClient, *, scope: str = ALL_SCOPES, redirect_uri: str = REDIRECT, **extra: Any) -> Any:
    """Call the consent endpoint and return ``(response, verifier)``.

    Args:
        client: Test client.
        scope: Space-separated scopes.
        redirect_uri: Loopback callback URL.
        **extra: Body fields that replace the defaults.

    Returns:
        The response and the PKCE verifier.
    """
    verifier, challenge = _pkce()
    body = {
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": scope,
        "device_name": "Alice's MacBook Pro",
    }
    body.update(extra)
    return client.post("/desktop/link/authorize", json=body), verifier


def _exchange(client: TestClient, code: str, verifier: str, **extra: Any) -> Any:
    """Exchange a link code at the token endpoint.

    Args:
        client: Test client.
        code: Link code.
        verifier: PKCE verifier.
        **extra: Body fields that replace the defaults.

    Returns:
        The token endpoint response.
    """
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": REDIRECT,
        "client_id": "skynet-desktop",
    }
    body.update(extra)
    return client.post("/desktop/link/token", json=body)


def _link(client: TestClient, scope: str = ALL_SCOPES) -> dict[str, Any]:
    """Run the full link flow and return the token response body.

    Args:
        client: Test client.
        scope: Space-separated scopes.

    Returns:
        The token pair body.
    """
    response, verifier = _authorize(client, scope=scope)
    assert response.status_code == 200, response.text
    tokens = _exchange(client, response.json()["code"], verifier)
    assert tokens.status_code == 200, tokens.text
    return tokens.json()


def _refresh(client: TestClient, refresh_token: str) -> Any:
    """Call the refresh grant.

    Args:
        client: Test client.
        refresh_token: Refresh token to rotate.

    Returns:
        The token endpoint response.
    """
    return client.post("/desktop/link/token", json={"grant_type": "refresh_token", "refresh_token": refresh_token})


def _bearer(token: str) -> dict[str, str]:
    """Return an Authorization header for ``token``."""
    return {"Authorization": f"Bearer {token}"}


def _code(response: Any) -> str:
    """Return the semantic error code from an error response."""
    return response.json()["code"]


def test_link_flow_issues_scoped_pair_and_whoami(harness: Any) -> None:
    """Consent, code exchange and whoami return the linked account."""
    client, _, _ = harness
    response, verifier = _authorize(client)
    assert response.status_code == 200
    body = response.json()
    assert body["expires_in"] == 60
    assert len(body["code"]) == 43

    tokens = _exchange(client, body["code"], verifier)
    assert tokens.status_code == 200, tokens.text
    pair = tokens.json()
    assert pair["token_type"] == "Bearer"
    assert pair["access_token"].startswith("skyl_at_")
    assert pair["refresh_token"].startswith("skyl_rt_")
    assert pair["expires_in"] == 3600
    assert pair["refresh_expires_in"] == 30 * 24 * 3600
    assert pair["scope"] == ALL_SCOPES
    assert pair["link_id"].startswith("dl_")
    assert pair["account"] == {"email": "alice@example.test", "name": "Alice Example"}

    who = client.get("/desktop/v1/whoami", headers=_bearer(pair["access_token"]))
    assert who.status_code == 200
    assert who.json() == {
        "email": "alice@example.test",
        "name": "Alice Example",
        "image": None,
        "link_id": pair["link_id"],
        "scopes": ALL_SCOPES.split(),
        "device_name": "Alice's MacBook Pro",
    }


def test_whoami_returns_the_profile_picture(harness: Any) -> None:
    """Whoami returns the picture saved at the last provider sign-in."""
    client, engine, _ = harness
    with Session(engine) as session:
        user = session.get(UserModel, "alice@example.test")
        assert user is not None
        user.image_url = "https://lh3.googleusercontent.com/a/alice"
        session.commit()
    response, verifier = _authorize(client)
    pair = _exchange(client, response.json()["code"], verifier).json()

    who = client.get("/desktop/v1/whoami", headers=_bearer(pair["access_token"]))

    assert who.json()["image"] == "https://lh3.googleusercontent.com/a/alice"


def test_authorize_saves_the_session_picture(harness: Any) -> None:
    """Approving a link saves the session picture, so whoami returns it (SKY-249)."""
    client, _, _ = harness
    response, verifier = _authorize(client, image="https://lh3.googleusercontent.com/a/alice")
    pair = _exchange(client, response.json()["code"], verifier).json()

    who = client.get("/desktop/v1/whoami", headers=_bearer(pair["access_token"]))

    assert who.json()["image"] == "https://lh3.googleusercontent.com/a/alice"


@pytest.mark.parametrize("image", ["http://example.test/a.png", "javascript:alert(1)", "https://x.test/" + "a" * 2048])
def test_authorize_ignores_an_unsafe_picture(harness: Any, image: str) -> None:
    """A non-https or too long picture is ignored, and the old picture stays.

    Args:
        harness: Test client, engine and settings.
        image: The picture URL the consent page sends.
    """
    client, engine, _ = harness
    with Session(engine) as session:
        user = session.get(UserModel, "alice@example.test")
        assert user is not None
        user.image_url = "https://lh3.googleusercontent.com/a/old"
        session.commit()

    response, _ = _authorize(client, image=image)

    assert response.status_code == 200
    with Session(engine) as session:
        user = session.get(UserModel, "alice@example.test")
        assert user is not None
        assert user.image_url == "https://lh3.googleusercontent.com/a/old"


def test_database_holds_only_hashes(harness: Any) -> None:
    """No plaintext code or token is stored in any desktop table."""
    client, engine, _ = harness
    response, verifier = _authorize(client)
    code = response.json()["code"]
    pair = _exchange(client, code, verifier).json()
    secrets_seen = {code, pair["access_token"], pair["refresh_token"]}
    with Session(engine) as session:
        values: list[str] = []
        for model in (DesktopLinkModel, DesktopTokenModel, DesktopLinkCodeModel):
            for row in session.scalars(select(model)):
                values.extend(str(getattr(row, column.key)) for column in inspect(model).columns)
    assert not secrets_seen & set(values)
    assert not any(secret in value for secret in secrets_seen for value in values)


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "http://localhost:54321/local/hosted-link/callback",
        "https://127.0.0.1:54321/local/hosted-link/callback",
        "http://127.0.0.2:54321/local/hosted-link/callback",
        "http://127.0.0.1:80/local/hosted-link/callback",
        "http://127.0.0.1:70000/local/hosted-link/callback",
        "http://127.0.0.1/local/hosted-link/callback",
        "http://127.0.0.1:54321/local/hosted-link/callback?x=1",
        "http://127.0.0.1:54321/local/hosted-link/callback/",
        "http://127.0.0.1:54321/other",
        "http://evil.example@127.0.0.1:54321/local/hosted-link/callback",
        "https://skynet.app/local/hosted-link/callback",
        "skynet://link/callback/",
        "skynet://link/callback?x=1",
        "skynet://link/callback#x",
        "skynet://link/other",
        "skynet://evil/callback",
        "skynet://evil@link/callback",
        "skynet:link/callback",
        "skynet:///link/callback",
        "SKYNET://link/callback",
        "skynet-evil://link/callback",
    ],
)
def test_authorize_rejects_non_loopback_redirects(harness: Any, redirect_uri: str) -> None:
    """Only the exact app URL or the exact RFC 8252 loopback callback form is accepted."""
    client, _, _ = harness
    response, _ = _authorize(client, redirect_uri=redirect_uri)
    assert response.status_code == 400
    assert _code(response) == "desktop.invalid_redirect"


def test_authorize_accepts_ipv6_loopback(harness: Any) -> None:
    """The IPv6 loopback form is accepted and must match at exchange."""
    client, _, _ = harness
    redirect = "http://[::1]:1024/local/hosted-link/callback"
    response, verifier = _authorize(client, redirect_uri=redirect)
    assert response.status_code == 200
    assert _exchange(client, response.json()["code"], verifier, redirect_uri=redirect).status_code == 200


def test_authorize_accepts_the_app_url(harness: Any) -> None:
    """The Mac app's ``skynet://`` callback is accepted and must match at exchange."""
    client, _, _ = harness
    response, verifier = _authorize(client, redirect_uri="skynet://link/callback")
    assert response.status_code == 200
    assert _exchange(client, response.json()["code"], verifier, redirect_uri=REDIRECT).status_code == 400

    response, verifier = _authorize(client, redirect_uri="skynet://link/callback")
    exchanged = _exchange(client, response.json()["code"], verifier, redirect_uri="skynet://link/callback")
    assert exchanged.status_code == 200


@pytest.mark.parametrize("scope", ["", "identity admin", "identity  settings", "everything"])
def test_authorize_rejects_unknown_or_empty_scope(harness: Any, scope: str) -> None:
    """A scope outside the desktop list is refused."""
    client, _, _ = harness
    response, _ = _authorize(client, scope=scope)
    assert response.status_code == 400
    assert _code(response) == "desktop.invalid_scope"


def test_authorize_rejects_plain_pkce(harness: Any) -> None:
    """Only the S256 challenge method is accepted."""
    client, _, _ = harness
    response, _ = _authorize(client, code_challenge_method="plain")
    assert response.status_code == 422


def test_authorize_refuses_personal_access_token(harness: Any) -> None:
    """A ``skyd_`` token cannot mint a desktop link; a browser session must."""
    client, _, _ = harness
    pat = client.post("/settings/api-token").json()["token"]
    client.app.dependency_overrides.clear()
    _, challenge = _pkce()
    response = client.post(
        "/desktop/link/authorize",
        headers=_bearer(pat),
        json={
            "redirect_uri": REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": ALL_SCOPES,
            "device_name": "Mac",
        },
    )
    assert response.status_code == 403
    assert _code(response) == "desktop.session_required"


def test_code_is_single_use_and_reuse_revokes_the_link(harness: Any) -> None:
    """A second exchange of one code fails and revokes the link it made."""
    client, _, _ = harness
    response, verifier = _authorize(client)
    code = response.json()["code"]
    pair = _exchange(client, code, verifier).json()
    again = _exchange(client, code, verifier)
    assert again.status_code == 400
    assert _code(again) == "desktop.invalid_grant"
    who = client.get("/desktop/v1/whoami", headers=_bearer(pair["access_token"]))
    assert who.status_code == 401
    assert _code(who) == "desktop.link_revoked"


@pytest.mark.parametrize(
    "override",
    [
        {"code_verifier": "x" * 64},
        {"redirect_uri": "http://127.0.0.1:54322/local/hosted-link/callback"},
        {"client_id": "someone-else"},
        {"code": "not-a-real-code"},
    ],
)
def test_code_exchange_rejects_mismatches(harness: Any, override: dict[str, str]) -> None:
    """A wrong verifier, redirect, client or code fails with invalid_grant."""
    client, _, _ = harness
    response, verifier = _authorize(client)
    code = override.pop("code", response.json()["code"])
    result = _exchange(client, code, verifier, **override)
    assert result.status_code == 400
    assert _code(result) == "desktop.invalid_grant"


def test_expired_code_is_refused(harness: Any) -> None:
    """A code older than its 60 s lifetime fails."""
    client, engine, _ = harness
    response, verifier = _authorize(client)
    with Session(engine) as session:
        for row in session.scalars(select(DesktopLinkCodeModel)):
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    result = _exchange(client, response.json()["code"], verifier)
    assert result.status_code == 400
    assert _code(result) == "desktop.invalid_grant"


def test_desktop_token_is_refused_on_session_routes(harness: Any) -> None:
    """``get_authenticated_user`` refuses ``skyl_`` tokens (default deny)."""
    client, _, _ = harness
    pair = _link(client)
    client.app.dependency_overrides.clear()
    for token in (pair["access_token"], pair["refresh_token"]):
        response = client.get("/session-only", headers=_bearer(token))
        assert response.status_code == 401
        assert _code(response) == "auth.desktop_token_not_allowed"
        assert client.get("/settings/api-token", headers=_bearer(token)).status_code == 401
        assert client.get("/settings/desktop-links", headers=_bearer(token)).status_code == 401


def test_sync_token_opens_allowlisted_account_routes(harness: Any) -> None:
    """A ``sync`` access token acts as its account on an allowlisted route."""
    client, _, _ = harness
    pair = _link(client)
    client.app.dependency_overrides.clear()
    response = client.get("/optimizations", headers=_bearer(pair["access_token"]))
    assert response.status_code == 200, response.text
    assert response.json() == {"username": "alice@example.test", "admin": False}


def test_sync_token_is_refused_on_excluded_routes(harness: Any) -> None:
    """A route outside the allowlist refuses even a ``sync`` access token."""
    client, _, _ = harness
    pair = _link(client)
    client.app.dependency_overrides.clear()
    response = client.get("/account/export", headers=_bearer(pair["access_token"]))
    assert response.status_code == 401
    assert _code(response) == "auth.desktop_token_not_allowed"


def test_allowlisted_route_needs_the_sync_scope(harness: Any) -> None:
    """A link without ``sync`` gets 403 on an allowlisted route."""
    client, _, _ = harness
    pair = _link(client, scope="identity")
    client.app.dependency_overrides.clear()
    response = client.get("/optimizations", headers=_bearer(pair["access_token"]))
    assert response.status_code == 403
    assert _code(response) == "desktop.scope_missing"


def test_allowlisted_route_refuses_a_refresh_token(harness: Any) -> None:
    """Only the access token opens an allowlisted route."""
    client, _, _ = harness
    pair = _link(client)
    client.app.dependency_overrides.clear()
    response = client.get("/optimizations", headers=_bearer(pair["refresh_token"]))
    assert response.status_code == 401
    assert _code(response) == "auth.invalid_token"


def test_allowlisted_route_refuses_a_revoked_link(harness: Any) -> None:
    """A revoke closes the allowlisted routes on the next call."""
    client, _, _ = harness
    pair = _link(client)
    assert client.delete(f"/settings/desktop-links/{pair['link_id']}").status_code in (200, 204)
    client.app.dependency_overrides.clear()
    response = client.get("/optimizations", headers=_bearer(pair["access_token"]))
    assert response.status_code == 401
    assert _code(response) == "desktop.link_revoked"


def test_sync_token_is_never_admin(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """An admin account that calls through the Mac link gets no admin rights."""
    monkeypatch.setattr(auth_mod.settings, "admin_usernames", "alice@example.test")
    client, _, _ = harness
    pair = _link(client)
    client.app.dependency_overrides.clear()
    response = client.get("/optimizations", headers=_bearer(pair["access_token"]))
    assert response.status_code == 200, response.text
    assert response.json()["admin"] is False


_POLICY_CASES: list[tuple[str, bool]] = [
    ("/optimizations", True),
    ("/optimizations/{optimization_id}/cancel", True),
    ("/datasets/library", True),
    ("/agent/conversations", True),
    ("/billing/wallet", True),
    ("/account/experience", True),
    ("/settings/user-preferences", True),
    ("/notifications", True),
    ("/admin/quotas", False),
    ("/settings/api-token", False),
    ("/settings/desktop-links", False),
    ("/desktop/link/authorize", False),
    ("/local/status", False),
    ("/auth/security/totp/setup", False),
    ("/oauth/consent", False),
    ("/secrets", False),
    ("/telemetry/summary", False),
    ("/account/export", False),
    ("/account/delete", False),
    ("/account/package-registry", True),
    ("/agent/protected-settings", False),
    ("/billing/checkout", False),
    ("/billing/subscription/checkout", False),
    ("/billing/portal", False),
    ("/billing/payment-methods/{payment_method_id}", False),
    ("/billing/byok/keys", False),
    ("/billing/byok/openrouter/oauth/start", False),
    ("/connectors/{provider}", True),
    ("/connectors/{provider}/credentials", False),
    ("/connectors/{provider}/oauth/start", False),
    ("/connectors/{provider}/picker", False),
    ("/connectors/github/file", True),
    ("/connectors/huggingface/token", False),
    ("/connectors/huggingface/oauth/start", False),
    ("/notifications/slack/oauth/start", False),
    ("/optimizations/{optimization_id}/sharing/transfer", True),
    ("/optimizations/{optimization_id}/visibility", True),
    ("/datasets/library/{dataset_id}/sharing/members", True),
    ("/folders/{folder_id}/sharing/transfer", True),
    ("/tagging-sessions/{session_id}/sharing/members", True),
    ("/agent/conversations/{conversation_id}/share", True),
    ("/notifications/push/subscriptions", True),
    ("/notifications/slack", True),
]

# Path words that often mark a credential, payment, export or access grant.
# The test checks path words only, not what a route does: a new open route
# with one of these words fails until someone reviews it.
_SENSITIVE_SEGMENTS = frozenset(
    {
        "checkout",
        "credentials",
        "delete",
        "export",
        "keys",
        "oauth",
        "payment-methods",
        "picker",
        "portal",
        "secrets",
        "share",
        "sharing",
        "token",
        "tokens",
        "visibility",
    }
)

# Reviewed open routes that carry a sensitive path word. A web session may
# share, claim and export the account's own items, so the Mac token may too.
# None of them returns a stored credential.
_REVIEWED_SENSITIVE_OPEN = frozenset(
    {
        "/agent/conversations/{conversation_id}/share",
        "/datasets/library/{dataset_id}/sharing",
        "/datasets/library/{dataset_id}/sharing/members",
        "/datasets/library/{dataset_id}/sharing/members/{username}",
        "/datasets/library/{dataset_id}/sharing/transfer",
        "/datasets/share/{token}",
        "/datasets/share/{token}/claim",
        "/folders/share/{token}/claim",
        "/folders/{folder_id}/sharing",
        "/folders/{folder_id}/sharing/members",
        "/folders/{folder_id}/sharing/members/{username}",
        "/folders/{folder_id}/sharing/transfer",
        "/optimizations/{optimization_id}/export",
        "/optimizations/{optimization_id}/sharing",
        "/optimizations/{optimization_id}/sharing/members",
        "/optimizations/{optimization_id}/sharing/members/{username}",
        "/optimizations/{optimization_id}/sharing/transfer",
        "/optimizations/{optimization_id}/visibility",
        "/share/{token}",
        "/share/{token}/claim",
        "/share/{token}/serve",
        "/tagging-sessions/share/{token}/claim",
        "/tagging-sessions/{session_id}/sharing",
        "/tagging-sessions/{session_id}/sharing/members",
        "/tagging-sessions/{session_id}/sharing/members/{username}",
        "/tagging-sessions/{session_id}/sharing/transfer",
    }
)


@pytest.fixture(scope="module")
def live_route_paths() -> set[str]:
    """Build the real app without starting it and return its route templates.

    Returns:
        Every ``APIRoute`` template that the production app serves.
    """
    with (
        patch("core.api.app.get_job_store", return_value=MagicMock()),
        patch("core.api.app.get_worker", return_value=MagicMock()),
        patch("core.api.app.DspyService"),
    ):
        application = create_app(registry=ServiceRegistry())
    return {route.path for route in application.routes if isinstance(route, APIRoute)}


@pytest.mark.parametrize(("path", "allowed"), _POLICY_CASES)
def test_sync_route_policy(path: str, allowed: bool) -> None:
    """The allowlist gives web-session rights and keeps secrets, money and admin closed."""
    assert desktop_auth.desktop_sync_route_allowed(path) is allowed


def test_policy_cases_are_live_routes(live_route_paths: set[str]) -> None:
    """Every policy case names a route the app really serves."""
    assert {path for path, _ in _POLICY_CASES} - live_route_paths == set()


def test_no_open_route_is_sensitive(live_route_paths: set[str]) -> None:
    """Every live sync route with a sensitive segment is one that someone reviewed."""
    opened = {path for path in live_route_paths if desktop_auth.desktop_sync_route_allowed(path)}
    assert opened
    sensitive = {path for path in opened if _SENSITIVE_SEGMENTS & set(path.split("/"))}
    assert sensitive == _REVIEWED_SENSITIVE_OPEN


def test_session_jwt_and_pat_are_refused_on_desktop_routes(harness: Any) -> None:
    """Desktop routes accept only a desktop access token."""
    client, _, _ = harness
    pat = client.post("/settings/api-token").json()["token"]
    pair = _link(client)
    for token in (pat, "eyJhbGciOiJIUzI1NiJ9.e30.sig", pair["refresh_token"]):
        response = client.get("/desktop/v1/whoami", headers=_bearer(token))
        assert response.status_code == 401
        assert _code(response) == "auth.invalid_token"
    assert client.get("/desktop/v1/whoami").status_code == 401


def test_scope_is_enforced(harness: Any) -> None:
    """A link without a scope gets 403 on routes that need it."""
    client, _, _ = harness
    pair = _link(client, scope="identity")
    assert client.get("/desktop/v1/whoami", headers=_bearer(pair["access_token"])).status_code == 200
    for path in ("/desktop/v1/balance", "/needs-sync"):
        response = client.get(path, headers=_bearer(pair["access_token"]))
        assert response.status_code == 403
        assert _code(response) == "desktop.scope_missing"


def test_unknown_scope_name_is_a_programming_error() -> None:
    """``get_desktop_principal`` refuses a scope outside the list."""
    with pytest.raises(ValueError):
        get_desktop_principal("admin")


def test_balance_reports_spendable_cents(harness: Any) -> None:
    """Balance returns the hosted spendable cents and the low-balance flag."""
    client, engine, _ = harness
    pair = _link(client)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(
                username="alice@example.test",
                stripe_customer_id="cus_test_desktop",
                balance_cents=1234,
                grant_remaining=0,
            )
        )
        session.commit()
    response = client.get("/desktop/v1/balance", headers=_bearer(pair["access_token"]))
    assert response.status_code == 200
    assert response.json() == {"available_cents": 1234, "currency": "usd", "low_balance": False}

    with Session(engine) as session:
        session.get(BillingCustomerModel, "alice@example.test").balance_cents = 20
        session.commit()
    low = client.get("/desktop/v1/balance", headers=_bearer(pair["access_token"])).json()
    assert low == {"available_cents": 20, "currency": "usd", "low_balance": True}


def test_expired_access_token_says_token_expired(harness: Any) -> None:
    """An access token past its hour gets 401 ``desktop.token_expired``."""
    client, engine, _ = harness
    pair = _link(client)
    with Session(engine) as session:
        for row in session.scalars(select(DesktopTokenModel).where(DesktopTokenModel.kind == "access")):
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    response = client.get("/desktop/v1/whoami", headers=_bearer(pair["access_token"]))
    assert response.status_code == 401
    assert _code(response) == "desktop.token_expired"


def test_refresh_rotates_the_pair(harness: Any) -> None:
    """A refresh returns a new pair; the new refresh token rotates again."""
    client, _, _ = harness
    pair = _link(client)
    second = _refresh(client, pair["refresh_token"])
    assert second.status_code == 200, second.text
    new = second.json()
    assert new["refresh_token"] != pair["refresh_token"]
    assert new["access_token"] != pair["access_token"]
    assert new["link_id"] == pair["link_id"]
    assert new["scope"] == ALL_SCOPES
    assert client.get("/desktop/v1/whoami", headers=_bearer(new["access_token"])).status_code == 200
    assert _refresh(client, new["refresh_token"]).status_code == 200


def test_refresh_retry_inside_grace_replaces_the_lost_pair(harness: Any) -> None:
    """A retried refresh inside 30 s gets a fresh pair; the lost pair is superseded."""
    client, _, _ = harness
    pair = _link(client)
    lost = _refresh(client, pair["refresh_token"]).json()
    retry = _refresh(client, pair["refresh_token"])
    assert retry.status_code == 200, retry.text
    fresh = retry.json()
    assert client.get("/desktop/v1/whoami", headers=_bearer(lost["access_token"])).status_code == 401
    assert client.get("/desktop/v1/whoami", headers=_bearer(fresh["access_token"])).status_code == 200
    late = _refresh(client, lost["refresh_token"])
    assert late.status_code == 401
    assert _code(late) == "desktop.link_revoked"


def test_honest_retry_inside_grace_keeps_the_link(harness: Any, mailbox: Mailbox) -> None:
    """A Mac that lost one or two refresh responses keeps a working link and gets no email."""
    client, engine, _ = harness
    pair = _link(client)
    mailbox.clear()
    _refresh(client, pair["refresh_token"])
    _refresh(client, pair["refresh_token"])
    retry = _refresh(client, pair["refresh_token"])
    assert retry.status_code == 200, retry.text
    kept = retry.json()
    assert kept["link_id"] == pair["link_id"]
    assert client.get("/desktop/v1/whoami", headers=_bearer(kept["access_token"])).status_code == 200
    rotated = _refresh(client, kept["refresh_token"])
    assert rotated.status_code == 200, rotated.text
    assert client.get("/desktop/v1/whoami", headers=_bearer(rotated.json()["access_token"])).status_code == 200
    with Session(engine) as session:
        assert session.get(DesktopLinkModel, pair["link_id"]).revoked_at is None
    assert mailbox == []


def test_replay_inside_grace_cannot_take_over_the_link(harness: Any, mailbox: Mailbox) -> None:
    """A stolen refresh token replayed inside 30 s ends the link at the Mac's next refresh (R782A-1)."""
    client, engine, _ = harness
    pair = _link(client)
    mailbox.clear()
    mac = _refresh(client, pair["refresh_token"])
    assert mac.status_code == 200
    thief = _refresh(client, pair["refresh_token"])
    assert thief.status_code == 200
    mac_next = _refresh(client, mac.json()["refresh_token"])
    assert mac_next.status_code == 401
    assert _code(mac_next) == "desktop.link_revoked"
    with Session(engine) as session:
        assert session.get(DesktopLinkModel, pair["link_id"]).revoked_at is not None
    assert _refresh(client, thief.json()["refresh_token"]).status_code == 401
    who = client.get("/desktop/v1/whoami", headers=_bearer(thief.json()["access_token"]))
    assert _code(who) == "desktop.link_revoked"
    assert len(mailbox) == 1
    assert "removed" in mailbox[0][1].lower()


def test_thief_first_then_mac_retry_still_detects_reuse(harness: Any, mailbox: Mailbox) -> None:
    """If the thief rotates first and the Mac retries, the thief's next refresh revokes the link."""
    client, _, _ = harness
    pair = _link(client)
    mailbox.clear()
    thief = _refresh(client, pair["refresh_token"]).json()
    assert _refresh(client, pair["refresh_token"]).status_code == 200
    stolen_next = _refresh(client, thief["refresh_token"])
    assert stolen_next.status_code == 401
    assert _code(stolen_next) == "desktop.link_revoked"
    assert len(mailbox) == 1


def test_refresh_reuse_after_grace_revokes_the_link(harness: Any) -> None:
    """Reuse of a rotated refresh token after 30 s revokes the whole link."""
    client, engine, _ = harness
    pair = _link(client)
    new = _refresh(client, pair["refresh_token"]).json()
    with Session(engine) as session:
        for row in session.scalars(select(DesktopTokenModel).where(DesktopTokenModel.used_at.is_not(None))):
            row.used_at = datetime.now(UTC) - timedelta(seconds=31)
        session.commit()
    reuse = _refresh(client, pair["refresh_token"])
    assert reuse.status_code == 401
    assert _code(reuse) == "desktop.link_revoked"
    who = client.get("/desktop/v1/whoami", headers=_bearer(new["access_token"]))
    assert who.status_code == 401
    assert _code(who) == "desktop.link_revoked"
    assert _refresh(client, new["refresh_token"]).status_code == 401


def test_refresh_reuse_after_successor_was_used_revokes_the_link(harness: Any) -> None:
    """Once the successor refresh token is used, the old one is a theft signal."""
    client, _, _ = harness
    pair = _link(client)
    second = _refresh(client, pair["refresh_token"]).json()
    third = _refresh(client, second["refresh_token"]).json()
    reuse = _refresh(client, pair["refresh_token"])
    assert reuse.status_code == 401
    assert _code(reuse) == "desktop.link_revoked"
    assert client.get("/desktop/v1/whoami", headers=_bearer(third["access_token"])).status_code == 401


@pytest.fixture
def mailbox(monkeypatch: pytest.MonkeyPatch) -> Mailbox:
    """Capture the security notices the router sends.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        The list that collects ``(to, subject, body, html)`` per sent notice.
    """
    sent: Mailbox = []
    monkeypatch.setattr(desktop_link, "email_configured", lambda: True)
    monkeypatch.setattr(
        desktop_link,
        "send_email",
        lambda to, subject, body, *, html_body: sent.append((to, subject, body, html_body)),
    )
    return sent


def test_new_link_sends_a_notice(harness: Any, mailbox: Mailbox) -> None:
    """A new link emails the account owner with the Mac name."""
    client, _, _ = harness
    _link(client)
    assert len(mailbox) == 1
    to, subject, body, html_body = mailbox[0]
    assert to == "alice@example.test"
    assert "linked" in subject.lower()
    assert body == (
        'The Mac "Alice\'s MacBook Pro" is now linked to your Skynet account.\n\n'
        "If you did not link this Mac, open Settings > Security on the Skynet website and remove it."
    )
    assert "skyl_" not in body
    assert html_body.startswith("<!doctype html>")
    assert "Alice&#x27;s MacBook Pro" in html_body
    assert "Settings &gt; Security" in html_body
    assert "Skynet sends this email" not in html_body
    assert html_body.count('class="sk-btn-a"') == 1
    assert "/?settings=security" in html_body
    assert "skyl_" not in html_body


def test_reuse_revocation_sends_a_notice(harness: Any, mailbox: Mailbox) -> None:
    """Revoking a link for refresh-token reuse emails the account owner."""
    client, _, _ = harness
    pair = _link(client)
    second = _refresh(client, pair["refresh_token"]).json()
    _refresh(client, second["refresh_token"])
    mailbox.clear()
    assert _refresh(client, pair["refresh_token"]).status_code == 401
    assert len(mailbox) == 1
    to, subject, body, html_body = mailbox[0]
    assert to == "alice@example.test"
    assert "removed" in subject.lower()
    assert body == (
        'Skynet removed the link to the Mac "Alice\'s MacBook Pro". '
        "An old sign-in token of this Mac was used again. Someone can have a copy of it.\n\n"
        "To continue, link the Mac again from the Skynet app."
    )
    assert html_body.startswith("<!doctype html>")
    assert "Alice&#x27;s MacBook Pro" in html_body
    assert "link the Mac again from the Skynet app" in html_body
    assert 'class="sk-btn-a"' not in html_body


def test_refresh_retry_inside_grace_sends_no_notice(harness: Any, mailbox: Mailbox) -> None:
    """Normal rotation and a retry inside 30 s send no email."""
    client, _, _ = harness
    pair = _link(client)
    mailbox.clear()
    _refresh(client, pair["refresh_token"])
    assert _refresh(client, pair["refresh_token"]).status_code == 200
    assert mailbox == []


def test_notice_failure_does_not_fail_the_call(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """An SMTP failure is logged, and the token call still succeeds."""
    client, _, _ = harness

    def _broken(to: str, subject: str, body: str, *, html_body: str) -> None:
        """Fail like an unreachable relay.

        Args:
            to: Recipient.
            subject: Subject line.
            body: Message body.
            html_body: HTML alternative.

        Raises:
            OSError: Always.
        """
        raise OSError("relay down")

    monkeypatch.setattr(desktop_link, "email_configured", lambda: True)
    monkeypatch.setattr(desktop_link, "send_email", _broken)
    pair = _link(client)
    assert client.get("/desktop/v1/whoami", headers=_bearer(pair["access_token"])).status_code == 200


def test_no_notice_without_smtp(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without an SMTP relay the router does not try to send."""
    client, _, _ = harness
    sent: list[str] = []
    monkeypatch.setattr(desktop_link, "email_configured", lambda: False)
    monkeypatch.setattr(desktop_link, "send_email", lambda to, subject, body, *, html_body: sent.append(to))
    _link(client)
    assert sent == []


@pytest.mark.parametrize("bad", ["skyl_rt_unknown", "not-a-token", ""])
def test_refresh_rejects_unknown_tokens(harness: Any, bad: str) -> None:
    """An unknown refresh token fails with invalid_grant."""
    client, _, _ = harness
    response = _refresh(client, bad)
    assert response.status_code == 400
    assert _code(response) == "desktop.invalid_grant"


def test_access_token_cannot_refresh(harness: Any) -> None:
    """The refresh grant accepts only a refresh token."""
    client, _, _ = harness
    pair = _link(client)
    response = _refresh(client, pair["access_token"])
    assert response.status_code == 400
    assert _code(response) == "desktop.invalid_grant"


def test_idle_refresh_token_expires(harness: Any) -> None:
    """A refresh token unused for 30 days fails."""
    client, engine, _ = harness
    pair = _link(client)
    with Session(engine) as session:
        for row in session.scalars(select(DesktopTokenModel).where(DesktopTokenModel.kind == "refresh")):
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    response = _refresh(client, pair["refresh_token"])
    assert response.status_code == 400
    assert _code(response) == "desktop.invalid_grant"


def test_absolute_expiry_ends_the_link(harness: Any) -> None:
    """After 365 days the link stops working and must be made again."""
    client, engine, _ = harness
    pair = _link(client)
    with Session(engine) as session:
        for link in session.scalars(select(DesktopLinkModel)):
            link.absolute_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    assert _code(client.get("/desktop/v1/whoami", headers=_bearer(pair["access_token"]))) == "desktop.link_revoked"
    assert _refresh(client, pair["refresh_token"]).status_code == 401
    assert client.get("/settings/desktop-links").json() == []


@pytest.mark.parametrize("kind", ["access_token", "refresh_token"])
def test_revoke_with_either_token_ends_the_link(harness: Any, kind: str) -> None:
    """Revoke with either token revokes the link and all its tokens."""
    client, _, _ = harness
    pair = _link(client)
    assert client.post("/desktop/link/revoke", json={"token": pair[kind]}).status_code == 200
    who = client.get("/desktop/v1/whoami", headers=_bearer(pair["access_token"]))
    assert _code(who) == "desktop.link_revoked"
    assert _refresh(client, pair["refresh_token"]).status_code == 401


@pytest.mark.parametrize("token", ["skyl_at_unknown", "anything", ""])
def test_revoke_unknown_token_still_returns_200(harness: Any, token: str) -> None:
    """Revoke answers 200 for any token (RFC 7009 style)."""
    client, _, _ = harness
    assert client.post("/desktop/link/revoke", json={"token": token}).status_code == 200


def test_linked_macs_list_and_remove(harness: Any) -> None:
    """The owner sees each live link and Remove revokes it at once."""
    client, _, session_user = harness
    first = _link(client)
    second = _link(client, scope="identity")
    listed = client.get("/settings/desktop-links")
    assert listed.status_code == 200
    rows = listed.json()
    assert {row["link_id"] for row in rows} == {first["link_id"], second["link_id"]}
    assert set(rows[0]) == {"link_id", "device_name", "scopes", "created_at", "last_used_at"}

    session_user.username = "mallory@example.test"
    assert client.get("/settings/desktop-links").json() == []
    assert client.delete(f"/settings/desktop-links/{first['link_id']}").status_code == 404

    session_user.username = "alice@example.test"
    assert client.delete(f"/settings/desktop-links/{first['link_id']}").status_code == 204
    assert _code(client.get("/desktop/v1/whoami", headers=_bearer(first["access_token"]))) == "desktop.link_revoked"
    assert [row["link_id"] for row in client.get("/settings/desktop-links").json()] == [second["link_id"]]
    assert client.delete("/settings/desktop-links/dl_missing").status_code == 404


def test_linked_macs_routes_refuse_personal_access_token(harness: Any) -> None:
    """Listing and removing Linked Macs needs a browser session, not a ``skyd_`` token (R782A-2)."""
    client, _, _ = harness
    pair = _link(client)
    pat = client.post("/settings/api-token").json()["token"]
    client.app.dependency_overrides.clear()
    listed = client.get("/settings/desktop-links", headers=_bearer(pat))
    assert listed.status_code == 403
    assert _code(listed) == "desktop.session_required"
    removed = client.delete(f"/settings/desktop-links/{pair['link_id']}", headers=_bearer(pat))
    assert removed.status_code == 403
    assert _code(removed) == "desktop.session_required"
    assert client.get("/desktop/v1/whoami", headers=_bearer(pair["access_token"])).status_code == 200


def test_eleventh_link_is_refused(harness: Any) -> None:
    """A user can hold at most 10 live links."""
    client, _, _ = harness
    for _ in range(10):
        _link(client, scope="identity")
    response, _ = _authorize(client)
    assert response.status_code == 409
    assert _code(response) == "desktop.too_many_links"


def test_removed_links_do_not_count_toward_the_cap(harness: Any) -> None:
    """Revoked links free their slot."""
    client, _, _ = harness
    links = [_link(client, scope="identity") for _ in range(10)]
    client.delete(f"/settings/desktop-links/{links[0]['link_id']}")
    response, _ = _authorize(client)
    assert response.status_code == 200


def test_idle_expired_link_is_gone(harness: Any) -> None:
    """A link with no usable refresh token is not listed, not removable, and frees its cap slot (R782B-2)."""
    client, engine, _ = harness
    links = [_link(client, scope="identity") for _ in range(10)]
    idle, rotated = links[0], links[1]
    assert _refresh(client, rotated["refresh_token"]).status_code == 200
    with Session(engine) as session:
        for row in session.scalars(
            select(DesktopTokenModel).where(
                DesktopTokenModel.link_id == idle["link_id"], DesktopTokenModel.kind == "refresh"
            )
        ):
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()

    listed = {row["link_id"] for row in client.get("/settings/desktop-links").json()}
    assert idle["link_id"] not in listed
    assert rotated["link_id"] in listed
    assert len(listed) == 9
    assert client.delete(f"/settings/desktop-links/{idle['link_id']}").status_code == 404
    assert _code(client.get("/desktop/v1/whoami", headers=_bearer(idle["access_token"]))) == "desktop.link_revoked"
    assert _code(_refresh(client, idle["refresh_token"])) == "desktop.invalid_grant"
    eleventh = _link(client, scope="identity")
    assert client.get("/desktop/v1/whoami", headers=_bearer(eleventh["access_token"])).status_code == 200
    response, _ = _authorize(client)
    assert _code(response) == "desktop.too_many_links"


def test_authorize_is_rate_limited_per_user(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The consent endpoint uses a per-user hourly limit of 10."""
    client, _, _ = harness
    calls: list[tuple[str, int, int]] = []

    class _Limiter:
        """Record each limit check."""

        def __init__(self, client: Any) -> None:
            """Ignore the Redis client."""

        def enforce(self, key: str, *, limit: int, window_seconds: int) -> None:
            """Record the call."""
            calls.append((key, limit, window_seconds))

    monkeypatch.setattr(desktop_link, "RateLimiter", _Limiter)
    pair = _link(client)
    _refresh(client, pair["refresh_token"])
    assert ("desktop-link-authorize:alice@example.test", 10, 3600) in calls
    assert (f"desktop-link-refresh:{pair['link_id']}", 60, 3600) in calls
    assert ("desktop-link-code:testclient", 30, 60) in calls


def test_code_rate_limit_uses_the_proxy_appended_address(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The code limit keys on the last X-Forwarded-For hop, not a spoofable first one."""
    client, _, _ = harness
    keys: list[str] = []

    class _Limiter:
        """Record each limit key."""

        def __init__(self, client: Any) -> None:
            """Ignore the Redis client."""

        def enforce(self, key: str, *, limit: int, window_seconds: int) -> None:
            """Record the key."""
            keys.append(key)

    monkeypatch.setattr(desktop_link, "RateLimiter", _Limiter)
    client.post(
        "/desktop/link/token",
        headers={"X-Forwarded-For": "6.6.6.6, 203.0.113.9"},
        json={
            "grant_type": "authorization_code",
            "code": "x" * 43,
            "code_verifier": "y" * 43,
            "redirect_uri": REDIRECT,
            "client_id": "skynet-desktop",
        },
    )
    assert keys == ["desktop-link-code:203.0.113.9"]


def test_account_deletion_removes_desktop_links(harness: Any) -> None:
    """Deleting the account deletes its links, tokens and codes."""
    client, engine, _ = harness
    pair = _link(client)
    _authorize(client)
    with Session(engine) as session:
        delete_account(session, "alice@example.test")
        session.commit()
    with Session(engine) as session:
        assert session.scalars(select(DesktopLinkModel)).all() == []
        assert session.scalars(select(DesktopTokenModel)).all() == []
        assert session.scalars(select(DesktopLinkCodeModel)).all() == []
    assert client.get("/desktop/v1/whoami", headers=_bearer(pair["access_token"])).status_code == 401


def test_stale_session_after_account_deletion_cannot_link_a_mac(harness: Any) -> None:
    """A session JWT that outlives the account can neither approve nor exchange a link."""
    client, engine, _ = harness
    with Session(engine) as session:
        delete_account(session, "alice@example.test")
        session.commit()
    response, _ = _authorize(client)
    assert response.status_code == 401
    assert _code(response) == "auth.invalid_token"

    verifier, challenge = _pkce()
    now = datetime.now(UTC)
    with Session(engine) as session:
        session.add(
            DesktopLinkCodeModel(
                code_hash=hash_api_token("orphan-code"),
                username="alice@example.test",
                code_challenge=challenge,
                redirect_uri=REDIRECT,
                scopes="identity",
                device_name="Mac",
                created_at=now,
                expires_at=now + timedelta(seconds=60),
            )
        )
        session.commit()
    exchange = _exchange(client, "orphan-code", verifier)
    assert exchange.status_code == 401
    assert _code(exchange) == "auth.invalid_token"
    with Session(engine) as session:
        assert session.scalars(select(DesktopLinkModel)).all() == []
        assert session.get(DesktopLinkCodeModel, hash_api_token("orphan-code")).used_at is not None


def test_account_export_lists_desktop_links_without_secrets(harness: Any) -> None:
    """The data export has one ``desktop_links`` row per link and no token or code material (R782A-3)."""
    client, engine, _ = harness
    first = _link(client)
    second = _link(client, scope="identity")
    client.delete(f"/settings/desktop-links/{second['link_id']}")
    _authorize(client)
    with Session(engine) as session:
        bundle = export_account(session, "alice@example.test")
        token_hashes = {row.token_hash for row in session.scalars(select(DesktopTokenModel))}
        code_hashes = {row.code_hash for row in session.scalars(select(DesktopLinkCodeModel))}
    rows = {row["id"]: row for row in bundle["desktop_links"]}
    assert set(rows) == {first["link_id"], second["link_id"]}
    assert set(rows[first["link_id"]]) == {
        "id",
        "device_name",
        "scopes",
        "created_at",
        "last_used_at",
        "revoked_at",
        "absolute_expires_at",
    }
    assert rows[first["link_id"]]["device_name"] == "Alice's MacBook Pro"
    assert rows[first["link_id"]]["scopes"] == ALL_SCOPES.split()
    assert rows[first["link_id"]]["revoked_at"] is None
    assert rows[second["link_id"]]["revoked_at"] is not None
    text = repr(bundle)
    for secret in (*token_hashes, *code_hashes, first["access_token"], first["refresh_token"]):
        assert secret not in text


def test_token_prefixes_are_distinct() -> None:
    """Access and refresh tokens carry their own ``skyl_`` prefixes."""
    access = desktop_auth.generate_desktop_token("access")
    refresh = desktop_auth.generate_desktop_token("refresh")
    assert access.startswith("skyl_at_")
    assert refresh.startswith("skyl_rt_")
    assert len(access) == len("skyl_at_") + 43
