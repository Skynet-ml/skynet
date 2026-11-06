"""Desktop link tokens: the Mac app's scoped credential for hosted (SKY-124).

A linked Mac holds an opaque ``skyl_at_`` access token and a rotating
``skyl_rt_`` refresh token. Hosted stores only their SHA-256 hashes, like the
``skyd_`` personal access tokens, so a revoke takes effect on the next call.

Routes that depend on :func:`get_desktop_principal` accept a desktop token
and each names the one scope it needs. The Mac app also uses its web account
through the normal session routes (SKY-223):
:func:`core.api.auth.get_authenticated_user` accepts a ``sync`` access token
on the routes that :func:`desktop_sync_route_allowed` opens, and refuses it
everywhere else.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Header, Request
from sqlalchemy import ColumnElement, exists, select, update
from sqlalchemy.orm import Session

from ..storage.models import DesktopLinkModel, DesktopTokenModel
from .errors import DomainError
from .monthly_active_users import enforce_monthly_active_user_limit
from .token_hashing import DESKTOP_TOKEN_PREFIX, hash_api_token

ACCESS_TOKEN_PREFIX = f"{DESKTOP_TOKEN_PREFIX}at_"
REFRESH_TOKEN_PREFIX = f"{DESKTOP_TOKEN_PREFIX}rt_"
TOKEN_KIND_ACCESS = "access"
TOKEN_KIND_REFRESH = "refresh"
_TOKEN_SECRET_BYTES = 32

DESKTOP_SCOPES: tuple[str, ...] = ("identity", "balance:read", "sync", "models", "connectors", "web")

ACCESS_TOKEN_TTL = timedelta(hours=1)
REFRESH_TOKEN_IDLE_TTL = timedelta(days=30)
LINK_ABSOLUTE_TTL = timedelta(days=365)

# Session-route templates a ``sync`` token may call: the account's own web data.
# A new top-level router stays closed until someone adds it here.
_SYNC_ROUTE_PREFIXES: tuple[str, ...] = (
    "/account",
    "/agent",
    "/analytics",
    "/billing",
    "/blackbox",
    "/connectors",
    "/continuous",
    "/dashboard",
    "/datasets",
    "/execution-budgets",
    "/execution-runtimes",
    "/folders",
    "/models",
    "/notifications",
    "/optimizations",
    "/optimize",
    "/optimizers",
    "/serve",
    "/settings/user-preferences",
    "/share",
    "/shared-with-me",
    "/tagging-sessions",
    "/transcribe",
    "/usage",
    "/users/search",
    "/wizard",
    "/workflows",
)
# A sync token has the rights of a web session on these routes, except the
# ones that return or write a stored credential, move money, or export or
# delete the account. A stolen Mac token must not reach them.
_SYNC_ROUTE_EXCLUDED_PREFIXES: tuple[str, ...] = (
    "/account/delete",
    "/account/export",
    "/agent/protected-settings",
    "/billing/byok/keys",
    "/billing/byok/openrouter",
    "/billing/checkout",
    "/billing/payment-methods",
    "/billing/portal",
    "/billing/subscription",
    "/billing/webhook",
    "/connectors/huggingface/oauth",
    "/connectors/huggingface/token",
    "/connectors/{provider}/credentials",
    "/connectors/{provider}/oauth",
    "/connectors/{provider}/picker",
    "/notifications/slack/oauth",
)


def _under(path: str, prefixes: tuple[str, ...]) -> bool:
    """Return whether ``path`` is one of ``prefixes`` or below one of them.

    Args:
        path: Route template, for example ``/optimizations/{job_id}``.
        prefixes: Path prefixes without a trailing slash.

    Returns:
        True on a whole-segment match.
    """
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in prefixes)


def desktop_sync_route_allowed(path: str) -> bool:
    """Return whether a ``sync`` desktop token may call this session route.

    Args:
        path: The matched route template, never the raw URL, so path
            parameters cannot change the decision.

    Returns:
        True when a web session may call the route and it is not excluded.
    """
    return _under(path, _SYNC_ROUTE_PREFIXES) and not _under(path, _SYNC_ROUTE_EXCLUDED_PREFIXES)


@dataclass(frozen=True)
class DesktopPrincipal:
    """The hosted account behind a valid desktop access token."""

    username: str
    link_id: str
    scopes: frozenset[str]
    device_name: str


def as_utc(value: datetime) -> datetime:
    """Return ``value`` as an aware UTC datetime.

    SQLite drops the zone of ``DateTime(timezone=True)`` columns, so the unit
    tests read naive values that are UTC by construction.

    Args:
        value: Datetime read from the database.

    Returns:
        The same instant, timezone-aware in UTC.
    """
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def generate_desktop_token(kind: str) -> str:
    """Return a fresh desktop token of ``kind`` with 256 bits of entropy.

    Args:
        kind: ``"access"`` or ``"refresh"``.

    Returns:
        The plaintext token, prefixed ``skyl_at_`` or ``skyl_rt_``.
    """
    prefix = ACCESS_TOKEN_PREFIX if kind == TOKEN_KIND_ACCESS else REFRESH_TOKEN_PREFIX
    return f"{prefix}{secrets.token_urlsafe(_TOKEN_SECRET_BYTES)}"


def has_live_refresh_token(link_id: Any, now: datetime) -> ColumnElement[bool]:
    """Build an EXISTS clause for a usable refresh token of a link.

    A link with no such token can never authenticate again, so it is gone
    after 30 days with no use (SKY-124 addendum 2.8).

    Args:
        link_id: Link id value, or the ``DesktopLinkModel.id`` column for a
            correlated subquery.
        now: Current UTC time.

    Returns:
        True when the link has a refresh token that is unexpired, unused and
        not superseded.
    """
    return exists().where(
        DesktopTokenModel.link_id == link_id,
        DesktopTokenModel.kind == TOKEN_KIND_REFRESH,
        DesktopTokenModel.used_at.is_(None),
        DesktopTokenModel.superseded_at.is_(None),
        DesktopTokenModel.expires_at > now,
    )


def link_is_live(session: Session, link: DesktopLinkModel, now: datetime) -> bool:
    """Return whether ``link`` is not revoked, not expired, and not idle-expired.

    Args:
        session: Open ORM session.
        link: Link row to check.
        now: Current UTC time.

    Returns:
        True when the link may still authenticate calls.
    """
    if link.revoked_at is not None or as_utc(link.absolute_expires_at) <= now:
        return False
    return bool(session.scalar(select(has_live_refresh_token(link.id, now))))


def resolve_desktop_principal(request: Request, token: str, scope: str) -> DesktopPrincipal:
    """Check a desktop access token and return its principal.

    Args:
        request: Incoming request, used to reach ``app.state.job_store``.
        token: Raw bearer token.
        scope: Scope the route needs.

    Returns:
        The principal behind the token.

    Raises:
        DomainError: 401 ``auth.invalid_token`` for an unknown or non-access
            token, 401 ``desktop.token_expired``, 401 ``desktop.link_revoked``,
            403 ``desktop.scope_missing``, or 500 when no store is configured.
    """
    job_store = getattr(request.app.state, "job_store", None)
    if job_store is None:
        raise DomainError("auth.not_configured", status=500)
    if not token.startswith(ACCESS_TOKEN_PREFIX):
        raise DomainError("auth.invalid_token", status=401)
    now = datetime.now(UTC)
    with Session(job_store.engine) as session:
        row = session.get(DesktopTokenModel, hash_api_token(token))
        if row is None or row.kind != TOKEN_KIND_ACCESS:
            raise DomainError("auth.invalid_token", status=401)
        link = session.get(DesktopLinkModel, row.link_id)
        if link is None or not link_is_live(session, link, now):
            raise DomainError("desktop.link_revoked", status=401)
        if as_utc(row.expires_at) <= now:
            raise DomainError("desktop.token_expired", status=401)
        scopes = frozenset(link.scopes.split())
        if scope not in scopes:
            raise DomainError("desktop.scope_missing", status=403, scope=scope)
        principal = DesktopPrincipal(
            username=link.username,
            link_id=link.id,
            scopes=scopes,
            device_name=link.device_name,
        )
        # A Core UPDATE, because account deletion can delete the link while
        # this call waits for its row lock; the ORM would raise StaleDataError
        # (R782F-2).
        recorded = session.execute(
            update(DesktopLinkModel)
            .where(DesktopLinkModel.id == link.id, DesktopLinkModel.revoked_at.is_(None))
            .values(last_used_at=now),
            execution_options={"synchronize_session": False},
        )
        if recorded.rowcount == 0:
            session.rollback()
            raise DomainError("desktop.link_revoked", status=401)
        session.commit()
    # Admin never applies to a desktop principal, so no MAU exemption either.
    enforce_monthly_active_user_limit(job_store, principal.username)
    return principal


def get_desktop_principal(scope: str) -> Callable[..., DesktopPrincipal]:
    """Build a FastAPI dependency that accepts only a desktop token with ``scope``.

    Args:
        scope: One of :data:`DESKTOP_SCOPES`.

    Returns:
        A dependency that returns the :class:`DesktopPrincipal` and sets
        ``request.state.auth_channel = "desktop"``.

    Raises:
        ValueError: When ``scope`` is not a desktop scope (a programming error).
    """
    if scope not in DESKTOP_SCOPES:
        raise ValueError(f"unknown desktop scope: {scope}")

    def dependency(request: Request, authorization: str | None = Header(default=None)) -> DesktopPrincipal:
        """Authenticate the desktop bearer token for this route.

        Args:
            request: Incoming request.
            authorization: HTTP Authorization header.

        Returns:
            The desktop principal.

        Raises:
            DomainError: 401 when the token is missing or invalid; 403 when it
                lacks the scope.
        """
        if not authorization or not authorization.startswith("Bearer "):
            raise DomainError("auth.missing_token", status=401)
        principal = resolve_desktop_principal(request, authorization.removeprefix("Bearer ").strip(), scope)
        request.state.auth_channel = "desktop"
        return principal

    return dependency
