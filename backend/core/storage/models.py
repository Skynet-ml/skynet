"""SQLAlchemy ORM models for job storage.

Defines the shared database models used by the PostgreSQL storage backend.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    false,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EMBEDDING_DIM = 512
JSON_STORE = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    """SQLAlchemy declarative base for all models."""


class ApiTokenModel(Base):
    """Per-user personal access token (one active token per user).

    Stores only the SHA-256 hash of the issued token; the plaintext is shown
    to the user once at creation and never persisted. ``username`` is the
    primary key, so generating a new token replaces the user's previous one
    (rotation). ``token_hash`` is uniquely indexed for the auth lookup, which
    queries by hash without ever holding the plaintext.
    """

    __tablename__ = "api_tokens"

    username: Mapped[str] = mapped_column(String(255), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    last4: Mapped[str] = mapped_column(String(4), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DesktopLinkModel(Base):
    """One Mac app linked to a hosted account (SKY-124).

    A link owns its desktop tokens. ``scopes`` is space-separated, in the
    OAuth ``scope`` form. Removing a link sets ``revoked_at``; it is never
    reactivated, so a re-link always makes a new row.
    """

    __tablename__ = "desktop_links"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    username: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    device_name: Mapped[str] = mapped_column(String(128), nullable=False)
    scopes: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DesktopTokenModel(Base):
    """A desktop access or refresh token, stored only as its SHA-256 hash.

    ``parent_hash`` names the refresh token that minted this token, so a
    retried refresh can replace the pair it minted. ``used_at`` marks a
    refresh token as rotated; a later use of it is the theft signal.
    ``superseded_at`` marks a refresh token whose pair a retry replaced; any
    use of it is also the theft signal.
    """

    __tablename__ = "desktop_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    link_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    parent_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DesktopLinkCodeModel(Base):
    """A single-use link code from the hosted consent page, stored as its hash."""

    __tablename__ = "desktop_link_codes"

    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    code_challenge: Mapped[str] = mapped_column(String(64), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(String(255), nullable=False)
    scopes: Mapped[str] = mapped_column(String(255), nullable=False)
    device_name: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    link_id: Mapped[str | None] = mapped_column(String(40), nullable=True)


class McpOAuthClientModel(Base):
    """An OAuth client of the remote MCP server, from dynamic registration (SKY-229).

    ``redirect_uris`` is a JSON list. Clients that use a client ID metadata
    document (CIMD) have no row; their document is fetched on use.
    """

    __tablename__ = "mcp_oauth_clients"

    client_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_name: Mapped[str] = mapped_column(String(128), nullable=False)
    redirect_uris: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class McpAuthCodeModel(Base):
    """A single-use MCP authorization code, stored as its hash (SKY-229)."""

    __tablename__ = "mcp_auth_codes"

    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    client_id: Mapped[str] = mapped_column(String(512), nullable=False)
    client_name: Mapped[str] = mapped_column(String(128), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(String(512), nullable=False)
    code_challenge: Mapped[str] = mapped_column(String(64), nullable=False)
    scopes: Mapped[str] = mapped_column(String(255), nullable=False)
    resource: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    grant_id: Mapped[str | None] = mapped_column(String(40), nullable=True)


class McpGrantModel(Base):
    """One approval of an MCP client for an account (SKY-229).

    A grant owns its tokens. ``resource`` is the MCP server URL the tokens are
    bound to. Revoking sets ``revoked_at``; a grant is never reactivated.
    """

    __tablename__ = "mcp_grants"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    username: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    client_id: Mapped[str] = mapped_column(String(512), nullable=False)
    client_name: Mapped[str] = mapped_column(String(128), nullable=False)
    scopes: Mapped[str] = mapped_column(String(255), nullable=False)
    resource: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class McpTokenModel(Base):
    """An MCP access or refresh token, stored only as its SHA-256 hash (SKY-229).

    ``used_at`` marks a refresh token as rotated; any later use of it revokes
    the grant.
    """

    __tablename__ = "mcp_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    grant_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentApprovalModel(Base):
    """Cross-replica handoff for a generalist-agent tool approval decision.

    The SSE stream awaiting an approval and the confirm POST resolving it can
    land on different backend replicas; the in-process future only works when
    they share a process. A confirm that finds no local future writes its
    decision here, and the awaiting replica's poll loop picks it up (and
    deletes the row). Rows are short-lived — consumed within seconds, purged
    opportunistically after the approval timeout.
    """

    __tablename__ = "agent_approvals"

    call_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class AgentSteerTurnModel(Base):
    """The input bound announced by one authenticated steering turn."""

    __tablename__ = "agent_steer_turns"

    owner: Mapped[str] = mapped_column(String(255), primary_key=True)
    steer_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    max_chars: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (Index("ix_agent_steer_turns_created_at", "created_at"),)


class AgentSteerMessageModel(Base):
    """A message the user sent into an agent turn that was still running.

    The steer POST and the streaming turn can land on different replicas, so
    the message waits here under the turn's client-chosen ``steer_key``. The
    running loop takes it at its next step; once the turn ends, the client
    withdraws whatever the loop never read. Taking sets ``claimed_at``;
    delivery sets ``taken_at`` at the model boundary.
    """

    __tablename__ = "agent_steer_messages"
    __table_args__ = (
        Index("ix_agent_steer_messages_key", "steer_key", "owner"),
        Index("ix_agent_steer_messages_owner_pending", "owner", "taken_at"),
        Index("ix_agent_steer_messages_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid4().hex)
    steer_key: Mapped[str] = mapped_column(String(64), nullable=False)
    owner: Mapped[str] = mapped_column(String(255), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    taken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentTurnModel(Base):
    """One resumable wizard-agent turn, shared across backend replicas.

    The turn runs as a server-side task on the replica that started it, while
    the browser's stream connection is capped by the hosting edge and may
    reconnect to any replica. This row carries what those other replicas need:
    the owner (resume/cancel authorization), the run status, a cancel request
    for the owning replica to act on, the owner's liveness heartbeat, and the
    last time any reader was attached. Rows (and their events) are purged an
    hour after their last heartbeat.
    """

    __tablename__ = "agent_turns"

    turn_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), index=True
    )
    last_read_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class AgentTurnEventModel(Base):
    """One SSE event of an :class:`AgentTurnModel`, in emission order.

    ``seq`` is the turn's monotonic event number (the SSE ``id``); a
    reconnecting stream replays every row after the last ``seq`` it saw.
    ``data`` is the event payload as JSON text, exactly as it was streamed.
    """

    __tablename__ = "agent_turn_events"

    turn_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    data: Mapped[str] = mapped_column(Text, nullable=False)


class UserModel(Base):
    """A Skynet account row, keyed by the cross-app identity.

    Backs the "create an account in Skynet" path, and — since OAuth (Google/
    GitHub) and SSO identities are mirrored here on sign-in — every identity
    that has signed in. The lowercased ``email`` is the primary key because it
    is also the cross-app identity (the ``username`` every other table — jobs,
    datasets, shares — keys ownership on), so an account and the work it owns
    line up on a single value. Only the scrypt ``password_hash`` is persisted;
    the plaintext password is never stored, and a row provisioned for an
    OAuth/SSO identity holds an empty hash that can never verify.
    """

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Whether the sign-up email address has been confirmed via an emailed code.
    # Defaults True so rows created before this column existed, and accounts
    # created on a deployment with no SMTP relay, are treated as verified; the
    # register route flips it to False only when it actually sends a code, and
    # login refuses an unverified account while email delivery is configured.
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Optional profile captured on the rich sign-up form. Nullable because
    # OAuth-provisioned rows and rows created before these columns existed
    # never set them, and ``job_role`` is optional even on the sign-up form.
    use_case: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # One of ``guided`` / ``standard`` / ``expert``: how much of the product
    # surface the UI shows this account. Rows from the original sign-up form
    # stored ``new`` / ``familiar``; the abstraction-level migration remapped them.
    experience_level: Mapped[str | None] = mapped_column(String(16), nullable=True)
    job_role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # First-login intake: NULL means the account has not finished (or chose to
    # rerun) the onboarding intake, so every pre-existing account sees it once.
    intake_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    intake_profile: Mapped[dict[str, Any] | None] = mapped_column(JSON_STORE, nullable=True)
    # Two-factor state for password sign-ins. A non-null ``totp_secret`` means
    # TOTP is enabled; ``totp_pending_secret`` holds the secret between setup
    # and the first verified code, so an abandoned setup never locks the
    # account. ``recovery_codes`` is a JSON array of scrypt hashes, each
    # consumed on use.
    totp_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    totp_pending_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    email_2fa_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    recovery_codes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Provider profile picture, refreshed on each OAuth sign-in. The Mac app
    # has no provider session, so desktop whoami returns this instead.
    image_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    # Our own and test accounts (synthetic ``.local`` fixtures, configured
    # admins, anything an admin flags) that every product metric excludes.
    # Defaults False so every new sign-up counts as a real user.
    is_internal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())


class UserIdentityModel(Base):
    """A Google or GitHub account that signs in to one Skynet account.

    Keyed by the provider's stable account id rather than its email, so a
    provider account linked from Settings keeps reaching the same Skynet
    account even when none of its emails match it.
    """

    __tablename__ = "user_identities"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    provider_account_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    username: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    provider_email: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class MonthlyActiveUserModel(Base):
    """One identity admitted to use the application during a UTC month."""

    __tablename__ = "monthly_active_users"

    month_start: Mapped[date] = mapped_column(Date, primary_key=True)
    username: Mapped[str] = mapped_column(String(255), primary_key=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class WizardAgentUsageModel(Base):
    """How many black-box wizard agent turns an identity started on one UTC day.

    The wizard agent is free to use, so instead of a credit charge each turn
    counts against a daily cap (``settings.wizard_agent_daily_turns``).
    """

    __tablename__ = "wizard_agent_usage"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    username: Mapped[str] = mapped_column(String(255), primary_key=True)
    turns: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class NotificationPreferenceModel(Base):
    """Per-identity routing of run notifications across the delivery channels.

    The row is separate from :class:`UserModel` because OAuth identities own
    jobs and shares without a local password-account row. A missing row, or a
    NULL ``channel_grid``, means the default grid of
    :mod:`core.notifications.events`.
    """

    __tablename__ = "notification_preferences"

    username: Mapped[str] = mapped_column(String(255), primary_key=True)
    # ``{event_type: {channel: bool}}``; cells absent here take the default grid.
    channel_grid: Mapped[dict[str, Any] | None] = mapped_column(JSON_STORE, nullable=True)
    hold_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=10, server_default="10")
    # Channels whose non-urgent sends wait ``hold_minutes``; NULL means email only.
    held_channels: Mapped[list[str] | None] = mapped_column(JSON_STORE, nullable=True)
    stuck_fraction: Mapped[float] = mapped_column(Float, nullable=False, default=0.25, server_default="0.25")
    budget_alert_fraction: Mapped[float] = mapped_column(Float, nullable=False, default=0.8, server_default="0.8")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class NotificationModel(Base):
    """One notification event for one identity: an inbox item plus its channel deliveries.

    Rows are kept forever. ``deliveries`` records each routed channel's state
    (``held``, ``sent``, ``failed``, ``dropped`` or ``unavailable``) so a later
    channel can be added without a schema change; ``hold_until`` is set while
    any channel still waits and is what the hold sweeper scans.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_username_created", "username", "created_at"),
        UniqueConstraint("username", "dedupe_key", name="uq_notifications_username_dedupe"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    optimization_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    facts: Mapped[dict[str, Any]] = mapped_column(JSON_STORE, nullable=False, default=dict)
    link: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Makes a repeated emit of the same event (a resumed run, a news file seen
    # twice) a no-op; NULL for events that may legitimately repeat.
    dedupe_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    in_inbox: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deliveries: Mapped[dict[str, Any]] = mapped_column(JSON_STORE, nullable=False, default=dict)
    hold_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class PushSubscriptionModel(Base):
    """One browser's Web Push subscription for one identity.

    The endpoint is the push service URL the browser handed out; it is unique
    per browser profile, so re-subscribing the same browser (even as another
    identity) moves the row rather than duplicating it.
    """

    __tablename__ = "push_subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    username: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    endpoint: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    p256dh: Mapped[str] = mapped_column(String(255), nullable=False)
    auth: Mapped[str] = mapped_column(String(255), nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SlackConnectionModel(Base):
    """One identity's install of the Skynet Slack app, where its Slack notifications go.

    Keyed by ``username`` like :class:`PushSubscriptionModel`. The bot token is
    Fernet-encrypted under ``BYOK_VAULT_KEY``. A ``broken`` row stays so the
    settings tab can say why Slack stopped; sends skip it until the user
    reconnects or picks a working destination.
    """

    __tablename__ = "slack_connections"

    username: Mapped[str] = mapped_column(String(255), primary_key=True)
    team_id: Mapped[str] = mapped_column(String(32), nullable=False)
    team_name: Mapped[str] = mapped_column(String(255), nullable=False)
    bot_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    slack_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    bot_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    # NULL sends a direct message to ``slack_user_id``.
    channel_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    channel_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dm_channel_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="connected", server_default="connected")
    broken_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class ModelPrivacyPreferenceModel(Base):
    """Which OpenRouter providers may receive a caller's prompts on platform-paid calls.

    A missing row means ``deny``: only providers that neither train on nor
    store prompts. BYOK calls follow the caller's own OpenRouter account settings.
    """

    __tablename__ = "model_privacy_preferences"

    username: Mapped[str] = mapped_column(String(255), primary_key=True)
    data_policy: Mapped[str] = mapped_column(String(16), nullable=False, default="deny")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class AgentEnginePreferenceModel(Base):
    """Which engine runs a caller's agent turns when no model is picked.

    Only local mode stores a row; a hosted caller always runs on OpenRouter.
    A missing row means the first ready subscription CLI.
    """

    __tablename__ = "agent_engine_preferences"

    username: Mapped[str] = mapped_column(String(255), primary_key=True)
    engine: Mapped[str] = mapped_column(String(16), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class PackageRegistryPreferenceModel(Base):
    """Store a caller's package index independently of local password accounts."""

    __tablename__ = "package_registry_preferences"

    username: Mapped[str] = mapped_column(String(255), primary_key=True)
    index_url: Mapped[str] = mapped_column(String(2048), nullable=False)


class WebAuthnCredentialModel(Base):
    """A registered passkey (WebAuthn credential) owned by one identity.

    ``user_email`` is a plain indexed column rather than a foreign key to
    ``users``: OAuth identities (Google/GitHub) have no ``users`` row yet can
    still register passkeys, since the email is the cross-provider identity.
    ``credential_id`` and ``public_key`` are stored base64url-encoded exactly
    as the WebAuthn ceremony emits them; ``sign_count`` tracks the
    authenticator's signature counter for clone detection.
    """

    __tablename__ = "webauthn_credentials"

    credential_id: Mapped[str] = mapped_column(String(1024), primary_key=True)
    user_email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    sign_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    transports: Mapped[str | None] = mapped_column(String(255), nullable=True)
    nickname: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WebAuthnChallengeModel(Base):
    """Single-use server-issued challenge for an in-flight WebAuthn ceremony.

    Stored server-side (not in a cookie) so registration and sign-in stay
    stateless across backend replicas. Keyed by the base64url challenge
    itself — the verify step extracts the challenge echoed in the client's
    ``clientDataJSON``, consumes the row, and rejects anything unknown or past
    ``expires_at``. Expired rows are purged opportunistically on each insert.
    """

    __tablename__ = "webauthn_challenges"

    challenge: Mapped[str] = mapped_column(String(255), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    user_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TwoFactorEmailCodeModel(Base):
    """The active emailed one-time sign-in code for a local account.

    One row per email (re-sending replaces it), holding only the scrypt hash
    of the 6-digit code. ``attempts`` counts failed verifies so a code dies
    after a handful of guesses well before ``expires_at``.
    """

    __tablename__ = "two_factor_email_codes"

    email: Mapped[str] = mapped_column(String(255), primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class PasswordResetCodeModel(Base):
    """The active password-reset code for a local account.

    One row per email (requesting again replaces it), holding only the scrypt
    hash of the emailed 6-digit code. ``attempts`` counts failed verifies so a
    code dies after a handful of guesses well before ``expires_at``. ``sent_at``
    backs a per-account resend cooldown: the request route is unauthenticated,
    so the cooldown is what keeps it from being a mail-bomb oracle.
    """

    __tablename__ = "password_reset_codes"

    email: Mapped[str] = mapped_column(String(255), primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class EmailVerificationCodeModel(Base):
    """The active email-confirmation code for a freshly registered account.

    One row per email (re-sending replaces it), holding only the scrypt hash of
    the emailed 6-digit code. ``attempts`` counts failed verifies so a code dies
    after a handful of guesses well before ``expires_at``. ``sent_at`` backs a
    per-account resend cooldown, since the resend route is unauthenticated and
    would otherwise be a mail-bomb oracle. Kept separate from the sign-in and
    reset code tables so a pending confirmation never collides with either.
    """

    __tablename__ = "email_verification_codes"

    email: Mapped[str] = mapped_column(String(255), primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class BillingCustomerModel(Base):
    """Per-user billing state synced from Stripe (one row per paying identity).

    Keyed on ``username`` (the lowercased email every other table owns rows by)
    rather than a foreign key to ``users`` so SSO accounts — which never get a
    ``users`` row — are billed too. ``stripe_customer_id`` is the durable link to
    Stripe. ``balance_cents`` is the denormalized spendable purchased balance
    in cents, kept in step with ``wallet_ledger`` on every mutation so a balance
    read is a single fast integer.
    """

    __tablename__ = "billing_customers"
    # The balance is prepaid: a balance below zero would mean the platform lent
    # tokens, so the DB refuses it outright — the clamped debit in
    # ``StripeBillingService.debit_run`` keeps writes inside these bounds.
    __table_args__ = (
        CheckConstraint(
            "balance_cents >= 0",
            name="ck_billing_customers_balance_cents_non_negative",
        ),
        CheckConstraint(
            "grant_remaining IS NULL OR grant_remaining >= 0",
            name="ck_billing_customers_grant_remaining_non_negative",
        ),
        CheckConstraint(
            "debt_cents >= 0",
            name="ck_billing_customers_debt_cents_non_negative",
        ),
    )

    username: Mapped[str] = mapped_column(String(255), primary_key=True)
    stripe_customer_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    balance_cents: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        nullable=False,
        default=0,
        server_default="0",
    )
    # ``grant_remaining`` is what is left of the account's one-time free
    # grant (500 cents = $5, seeded once and never renewed). NULL until the first
    # wallet read or run seeds it; seeding is lazy-evaluated on read, never
    # cron'd.
    grant_remaining: Mapped[int | None] = mapped_column(BigInteger().with_variant(Integer(), "sqlite"), nullable=True)
    # Mirror of the account's Skynet Pro subscription, written only by the
    # Stripe webhook. Stripe stays the source of truth; these columns exist so
    # every quota check reads the plan from the local DB instead of calling
    # Stripe on the request path.
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subscription_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    subscription_current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    subscription_cancel_at_period_end: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    # When the subscription entered ``past_due``; anchors the bounded Pro grace
    # period and is cleared once the status moves on.
    subscription_past_due_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Cents a refund or chargeback reversed after they were already spent.
    # The platform paid for those tokens, so the account owes them back: while
    # this is positive the balance and grant are held at zero (nothing is
    # spendable) and the next top-up repays it before crediting anything.
    debt_cents: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        nullable=False,
        default=0,
        server_default="0",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class WalletLedgerModel(Base):
    """Immutable, append-only record of every balance movement for an account.

    Each row is a signed ``delta_cents`` against ``username``: positive for a
    top-up (pack purchase) or monthly grant, negative for a run charge. The
    running sum is denormalized onto ``billing_customers.balance_cents`` for
    fast gating; this table is the audit trail that explains that balance and
    backs the wallet's usage ledger. ``stripe_event_id`` ties a top-up row to the
    webhook event that created it so a redelivered event can't double-credit.
    """

    __tablename__ = "wallet_ledger"
    # A named unique index, not a column UNIQUE constraint: migration 927cb56e104a
    # created it with CREATE UNIQUE INDEX (renamed with the table), so declaring it
    # the same way keeps autogenerate from proposing a drop-and-recreate.
    __table_args__ = (Index("wallet_ledger_settlement_key_key", "settlement_key", unique=True),)

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    username: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    delta_cents: Mapped[int] = mapped_column(BigInteger().with_variant(Integer(), "sqlite"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    budget_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    settlement_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Measured usage behind a run row's charge; None on top-ups/grants and on
    # rows written before token metering landed.
    input_tokens: Mapped[int | None] = mapped_column(BigInteger().with_variant(Integer(), "sqlite"), nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger().with_variant(Integer(), "sqlite"), nullable=True)
    # Cost the account could not cover when the row was written: a run charge
    # clamped to the balance, or a clawback beyond the balance (carried as
    # debt). Kept so revenue lost to the balance floor is queryable instead of
    # only living in a log line. None/0 when the full amount was collected.
    uncollected_cents: Mapped[int | None] = mapped_column(BigInteger().with_variant(Integer(), "sqlite"), nullable=True)
    stripe_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # The PaymentIntent behind a top-up (``pi_…``), the join key a refund or
    # dispute webhook uses to find the account and the cents to claw back —
    # it is the one id present on the checkout, charge, and dispute objects
    # alike. None on grants/runs/adjustments and on top-ups predating this column.
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # The job a ``run`` row charged, so cost per run is a sum over this table.
    # None on interactive debits (serve, agent chat), non-run rows, and rows
    # written before run attribution landed.
    optimization_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # Cents of a ``run`` row's charge drawn from the free grant; the paid part
    # is ``-delta_cents - from_grant_cents``. On a positive restart-waiver row,
    # the cents returned to the grant. None on non-run and older rows.
    from_grant_cents: Mapped[int | None] = mapped_column(BigInteger().with_variant(Integer(), "sqlite"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), index=True
    )


class ExecutionBudgetModel(Base):
    """Account-owned spending authority shared by setup and its eventual root job."""

    __tablename__ = "execution_budgets"
    __table_args__ = (
        UniqueConstraint("username", "creation_key", name="uq_execution_budget_creation"),
        CheckConstraint("total_cents > 0", name="ck_execution_budget_total"),
        CheckConstraint(
            "settled_units >= 0 AND wallet_settled_units >= 0 AND reserved_units >= 0 "
            "AND wallet_reserved_units >= 0 AND billed_cents >= 0",
            name="ck_execution_budget_amounts",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    creation_key: Mapped[str] = mapped_column(String(128), nullable=False)
    creation_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    total_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    uncapped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="open")
    blocked_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True, unique=True)
    settled_units: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    wallet_settled_units: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reserved_units: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    wallet_reserved_units: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    billed_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExecutionOperationModel(Base):
    """One bounded physical attempt whose outstanding coverage survives lost responses."""

    __tablename__ = "execution_operations"
    __table_args__ = (
        UniqueConstraint("budget_id", "operation_key", "attempt", name="uq_execution_operation_attempt"),
        CheckConstraint("phase IN ('setup', 'run')", name="ck_execution_operation_phase"),
        CheckConstraint(
            "max_units >= 0 AND max_wallet_units >= 0 AND actual_units >= 0 AND actual_wallet_units >= 0",
            name="ck_execution_operation_amounts",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    budget_id: Mapped[str] = mapped_column(ForeignKey("execution_budgets.id"), nullable=False, index=True)
    operation_key: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(String(8), nullable=False)
    cost_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    role: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    admission_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    price_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_STORE, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="reserved")
    max_units: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_wallet_units: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actual_units: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    actual_wallet_units: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    provider_request_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExecutionUsageEvidenceModel(Base):
    """Append-only provider evidence and its once-only incremental wallet settlement."""

    __tablename__ = "execution_usage_evidence"
    __table_args__ = (UniqueConstraint("operation_id", "evidence_key", name="uq_execution_usage_evidence"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    operation_id: Mapped[str] = mapped_column(ForeignKey("execution_operations.id"), nullable=False, index=True)
    evidence_key: Mapped[str] = mapped_column(String(128), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    actual_units: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actual_wallet_units: Mapped[int] = mapped_column(BigInteger, nullable=False)
    billed_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    final: Mapped[bool] = mapped_column(Boolean, nullable=False)
    issue: Mapped[str | None] = mapped_column(String(32), nullable=True)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON_STORE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BillingWebhookEventModel(Base):
    """Idempotency ledger of Stripe webhook events already applied.

    The webhook endpoint records each ``evt_…`` id here inside the same
    transaction that applies the event's effect, and skips any event whose id is
    already present. Stripe guarantees at-least-once delivery, so without this a
    retried ``checkout.session.completed`` would credit the same purchase twice.
    """

    __tablename__ = "billing_webhook_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class BillingProviderKeyModel(Base):
    """One stored BYOK provider connection for an account, encrypted at rest.

    Backs BYOK mode: the user's provider key pays model usage while the Skynet balance
    covers only the platform fee and sandbox usage. The secret is never stored
    in plaintext — only ``secret_ciphertext`` (the Fernet-encrypted bytes) is
    persisted, so a database dump never leaks a usable key. ``last4`` is the
    recognizable tail kept for masked display, and ``status`` records whether the
    connection was checked against its provider (``unverified`` / ``verified`` /
    ``invalid``) so the UI can tell a typo'd key from a working one before a job
    ever runs. ``api_base`` and ``params`` carry an optional custom endpoint and
    extra LiteLLM kwargs so a connection can target any OpenAI-compatible host,
    and ``label`` is an optional user-facing name. The surrogate ``id`` is the
    primary key, so an account may hold several connections for one provider
    (e.g. two OpenAI-compatible endpoints); ``(username, provider)`` is indexed
    for the run-path and settings lookups.
    """

    __tablename__ = "billing_provider_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid4().hex)
    username: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    api_base: Mapped[str | None] = mapped_column(String(255), nullable=True)
    params: Mapped[dict[str, Any]] = mapped_column(JSON_STORE, nullable=False, default=dict)
    secret_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    last4: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="unverified", server_default="unverified")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (Index("ix_billing_provider_keys_username_provider", "username", "provider"),)


class UserConnectorModel(Base):
    """One linked third-party data account (a "connector") for a user.

    Connectors let a user import data from services they already use — today
    Hugging Face datasets — with their own account, so private and gated
    resources resolve with their permissions. Only Fernet ciphertext of the
    access token (and, for OAuth links, the refresh token) is persisted, under
    the same vault key as BYOK provider keys. ``auth_method`` records how the
    link was made (``oauth`` or a pasted ``token``), ``account_label`` is the
    remote account name for display, ``token_expires_at`` drives OAuth refresh,
    and ``status`` flips to ``invalid`` once the remote rejects the credential
    so the UI can prompt a reconnect. One row per ``(username, provider)``.
    """

    __tablename__ = "user_connectors"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid4().hex)
    username: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    auth_method: Mapped[str] = mapped_column(String(16), nullable=False)
    account_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Google echoes every scope the user ever granted the client (include_granted_scopes),
    # so the list grows with each linked Google connector and outruns any fixed width.
    scopes: Mapped[str | None] = mapped_column(Text, nullable=True)
    secret_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    refresh_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="connected", server_default="connected")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (UniqueConstraint("username", "provider", name="uq_user_connectors_username_provider"),)


class UserSecretModel(Base):
    """One named secret a user saved on their account for reuse across runs.

    Repository runs read these as environment variables inside the sandbox
    (build tokens, test credentials). Only Fernet ciphertext under the vault
    key is persisted; ``name`` is the environment variable name and is unique
    per user.
    """

    __tablename__ = "user_secrets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid4().hex)
    username: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    secret_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (UniqueConstraint("username", "name", name="uq_user_secrets_username_name"),)


class ProtectedCredentialModel(Base):
    """One execution-scoped secret encrypted for use by a trusted parent relay.

    Public submission payloads retain only ``id`` and ``revision``. The raw
    credential is encrypted before job or preflight persistence and decrypted
    only in the API/worker parent immediately before it configures the scoped
    relay. ``audience_hash`` prevents a reference saved for one endpoint from
    being reused against another endpoint, while ``binding_id`` confines it to
    the execution budget that introduced it.
    """

    __tablename__ = "protected_credentials"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid4().hex)
    username: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    binding_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("execution_budgets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    audience_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    secret_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (UniqueConstraint("username", "binding_id", "purpose", name="uq_protected_credential_binding"),)


class BillingOpenRouterKeyModel(Base):
    """One platform-provisioned OpenRouter runtime key per account.

    Backs provider-side spend capping for managed runs: when
    ``OPENROUTER_PROVISIONING_KEY`` is configured, the worker mints one
    OpenRouter runtime key per account and syncs its spend limit to the
    account's balance before each managed dispatch, so upstream spend is
    capped at OpenRouter itself even if every backend-side gate fails (see
    ``core.billing.openrouter_keys``). Only the Fernet-encrypted secret is
    persisted — the same at-rest contract as the BYOK vault — and ``key_hash``
    addresses the key in OpenRouter's key-management API. Deleting a row forces
    a fresh key to be minted on the account's next managed dispatch.
    """

    __tablename__ = "billing_openrouter_keys"

    username: Mapped[str] = mapped_column(String(255), primary_key=True)
    key_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    secret_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class SearchQueryLogModel(Base):
    """Anonymous log of public-corpus search queries, powering trending searches.

    One row is written per public ``POST /dashboard/search`` that carries a
    non-empty query, first page only — later pages are pagination of the same
    search, not new intent. Owner-scoped ("mine") searches are never logged;
    only the cross-user public corpus is. No user identity, IP, session, or
    filter state is stored — just the normalized query text and a timestamp —
    so a top-N trending aggregate can be computed without profiling anyone.
    """

    __tablename__ = "search_query_log"

    # BigInteger on Postgres (BIGSERIAL); INTEGER on SQLite so its rowid
    # autoincrement kicks in for the create_all-based test stores.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    query_text: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        index=True,
    )


class TelemetryEventModel(Base):
    """Append-only product-telemetry event — how the app is actually used.

    One row per captured interaction (a page view, a labelled click, a named
    flow event). The browser SDK batches events to ``POST /telemetry/events``
    and they land here, queryable beside the rest of the product data.

    Identity is split deliberately. ``username`` is the *server-trusted* caller
    derived from the request's auth token (never from the request body), so it
    is set only when the batch was sent authenticated; the client can't forge
    another user's identity onto an event. ``anonymous_id`` is an opaque,
    per-browser id the SDK generates (no email, no name), letting pre-login and
    logged-out activity be funnel-counted without profiling a person.
    ``session_id`` scopes a single browsing session. No IP address is stored,
    and ``properties`` is contractually PII-free (the SDK only emits structural
    descriptors), so aggregates can be computed without holding personal data.

    ``occurred_at`` is the client-reported event time (best-effort, clock-skewed);
    ``received_at`` is the authoritative server insert time the read endpoints
    order and bucket on.
    """

    __tablename__ = "telemetry_events"

    # BigInteger on Postgres (BIGSERIAL); INTEGER on SQLite so its rowid
    # autoincrement kicks in for the create_all-based test stores.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    event_name: Mapped[str] = mapped_column(String(80), nullable=False)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        index=True,
    )
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    anonymous_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    locale: Mapped[str | None] = mapped_column(String(16), nullable=True)
    app_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    properties: Mapped[dict[str, Any]] = mapped_column(JSON_STORE, default=dict)
    context: Mapped[dict[str, Any]] = mapped_column(JSON_STORE, default=dict)

    # Top-events-over-time and per-user activity are the two read shapes; each
    # filters on its leading column and orders by recency, so a composite beats
    # two standalone indexes. The bare received_at index above still serves the
    # global time-series (all events, no name/user filter).
    __table_args__ = (
        Index("ix_telemetry_events_name_received", "event_name", "received_at"),
        Index("ix_telemetry_events_user_received", "username", "received_at"),
    )


class OptimizationShareLinkModel(Base):
    """Per-optimization sharing config keyed by a public link token.

    The ``token`` is the unguessable capability identifier embedded in the
    public ``/share/<token>`` URL. It is stored in plaintext because it IS the
    public identifier (like a ChatGPT share id), not a credential hash. The
    active (``revoked_at IS NULL``) row per optimization holds the sharing
    config. ``general_access`` selects the link policy: ``'restricted'`` (owner
    + invited members only) or ``'anyone'`` (anyone holding the link has
    access). ``general_role`` is the tier an ``'anyone'`` link grants a
    *signed-in* visitor — ``'viewer'`` or ``'editor'`` (never ``'owner'``;
    ownership is not transferred by link). An anonymous (logged-out) visitor
    never elevates past the read-only ``view`` tier regardless of
    ``general_role``, so a bare URL can never run inference on the owner's key.
    Revoking sets ``revoked_at`` so the public route returns 404 thereafter.
    Rows are removed when the optimization is deleted.
    """

    __tablename__ = "optimization_share_links"

    token: Mapped[str] = mapped_column(String(48), primary_key=True)
    optimization_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    general_access: Mapped[str] = mapped_column(
        String(16), nullable=False, default="restricted", server_default="restricted"
    )
    general_role: Mapped[str] = mapped_column(String(16), nullable=False, default="viewer", server_default="viewer")


class OptimizationShareGrantModel(Base):
    """A single per-user access grant on a shared optimization.

    Each row invites one ``grantee_username`` to an optimization with a tier
    ``role`` (``'viewer'`` / ``'editor'`` / ``'owner'``). The pair
    ``(optimization_id, grantee_username)`` is the primary key, so re-inviting a
    user replaces their existing grant. ``general_access`` on the link and these
    per-user grants coexist: an anyone-link can be on while named members hold
    higher roles. Rows are removed when the optimization is deleted.
    """

    __tablename__ = "optimization_share_grants"

    optimization_id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    grantee_username: Mapped[str] = mapped_column(String(255), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    # The composite PK leads with optimization_id, so "list everything shared
    # with this user" (filtering on grantee_username alone) could not use it.
    __table_args__ = (Index("ix_optimization_share_grants_grantee", "grantee_username"),)


class JobModel(Base):
    """SQLAlchemy model for the jobs table.

    The ``claimed_by`` / ``claimed_at`` / ``lease_expires_at`` triplet implements
    a DB-backed work queue safe for multi-pod horizontal scaling: each worker
    atomically claims a row via ``SELECT ... FOR UPDATE SKIP LOCKED`` and
    extends the lease while it holds the job. A pod that crashes leaves an
    expired lease which any other pod is free to re-claim on its next poll.
    """

    __tablename__ = "jobs"

    optimization_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    estimated_remaining_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    latest_metrics: Mapped[dict[str, Any]] = mapped_column(JSON_STORE, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON_STORE, nullable=True)
    payload_overview: Mapped[dict[str, Any]] = mapped_column(JSON_STORE, default=dict)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON_STORE, nullable=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    optimization_type: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # "single" vs "workflow" — orthogonal to optimization_type. Hoisted from the
    # payload overview at submit so a run's shape is a first-class indexed column
    # rather than a JSON probe against module_name. NULL on legacy/child rows.
    composition: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    code_version: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    # The image a protected run first executed in. Every later box for the run,
    # a resume after a redeploy included, boots from it so checkpoints stay loadable.
    sandbox_image: Mapped[str | None] = mapped_column(String(255), nullable=True)
    execution_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    execution_budget_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    execution_budget_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    result_availability: Mapped[str | None] = mapped_column(String(16), nullable=True)
    terminal_evidence: Mapped[dict[str, Any] | None] = mapped_column(JSON_STORE, nullable=True)
    recovery: Mapped[dict[str, Any] | None] = mapped_column(JSON_STORE, nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set once, by the worker that wins the CAS update inside
    # ``claim_completion_notification`` — guarantees a single Slack/Teams
    # message per job even when orphan recovery re-runs a row.
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Kinds of in-run email already sent for this run (``new_best``,
    # ``stage:baseline``, ``digest:<window>`` ...). Kept on the row rather than
    # in worker memory so a resumed or orphan-recovered run never re-sends one,
    # and cleared with ``notified_at`` when the run is restarted from scratch.
    notification_claims: Mapped[list[str] | None] = mapped_column(JSON_STORE, nullable=True)
    # Optional client-supplied dedup key; lookups are scoped per submitter so
    # two users may legitimately reuse the same key without colliding.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Precomputed byte size of this job's JSON columns (payload + result +
    # overview), maintained at write time so the unified per-user storage total
    # is a single indexed SUM rather than a scan that re-serializes every payload.
    stored_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    # Running sum of every completed leg's wall-clock duration. ``requeue_for_resume``
    # folds the finished leg in before clearing the timestamps, so elapsed reports net
    # active compute across resumes without ever counting the paused gap between legs.
    accumulated_runtime_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")
    # Distributed grid search: a (generation, reflection) pair fanned out as
    # its own claimable row points at its grid parent here and carries its
    # global pair index. NULL for every user-visible job — child rows are
    # scheduling internals, hidden from listings and deleted with the parent
    # via the cascading self-FK.
    parent_optimization_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("jobs.optimization_id", ondelete="CASCADE"), nullable=True, index=True
    )
    pair_index: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        Index("ix_jobs_status_created_at", "status", "created_at"),
        # "my optimizations, newest first" — the dominant list/sidebar query —
        # becomes a single backward index range scan instead of filter + sort.
        Index("ix_jobs_username_created_at", "username", "created_at"),
        Index("ix_jobs_lease_expires_at", "lease_expires_at"),
        # Lookup index for idempotency dedup; the corresponding PG-only
        # uniqueness guard (partial on idempotency_key IS NOT NULL) lives in
        # the alembic migration so two concurrent submits with the same key
        # cannot both create rows.
        Index("ix_jobs_username_idempotency_key", "username", "idempotency_key"),
    )


class ProgressEventModel(Base):
    """SQLAlchemy model for the job_progress_events table."""

    __tablename__ = "job_progress_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    optimization_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    event: Mapped[str | None] = mapped_column(String(255), nullable=True)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON_STORE, default=dict)

    __table_args__ = (Index("ix_job_progress_events_optimization_timestamp", "optimization_id", "timestamp"),)


class LogEntryModel(Base):
    """SQLAlchemy model for the job_logs table."""

    __tablename__ = "job_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    optimization_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    level: Mapped[str] = mapped_column(String(20), nullable=False)
    logger: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    pair_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Structured run-log fields, see core.run_log. NULL source is a row written
    # before they existed, which was always the host's.
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    event: Mapped[str | None] = mapped_column(String(255), nullable=True)
    fields: Mapped[dict[str, Any] | None] = mapped_column(JSON_STORE, nullable=True)
    candidate: Mapped[str | None] = mapped_column(String(64), nullable=True)
    case_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Backs the per-job ordered reads (get_logs), which otherwise scan on the
    # single-column optimization_id index and sort timestamps in memory.
    __table_args__ = (Index("ix_job_logs_optimization_timestamp", "optimization_id", "timestamp"),)


class GepaCheckpointModel(Base):
    """The latest GEPA optimizer state for one in-flight GEPA run.

    Holds the pickled ``gepa_state.bin`` the GEPA engine writes at every
    iteration, so a run that dies mid-optimization (provider timeout, stalled
    worker, pod crash, user cancel) can be resumed from its last completed
    iteration rather than restarting and re-spending the whole budget. Keyed by
    ``(optimization_id, pair_index)``: a single run uses the sentinel
    ``pair_index = -1`` (one row), while a grid search keeps one row per
    in-flight model pair (``pair_index`` 0..N-1) — each pair is its own GEPA run.
    A row is replaced in place on each iteration and deleted once that run/pair
    succeeds, so its ``stored_bytes`` (the blob length, folded into the owner's
    "optimizations" footprint) only counts while a resumable failure is pending.
    Deleted with its parent job via the cascading foreign key. Postgres stores
    ``data`` as BYTEA; the per-row whole-blob read/write is the only access
    pattern, so it lives apart from the hot ``jobs`` table.
    """

    __tablename__ = "gepa_checkpoints"

    optimization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("jobs.optimization_id", ondelete="CASCADE"), primary_key=True
    )
    pair_index: Mapped[int] = mapped_column(Integer, primary_key=True, default=-1)
    iteration: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    manifest: Mapped[dict[str, Any] | None] = mapped_column(JSON_STORE, nullable=True)
    stored_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class GridPairResultModel(Base):
    """One completed grid-search pair's result, persisted so resume can skip it.

    A grid search runs one GEPA optimization per model pair and keeps no partial
    results until the whole sweep finishes — so an interrupted grid would
    otherwise re-run every pair. Persisting each pair's :class:`PairResult` as it
    completes lets a resumed grid keep the finished pairs (``pair_index`` →
    result) and re-run only the rest. Keyed by ``(optimization_id, pair_index)``;
    ``stored_bytes`` folds into the owner's "optimizations" footprint and the
    rows are dropped once the grid succeeds (or with the parent job via the
    cascading foreign key).
    """

    __tablename__ = "grid_pair_results"

    optimization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("jobs.optimization_id", ondelete="CASCADE"), primary_key=True
    )
    pair_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSON_STORE, nullable=False)
    stored_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class BlackboxAgentRunModel(Base):
    """One sandboxed agent run of a black-box job: its answer, transcript and outcome.

    An agent target evaluates every ``(version, case)`` pair in its own
    sandbox. The worker writes the row when the run starts, appends the
    transcript as the box streams it, and rewrites the row with the answer
    and outcome when the run ends, so the run view can show what each
    version did on each case, live or after the fact. ``phase``/``trial``/
    ``example_id`` tie the row to the progress events that name the same
    run. ``stored_bytes`` folds into the owner's "optimizations" footprint;
    rows go with their job via the cascading foreign key.
    """

    __tablename__ = "blackbox_agent_runs"

    optimization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("jobs.optimization_id", ondelete="CASCADE"), primary_key=True
    )
    run_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phase: Mapped[str] = mapped_column(String(16), nullable=False)
    trial: Mapped[int | None] = mapped_column(Integer, nullable=True)
    example_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    case_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    label: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timed_out: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    elapsed_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    usage: Mapped[dict[str, Any]] = mapped_column(JSON_STORE, nullable=False, default=dict)
    check_result: Mapped[dict[str, Any] | None] = mapped_column(JSON_STORE, nullable=True)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript: Mapped[str] = mapped_column(Text, nullable=False, default="")
    stored_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class UserQuotaOverrideModel(Base):
    """SQLAlchemy model for live per-user quota overrides."""

    __tablename__ = "user_quota_overrides"

    username: Mapped[str] = mapped_column(String(255), primary_key=True)
    quota: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)


class UserQuotaAuditModel(Base):
    """SQLAlchemy model for quota administration audit events."""

    __tablename__ = "user_quota_audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    target_username: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    old_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)
    new_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class UserStorageQuotaOverrideModel(Base):
    """SQLAlchemy model for per-user storage-budget overrides.

    ``quota_bytes`` is a per-user ceiling in bytes that replaces the global
    ``settings.user_storage_quota_bytes`` default for this user. Uses
    ``BigInteger`` because an admin override may grant multi-gigabyte budgets
    that exceed the signed 32-bit range (2_147_483_647).
    """

    __tablename__ = "user_storage_quota_overrides"

    username: Mapped[str] = mapped_column(String(255), primary_key=True)
    quota_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)


class JobEmbeddingModel(Base):
    """Per-job embedding row backing the recommendation service.

    One row is written after a job finishes successfully. Three named
    aspects are embedded independently so a similarity search can
    weigh them separately (``summary`` = LLM-authored task description,
    ``code`` = signature + metric source, ``schema`` = dataset schema
    digest). All use the configured embedding API model,
    MRL-truncated to ``EMBEDDING_DIM``.

    Metadata (``optimization_type``, ``winning_model``, ``winning_rank``)
    is denormalized from ``jobs`` so the search can filter and rerank
    without an extra join per-candidate. ``updated_at`` advances on every
    refresh so dashboard caches can detect resumed jobs without replacing the
    row.
    """

    __tablename__ = "job_embeddings"

    optimization_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    optimization_type: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    winning_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    winning_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), index=True
    )
    embedding_summary: Mapped[Any] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    embedding_code: Mapped[Any] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    embedding_schema: Mapped[Any] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    is_recommendable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", index=True
    )
    is_private: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false", index=True)
    baseline_metric: Mapped[float | None] = mapped_column(Float, nullable=True)
    optimized_metric: Mapped[float | None] = mapped_column(Float, nullable=True)
    summary_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    signature_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    metric_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    optimizer_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    optimizer_kwargs: Mapped[dict[str, Any] | None] = mapped_column(JSON_STORE, nullable=True)
    module_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    task_name: Mapped[str | None] = mapped_column(String(255), nullable=True)


class AgentConversationModel(Base):
    """Persisted generalist-agent conversation header.

    One row per user-owned thread. ``title`` is auto-derived from the first
    user message and may be edited via PATCH. ``pinned`` and ``archived_at``
    are mutually independent — an archived row may still be pinned.
    """

    __tablename__ = "agent_conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Unguessable public read-only link token; NULL when the chat is not shared.
    share_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # When the current share link was made; NULL while the chat is not shared.
    shared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        Index("ix_agent_conversations_user_updated", "username", "updated_at"),
        Index("ix_agent_conversations_user_pinned", "username", "pinned"),
        Index("ix_agent_conversations_share_token", "share_token", unique=True),
    )


class AgentMessageModel(Base):
    """Single turn inside an :class:`AgentConversationModel`.

    ``tool_calls`` is the rendered ``AgentToolCall[]`` payload exactly as the
    frontend stores it in React state — kept as JSON rather than normalized
    so the renderer needs no migration when tool shapes change.
    """

    __tablename__ = "agent_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON_STORE, nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Five nullable training-metadata columns feed the training-ground harness
    # (training_ground_SPEC.md §4). Old rows predate the migration and stay
    # NULL; the optimize CLI filters them out via WHERE wizard_state_before IS NOT NULL.
    wizard_state_before: Mapped[dict[str, Any] | None] = mapped_column(JSON_STORE, nullable=True)
    wizard_state_after: Mapped[dict[str, Any] | None] = mapped_column(JSON_STORE, nullable=True)
    allowed_tools: Mapped[list[str] | None] = mapped_column(JSON_STORE, nullable=True)
    tool_schema_hashes: Mapped[dict[str, str] | None] = mapped_column(JSON_STORE, nullable=True)
    router_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON_STORE, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), index=True
    )

    __table_args__ = (Index("ix_agent_messages_conv_created", "conversation_id", "created_at"),)


class ConversationEmbeddingModel(Base):
    """Per-conversation embedding row backing the agent-history search.

    Mirrors :class:`JobEmbeddingModel` so the search dispatch and the
    backfill / purge plumbing can be lifted from the optimization corpus
    with a different source table. The ``summary_text`` column holds the
    exact prose that was embedded — concatenated user turns (and a slice
    of assistant replies) capped to a budget — so lexical fallback can hit
    the same text the vector was built from.
    """

    __tablename__ = "conversation_embeddings"

    conversation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_conversations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    username: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    embedding_summary: Mapped[Any] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    summary_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Snapshot of ``conversation.updated_at`` at embed time. Used by the
    # backfill sweep to detect stale rows (conversation got new turns after
    # the last embed) without diffing message content.
    embedded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


# Holds rows the wizard parsed in the browser so the generalist agent can
# submit ``/run`` without re-shipping the dataset through its context. Rows
# live here only between upload and submit; the frontend stages on upload,
# the /run handler dereferences by id, and stale rows are evicted by user/age.
class AgentStagedDatasetModel(Base):
    """Server-side cache of wizard dataset rows for agent-driven submits."""

    __tablename__ = "agent_staged_datasets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    dataset_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    rows: Mapped[list[dict[str, Any]]] = mapped_column(JSON_STORE, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), index=True
    )

    __table_args__ = (Index("ix_agent_staged_datasets_user_created", "username", "created_at"),)


class TaggingSessionModel(Base):
    """Persisted text-labeling (tagger) session — one row per saved session.

    Mirrors the in-memory ``useTagger`` hook so a user can resume annotating
    across refreshes and devices, the way optimizations persist. ``config``,
    ``columns`` and ``data`` are captured once when annotating begins and are
    immutable thereafter; ``annotations``, ``current_index`` and ``phase`` (with
    the denormalized ``tagged_count``) advance as rows are labeled and are the
    only columns the autosave PUT rewrites, so the large ``data`` TOAST is not
    re-written on every keystroke. ``row_count``/``tagged_count`` are
    denormalized so the sidebar list never loads the heavy JSON columns.
    Ownership is by ``username`` (compared in application code, not a DB FK).
    """

    __tablename__ = "tagging_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    phase: Mapped[str] = mapped_column(String(20), nullable=False, default="annotating")
    config: Mapped[dict[str, Any]] = mapped_column(JSON_STORE, nullable=False, default=dict)
    columns: Mapped[list[str]] = mapped_column(JSON_STORE, nullable=False, default=list)
    data: Mapped[list[dict[str, Any]]] = mapped_column(JSON_STORE, nullable=False, default=list)
    annotations: Mapped[dict[str, Any]] = mapped_column(JSON_STORE, nullable=False, default=dict)
    # AI co-tagging state (assist mode, interview, rubric, predictions with
    # provenance/confidence, review rounds, auto-tag job bookkeeping). NULL for
    # plain manual sessions; ``annotations`` stays the single source of truth
    # for final labels regardless of who produced them.
    assist: Mapped[dict[str, Any] | None] = mapped_column(JSON_STORE, nullable=True, default=None)
    current_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tagged_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        Index("ix_tagging_sessions_user_updated", "username", "updated_at"),
        Index("ix_tagging_sessions_user_pinned", "username", "pinned"),
    )


class DatasetModel(Base):
    """One saved dataset in a user's personal library — a single file reference.

    Metadata only: the row bytes live in the one-to-one :class:`DatasetBlobModel`
    so this hot table stays narrow (a JSONB-inlined dataset would TOAST and slow
    every list query). ``byte_size`` is the uncompressed logical size shown to
    the user; ``stored_bytes`` is the compressed size that counts against the
    per-user quota. ``content_hash`` is the SHA-256 of the canonical uncompressed
    bytes — indexed with ``owner_username`` so a re-save of identical bytes can
    dedupe instead of storing a second copy. ``column_schema`` carries the saved
    column roles/kinds/order so picking the dataset in the submit wizard
    pre-fills the column step. ``source`` records the producing surface
    (``'upload'`` / ``'tagger'`` / ``'optimization'``).
    """

    __tablename__ = "datasets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_username: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="upload")
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    column_count: Mapped[int] = mapped_column(Integer, nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    stored_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    column_schema: Mapped[dict[str, Any]] = mapped_column(JSON_STORE, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (Index("ix_datasets_owner_content_hash", "owner_username", "content_hash"),)


class DatasetBlobModel(Base):
    """The row bytes for one :class:`DatasetModel`, stored compressed and apart.

    Split from the metadata table so the whole-file read pattern avoids the
    random-access penalty TOAST imposes on a large JSONB column, and so the
    bytes can later move behind an object-store seam without touching the
    metadata schema. ``data`` holds the ``compression``-encoded serialization of
    the rows in ``content_type`` (``'csv'`` / ``'json'``). Deleted with its
    parent dataset via the cascading foreign key.
    """

    __tablename__ = "dataset_blobs"

    dataset_id: Mapped[str] = mapped_column(String(36), ForeignKey("datasets.id", ondelete="CASCADE"), primary_key=True)
    content_type: Mapped[str] = mapped_column(String(16), nullable=False)
    compression: Mapped[str] = mapped_column(String(16), nullable=False)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class DatasetShareLinkModel(Base):
    """Per-dataset sharing config keyed by a public link token.

    Mirrors :class:`OptimizationShareLinkModel` for the dataset library: the
    ``token`` is the unguessable capability embedded in the public
    ``/datasets/share/<token>`` URL, stored in plaintext because it IS the
    public identifier, not a credential hash. The active (``revoked_at IS
    NULL``) row per dataset holds the config. ``general_access`` is
    ``'restricted'`` (owner + invited members only) or ``'anyone'`` (anyone
    holding the link). ``general_role`` is the tier an ``'anyone'`` link grants
    a signed-in visitor — ``'viewer'`` or ``'editor'`` (never ``'owner'``).
    Unlike the optimization variant, the ``dataset_id`` foreign key cascades, so
    deleting a dataset removes its link rows automatically.
    """

    __tablename__ = "dataset_share_links"

    token: Mapped[str] = mapped_column(String(48), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("datasets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    general_access: Mapped[str] = mapped_column(
        String(16), nullable=False, default="restricted", server_default="restricted"
    )
    general_role: Mapped[str] = mapped_column(String(16), nullable=False, default="viewer", server_default="viewer")


class DatasetShareGrantModel(Base):
    """A single per-user access grant on a shared library dataset.

    Mirrors :class:`OptimizationShareGrantModel`: each row invites one
    ``grantee_username`` to a dataset with a tier ``role`` (``'viewer'`` /
    ``'editor'`` / ``'owner'``). The pair ``(dataset_id, grantee_username)`` is
    the primary key, so re-inviting a user replaces their grant. The
    ``dataset_id`` foreign key cascades, removing grants when the dataset is
    deleted.
    """

    __tablename__ = "dataset_share_grants"

    dataset_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("datasets.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )
    grantee_username: Mapped[str] = mapped_column(String(255), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    # The composite PK leads with dataset_id, so "list everything shared with
    # this user" (filtering on grantee_username alone) could not use it.
    __table_args__ = (Index("ix_dataset_share_grants_grantee", "grantee_username"),)


class TaggingSessionShareLinkModel(Base):
    """Per-session sharing config keyed by a public link token.

    Mirrors :class:`DatasetShareLinkModel` for saved tagger sessions: the
    ``token`` is the unguessable capability embedded in the public
    ``/tagger/share/<token>`` URL, stored in plaintext because it IS the public
    identifier, not a credential hash. The active (``revoked_at IS NULL``) row
    per session holds the config; ``general_access`` and ``general_role`` carry
    the same semantics as the dataset variant. The ``session_id`` foreign key
    cascades, so deleting a session removes its link rows automatically.
    """

    __tablename__ = "tagging_session_share_links"

    token: Mapped[str] = mapped_column(String(48), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tagging_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    general_access: Mapped[str] = mapped_column(
        String(16), nullable=False, default="restricted", server_default="restricted"
    )
    general_role: Mapped[str] = mapped_column(String(16), nullable=False, default="viewer", server_default="viewer")


class TaggingSessionShareGrantModel(Base):
    """A single per-user access grant on a shared tagger session.

    Mirrors :class:`DatasetShareGrantModel`: each row invites one
    ``grantee_username`` to a session with a tier ``role`` (``'viewer'`` /
    ``'editor'`` / ``'owner'``). The pair ``(session_id, grantee_username)`` is
    the primary key, so re-inviting a user replaces their grant. The
    ``session_id`` foreign key cascades, removing grants when the session is
    deleted.
    """

    __tablename__ = "tagging_session_share_grants"

    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tagging_sessions.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )
    grantee_username: Mapped[str] = mapped_column(String(255), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    # The composite PK leads with session_id, so "list everything shared with
    # this user" (filtering on grantee_username alone) could not use it.
    __table_args__ = (Index("ix_tagging_session_share_grants_grantee", "grantee_username"),)


class RunFolderModel(Base):
    """A user-owned folder that groups optimizations in the sidebar.

    Folders follow Google Drive's "My Drive" model: one owner, an optional
    ``parent_id`` for nesting, and access that subfolders and runs filed inside
    inherit from every ancestor. ``editors_can_share`` mirrors Drive's
    ``writersCanShare``: when False only the owner may change who has access.
    Deleting a folder deletes its subfolders but never the runs filed in them.
    """

    __tablename__ = "run_folders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    owner_username: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    editors_can_share: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class RunFolderShareLinkModel(Base):
    """Per-folder link-sharing config keyed by a public link token.

    Same semantics as :class:`DatasetShareLinkModel`; rows are removed with the
    folder.
    """

    __tablename__ = "run_folder_share_links"

    token: Mapped[str] = mapped_column(String(48), primary_key=True)
    folder_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    general_access: Mapped[str] = mapped_column(
        String(16), nullable=False, default="restricted", server_default="restricted"
    )
    general_role: Mapped[str] = mapped_column(String(16), nullable=False, default="viewer", server_default="viewer")


class RunFolderShareGrantModel(Base):
    """A single per-user access grant on a run folder.

    Mirrors :class:`DatasetShareGrantModel`. A grant reaches every subfolder and
    run beneath the folder; rows are removed with the folder.
    """

    __tablename__ = "run_folder_share_grants"

    folder_id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    grantee_username: Mapped[str] = mapped_column(String(255), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    # The composite PK leads with folder_id, so "list everything shared with
    # this user" (filtering on grantee_username alone) could not use it.
    __table_args__ = (Index("ix_run_folder_share_grants_grantee", "grantee_username"),)


class RunFolderItemModel(Base):
    """Files one optimization into one folder.

    ``optimization_id`` is the primary key, which enforces Drive's single-parent
    rule: a run lives in at most one folder.
    """

    __tablename__ = "run_folder_items"

    optimization_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    folder_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    added_by: Mapped[str] = mapped_column(String(255), nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class ContinuousPlanModel(Base):
    """A schedule that keeps one successful run improving with fresh, linked runs.

    ``source_optimization_id`` is the run the plan was enabled from and
    ``incumbent_optimization_id`` the version the customer approved last.
    ``permitted`` names the inputs that may refresh without a setup change, and
    ``baseline_snapshot`` holds the inputs of the last reviewed cycle, so a check
    with no permitted change records a skip instead of a run. ``pause_reason``
    keeps a customer's pause apart from an allowance or funds pause, because a
    monthly reset may lift only the latter. ``schedule_rule`` is the repeat rule
    (``core.worker.continuous_schedule.ScheduleRule``); ``schedule`` keeps its
    frequency name, and plans saved before rules existed have only that.
    """

    __tablename__ = "continuous_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    username: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    source_optimization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    incumbent_optimization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    pause_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    schedule: Mapped[str] = mapped_column(String(16), nullable=False)
    schedule_rule: Mapped[dict[str, Any] | None] = mapped_column(JSON_STORE, nullable=True)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    permitted: Mapped[dict[str, Any]] = mapped_column(JSON_STORE, nullable=False, default=dict)
    source: Mapped[dict[str, Any] | None] = mapped_column(JSON_STORE, nullable=True)
    pending_inputs: Mapped[dict[str, Any]] = mapped_column(JSON_STORE, nullable=False, default=dict)
    baseline_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_STORE, nullable=False, default=dict)
    monthly_limit_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cycle_budget_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (UniqueConstraint("source_optimization_id", name="uq_continuous_plans_source_optimization_id"),)


class ContinuousCycleModel(Base):
    """One scheduled check of a continuous plan and the fresh run it started, if any.

    ``slot`` is the scheduled time the check claimed. The unique
    ``(plan_id, slot)`` pair is what stops overlapping checks, retries and
    several workers from recording the same check twice. ``month`` is the
    ``YYYY-MM`` allowance the cycle's spend counts against, kept after the
    month rolls over.
    """

    __tablename__ = "continuous_cycles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    plan_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    slot: Mapped[str] = mapped_column(String(40), nullable=False)
    month: Mapped[str] = mapped_column(String(7), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    changes: Mapped[list[str]] = mapped_column(JSON_STORE, nullable=False, default=list)
    detail_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail_params: Mapped[dict[str, Any]] = mapped_column(JSON_STORE, nullable=False, default=dict)
    snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON_STORE, nullable=True)
    optimization_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    execution_budget_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    incumbent_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    candidate_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    spent_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (UniqueConstraint("plan_id", "slot", name="uq_continuous_cycles_plan_slot"),)
