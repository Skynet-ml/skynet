"""Hosted side of the Mac account link (SKY-124 PR 1). [INTERNAL]

The flow is OAuth 2.0 authorization code with PKCE for a native app
(RFC 8252). A signed-in browser user approves a link on the hosted consent
page, which calls ``/desktop/link/authorize`` with the session JWT. The Mac
exchanges the single-use code for a ``skyl_`` token pair, refreshes it with
rotation and reuse detection, and can revoke it. The account owner lists and
removes linked Macs under ``/settings/desktop-links``.

Codes and tokens are stored only as SHA-256 hashes.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, NamedTuple

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ...billing.service import StripeBillingService
from ...notifications.email_render import EmailContent, absolute_url, render_email_html
from ...storage.models import DesktopLinkCodeModel, DesktopLinkModel, DesktopTokenModel, UserModel
from ..auth import AuthenticatedUser, get_authenticated_user, hash_api_token
from ..desktop_auth import (
    ACCESS_TOKEN_TTL,
    DESKTOP_SCOPES,
    LINK_ABSOLUTE_TTL,
    REFRESH_TOKEN_IDLE_TTL,
    TOKEN_KIND_ACCESS,
    TOKEN_KIND_REFRESH,
    DesktopPrincipal,
    as_utc,
    generate_desktop_token,
    get_desktop_principal,
    has_live_refresh_token,
    link_is_live,
)
from ..email_sender import email_configured, send_email
from ..errors import DomainError
from ..rate_limit import RateLimiter, shared_redis_client

logger = logging.getLogger(__name__)
# Width of ``users.image_url``; a longer picture URL is dropped, not rejected.
_IMAGE_URL_MAX_LENGTH = 2048

DESKTOP_CLIENT_ID = "skynet-desktop"
LINK_CODE_TTL = timedelta(seconds=60)
# A retried refresh inside this window (lost response) gets a fresh pair
# instead of tripping reuse detection. The pair it replaces is superseded, so a
# later use of that pair's refresh token is still a theft signal.
REFRESH_REUSE_GRACE = timedelta(seconds=30)
MAX_LIVE_LINKS = 10
# Matches LOW_BALANCE_USD in frontend/src/features/billing/lib/wallet.ts.
LOW_BALANCE_CENTS = 50

# Mac app builds before SKY-228 still send the loopback form; newer builds use the app URL.
_LOOPBACK_REDIRECT = re.compile(r"^http://(?:127\.0\.0\.1|\[::1\]):([1-9][0-9]{3,4})/local/hosted-link/callback$")
APP_REDIRECT = "skynet://link/callback"

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]
IdentityDep = Annotated[DesktopPrincipal, Depends(get_desktop_principal("identity"))]
BalanceDep = Annotated[DesktopPrincipal, Depends(get_desktop_principal("balance:read"))]


# Consent-page request; sent with the browser session JWT.
class LinkAuthorizeRequest(BaseModel):
    redirect_uri: str = Field(max_length=255, description="Exact loopback callback of the Mac app.")
    code_challenge: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$", description="PKCE S256 challenge.")
    code_challenge_method: Literal["S256"] = Field(description="Only S256 is accepted.")
    scope: str = Field(max_length=255, description="Space-separated desktop scopes.")
    device_name: str = Field(min_length=1, max_length=128, description="Mac name shown in Linked Macs.")
    image: str = Field(default="", description="Profile picture URL of the browser session, if any.")


class LinkAuthorizeResponse(BaseModel):
    code: str = Field(description="Single-use link code.")
    expires_in: int = Field(description="Code lifetime in seconds.")


class CodeGrantRequest(BaseModel):
    grant_type: Literal["authorization_code"]
    code: str = Field(max_length=128)
    code_verifier: str = Field(min_length=43, max_length=128, pattern=r"^[A-Za-z0-9._~-]+$")
    redirect_uri: str = Field(max_length=255)
    client_id: str = Field(max_length=64)


class RefreshGrantRequest(BaseModel):
    grant_type: Literal["refresh_token"]
    refresh_token: str = Field(max_length=128)


class LinkAccount(BaseModel):
    email: str
    name: str


class LinkTokenResponse(BaseModel):
    token_type: Literal["Bearer"] = "Bearer"
    access_token: str
    expires_in: int
    refresh_token: str
    refresh_expires_in: int
    scope: str
    link_id: str
    account: LinkAccount


class LinkRevokeRequest(BaseModel):
    token: str = Field(max_length=128)


class DesktopWhoami(BaseModel):
    email: str
    name: str
    image: str | None = None
    link_id: str
    scopes: list[str]
    device_name: str


class DesktopBalance(BaseModel):
    available_cents: int
    currency: Literal["usd"] = "usd"
    low_balance: bool


# One row of the linked Macs list in Settings > Security.
class DesktopLinkInfo(BaseModel):
    link_id: str
    device_name: str
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None


def _canonical_scopes(raw: str) -> str:
    """Validate a space-separated scope string and return it in canonical order.

    Args:
        raw: Scope string from the request.

    Returns:
        The scopes in :data:`DESKTOP_SCOPES` order, space-separated.

    Raises:
        DomainError: 400 ``desktop.invalid_scope`` for an empty, unknown or
            badly spaced scope.
    """
    requested = raw.split(" ")
    if not raw or any(scope not in DESKTOP_SCOPES for scope in requested):
        raise DomainError("desktop.invalid_scope", status=400)
    return " ".join(scope for scope in DESKTOP_SCOPES if scope in requested)


def _check_redirect(redirect_uri: str) -> None:
    """Accept only the Mac app's callback: its app URL or the RFC 8252 loopback form.

    Args:
        redirect_uri: Redirect URI from the request.

    Raises:
        DomainError: 400 ``desktop.invalid_redirect`` for any other value.
    """
    if redirect_uri == APP_REDIRECT:
        return
    match = _LOOPBACK_REDIRECT.match(redirect_uri)
    if match is None or not 1024 <= int(match.group(1)) <= 65535:
        raise DomainError("desktop.invalid_redirect", status=400)


def _require_session(request: Request) -> None:
    """Refuse a personal access token on a browser-session route.

    Args:
        request: Incoming request, after ``get_authenticated_user`` ran.

    Raises:
        DomainError: 403 ``desktop.session_required`` for a ``skyd_`` caller.
    """
    if getattr(request.state, "auth_channel", None) == "api_token":
        raise DomainError("desktop.session_required", status=403)


def _s256(verifier: str) -> str:
    """Return the PKCE S256 challenge for ``verifier``."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _client_address(request: Request) -> str:
    """Return the caller address for the code-exchange rate limit.

    The last ``X-Forwarded-For`` hop is the one the hosting proxy appended, so
    a client cannot pick its own bucket by sending the header.

    Args:
        request: Incoming request.

    Returns:
        The address, or ``"unknown"`` when none is available.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
    if hops:
        return hops[-1]
    return request.client.host if request.client else "unknown"


def _limit(key: str, *, limit: int, window_seconds: int) -> None:
    """Apply the shared Redis rate limiter to ``key``.

    Args:
        key: Caller-scoped limiter key.
        limit: Maximum hits in the window.
        window_seconds: Window length in seconds.

    Raises:
        DomainError: 429 ``rate_limit.exceeded`` over the limit.
    """
    RateLimiter(shared_redis_client()).enforce(key, limit=limit, window_seconds=window_seconds)


def _live_link_count(session: Session, username: str, now: datetime) -> int:
    """Count the user's live links: not revoked, not expired, not idle-expired.

    Args:
        session: Open ORM session.
        username: Account identity.
        now: Current UTC time.

    Returns:
        Number of live links.
    """
    return int(
        session.scalar(
            select(func.count())
            .select_from(DesktopLinkModel)
            .where(
                DesktopLinkModel.username == username,
                DesktopLinkModel.revoked_at.is_(None),
                DesktopLinkModel.absolute_expires_at > now,
                has_live_refresh_token(DesktopLinkModel.id, now),
            )
        )
        or 0
    )


def _revoke_link(session: Session, link_id: str, now: datetime) -> None:
    """Mark a link revoked so every token of it fails on the next call.

    Token rows stay, so a later call with one of them reports
    ``desktop.link_revoked`` instead of an unknown token.

    Args:
        session: Open ORM session; the caller commits.
        link_id: Link to revoke.
        now: Current UTC time.
    """
    session.execute(
        update(DesktopLinkModel)
        .where(DesktopLinkModel.id == link_id, DesktopLinkModel.revoked_at.is_(None))
        .values(revoked_at=now)
    )


def _revoke_for_reuse(session: Session, link: DesktopLinkModel, now: datetime) -> None:
    """Revoke ``link`` for refresh-token reuse, email the owner, and refuse the call.

    Args:
        session: Open ORM session; committed here.
        link: Link whose refresh token was reused.
        now: Current UTC time.

    Raises:
        DomainError: Always, 401 ``desktop.link_revoked``.
    """
    _revoke_link(session, link.id, now)
    session.commit()
    # Sent inline: background tasks do not run when the route raises.
    _send_notice(*_link_reuse_notice(link))
    raise DomainError("desktop.link_revoked", status=401)


# Settings > Security lists the linked Macs and removes them.
_SECURITY_SETTINGS_PATH = "/?settings=security"


class LinkNotice(NamedTuple):
    """A link security notice, unpackable as ``to, subject, body, html``."""

    to: str
    subject: str
    body: str
    html: str


def _send_notice(to: str, subject: str, body: str, html_body: str) -> None:
    """Email the account owner a link security notice, best effort.

    A notice must never fail or change the token call that caused it.

    Args:
        to: Account email.
        subject: Subject line.
        body: Plain-text body.
        html_body: HTML alternative on the shared email layout.
    """
    if not email_configured():
        return
    try:
        send_email(to, subject, body, html_body=html_body)
    except Exception:  # A failed notice must not fail the token call.
        logger.warning("desktop link notice was not sent", exc_info=True)


def _notice_html(subject: str, detail: str, tone: Literal["warn", "bad"], button: tuple[str, str] | None) -> str:
    """Render a link notice on the shared email layout.

    Args:
        subject: Subject line, also the headline.
        detail: The notice text, as in the plain-text body.
        tone: Status color of the "Security" label.
        button: Optional ``(label, url)`` call to action.

    Returns:
        The HTML alternative of the notice.
    """
    return render_email_html(
        EmailContent(
            locale="en",
            subject=subject,
            headline=subject,
            status=("Security", tone),
            detail=detail,
            button=button,
        )
    )


def _link_created_notice(link: DesktopLinkModel) -> LinkNotice:
    """Build the notice for a new link.

    Args:
        link: The new link.

    Returns:
        The ``(to, subject, body, html)`` of the notice.
    """
    linked = f'The Mac "{link.device_name}" is now linked to your Skynet account.'
    action = "If you did not link this Mac, open Settings > Security on the Skynet website and remove it."
    subject = "A Mac was linked to your Skynet account"
    button = ("Open Settings", absolute_url(_SECURITY_SETTINGS_PATH))
    html_body = _notice_html(subject, f"{linked} {action}", "warn", button)
    return LinkNotice(link.username, subject, f"{linked}\n\n{action}", html_body)


def _link_reuse_notice(link: DesktopLinkModel) -> LinkNotice:
    """Build the notice for a link revoked for refresh-token reuse.

    Args:
        link: The revoked link.

    Returns:
        The ``(to, subject, body, html)`` of the notice.
    """
    removed = (
        f'Skynet removed the link to the Mac "{link.device_name}". '
        "An old sign-in token of this Mac was used again. Someone can have a copy of it."
    )
    action = "To continue, link the Mac again from the Skynet app."
    subject = "Skynet removed a linked Mac"
    # No button: the Mac links again from the Mac app, not from a web page.
    html_body = _notice_html(subject, f"{removed} {action}", "bad", None)
    return LinkNotice(link.username, subject, f"{removed}\n\n{action}", html_body)


def _account(session: Session, username: str) -> LinkAccount:
    """Return the account email and display name for ``username``.

    Args:
        session: Open ORM session.
        username: Account identity (lowercased email).

    Returns:
        The account, with the email as name when no user row exists.
    """
    user = session.get(UserModel, username)
    return LinkAccount(email=username, name=user.name if user is not None and user.name else username)


def _mint_pair(session: Session, link: DesktopLinkModel, parent_hash: str | None, now: datetime) -> LinkTokenResponse:
    """Create an access and a refresh token for ``link``.

    Args:
        session: Open ORM session; the caller commits.
        link: Live link that owns the tokens.
        parent_hash: Hash of the refresh token this pair replaces, if any.
        now: Current UTC time.

    Returns:
        The token response with the plaintext tokens.
    """
    access = generate_desktop_token(TOKEN_KIND_ACCESS)
    refresh = generate_desktop_token(TOKEN_KIND_REFRESH)
    for token, kind, ttl in (
        (access, TOKEN_KIND_ACCESS, ACCESS_TOKEN_TTL),
        (refresh, TOKEN_KIND_REFRESH, REFRESH_TOKEN_IDLE_TTL),
    ):
        session.add(
            DesktopTokenModel(
                token_hash=hash_api_token(token),
                link_id=link.id,
                kind=kind,
                parent_hash=parent_hash,
                created_at=now,
                expires_at=now + ttl,
            )
        )
    return LinkTokenResponse(
        access_token=access,
        expires_in=int(ACCESS_TOKEN_TTL.total_seconds()),
        refresh_token=refresh,
        refresh_expires_in=int(REFRESH_TOKEN_IDLE_TTL.total_seconds()),
        scope=link.scopes,
        link_id=link.id,
        account=_account(session, link.username),
    )


def _link_info(link: DesktopLinkModel) -> DesktopLinkInfo:
    """Convert a link row to its Settings representation."""
    return DesktopLinkInfo(
        link_id=link.id,
        device_name=link.device_name,
        scopes=link.scopes.split(),
        created_at=as_utc(link.created_at),
        last_used_at=as_utc(link.last_used_at) if link.last_used_at is not None else None,
    )


def create_desktop_link_router(*, job_store) -> APIRouter:
    """Build the hosted desktop link router.

    Args:
        job_store: Job-store instance whose ORM engine backs the routes.

    Returns:
        A FastAPI ``APIRouter`` with the link, token, revoke, whoami,
        balance and Linked Macs routes.
    """
    router = APIRouter()
    billing = StripeBillingService(engine=job_store.engine)

    def exchange_code(body: CodeGrantRequest, request: Request, tasks: BackgroundTasks) -> LinkTokenResponse:
        """Exchange a link code and PKCE verifier for a new link and token pair.

        Args:
            body: Code grant.
            request: Incoming request, for the per-address limit.
            tasks: Response background tasks; the link notice goes there.

        Returns:
            The new token pair.

        Raises:
            DomainError: 400 ``desktop.invalid_grant``, 401
                ``auth.invalid_token`` when the account is gone, 409
                ``desktop.too_many_links``, or 429.
        """
        _limit(f"desktop-link-code:{_client_address(request)}", limit=30, window_seconds=60)
        now = datetime.now(UTC)
        with Session(job_store.engine) as session:
            code = session.get(DesktopLinkCodeModel, hash_api_token(body.code))
            if code is None:
                raise DomainError("desktop.invalid_grant", status=400)
            # User row, then code: the account deletion takes the user lock
            # first, so the opposite order deadlocks. After a deletion this
            # finds no user and the re-read finds no code (R782E-1).
            owner = session.get(UserModel, code.username, with_for_update=True)
            code = session.get(
                DesktopLinkCodeModel, hash_api_token(body.code), with_for_update=True, populate_existing=True
            )
            if code is None:
                raise DomainError("desktop.invalid_grant", status=400)
            if code.used_at is not None:
                # A replayed code means someone else holds it: end what it made.
                if code.link_id is not None:
                    _revoke_link(session, code.link_id, now)
                    session.commit()
                raise DomainError("desktop.invalid_grant", status=400)
            matches = (
                body.client_id == DESKTOP_CLIENT_ID
                and body.redirect_uri == code.redirect_uri
                and secrets.compare_digest(_s256(body.code_verifier), code.code_challenge)
            )
            # Any wrong attempt spends the code, so a verifier cannot be guessed.
            code.used_at = now
            if not matches or as_utc(code.expires_at) <= now:
                session.commit()
                raise DomainError("desktop.invalid_grant", status=400)
            # An orphan code, such as one left by an older release, never links.
            if owner is None:
                session.commit()
                raise DomainError("auth.invalid_token", status=401)
            if _live_link_count(session, code.username, now) >= MAX_LIVE_LINKS:
                session.commit()
                raise DomainError("desktop.too_many_links", status=409, limit=MAX_LIVE_LINKS)
            link = DesktopLinkModel(
                id=f"dl_{uuid.uuid4()}",
                username=code.username,
                device_name=code.device_name,
                scopes=code.scopes,
                created_at=now,
                absolute_expires_at=now + LINK_ABSOLUTE_TTL,
            )
            session.add(link)
            code.link_id = link.id
            response = _mint_pair(session, link, None, now)
            session.commit()
            tasks.add_task(_send_notice, *_link_created_notice(link))
        return response

    def refresh_pair(body: RefreshGrantRequest) -> LinkTokenResponse:
        """Rotate a refresh token, with reuse detection.

        Args:
            body: Refresh grant.

        Returns:
            The new token pair.

        Raises:
            DomainError: 400 ``desktop.invalid_grant`` for an unknown, expired
                or non-refresh token; 401 ``desktop.link_revoked`` for a dead
                link, a superseded refresh token, or a reuse outside the grace
                window; 429.
        """
        now = datetime.now(UTC)
        with Session(job_store.engine) as session:
            token_hash = hash_api_token(body.refresh_token)
            row = session.get(DesktopTokenModel, token_hash)
            if row is None or row.kind != TOKEN_KIND_REFRESH:
                raise DomainError("desktop.invalid_grant", status=400)
            # Lock the link before the token, so every refresh of one link runs
            # in turn. Locking only the presented token let a replayed older
            # token miss a concurrent rotation of its successor (READ COMMITTED)
            # and fork the link into two live pairs.
            link = session.get(DesktopLinkModel, row.link_id, with_for_update=True)
            row = session.get(DesktopTokenModel, token_hash, with_for_update=True, populate_existing=True)
            # The idle rule of link_is_live is left out on purpose: the
            # presented token decides it below, and an idle-expired token keeps
            # its 400 desktop.invalid_grant answer.
            if link is None or link.revoked_at is not None or as_utc(link.absolute_expires_at) <= now:
                raise DomainError("desktop.link_revoked", status=401)
            _limit(f"desktop-link-refresh:{link.id}", limit=60, window_seconds=3600)
            if row.superseded_at is not None:
                # A grace retry replaced this pair, so the honest Mac never holds it.
                _revoke_for_reuse(session, link, now)
            if row.used_at is not None:
                successor_used = session.scalar(
                    select(func.count())
                    .select_from(DesktopTokenModel)
                    .where(
                        DesktopTokenModel.parent_hash == token_hash,
                        DesktopTokenModel.kind == TOKEN_KIND_REFRESH,
                        DesktopTokenModel.used_at.is_not(None),
                    )
                )
                if successor_used or now - as_utc(row.used_at) > REFRESH_REUSE_GRACE:
                    _revoke_for_reuse(session, link, now)
                # The caller cannot be told from a thief here. Kill the access
                # token of the replaced pair, and keep its refresh row as a
                # superseded marker: whoever holds the other pair is caught
                # at its next refresh.
                children = session.scalars(
                    select(DesktopTokenModel).where(
                        DesktopTokenModel.parent_hash == token_hash, DesktopTokenModel.superseded_at.is_(None)
                    )
                ).all()
                for child in children:
                    if child.kind == TOKEN_KIND_REFRESH:
                        child.superseded_at = now
                    else:
                        session.delete(child)
            elif as_utc(row.expires_at) <= now:
                raise DomainError("desktop.invalid_grant", status=400)
            else:
                row.used_at = now
            response = _mint_pair(session, link, token_hash, now)
            session.commit()
        return response

    @router.post(
        "/desktop/link/authorize",
        response_model=LinkAuthorizeResponse,
        summary="Approve a Mac link and issue a single-use link code",
    )
    def authorize_link(
        body: LinkAuthorizeRequest, request: Request, user: AuthenticatedUserDep
    ) -> LinkAuthorizeResponse:
        """Issue a 60 s single-use link code for the signed-in browser user.

        Args:
            body: Consent request from the hosted consent page.
            request: Incoming request; tells a session JWT from a PAT.
            user: Signed-in user who approves the link.

        Returns:
            The link code and its lifetime.

        Raises:
            DomainError: 403 ``desktop.session_required`` for a PAT caller,
                400 ``desktop.invalid_redirect`` or ``desktop.invalid_scope``,
                401 ``auth.invalid_token`` when the account is gone,
                409 ``desktop.too_many_links``, or 429.
        """
        _require_session(request)
        _limit(f"desktop-link-authorize:{user.username}", limit=10, window_seconds=3600)
        _check_redirect(body.redirect_uri)
        scopes = _canonical_scopes(body.scope)
        device_name = body.device_name.strip()
        if not device_name:
            raise DomainError("desktop.invalid_device_name", status=400)
        now = datetime.now(UTC)
        code = secrets.token_urlsafe(32)
        with Session(job_store.engine) as session:
            # A session JWT outlives an account deletion until it expires. The
            # lock waits for an open deletion, so no code outlives it (R782E-1).
            account = session.get(UserModel, user.username, with_for_update=True)
            if account is None:
                raise DomainError("auth.invalid_token", status=401)
            # An account whose last provider sign-in predates the picture column
            # has no image_url, so the Mac app showed initials (SKY-249). Same
            # rule as /auth/oauth/provision; anything else is ignored, not rejected.
            if body.image.startswith("https://") and len(body.image) <= _IMAGE_URL_MAX_LENGTH:
                account.image_url = body.image
            if _live_link_count(session, user.username, now) >= MAX_LIVE_LINKS:
                raise DomainError("desktop.too_many_links", status=409, limit=MAX_LIVE_LINKS)
            session.add(
                DesktopLinkCodeModel(
                    code_hash=hash_api_token(code),
                    username=user.username,
                    code_challenge=body.code_challenge,
                    redirect_uri=body.redirect_uri,
                    scopes=scopes,
                    device_name=device_name,
                    created_at=now,
                    expires_at=now + LINK_CODE_TTL,
                )
            )
            session.commit()
        return LinkAuthorizeResponse(code=code, expires_in=int(LINK_CODE_TTL.total_seconds()))

    @router.post(
        "/desktop/link/token",
        response_model=LinkTokenResponse,
        summary="Exchange a link code, or rotate a refresh token",
    )
    def link_token(
        body: Annotated[CodeGrantRequest | RefreshGrantRequest, Body(discriminator="grant_type")],
        request: Request,
        tasks: BackgroundTasks,
    ) -> LinkTokenResponse:
        """Run the ``authorization_code`` or ``refresh_token`` grant.

        Args:
            body: The grant.
            request: Incoming request.
            tasks: Response background tasks.

        Returns:
            A new token pair.
        """
        if isinstance(body, CodeGrantRequest):
            return exchange_code(body, request, tasks)
        return refresh_pair(body)

    @router.post("/desktop/link/revoke", summary="Revoke the link that owns a desktop token")
    def revoke_link(body: LinkRevokeRequest) -> dict[str, bool]:
        """Revoke the link of an access or refresh token; always 200 (RFC 7009).

        Args:
            body: The token to revoke.

        Returns:
            ``{"ok": true}`` whether or not the token was known.
        """
        now = datetime.now(UTC)
        with Session(job_store.engine) as session:
            row = session.get(DesktopTokenModel, hash_api_token(body.token))
            if row is not None:
                _revoke_link(session, row.link_id, now)
                session.commit()
        return {"ok": True}

    @router.get("/desktop/v1/whoami", response_model=DesktopWhoami, summary="Linked account of this Mac")
    def whoami(principal: IdentityDep) -> DesktopWhoami:
        """Return the account and link behind the desktop token.

        Args:
            principal: Desktop principal with the ``identity`` scope.

        Returns:
            The account email, name, profile picture, link id, scopes and Mac name.
        """
        with Session(job_store.engine) as session:
            account = _account(session, principal.username)
            user = session.get(UserModel, principal.username)
            image = user.image_url if user is not None else None
        return DesktopWhoami(
            email=account.email,
            name=account.name,
            image=image,
            link_id=principal.link_id,
            scopes=[scope for scope in DESKTOP_SCOPES if scope in principal.scopes],
            device_name=principal.device_name,
        )

    @router.get("/desktop/v1/balance", response_model=DesktopBalance, summary="Spendable hosted balance")
    def balance(principal: BalanceDep) -> DesktopBalance:
        """Return the hosted spendable balance (free grant plus paid, less holds).

        Args:
            principal: Desktop principal with the ``balance:read`` scope.

        Returns:
            Spendable cents and the low-balance flag.
        """
        available = billing.spendable_cents(principal.username)
        return DesktopBalance(available_cents=available, low_balance=available < LOW_BALANCE_CENTS)

    @router.get(
        "/settings/desktop-links",
        response_model=list[DesktopLinkInfo],
        summary="List the caller's linked Macs",
    )
    def list_links(request: Request, user: AuthenticatedUserDep) -> list[DesktopLinkInfo]:
        """List the caller's live links, newest first.

        Args:
            request: Incoming request; tells a session JWT from a PAT.
            user: Authenticated caller.

        Returns:
            One row per live link.

        Raises:
            DomainError: 403 ``desktop.session_required`` for a PAT caller.
        """
        _require_session(request)
        now = datetime.now(UTC)
        with Session(job_store.engine) as session:
            links = session.scalars(
                select(DesktopLinkModel)
                .where(
                    DesktopLinkModel.username == user.username,
                    DesktopLinkModel.revoked_at.is_(None),
                    DesktopLinkModel.absolute_expires_at > now,
                    has_live_refresh_token(DesktopLinkModel.id, now),
                )
                .order_by(DesktopLinkModel.created_at.desc())
            ).all()
            return [_link_info(link) for link in links]

    @router.delete(
        "/settings/desktop-links/{link_id}",
        status_code=204,
        summary="Remove a linked Mac",
    )
    def remove_link(link_id: str, request: Request, user: AuthenticatedUserDep) -> None:
        """Revoke one of the caller's links and all its tokens at once.

        Args:
            link_id: Link to remove.
            request: Incoming request; tells a session JWT from a PAT.
            user: Authenticated caller; must own the link.

        Raises:
            DomainError: 403 ``desktop.session_required`` for a PAT caller;
                404 ``desktop.link_not_found`` when the caller has no such
                live link.
        """
        _require_session(request)
        now = datetime.now(UTC)
        with Session(job_store.engine) as session:
            link = session.get(DesktopLinkModel, link_id)
            if link is None or link.username != user.username or not link_is_live(session, link, now):
                raise DomainError("desktop.link_not_found", status=404)
            _revoke_link(session, link.id, now)
            session.commit()

    return router
