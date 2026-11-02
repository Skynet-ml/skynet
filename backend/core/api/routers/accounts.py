"""Email/password account registration and sign-in. [INTERNAL]

Backs the "create an account in Skynet" path on the login screen. These are the
only unauthenticated routes in the API — they bootstrap a session before any
token exists. To keep them from being a public abuse vector they are gated by
the shared ``BACKEND_AUTH_SECRET``: only the Skynet frontend, which holds that
secret, may call them (server-side, from its NextAuth credentials provider).
OAuth (Google/GitHub) and SSO sign-ins authenticate at the provider; the
frontend then mirrors that identity here through ``/auth/oauth/provision`` so
it has the same ``users`` row (minus a password) and first-sign-in signal as a
local account.
"""

from __future__ import annotations

import hmac
import logging
import re
from datetime import UTC, datetime
from typing import Annotated, TypeGuard

from fastapi import APIRouter, Header
from pydantic import BaseModel, Field
from sqlalchemy import delete
from sqlalchemy.orm import Session

from ...brand import APP_NAME
from ...config import settings
from ...notifications.email_render import render_account_email
from ...storage.models import UserIdentityModel, UserModel
from ..email_sender import email_configured, send_email
from ..email_verification import (
    issue_verification_code,
    verification_code_on_cooldown,
    verify_verification_code,
)
from ..errors import DomainError
from ..login_throttle import LoginThrottle
from ..monthly_active_users import enforce_monthly_active_user_limit
from ..password_policy import validate_password
from ..password_reset import issue_reset_code, reset_code_on_cooldown, verify_reset_code
from ..passwords import hash_password, verify_password
from ..rate_limit import enforce_account_rate
from ..two_factor import enforce_second_factor
from .experience import new_account_intake_completed_at, normalize_experience_level

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_USE_CASES = frozenset({"classification", "extraction", "rag_agents", "generation", "other"})
_JOB_ROLES = frozenset({"engineer", "researcher", "pm", "other"})
# Width of ``users.image_url``; a longer picture URL is dropped, not rejected.
_IMAGE_URL_MAX_LENGTH = 2048
# Accounts store no language preference and these internal routes carry no
# request locale, so code emails keep the English they were always sent in.
ACCOUNT_EMAIL_LOCALE = "en"


def _send_account_code(email: str, kind: str, code: str) -> None:
    """Render an account code email and send it with its HTML and text parts.

    Args:
        email: Recipient address.
        kind: ``verification``, ``password_reset`` or ``sign_in``.
        code: The one-time code.

    Raises:
        OSError: When the SMTP relay is unreachable.
        RuntimeError: When the relay rejects the message.
    """
    subject, html, text = render_account_email(kind, ACCOUNT_EMAIL_LOCALE, code)
    send_email(email, subject, text, html_body=html)


# Credentials supplied when creating a Skynet-native account.
class RegisterRequest(BaseModel):
    email: str = Field(description="Account email; also the cross-app identity.")
    password: str = Field(description="Plaintext password; stored only as a scrypt hash.")
    name: str = Field(default="", description="Display name shown in the app header.")
    use_case: str = Field(default="", description="Primary optimization use-case from sign-up.")
    experience_level: str = Field(default="", description="Self-reported optimization experience.")
    job_role: str = Field(default="", description="Optional job function from sign-up.")


# Credentials supplied when signing in to a Skynet-native account. At most one
# of the three second-factor codes is honored (TOTP > email > recovery).
class LoginRequest(BaseModel):
    email: str = Field(description="Account email.")
    password: str = Field(description="Plaintext password to verify.")
    totp_code: str = Field(default="", description="Authenticator-app code when TOTP 2FA is enabled.")
    email_code: str = Field(default="", description="Emailed one-time code when email 2FA is enabled.")
    recovery_code: str = Field(default="", description="Single-use recovery code standing in for a lost factor.")


# Identity the frontend has already authenticated with an external provider
# (Google, GitHub, SSO) and now mirrors into ``users``.
class OAuthProvisionRequest(BaseModel):
    email: str = Field(description="Provider-asserted email; also the cross-app identity.")
    name: str = Field(default="", description="Display name from the provider profile.")
    other_emails: list[str] = Field(
        default_factory=list,
        max_length=50,
        description=(
            "Other provider-verified emails on the same identity; an existing account "
            "under one of them is reused when none exists under ``email``."
        ),
    )
    provider: str = Field(default="", max_length=32, description="Provider id, such as 'google' or 'github'.")
    provider_account_id: str = Field(
        default="",
        max_length=255,
        description="The provider's stable account id; a linked one decides the account before any email does.",
    )
    # No max_length: a picture is cosmetic, so an oversized one must not fail the sign-in.
    image: str = Field(default="", description="Profile picture URL from the provider profile.")


# Provider picture of a session that provision never saw: the web session
# started before ``users.image_url`` existed, or before the picture changed.
class OAuthPictureRequest(BaseModel):
    email: str = Field(description="The signed-in account.")
    image: str = Field(default="", description="Profile picture URL from the provider profile.")


# Links a provider account to the signed-in account from Settings.
class OAuthLinkRequest(BaseModel):
    username: str = Field(description="The signed-in account the provider account joins.")
    provider: str = Field(min_length=1, max_length=32, description="Provider id, such as 'google' or 'github'.")
    provider_account_id: str = Field(min_length=1, max_length=255, description="The provider's stable account id.")
    provider_email: str = Field(default="", max_length=255, description="The provider account's email, for display.")


# The resolved account the frontend turns into a session — never carries a secret.
class AccountInfo(BaseModel):
    email: str = Field(description="Lowercased account email, which is the identity.")
    name: str = Field(description="Display name.")
    role: str = Field(description="Authorization role: 'admin' or 'user'.")
    first_login: bool = Field(default=False, description="Whether this sign-in is the account's first.")


# Simple acknowledgement for state-changing auth calls. Lives here (not in the
# account-security router) because that router imports from this leaf module, so
# a shared response model can only flow in this direction without a cycle.
class OkResponse(BaseModel):
    ok: bool = Field(default=True, description="Always true on success.")


# Account email to email a password-reset code to (forgot-password step one).
class PasswordResetRequest(BaseModel):
    email: str = Field(description="Account email to send a one-time reset code to.")


# Reset code plus the new password (forgot-password step two).
class PasswordResetConfirm(BaseModel):
    email: str = Field(description="Account email being reset.")
    code: str = Field(description="One-time code delivered by email.")
    new_password: str = Field(description="New plaintext password; stored only as a scrypt hash.")


# Account email to (re-)send an email-confirmation code to.
class EmailVerifyRequest(BaseModel):
    email: str = Field(description="Account email to send a one-time confirmation code to.")


# Emailed confirmation code proving control of the sign-up address.
class EmailVerifyConfirm(BaseModel):
    email: str = Field(description="Account email being confirmed.")
    code: str = Field(description="One-time code delivered by email.")


def _normalise_email(raw: str) -> str:
    """Lowercase and trim an email for use as the stable identity.

    Args:
        raw: Email as supplied by the client.

    Returns:
        The normalized email.
    """
    return raw.strip().lower()


def _existing_account(session: Session, emails: list[str]) -> UserModel | None:
    """Return the first account registered under one of the given emails.

    Args:
        session: Open database session.
        emails: Provider-verified emails, in the provider's order.

    Returns:
        The matching account row, or None when no email has one.
    """
    for raw in emails:
        candidate = _normalise_email(raw)
        if not _EMAIL_RE.match(candidate):
            continue
        row = session.get(UserModel, candidate)
        if row is not None:
            return row
    return None


def _linked_account(session: Session, provider: str, provider_account_id: str) -> UserModel | None:
    """Return the account a provider account was linked to, if any.

    Args:
        session: Open database session.
        provider: Provider id.
        provider_account_id: The provider's stable account id.

    Returns:
        The linked account row, or None when the provider account is unlinked.
    """
    if not provider or not provider_account_id:
        return None
    identity = session.get(UserIdentityModel, (provider, provider_account_id))
    if identity is None:
        return None
    return session.get(UserModel, str(identity.username))


def _coerce_choice(value: str, allowed: frozenset[str]) -> str | None:
    """Return the value if it is a recognized choice, else ``None``.

    Args:
        value: Raw choice string from the client.
        allowed: The permitted values for this field.

    Returns:
        The trimmed value when it is in ``allowed``; ``None`` otherwise. An
        unknown or blank choice is stored as "not provided" rather than
        rejected, since these profile fields are non-critical metadata and the
        sign-up form already constrains input to the known options.
    """
    cleaned = value.strip()
    return cleaned if cleaned in allowed else None


def _role_for(email: str) -> str:
    """Resolve the authorization role for an account email.

    Args:
        email: Normalized account email.

    Returns:
        ``"admin"`` when the email is in the admin allowlist, else ``"user"``.
    """
    return "admin" if email in settings.admin_usernames_set else "user"


def _has_password(row: UserModel | None) -> TypeGuard[UserModel]:
    """Report whether a ``users`` row is a local email/password account.

    Args:
        row: The account row, or ``None`` when the identity has no row.

    Returns:
        True only for a row holding a password hash. A row provisioned for an
        OAuth/SSO identity carries an empty hash, so password-only features
        (password confirmation, 2FA) must not treat it as a local account.
    """
    return row is not None and bool(row.password_hash)


def _require_internal_auth(header_value: str | None) -> None:
    """Authorize a call from the trusted frontend via the shared secret.

    Args:
        header_value: Value of the ``X-Internal-Auth`` request header.

    Raises:
        DomainError: 500 when no shared secret is configured (the deployment is
            half-wired); 403 when the header is missing or does not match.
    """
    secret = settings.backend_auth_secret
    if secret is None:
        raise DomainError("auth.not_configured", status=500)
    if not header_value or not hmac.compare_digest(header_value, secret.get_secret_value()):
        raise DomainError("auth.missing_token", status=403)


def _enforce_signup_cap(job_store) -> None:
    """Refuse registrations at either the account or monthly-active cap.

    The registered-account ceiling bounds sign-up spam while the monthly-active
    ceiling controls actual application demand. Both are independently
    optional. Counts run outside the insert transaction, so simultaneous
    registrations can overshoot the account ceiling by a few rows; monthly
    admission itself remains atomic.

    Args:
        job_store: Job-store instance exposing the applicable counters.

    Raises:
        DomainError: 503 ``accounts.signups_closed`` when either cap is full.
    """
    account_cap = settings.max_total_users
    account_counter = getattr(job_store, "count_users", None)
    if account_cap > 0 and callable(account_counter) and account_counter() >= account_cap:
        raise DomainError("accounts.signups_closed", status=503)
    monthly_cap = settings.max_monthly_active_users
    monthly_counter = getattr(job_store, "count_monthly_active_users", None)
    if monthly_cap <= 0 or not callable(monthly_counter):
        return
    now = datetime.now(UTC)
    month_start = now.date().replace(day=1)
    if monthly_counter(month_start) >= monthly_cap:
        raise DomainError("accounts.signups_closed", status=503)


def create_accounts_router(*, job_store, login_throttle: LoginThrottle | None = None) -> APIRouter:
    """Build the email/password account router.

    Args:
        job_store: Job-store instance whose ORM engine backs the routes.
        login_throttle: Per-email failed-login limiter guarding ``/auth/login``.
            Defaults to a fresh in-memory instance scoped to this router (and so
            to the process); tests inject one with a controlled clock or lower
            threshold.

    Returns:
        A FastAPI ``APIRouter`` exposing register + login for local accounts.
    """
    router = APIRouter()
    throttle = login_throttle or LoginThrottle()

    @router.post(
        "/auth/register",
        response_model=AccountInfo,
        status_code=201,
        summary=f"Create a {APP_NAME}-native email/password account",
    )
    def register(
        body: RegisterRequest,
        x_internal_auth: Annotated[str | None, Header()] = None,
    ) -> AccountInfo:
        """Create a new local account and return its identity.

        When email delivery is configured the account is created unverified and
        a one-time confirmation code is emailed; sign-in is refused until it is
        confirmed. On a deployment with no SMTP relay there is nothing to verify
        against, so the account is created already-verified. A failure to send
        the code is swallowed: the account exists and the user can trigger a
        resend from the verification step.

        Args:
            body: Email, password, and optional display name.
            x_internal_auth: Shared-secret header proving the caller is the
                trusted frontend.

        Returns:
            The created account (email, display name, role).

        Raises:
            DomainError: 403 on a bad internal secret; 422 on an invalid email
                or a password the acceptance policy rejects; 409 when the email
                is already registered.
        """
        _require_internal_auth(x_internal_auth)
        email = _normalise_email(body.email)
        if not _EMAIL_RE.match(email):
            raise DomainError("accounts.invalid_email", status=422)
        enforce_account_rate(email, "register")
        validate_password(body.password, email)
        name = body.name.strip() or email
        verified = not email_configured()
        _enforce_signup_cap(job_store)
        with Session(job_store.engine) as session:
            if session.get(UserModel, email) is not None:
                raise DomainError("accounts.email_taken", status=409)
            session.add(
                UserModel(
                    email=email,
                    name=name,
                    password_hash=hash_password(body.password),
                    created_at=datetime.now(UTC),
                    email_verified=verified,
                    use_case=_coerce_choice(body.use_case, _USE_CASES),
                    experience_level=normalize_experience_level(body.experience_level),
                    job_role=_coerce_choice(body.job_role, _JOB_ROLES),
                    intake_completed_at=new_account_intake_completed_at(),
                )
            )
            code = None if verified else issue_verification_code(session, email)
            session.commit()
        if code is not None:
            # Best-effort: the account already exists, so a send failure must
            # not fail registration — the user can trigger a resend. Logged so a
            # broken relay surfaces (the alert sink forwards warnings) instead of
            # silently stranding new sign-ups on the verification step.
            try:
                _send_account_code(email, "verification", code)
            except (OSError, RuntimeError):
                logger.warning("Failed to send verification email at registration", exc_info=True)
        return AccountInfo(email=email, name=name, role=_role_for(email))

    @router.post(
        "/auth/login",
        response_model=AccountInfo,
        summary="Verify email/password credentials",
    )
    def login(
        body: LoginRequest,
        x_internal_auth: Annotated[str | None, Header()] = None,
    ) -> AccountInfo:
        """Verify credentials and return the account identity.

        Args:
            body: Email and password to verify.
            x_internal_auth: Shared-secret header proving the caller is the
                trusted frontend.

        Returns:
            The authenticated account (email, display name, role) and whether
            this is its first successful sign-in.

        Raises:
            DomainError: 403 on a bad internal secret, or when the account's
                email is not yet confirmed (``accounts.email_not_verified``);
                429 (``accounts.too_many_attempts``) when the email is locked
                out after repeated failures; 401 when the email is unknown, the
                password does not match, or the account's second factor is
                missing (``accounts.two_factor_required``, carrying the usable
                methods in ``params``) or wrong (``accounts.invalid_second_factor``).
        """
        _require_internal_auth(x_internal_auth)
        email = _normalise_email(body.email)
        throttle.check(email)
        with Session(job_store.engine) as session:
            row = session.get(UserModel, email)
            if row is None or not verify_password(body.password, str(row.password_hash)):
                throttle.record_failure(email)
                raise DomainError("accounts.invalid_credentials", status=401)
            # Only meaningful once the password is proven, so an unconfirmed
            # account never becomes an oracle for a wrong password. Gated on
            # ``email_configured`` so a mail-less deployment (where accounts are
            # created already-verified) can still sign in.
            if email_configured() and not bool(row.email_verified):
                raise DomainError("accounts.email_not_verified", status=403)
            try:
                enforce_second_factor(
                    session,
                    row,
                    totp_code=body.totp_code,
                    email_code=body.email_code,
                    recovery_code=body.recovery_code,
                )
            except DomainError as exc:
                # A missing factor is the benign first leg of a legit 2FA login;
                # only a *wrong* code counts as a guess against the throttle.
                if exc.code == "accounts.invalid_second_factor":
                    throttle.record_failure(email)
                raise
            enforce_monthly_active_user_limit(
                job_store,
                email,
                exempt=_role_for(email) == "admin",
            )
            first_login = row.last_login_at is None
            row.last_login_at = datetime.now(UTC)
            name = str(row.name)
            session.commit()
        throttle.reset(email)
        return AccountInfo(email=email, name=name, role=_role_for(email), first_login=first_login)

    @router.post(
        "/auth/oauth/provision",
        response_model=AccountInfo,
        summary="Record a sign-in through an external identity provider",
    )
    def provision_oauth(
        body: OAuthProvisionRequest,
        x_internal_auth: Annotated[str | None, Header()] = None,
    ) -> AccountInfo:
        """Mirror a provider-authenticated identity into ``users`` and return it.

        The frontend has already authenticated the user with Google, GitHub, or
        the SSO IdP; nothing is verified here beyond the internal secret. The
        first sign-in creates a row with no password, so ``/auth/login`` can
        never sign in to it, and later sign-ins only stamp ``last_login_at``. A
        local account with the same email is reused untouched, so one person
        stays one identity however they sign in. When no account exists under
        the email, an account under one of the provider's other verified emails
        is reused instead. An existing account whose email was never confirmed
        loses its password, since the provider is the first proof of ownership.

        Args:
            body: Provider-asserted email, display name, other verified emails
                and profile picture URL (kept only when it is ``https``).
            x_internal_auth: Shared-secret header proving the caller is the
                trusted frontend.

        Returns:
            The account (email, display name, role) and whether this is its
            first successful sign-in.

        Raises:
            DomainError: 403 on a bad internal secret; 422 on an invalid email.
        """
        _require_internal_auth(x_internal_auth)
        email = _normalise_email(body.email)
        if not _EMAIL_RE.match(email):
            raise DomainError("accounts.invalid_email", status=422)
        now = datetime.now(UTC)
        with Session(job_store.engine) as session:
            row = _linked_account(session, body.provider, body.provider_account_id)
            linked = row is not None
            if row is None:
                row = session.get(UserModel, email)
            if row is None:
                row = _existing_account(session, body.other_emails)
            if row is not None and not linked and not bool(row.email_verified):
                # The password was set by whoever registered the address without
                # ever proving it; the provider just proved it, so that password
                # must not keep a way into the owner's account.
                row.password_hash = ""
                row.email_verified = True
            if row is None:
                # The provider already vouched for the email, and an empty hash
                # never verifies, so the row is verified yet unreachable through
                # the password path until the user sets a password.
                row = UserModel(
                    email=email,
                    name=body.name.strip() or email,
                    password_hash="",
                    created_at=now,
                    email_verified=True,
                    intake_completed_at=new_account_intake_completed_at(),
                )
                session.add(row)
            if not linked and body.provider and body.provider_account_id:
                session.merge(
                    UserIdentityModel(
                        provider=body.provider,
                        provider_account_id=body.provider_account_id,
                        username=str(row.email),
                        provider_email=email,
                        created_at=now,
                    )
                )
            if body.image.startswith("https://") and len(body.image) <= _IMAGE_URL_MAX_LENGTH:
                row.image_url = body.image
            first_login = row.last_login_at is None
            row.last_login_at = now
            email = str(row.email)
            name = str(row.name)
            session.commit()
        return AccountInfo(email=email, name=name, role=_role_for(email), first_login=first_login)

    @router.post("/auth/local/session", response_model=AccountInfo, include_in_schema=False)
    def local_session(x_internal_auth: Annotated[str | None, Header()] = None) -> AccountInfo:
        """Sign in the single local-mode user, creating it on first use.

        Only the Mac app's own frontend holds the internal secret, and the
        hosted-safety check keeps local mode off any network-reachable
        deployment, so no password is needed. The row has an empty hash, so
        ``/auth/login`` can never sign in to it.

        Args:
            x_internal_auth: Shared-secret header proving the caller is the
                trusted frontend.

        Returns:
            The local account and whether this is its first sign-in.

        Raises:
            DomainError: 404 outside local mode; 403 on a bad internal secret.
        """
        if not settings.local_mode:
            raise DomainError("local.not_available", status=404)
        _require_internal_auth(x_internal_auth)
        email = _normalise_email(settings.local_user_email)
        now = datetime.now(UTC)
        with Session(job_store.engine) as session:
            row = session.get(UserModel, email)
            if row is None:
                row = UserModel(
                    email=email,
                    name=email,
                    password_hash="",
                    created_at=now,
                    email_verified=True,
                    intake_completed_at=new_account_intake_completed_at(),
                )
                session.add(row)
            first_login = row.last_login_at is None
            row.last_login_at = now
            name = str(row.name)
            session.commit()
        return AccountInfo(email=email, name=name, role=_role_for(email), first_login=first_login)

    @router.post("/auth/oauth/picture", include_in_schema=False)
    def save_oauth_picture(
        body: OAuthPictureRequest,
        x_internal_auth: Annotated[str | None, Header()] = None,
    ) -> dict[str, bool]:
        """Save the provider picture of a signed-in session on its account.

        Hosted whoami gives the Mac app ``users.image_url``, but the web app
        shows the session picture. The frontend calls this once per session
        so the two agree without a new sign-in or a new Mac link. Like
        provision, only an https URL that fits the column is kept.

        Args:
            body: The account email and its session picture URL.
            x_internal_auth: Shared-secret header proving the caller is the
                trusted frontend.

        Returns:
            ``{"ok": True}``, also when there is no account or the URL is ignored.
        """
        _require_internal_auth(x_internal_auth)
        if not (body.image.startswith("https://") and len(body.image) <= _IMAGE_URL_MAX_LENGTH):
            return {"ok": True}
        with Session(job_store.engine) as session:
            row = session.get(UserModel, _normalise_email(body.email))
            if row is not None and row.image_url != body.image:
                row.image_url = body.image
                session.commit()
        return {"ok": True}

    @router.post(
        "/auth/oauth/link",
        response_model=AccountInfo,
        summary="Link a provider account to a signed-in account",
    )
    def link_oauth(
        body: OAuthLinkRequest,
        x_internal_auth: Annotated[str | None, Header()] = None,
    ) -> AccountInfo:
        """Make a Google or GitHub account sign in to an existing account.

        The frontend calls this after the user, already signed in, finishes
        the provider's consent screen from Settings. Linking goes by the
        provider's account id, so the provider's emails never have to match.
        Each account keeps one link per provider; a new one replaces the old.

        Args:
            body: The signed-in account and the provider account joining it.
            x_internal_auth: Shared-secret header proving the caller is the
                trusted frontend.

        Returns:
            The signed-in account, which the session keeps.

        Raises:
            DomainError: 403 on a bad internal secret; 404 when the account
                does not exist; 409 when the provider account already signs in
                to a different account.
        """
        _require_internal_auth(x_internal_auth)
        username = _normalise_email(body.username)
        with Session(job_store.engine) as session:
            row = session.get(UserModel, username, with_for_update=True)
            if row is None:
                raise DomainError("accounts.not_found", status=404)
            existing = session.get(UserIdentityModel, (body.provider, body.provider_account_id))
            if existing is not None and str(existing.username) != username:
                raise DomainError("accounts.identity_in_use", status=409)
            if existing is None:
                session.execute(
                    delete(UserIdentityModel).where(
                        UserIdentityModel.username == username, UserIdentityModel.provider == body.provider
                    )
                )
                session.add(
                    UserIdentityModel(
                        provider=body.provider,
                        provider_account_id=body.provider_account_id,
                        username=username,
                        provider_email=_normalise_email(body.provider_email),
                        created_at=datetime.now(UTC),
                    )
                )
            session.commit()
            return AccountInfo(email=username, name=str(row.name), role=_role_for(username))

    @router.post(
        "/auth/password-reset/request",
        response_model=OkResponse,
        summary="Email a one-time password-reset code (internal)",
    )
    def password_reset_request(
        body: PasswordResetRequest,
        x_internal_auth: Annotated[str | None, Header()] = None,
    ) -> OkResponse:
        """Email a reset code to an account that has forgotten its password.

        An unknown address is refused, so the sign-in card can say so instead
        of asking for a code that will never arrive. A per-account cooldown
        bounds how often a code is sent, keeping it from being a mail-bomb
        oracle since no password is required here.

        Args:
            body: The account email to send a code to.
            x_internal_auth: Shared-secret header proving the trusted frontend.

        Returns:
            Acknowledgement that a code was sent, or is still on its way.

        Raises:
            DomainError: 403 on a bad internal secret; 422 when the deployment
                has no SMTP relay (a global, account-independent condition);
                404 ``accounts.not_found`` when no account uses the email; 502
                when the relay rejects the send.
        """
        _require_internal_auth(x_internal_auth)
        if not email_configured():
            raise DomainError("accounts.email_delivery_unavailable", status=422)
        email = _normalise_email(body.email)
        enforce_account_rate(email, "pwreset")
        with Session(job_store.engine) as session:
            if session.get(UserModel, email) is None:
                raise DomainError("accounts.not_found", status=404)
            if reset_code_on_cooldown(session, email):
                return OkResponse()
            code = issue_reset_code(session, email)
            session.commit()
        try:
            _send_account_code(email, "password_reset", code)
        except (OSError, RuntimeError) as exc:
            raise DomainError("accounts.email_send_failed", status=502) from exc
        return OkResponse()

    @router.post(
        "/auth/password-reset/confirm",
        response_model=OkResponse,
        summary="Set a new password with a reset code (internal)",
    )
    def password_reset_confirm(
        body: PasswordResetConfirm,
        x_internal_auth: Annotated[str | None, Header()] = None,
    ) -> OkResponse:
        """Verify a reset code and set the account's new password.

        The account's second factor is left untouched, so a reset restores only
        password knowledge and a later sign-in still passes through 2FA.

        Args:
            body: Account email, the emailed code, and the new password.
            x_internal_auth: Shared-secret header proving the trusted frontend.

        Returns:
            Acknowledgement.

        Raises:
            DomainError: 403 on a bad internal secret; 422 when the new password
                fails the acceptance policy, or ``accounts.invalid_reset_code``
                when the code is unknown, wrong, expired, or spent (also raised
                for an unknown email, so neither leaks account existence).
        """
        _require_internal_auth(x_internal_auth)
        email = _normalise_email(body.email)
        validate_password(body.new_password, email)
        with Session(job_store.engine) as session:
            row = session.get(UserModel, email)
            if row is None or not verify_reset_code(session, email, body.code):
                session.commit()
                raise DomainError("accounts.invalid_reset_code", status=422)
            row.password_hash = hash_password(body.new_password)
            session.commit()
        return OkResponse()

    @router.post(
        "/auth/email-verify/request",
        response_model=OkResponse,
        summary="Email a one-time account-confirmation code (internal)",
    )
    def email_verify_request(
        body: EmailVerifyRequest,
        x_internal_auth: Annotated[str | None, Header()] = None,
    ) -> OkResponse:
        """Email a confirmation code to an account that has not verified yet.

        Returns the same acknowledgement whether or not the address has an
        account and whether or not it is already verified, so this route can't
        be used to enumerate accounts or their verification state. A per-account
        cooldown bounds how often a code is sent, keeping it from being a
        mail-bomb oracle since no password is required here.

        Args:
            body: The account email to send a code to.
            x_internal_auth: Shared-secret header proving the trusted frontend.

        Returns:
            Acknowledgement (identical for known, unknown, and already-verified
            emails).

        Raises:
            DomainError: 403 on a bad internal secret; 422 when the deployment
                has no SMTP relay; 502 when the relay rejects the send.
        """
        _require_internal_auth(x_internal_auth)
        if not email_configured():
            raise DomainError("accounts.email_delivery_unavailable", status=422)
        email = _normalise_email(body.email)
        enforce_account_rate(email, "verify")
        with Session(job_store.engine) as session:
            row = session.get(UserModel, email)
            nothing_to_send = row is None or bool(row.email_verified) or verification_code_on_cooldown(session, email)
            if nothing_to_send:
                return OkResponse()
            code = issue_verification_code(session, email)
            session.commit()
        try:
            _send_account_code(email, "verification", code)
        except (OSError, RuntimeError) as exc:
            raise DomainError("accounts.email_send_failed", status=502) from exc
        return OkResponse()

    @router.post(
        "/auth/email-verify/confirm",
        response_model=OkResponse,
        summary="Confirm an account email with a one-time code (internal)",
    )
    def email_verify_confirm(
        body: EmailVerifyConfirm,
        x_internal_auth: Annotated[str | None, Header()] = None,
    ) -> OkResponse:
        """Verify an emailed code and mark the account's email confirmed.

        Confirming grants no session — it only flips the verified flag, so a
        later sign-in still needs the password (and any second factor). The same
        error is returned for an unknown email and a bad code, so neither leaks
        account existence.

        Args:
            body: Account email and the emailed confirmation code.
            x_internal_auth: Shared-secret header proving the trusted frontend.

        Returns:
            Acknowledgement.

        Raises:
            DomainError: 403 on a bad internal secret;
                ``accounts.invalid_verification_code`` (422) when the code is
                unknown, wrong, expired, or spent.
        """
        _require_internal_auth(x_internal_auth)
        email = _normalise_email(body.email)
        with Session(job_store.engine) as session:
            row = session.get(UserModel, email)
            if row is None or not verify_verification_code(session, email, body.code):
                session.commit()
                raise DomainError("accounts.invalid_verification_code", status=422)
            row.email_verified = True
            session.commit()
        return OkResponse()

    return router
