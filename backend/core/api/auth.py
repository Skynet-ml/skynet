"""Backend API authentication and authorization helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Header, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..storage.models import ApiTokenModel, UserModel
from .desktop_auth import desktop_sync_route_allowed, resolve_desktop_principal
from .errors import DomainError
from .monthly_active_users import enforce_monthly_active_user_limit
from .token_hashing import DESKTOP_TOKEN_PREFIX, hash_api_token

_EXPECTED_ALGORITHM = "HS256"
# Both spellings verify so the frontend and backend can deploy in either order;
# a token is still bound to this backend by the shared HMAC secret.
_EXPECTED_AUDIENCES = frozenset({"app-backend", "skynet-backend"})
_EXPECTED_ISSUERS = frozenset({"app-frontend", "skynet-frontend"})

# Personal Access Token (PAT) — a long-lived, user-generated API credential
# for programmatic backend access, distinct from the short-lived session JWT
# the frontend mints. The ``skyd_`` prefix makes it identifiable in logs and
# secret scanners and lets the auth dependency route it to the DB-backed path
# instead of JWT verification. Stored only as a SHA-256 hash; the plaintext is
# shown to the user exactly once at creation.
API_TOKEN_PREFIX = "skyd_"
_API_TOKEN_SECRET_BYTES = 32
# A PAT stops working one year after it is generated (SKY-150), so a leaked or
# forgotten token cannot live forever. Generating a new one starts a new year.
# The age comes from ``created_at``, so no schema change is needed.
API_TOKEN_MAX_AGE = timedelta(days=365)

__all__ = ["DESKTOP_TOKEN_PREFIX", "hash_api_token"]


@dataclass(frozen=True)
class AuthenticatedUser:
    """Authenticated backend API user."""

    username: str
    role: str
    groups: tuple[str, ...]
    # True for a Mac link token (SKY-223); such a caller is never an admin.
    desktop: bool = False


def _decode_base64url(value: str) -> bytes:
    """Decode an unpadded base64url string.

    Args:
        value: Base64url string to decode.

    Returns:
        Decoded bytes.
    """
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}".encode("ascii"))


def _json_part(value: str) -> dict[str, Any]:
    """Decode one JWT JSON segment.

    Args:
        value: Encoded JWT segment.

    Returns:
        Decoded JSON object.

    Raises:
        DomainError: When the segment is malformed.
    """
    try:
        decoded = json.loads(_decode_base64url(value))
    except (ValueError, json.JSONDecodeError) as exc:
        raise DomainError("auth.invalid_token", status=401) from exc
    if not isinstance(decoded, dict):
        raise DomainError("auth.invalid_token", status=401)
    return decoded


def _verify_hs256(token: str, secret: str) -> dict[str, Any]:
    """Verify an HS256 JWT and return its payload.

    Args:
        token: JWT from the Authorization header.
        secret: Shared HMAC secret.

    Returns:
        Verified payload.

    Raises:
        DomainError: When the token is malformed, unsigned, expired, or for
            another audience/issuer.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise DomainError("auth.invalid_token", status=401)
    header = _json_part(parts[0])
    payload = _json_part(parts[1])
    if header.get("alg") != _EXPECTED_ALGORITHM or header.get("typ") != "JWT":
        raise DomainError("auth.invalid_token", status=401)
    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    expected = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    actual = _decode_base64url(parts[2])
    if not hmac.compare_digest(expected, actual):
        raise DomainError("auth.invalid_token", status=401)
    now = int(time.time())
    if payload.get("aud") not in _EXPECTED_AUDIENCES or payload.get("iss") not in _EXPECTED_ISSUERS:
        raise DomainError("auth.invalid_token", status=401)
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < now:
        raise DomainError("auth.invalid_token", status=401)
    return payload


def _normalise_groups(groups: Any) -> tuple[str, ...]:
    """Normalize token group claims into lowercase strings.

    Args:
        groups: Raw ``groups`` claim.

    Returns:
        Tuple of normalized groups.
    """
    if isinstance(groups, str):
        values: Sequence[Any] = [groups]
    elif isinstance(groups, Sequence):
        values = groups
    else:
        values = []
    return tuple(str(group).strip().lower() for group in values if str(group).strip())


def generate_api_token() -> str:
    """Return a fresh ``skyd_``-prefixed personal access token.

    Returns:
        A token string carrying 256 bits of CSPRNG entropy after the prefix.
    """
    return f"{API_TOKEN_PREFIX}{secrets.token_urlsafe(_API_TOKEN_SECRET_BYTES)}"


def is_api_token(token: str) -> bool:
    """Return whether ``token`` is shaped like a Skynet personal access token.

    Args:
        token: Raw bearer credential (without the ``Bearer `` scheme prefix).

    Returns:
        True when ``token`` carries the PAT prefix.
    """
    return token.startswith(API_TOKEN_PREFIX)


def token_last4(token: str) -> str:
    """Return the last four characters of ``token`` for display.

    Args:
        token: Raw token.

    Returns:
        The trailing four characters.
    """
    return token[-4:]


def _as_utc(value: datetime) -> datetime:
    """Return ``value`` as an aware UTC datetime.

    SQLite hands back naive datetimes for ``DateTime(timezone=True)``
    columns; PostgreSQL hands back aware ones.

    Args:
        value: Stored timestamp.

    Returns:
        The same instant with UTC tzinfo.
    """
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def api_token_expired(created_at: datetime, now: datetime) -> bool:
    """Return whether a PAT generated at ``created_at`` is past :data:`API_TOKEN_MAX_AGE`.

    Args:
        created_at: The token row's stored generation time.
        now: Current aware UTC time.

    Returns:
        ``True`` when the token no longer authenticates.
    """
    return _as_utc(created_at) + API_TOKEN_MAX_AGE <= now


def _account_exists(session: Session, username: str) -> bool:
    """Return whether a ``users`` row exists for ``username``.

    Args:
        session: Open ORM session.
        username: Lowercased account identity (the email).

    Returns:
        True when the account row exists.
    """
    return session.scalar(select(UserModel.email).where(UserModel.email == username)) is not None


def _require_account(request: Request, username: str) -> None:
    """Refuse a session JWT whose account no longer exists.

    Session JWTs are stateless: the frontend keeps minting them for a browser
    that signed in before the account was deleted. Checking the ``users`` row
    here, in the one shared auth dependency, makes every authenticated route
    refuse a deleted account (SKY-150).

    Args:
        request: Incoming request, used to reach ``app.state.job_store``.
        username: Identity from the verified JWT.

    Raises:
        DomainError: 500 when the store is unavailable; 401
            ``auth.account_not_found`` when the account row is gone, so the
            web app can sign the browser out instead of retrying forever.
    """
    job_store = getattr(getattr(getattr(request, "app", None), "state", None), "job_store", None)
    engine = getattr(job_store, "engine", None)
    if engine is None:
        raise DomainError("auth.not_configured", status=500)
    with Session(engine) as session:
        if not _account_exists(session, username):
            raise DomainError("auth.account_not_found", status=401)


def _authenticate_api_token(request: Request, token: str) -> AuthenticatedUser:
    """Resolve the user behind a personal access token.

    Looks the token's SHA-256 hash up in ``api_tokens`` via the app-level job
    store, stamps ``last_used_at``, and derives the role from the admin
    username allowlist. Group-based admin doesn't apply to PATs — they carry a
    bare user identity, not the SSO ``groups`` claim a session JWT does.

    Args:
        request: Incoming request, used to reach ``app.state.job_store``.
        token: Raw ``skyd_`` token from the Authorization header.

    Returns:
        The authenticated user behind the token.

    Raises:
        DomainError: 500 when the store is unavailable; 401 when the token
            matches no active row, is older than :data:`API_TOKEN_MAX_AGE`, or
            belongs to an account that no longer exists.
    """
    job_store = getattr(request.app.state, "job_store", None)
    if job_store is None:
        raise DomainError("auth.not_configured", status=500)
    token_hash = hash_api_token(token)
    now = datetime.now(UTC)
    with Session(job_store.engine) as session:
        row = session.query(ApiTokenModel).filter(ApiTokenModel.token_hash == token_hash).one_or_none()
        if row is None or api_token_expired(row.created_at, now):
            raise DomainError("auth.invalid_token", status=401)
        username = str(row.username)
        # Token creation and account deletion lock the users row, so no new
        # token outlives a deletion; this refuses a token row left by any older
        # path. No created_at comparison: OAuth accounts got their first row only
        # at a sign-in after 2026-08-28, later than PATs they already held.
        if not _account_exists(session, username):
            raise DomainError("auth.invalid_token", status=401)
        row.last_used_at = now
        session.commit()
    role = "admin" if username in settings.admin_usernames_set else "user"
    return AuthenticatedUser(username=username, role=role, groups=())


def _authenticate_desktop_token(request: Request, token: str) -> AuthenticatedUser:
    """Resolve a Mac link token on a route of the desktop sync allowlist.

    The Mac app reads and writes its web account through hosted (SKY-223).
    The link must carry the ``sync`` scope. Credential, security, billing
    payment and admin routes stay closed to it.

    Args:
        request: Incoming request; its matched route template selects the policy.
        token: Raw ``skyl_`` token from the Authorization header.

    Returns:
        The link's account, as a non-admin user.

    Raises:
        DomainError: 401 ``auth.desktop_token_not_allowed`` off the allowlist,
            or the desktop token errors of :func:`resolve_desktop_principal`.
    """
    route_path = getattr(request.scope.get("route"), "path", None)
    if route_path is None or not desktop_sync_route_allowed(route_path):
        raise DomainError("auth.desktop_token_not_allowed", status=401)
    principal = resolve_desktop_principal(request, token, "sync")
    request.state.auth_channel = "desktop"
    return AuthenticatedUser(username=principal.username, role="user", groups=(), desktop=True)


def get_authenticated_user(request: Request, authorization: str | None = Header(default=None)) -> AuthenticatedUser:
    """Verify the bearer credential and return the authenticated user.

    Accepts two credential types on ``Authorization: Bearer``: a long-lived
    personal access token (``skyd_…``, resolved against the ``api_tokens``
    table) for programmatic API access, or the short-lived HS256 session JWT
    the frontend mints for the web app. The PAT prefix selects the path.
    A desktop link token (``skyl_…``) works only on the routes of the desktop
    sync allowlist, with the ``sync`` scope; elsewhere it is refused.

    A PAT call is stamped ``request.state.auth_channel = "api_token"`` so
    downstream telemetry can tell a production integration from the web app
    without trusting anything the caller sends in the body.

    Args:
        request: Incoming request (used to reach the store for PAT lookup and
            to carry the auth channel).
        authorization: HTTP Authorization header.

    Returns:
        The authenticated backend user.

    Raises:
        DomainError: When authentication is missing or invalid, or 401
            ``auth.desktop_token_not_allowed`` for a desktop link token on a
            route outside the desktop sync allowlist.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise DomainError("auth.missing_token", status=401)
    token = authorization.removeprefix("Bearer ").strip()
    if token.startswith(DESKTOP_TOKEN_PREFIX):
        # The desktop check already enforces the MAU cap.
        return _authenticate_desktop_token(request, token)
    if is_api_token(token):
        user = _authenticate_api_token(request, token)
        request.state.auth_channel = "api_token"
    else:
        if settings.backend_auth_secret is None:
            raise DomainError("auth.not_configured", status=500)
        payload = _verify_hs256(token, settings.backend_auth_secret.get_secret_value())
        username = str(payload.get("name") or payload.get("email") or payload.get("sub") or "").strip().lower()
        if not username:
            raise DomainError("auth.invalid_token", status=401)
        _require_account(request, username)
        role = str(payload.get("role") or "user").strip().lower()
        user = AuthenticatedUser(
            username=username,
            role=role,
            groups=_normalise_groups(payload.get("groups")),
        )
    app_state = getattr(getattr(request, "app", None), "state", None)
    enforce_monthly_active_user_limit(
        getattr(app_state, "job_store", None),
        user.username,
        exempt=is_admin(user),
    )
    return user


def is_admin(user: AuthenticatedUser) -> bool:
    """Return ``True`` when ``user`` matches the configured admin allowlist.

    Non-raising mirror of :func:`require_admin_user` — used by routes that
    grant admins extra privilege without forbidding the non-admin caller.

    Args:
        user: Authenticated user resolved from a signed backend token.

    Returns:
        Whether the user is recognised as a backend admin.
    """
    # The session JWT is HS256-signed by the trusted frontend, which already
    # resolves admin status from AUTH_ADMINS/AUTH_ADMIN_GROUPS. Honour that
    # claim so the admin UI (tab visibility) and the API can't drift apart and
    # leave a frontend-admin staring at a 403. admin_usernames/admin_groups
    # below remain a break-glass override independent of the frontend.
    if user.desktop:
        return False
    if user.role == "admin":
        return True
    if user.username in settings.admin_usernames_set:
        return True
    return bool(settings.admin_groups_set and settings.admin_groups_set.intersection(user.groups))


def require_admin_user(user: AuthenticatedUser) -> AuthenticatedUser:
    """Return ``user`` only when backend admin authorization passes.

    Args:
        user: Authenticated user resolved from a signed backend token.

    Returns:
        The same user when authorized.

    Raises:
        DomainError: When the user is not a backend admin.
    """
    if is_admin(user):
        return user
    raise DomainError("admin.forbidden", status=403)
