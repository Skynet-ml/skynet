"""Tests for signed backend API authentication."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ...storage.models import ApiTokenModel, UserModel
from .. import auth as auth_mod
from ..auth import (
    AuthenticatedUser,
    generate_api_token,
    get_authenticated_user,
    hash_api_token,
    require_admin_user,
)
from ..errors import DomainError

# Tests that fail before any store lookup (bad header, signature, audience)
# pass a placeholder for ``request``.
_NO_REQUEST = cast(Any, None)


def _base64url(value: bytes) -> str:
    """Encode bytes as unpadded base64url."""
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _sign(payload: dict[str, Any], secret: str = "test-secret") -> str:
    """Sign a test HS256 JWT.

    Args:
        payload: JWT payload to encode.
        secret: HMAC secret.

    Returns:
        A compact JWT string.
    """
    header = _base64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode("utf-8"))
    body = _base64url(json.dumps(payload).encode("utf-8"))
    signature = _base64url(
        hmac.new(secret.encode("utf-8"), f"{header}.{body}".encode("ascii"), hashlib.sha256).digest()
    )
    return f"{header}.{body}.{signature}"


def _store_request(*accounts: str) -> Any:
    """Build a request stand-in whose store holds ``accounts`` as ``users`` rows.

    Args:
        accounts: Emails to create account rows for.

    Returns:
        The request stand-in.
    """
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    UserModel.__table__.create(engine)
    ApiTokenModel.__table__.create(engine)
    with Session(engine) as session:
        for email in accounts:
            session.add(UserModel(email=email, name=email, password_hash="x"))
        session.commit()
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(job_store=SimpleNamespace(engine=engine))),
        state=SimpleNamespace(),
    )


def _payload(**overrides: Any) -> dict[str, Any]:
    """Build a valid backend auth payload with optional overrides."""
    now = int(time.time())
    payload: dict[str, Any] = {
        "aud": "skynet-backend",
        "iss": "skynet-frontend",
        "sub": "alice@example.com",
        "name": "alice@example.com",
        "role": "user",
        "groups": ["skynet-admins"],
        "iat": now,
        "exp": now + 300,
    }
    payload.update(overrides)
    return payload


def test_get_authenticated_user_accepts_valid_signed_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid backend token resolves to an authenticated user."""
    monkeypatch.setattr(auth_mod.settings, "backend_auth_secret", SecretStr("test-secret"))
    token = _sign(_payload())

    user = get_authenticated_user(_store_request("alice@example.com"), f"Bearer {token}")

    assert user.username == "alice@example.com"
    assert user.groups == ("skynet-admins",)


@pytest.mark.parametrize(
    ("audience", "issuer"),
    [
        ("skynet-backend", "skynet-frontend"),
        ("app-backend", "app-frontend"),
        ("app-backend", "skynet-frontend"),
        ("skynet-backend", "app-frontend"),
    ],
)
def test_get_authenticated_user_accepts_both_audience_and_issuer_spellings(
    monkeypatch: pytest.MonkeyPatch, audience: str, issuer: str
) -> None:
    """Tokens minted with the pre-rename or the neutral ``aud``/``iss`` both verify."""
    monkeypatch.setattr(auth_mod.settings, "backend_auth_secret", SecretStr("test-secret"))
    token = _sign(_payload(aud=audience, iss=issuer))

    assert (
        get_authenticated_user(_store_request("alice@example.com"), f"Bearer {token}").username == "alice@example.com"
    )


@pytest.mark.parametrize(("audience", "issuer"), [("other-backend", "app-frontend"), ("app-backend", "other")])
def test_get_authenticated_user_rejects_a_foreign_audience_or_issuer(
    monkeypatch: pytest.MonkeyPatch, audience: str, issuer: str
) -> None:
    """A correctly signed token for another audience or issuer is rejected."""
    monkeypatch.setattr(auth_mod.settings, "backend_auth_secret", SecretStr("test-secret"))
    token = _sign(_payload(aud=audience, iss=issuer))

    with pytest.raises(HTTPException) as exc:
        get_authenticated_user(_NO_REQUEST, f"Bearer {token}")

    assert exc.value.status_code == 401


def test_get_authenticated_user_rejects_missing_token() -> None:
    """A missing Authorization header is rejected."""
    with pytest.raises(HTTPException) as exc:
        get_authenticated_user(_NO_REQUEST, None)

    assert exc.value.status_code == 401


def test_get_authenticated_user_rejects_bad_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token signed with another secret is rejected."""
    monkeypatch.setattr(auth_mod.settings, "backend_auth_secret", SecretStr("test-secret"))
    token = _sign(_payload(), secret="wrong-secret")

    with pytest.raises(HTTPException) as exc:
        get_authenticated_user(_NO_REQUEST, f"Bearer {token}")

    assert exc.value.status_code == 401


def test_require_admin_user_accepts_matching_idp_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """An IdP group configured as admin grants backend admin access."""
    monkeypatch.setattr(auth_mod.settings, "admin_usernames", "")
    monkeypatch.setattr(auth_mod.settings, "admin_groups", "skynet-admins")
    user = AuthenticatedUser(username="alice@example.com", role="user", groups=("skynet-admins",))

    assert require_admin_user(user) is user


def test_require_admin_user_rejects_non_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """A user with no matching break-glass username or group is rejected."""
    monkeypatch.setattr(auth_mod.settings, "admin_usernames", "")
    monkeypatch.setattr(auth_mod.settings, "admin_groups", "skynet-admins")
    user = AuthenticatedUser(username="alice@example.com", role="user", groups=("other",))

    with pytest.raises(HTTPException) as exc:
        require_admin_user(user)

    assert exc.value.status_code == 403


def test_require_admin_user_accepts_signed_role_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    """The frontend's signed role=="admin" claim grants backend admin access."""
    monkeypatch.setattr(auth_mod.settings, "admin_usernames", "")
    monkeypatch.setattr(auth_mod.settings, "admin_groups", "")
    user = AuthenticatedUser(username="alice@example.com", role="admin", groups=())

    assert require_admin_user(user) is user


def test_desktop_user_is_never_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Mac link caller gets no admin rights from any admin source."""
    monkeypatch.setattr(auth_mod.settings, "admin_usernames", "alice@example.com")
    monkeypatch.setattr(auth_mod.settings, "admin_groups", "skynet-admins")
    user = AuthenticatedUser(username="alice@example.com", role="admin", groups=("skynet-admins",), desktop=True)

    with pytest.raises(DomainError):
        require_admin_user(user)


def test_desktop_token_without_a_route_is_refused() -> None:
    """A ``skyl_`` token outside a matched route fails closed."""
    request = cast(Any, SimpleNamespace(scope={}, method="GET", state=SimpleNamespace()))

    with pytest.raises(DomainError) as excinfo:
        get_authenticated_user(request, authorization="Bearer skyl_at_example")

    assert excinfo.value.code == "auth.desktop_token_not_allowed"


def _pat_request(username: str = "alice@example.com") -> tuple[Any, str]:
    """Build a request stand-in whose store holds one active PAT.

    Args:
        username: Owner of the stored token.

    Returns:
        The request stand-in and the raw ``skyd_`` token it accepts.
    """
    request = _store_request(username)
    token = generate_api_token()
    with Session(request.app.state.job_store.engine) as session:
        session.add(ApiTokenModel(username=username, token_hash=hash_api_token(token), last4=token[-4:]))
        session.commit()
    return request, token


def test_pat_auth_stamps_api_token_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PAT call marks the request's auth channel as ``api_token``."""
    monkeypatch.setattr(auth_mod.settings, "max_monthly_active_users", 0)
    request, token = _pat_request()

    user = get_authenticated_user(request, f"Bearer {token}")

    assert user.username == "alice@example.com"
    assert request.state.auth_channel == "api_token"


def test_session_jwt_auth_leaves_channel_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """A session-JWT call carries no ``api_token`` stamp, so serve reads it as ``ui``."""
    monkeypatch.setattr(auth_mod.settings, "backend_auth_secret", SecretStr("test-secret"))
    monkeypatch.setattr(auth_mod.settings, "max_monthly_active_users", 0)
    request, _token = _pat_request()

    get_authenticated_user(request, f"Bearer {_sign(_payload())}")

    assert not hasattr(request.state, "auth_channel")


def test_session_jwt_for_a_deleted_account_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid, unexpired JWT whose ``users`` row is gone gets 401 (SKY-150)."""
    monkeypatch.setattr(auth_mod.settings, "backend_auth_secret", SecretStr("test-secret"))
    monkeypatch.setattr(auth_mod.settings, "max_monthly_active_users", 0)
    request = _store_request("someone-else@example.com")

    with pytest.raises(DomainError) as exc:
        get_authenticated_user(request, f"Bearer {_sign(_payload())}")

    assert exc.value.status_code == 401
    # A distinct code lets the web app sign the browser out instead of retrying.
    assert exc.value.code == "auth.account_not_found"


def test_session_jwt_without_a_store_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no store to check the account against, a valid JWT is not accepted."""
    monkeypatch.setattr(auth_mod.settings, "backend_auth_secret", SecretStr("test-secret"))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()), state=SimpleNamespace())

    with pytest.raises(HTTPException) as exc:
        get_authenticated_user(cast(Any, request), f"Bearer {_sign(_payload())}")

    assert exc.value.status_code == 500
