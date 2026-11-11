"""Gather-and-purge logic behind self-service data export and account deletion.

Both operations key entirely on the caller's ``username`` — the lowercased
email every table owns rows by. There are no database foreign keys pointing at
the ``users`` row (OAuth accounts have no such row yet still own work), so
deletion cannot lean on a cascade from a single identity row: every owned table
is cleared explicitly here, children before parents so the Postgres foreign
keys inside each object tree stay satisfied.

Two deliberate exceptions to "hard delete everything":

* Financial records (``billing_customers``, ``wallet_ledger``) and the quota
  audit trail are *anonymized*, not deleted — their retention has an accounting
  and audit basis, so the identity link is severed to a stable, PII-free
  tombstone token while the rows themselves survive.
  The wallet (``billing_customers``) is also *closed*: its spendable balance
  and free grant go to zero, and a ``closure`` ledger row records the
  forfeited balance.
* Product telemetry keeps its aggregate rows but drops the ``username`` that
  tied them to a person, leaving only the already-anonymous per-browser id.

Nothing here makes a live call to Stripe, OpenRouter, or any other provider;
severing the local link is the whole of the operation. Deprovisioning the
upstream Stripe customer or OpenRouter runtime key is left to a follow-up.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from ..notifications.preferences import settings_from_row
from ..storage.models import (
    AgentConversationModel,
    AgentEnginePreferenceModel,
    AgentMessageModel,
    AgentStagedDatasetModel,
    ApiTokenModel,
    BillingCustomerModel,
    BillingOpenRouterKeyModel,
    BillingProviderKeyModel,
    ConversationEmbeddingModel,
    DatasetBlobModel,
    DatasetModel,
    DatasetShareGrantModel,
    DatasetShareLinkModel,
    DesktopLinkCodeModel,
    DesktopLinkModel,
    DesktopTokenModel,
    GepaCheckpointModel,
    GridPairResultModel,
    JobEmbeddingModel,
    JobModel,
    LogEntryModel,
    McpAuthCodeModel,
    McpGrantModel,
    McpTokenModel,
    ModelPrivacyPreferenceModel,
    NotificationModel,
    NotificationPreferenceModel,
    OptimizationShareGrantModel,
    OptimizationShareLinkModel,
    PackageRegistryPreferenceModel,
    ProgressEventModel,
    PushSubscriptionModel,
    SetupDraftModel,
    SlackConnectionModel,
    TaggingSessionModel,
    TaggingSessionShareGrantModel,
    TaggingSessionShareLinkModel,
    TelemetryEventModel,
    TwoFactorEmailCodeModel,
    UserConnectorModel,
    UserIdentityModel,
    UserModel,
    UserQuotaAuditModel,
    UserQuotaOverrideModel,
    UserStorageQuotaOverrideModel,
    WalletLedgerModel,
    WebAuthnChallengeModel,
    WebAuthnCredentialModel,
)

EXPORT_FORMAT = "skynet-account-export/v2"


@dataclass
class AccountDeletionSummary:
    """Row-count outcome of an account deletion.

    Attributes:
        deleted_rows: Rows hard-deleted across every table the account owned.
        anonymized_rows: Retained rows whose identity link was severed
            (financial, audit, and telemetry records).
    """

    deleted_rows: int
    anonymized_rows: int


def _iso(value: datetime | None) -> str | None:
    """Render a datetime as an ISO-8601 string, passing ``None`` through.

    Args:
        value: A timezone-aware datetime, or ``None``.

    Returns:
        The ISO-8601 rendering, or ``None`` when the input was ``None``.
    """
    return value.isoformat() if value is not None else None


def _anonymized_username(username: str) -> str:
    """Derive the stable, PII-free tombstone that replaces a deleted identity.

    The token is a truncated SHA-256 of the username, so the same person maps
    to the same token across every retained table (keeping the financial trail
    internally joinable) while carrying no recoverable email.

    Args:
        username: The account's lowercased-email identity.

    Returns:
        A ``deleted-user-<hash>`` token with no personal data.
    """
    digest = hashlib.sha256(username.encode("utf-8")).hexdigest()[:16]
    return f"deleted-user-{digest}"


def export_account(session: Session, username: str) -> dict[str, Any]:
    """Collect every record owned by ``username`` into a JSON-safe bundle.

    Secrets are never included — the password hash, TOTP/recovery secrets,
    encrypted provider keys, and token hashes are all withheld; only the
    non-sensitive fact that each exists (a masked tail, a boolean, a count) is
    reported. Bulk payloads that are recoverable through their own download
    surface (raw run payloads, raw dataset bytes, raw tagger source rows) are
    summarized rather than inlined to keep the bundle bounded.

    Args:
        session: An open session on the store's engine.
        username: The caller's lowercased-email identity.

    Returns:
        A JSON-serializable dict describing the account and all data it owns.
    """
    user = session.get(UserModel, username)
    account: dict[str, Any] | None = None
    if user is not None:
        recovery_codes = json.loads(user.recovery_codes) if user.recovery_codes else []
        account = {
            "email": user.email,
            "name": user.name,
            "created_at": _iso(user.created_at),
            "last_login_at": _iso(user.last_login_at),
            "use_case": user.use_case,
            "experience_level": user.experience_level,
            "job_role": user.job_role,
            "intake_completed_at": _iso(user.intake_completed_at),
            "intake_profile": user.intake_profile,
            "two_factor": {
                "totp_enabled": user.totp_secret is not None,
                "email_codes_enabled": bool(user.email_2fa_enabled),
                "recovery_codes_remaining": len(recovery_codes),
            },
        }

    registry_row = session.get(PackageRegistryPreferenceModel, username)
    privacy_row = session.get(ModelPrivacyPreferenceModel, username)
    engine_row = session.get(AgentEnginePreferenceModel, username)
    notification_row = session.get(NotificationPreferenceModel, username)
    notification_settings = settings_from_row(notification_row)
    notification_preferences = {
        "grid": notification_settings.grid,
        "hold_minutes": notification_settings.hold_minutes,
        "held_channels": list(notification_settings.held_channels),
        "stuck_fraction": notification_settings.stuck_fraction,
        "budget_alert_fraction": notification_settings.budget_alert_fraction,
    }

    jobs = session.scalars(select(JobModel).where(JobModel.username == username).order_by(JobModel.created_at)).all()
    optimizations = [
        {
            "optimization_id": job.optimization_id,
            "status": job.status,
            "optimization_type": job.optimization_type,
            "created_at": _iso(job.created_at),
            "started_at": _iso(job.started_at),
            "completed_at": _iso(job.completed_at),
            "latest_metrics": job.latest_metrics,
            "payload_overview": job.payload_overview,
            "result": job.result,
        }
        for job in jobs
    ]

    datasets = session.scalars(
        select(DatasetModel).where(DatasetModel.owner_username == username).order_by(DatasetModel.created_at)
    ).all()
    datasets_out = [
        {
            "id": dataset.id,
            "name": dataset.name,
            "source": dataset.source,
            "row_count": dataset.row_count,
            "column_count": dataset.column_count,
            "byte_size": dataset.byte_size,
            "column_schema": dataset.column_schema,
            "created_at": _iso(dataset.created_at),
        }
        for dataset in datasets
    ]

    tagging_sessions = session.scalars(
        select(TaggingSessionModel)
        .where(TaggingSessionModel.username == username)
        .order_by(TaggingSessionModel.created_at)
    ).all()
    tagging_out = [
        {
            "id": tagging.id,
            "name": tagging.name,
            "phase": tagging.phase,
            "row_count": tagging.row_count,
            "tagged_count": tagging.tagged_count,
            "config": tagging.config,
            "annotations": tagging.annotations,
            "created_at": _iso(tagging.created_at),
            "updated_at": _iso(tagging.updated_at),
        }
        for tagging in tagging_sessions
    ]

    conversations = session.scalars(
        select(AgentConversationModel)
        .where(AgentConversationModel.username == username)
        .order_by(AgentConversationModel.created_at)
    ).all()
    conversations_out = []
    for conversation in conversations:
        messages = session.scalars(
            select(AgentMessageModel)
            .where(AgentMessageModel.conversation_id == conversation.id)
            .order_by(AgentMessageModel.created_at)
        ).all()
        conversations_out.append(
            {
                "id": conversation.id,
                "title": conversation.title,
                "created_at": _iso(conversation.created_at),
                "updated_at": _iso(conversation.updated_at),
                "messages": [
                    {
                        "role": message.role,
                        "content": message.content,
                        "model": message.model,
                        "created_at": _iso(message.created_at),
                    }
                    for message in messages
                ],
            }
        )

    customer = session.get(BillingCustomerModel, username)
    ledger = session.scalars(
        select(WalletLedgerModel).where(WalletLedgerModel.username == username).order_by(WalletLedgerModel.created_at)
    ).all()
    billing = {
        "balance_cents": int(customer.balance_cents) if customer is not None else 0,
        "grant_remaining": (
            int(customer.grant_remaining) if customer is not None and customer.grant_remaining is not None else None
        ),
        "ledger": [
            {
                "delta_cents": int(entry.delta_cents),
                "kind": entry.kind,
                "description": entry.description,
                "model": entry.model,
                "created_at": _iso(entry.created_at),
            }
            for entry in ledger
        ],
    }

    token = session.get(ApiTokenModel, username)
    api_token = (
        {
            "last4": token.last4,
            "created_at": _iso(token.created_at),
            "last_used_at": _iso(token.last_used_at),
        }
        if token is not None
        else None
    )

    desktop_link_rows = session.scalars(
        select(DesktopLinkModel).where(DesktopLinkModel.username == username).order_by(DesktopLinkModel.created_at)
    ).all()
    desktop_links = [
        {
            "id": link.id,
            "device_name": link.device_name,
            "scopes": link.scopes.split(),
            "created_at": _iso(link.created_at),
            "last_used_at": _iso(link.last_used_at),
            "revoked_at": _iso(link.revoked_at),
            "absolute_expires_at": _iso(link.absolute_expires_at),
        }
        for link in desktop_link_rows
    ]

    provider_keys = session.scalars(
        select(BillingProviderKeyModel).where(BillingProviderKeyModel.username == username)
    ).all()
    provider_keys_out = [
        {
            "provider": key.provider,
            "label": key.label,
            "last4": key.last4,
            "status": key.status,
            "created_at": _iso(key.created_at),
        }
        for key in provider_keys
    ]

    passkeys = session.scalars(
        select(WebAuthnCredentialModel).where(WebAuthnCredentialModel.user_email == username)
    ).all()
    passkeys_out = [
        {
            "nickname": passkey.nickname,
            "created_at": _iso(passkey.created_at),
            "last_used_at": _iso(passkey.last_used_at),
        }
        for passkey in passkeys
    ]

    identities = session.scalars(
        select(UserIdentityModel).where(UserIdentityModel.username == username).order_by(UserIdentityModel.created_at)
    ).all()
    sign_in_methods = [
        {
            "provider": identity.provider,
            "provider_email": identity.provider_email,
            "created_at": _iso(identity.created_at),
        }
        for identity in identities
    ]

    notifications = session.scalars(
        select(NotificationModel).where(NotificationModel.username == username).order_by(NotificationModel.created_at)
    ).all()
    notifications_out = [
        {
            "event_type": item.event_type,
            "optimization_id": item.optimization_id,
            "title": item.title,
            "facts": item.facts,
            "link": item.link,
            "in_inbox": item.in_inbox,
            "created_at": _iso(item.created_at),
            "read_at": _iso(item.read_at),
            "deliveries": item.deliveries,
        }
        for item in notifications
    ]

    # The endpoint URL is a bearer capability for pushing to the browser, so only its host is reported.
    push_rows = session.scalars(
        select(PushSubscriptionModel)
        .where(PushSubscriptionModel.username == username)
        .order_by(PushSubscriptionModel.created_at)
    ).all()
    push_subscriptions = [
        {
            "push_service": urlsplit(sub.endpoint).hostname,
            "user_agent": sub.user_agent,
            "created_at": _iso(sub.created_at),
            "last_used_at": _iso(sub.last_used_at),
        }
        for sub in push_rows
    ]

    slack_row = session.get(SlackConnectionModel, username)
    slack = (
        {
            "team_name": slack_row.team_name,
            "channel_name": slack_row.channel_name,
            "status": slack_row.status,
            "created_at": _iso(slack_row.created_at),
            "updated_at": _iso(slack_row.updated_at),
        }
        if slack_row is not None
        else None
    )

    connector_rows = session.scalars(
        select(UserConnectorModel)
        .where(UserConnectorModel.username == username)
        .order_by(UserConnectorModel.created_at)
    ).all()
    connectors = [
        {
            "provider": connector.provider,
            "auth_method": connector.auth_method,
            "account_label": connector.account_label,
            "scopes": connector.scopes,
            "status": connector.status,
            "created_at": _iso(connector.created_at),
            "updated_at": _iso(connector.updated_at),
        }
        for connector in connector_rows
    ]

    draft_row = session.get(SetupDraftModel, username)
    setup_draft = (
        None
        if draft_row is None or draft_row.deleted
        else {
            "left_at": draft_row.left_at,
            "values": draft_row.draft_values,
            "chat": draft_row.chat,
            "staged_dataset_id": draft_row.staged_dataset_id,
            "revision": draft_row.revision,
            "updated_at": _iso(draft_row.updated_at),
        }
    )

    return {
        "export_format": EXPORT_FORMAT,
        "exported_at": _iso(datetime.now(UTC)),
        "username": username,
        "account": account,
        "optimizations": optimizations,
        "datasets": datasets_out,
        "tagging_sessions": tagging_out,
        "agent_conversations": conversations_out,
        "setup_draft": setup_draft,
        "billing": billing,
        "api_token": api_token,
        "desktop_links": desktop_links,
        "provider_keys": provider_keys_out,
        "passkeys": passkeys_out,
        "sign_in_methods": sign_in_methods,
        "connectors": connectors,
        "notifications": notifications_out,
        "push_subscriptions": push_subscriptions,
        "slack": slack,
        "notification_preferences": notification_preferences,
        "model_privacy": {"data_policy": privacy_row.data_policy if privacy_row is not None else "deny"},
        "agent_engine": {"engine": engine_row.engine if engine_row is not None else None},
        "package_registry": {"index_url": registry_row.index_url if registry_row else "https://pypi.org/simple"},
    }


def delete_account(session: Session, username: str) -> AccountDeletionSummary:
    """Purge everything owned by ``username`` within the given session.

    Runs entirely inside the caller's transaction (the caller commits), so a
    failure part-way rolls the whole deletion back. Owned content and secrets
    are hard-deleted; financial, audit, and telemetry rows are anonymized in
    place. See the module docstring for why those three survive.

    Args:
        session: An open session on the store's engine; the caller commits.
        username: The caller's lowercased-email identity.

    Returns:
        Counts of hard-deleted and anonymized rows.
    """
    deleted = 0
    anonymized = 0

    def _run_delete(statement: Any) -> None:
        """Delete owned rows and accumulate their count.

        Args:
            statement: Account-scoped SQL deletion.
        """
        nonlocal deleted
        result = session.execute(statement, execution_options={"synchronize_session": False})
        deleted += result.rowcount or 0

    def _run_anonymize(statement: Any) -> None:
        """Anonymize retained rows and accumulate their count.

        Args:
            statement: Account-scoped SQL update.
        """
        nonlocal anonymized
        result = session.execute(statement, execution_options={"synchronize_session": False})
        anonymized += result.rowcount or 0

    owned_job_ids = list(session.scalars(select(JobModel.optimization_id).where(JobModel.username == username)))
    child_job_ids = (
        list(
            session.scalars(select(JobModel.optimization_id).where(JobModel.parent_optimization_id.in_(owned_job_ids)))
        )
        if owned_job_ids
        else []
    )
    job_ids = list({*owned_job_ids, *child_job_ids})
    if job_ids:
        _run_delete(delete(ProgressEventModel).where(ProgressEventModel.optimization_id.in_(job_ids)))
        _run_delete(delete(LogEntryModel).where(LogEntryModel.optimization_id.in_(job_ids)))
        _run_delete(delete(GepaCheckpointModel).where(GepaCheckpointModel.optimization_id.in_(job_ids)))
        _run_delete(delete(GridPairResultModel).where(GridPairResultModel.optimization_id.in_(job_ids)))
        _run_delete(delete(OptimizationShareGrantModel).where(OptimizationShareGrantModel.optimization_id.in_(job_ids)))
        _run_delete(delete(OptimizationShareLinkModel).where(OptimizationShareLinkModel.optimization_id.in_(job_ids)))
        _run_delete(delete(JobModel).where(JobModel.optimization_id.in_(job_ids)))
    _run_delete(delete(JobEmbeddingModel).where(JobEmbeddingModel.user_id == username))
    _run_delete(delete(OptimizationShareLinkModel).where(OptimizationShareLinkModel.created_by == username))
    _run_delete(delete(OptimizationShareGrantModel).where(OptimizationShareGrantModel.grantee_username == username))
    _run_delete(delete(OptimizationShareGrantModel).where(OptimizationShareGrantModel.created_by == username))

    dataset_ids = list(session.scalars(select(DatasetModel.id).where(DatasetModel.owner_username == username)))
    if dataset_ids:
        _run_delete(delete(DatasetBlobModel).where(DatasetBlobModel.dataset_id.in_(dataset_ids)))
        _run_delete(delete(DatasetShareLinkModel).where(DatasetShareLinkModel.dataset_id.in_(dataset_ids)))
        _run_delete(delete(DatasetShareGrantModel).where(DatasetShareGrantModel.dataset_id.in_(dataset_ids)))
        _run_delete(delete(DatasetModel).where(DatasetModel.id.in_(dataset_ids)))
    _run_delete(delete(DatasetShareLinkModel).where(DatasetShareLinkModel.created_by == username))
    _run_delete(delete(DatasetShareGrantModel).where(DatasetShareGrantModel.grantee_username == username))
    _run_delete(delete(DatasetShareGrantModel).where(DatasetShareGrantModel.created_by == username))

    session_ids = list(session.scalars(select(TaggingSessionModel.id).where(TaggingSessionModel.username == username)))
    if session_ids:
        _run_delete(
            delete(TaggingSessionShareLinkModel).where(TaggingSessionShareLinkModel.session_id.in_(session_ids))
        )
        _run_delete(
            delete(TaggingSessionShareGrantModel).where(TaggingSessionShareGrantModel.session_id.in_(session_ids))
        )
        _run_delete(delete(TaggingSessionModel).where(TaggingSessionModel.id.in_(session_ids)))
    _run_delete(delete(TaggingSessionShareLinkModel).where(TaggingSessionShareLinkModel.created_by == username))
    _run_delete(delete(TaggingSessionShareGrantModel).where(TaggingSessionShareGrantModel.grantee_username == username))
    _run_delete(delete(TaggingSessionShareGrantModel).where(TaggingSessionShareGrantModel.created_by == username))

    conversation_ids = list(
        session.scalars(select(AgentConversationModel.id).where(AgentConversationModel.username == username))
    )
    if conversation_ids:
        _run_delete(delete(AgentMessageModel).where(AgentMessageModel.conversation_id.in_(conversation_ids)))
    _run_delete(delete(ConversationEmbeddingModel).where(ConversationEmbeddingModel.username == username))
    _run_delete(delete(AgentConversationModel).where(AgentConversationModel.username == username))

    # Passkey sign-in writes its passkey row, then the user row, so passkey
    # rows go before the user lock. Email-code sign-in writes the user row,
    # then deletes its code, so codes go after it. Either other order
    # deadlocks with a concurrent sign-in (R782F-1, R782H-1).
    _run_delete(delete(WebAuthnCredentialModel).where(WebAuthnCredentialModel.user_email == username))
    _run_delete(delete(WebAuthnChallengeModel).where(WebAuthnChallengeModel.user_email == username))
    # Authorize and the code exchange lock this row before they write, so
    # they wait for the deletion and then find no account (R782E-1). An
    # API-token create takes a share lock on it, so it either commits before
    # the token sweep below or finds no account and adds no token (SKY-150).
    session.execute(select(UserModel.email).where(UserModel.email == username).with_for_update())
    # A first provider sign-in adds its identity row and writes the user row
    # in one transaction, so identities go after the user lock. Before it, a
    # sign-in could add an identity after this delete and outlive the
    # account (R782I-1).
    _run_delete(delete(UserIdentityModel).where(UserIdentityModel.username == username))
    _run_delete(delete(ApiTokenModel).where(ApiTokenModel.username == username))
    _run_delete(delete(TwoFactorEmailCodeModel).where(TwoFactorEmailCodeModel.email == username))
    # Codes, then links, then tokens. An exchange that got the user lock
    # first commits before this runs, so the link delete sees its link
    # (R782D-1). Links before tokens: a desktop refresh locks the link, then
    # the token, and the opposite order deadlocks (R782C-1).
    _run_delete(delete(DesktopLinkCodeModel).where(DesktopLinkCodeModel.username == username))
    desktop_link_ids = list(
        session.scalars(
            delete(DesktopLinkModel).where(DesktopLinkModel.username == username).returning(DesktopLinkModel.id),
            execution_options={"synchronize_session": False},
        )
    )
    deleted += len(desktop_link_ids)
    if desktop_link_ids:
        _run_delete(delete(DesktopTokenModel).where(DesktopTokenModel.link_id.in_(desktop_link_ids)))
    # Same order as the desktop rows: an MCP refresh locks the grant, then the token.
    _run_delete(delete(McpAuthCodeModel).where(McpAuthCodeModel.username == username))
    mcp_grant_ids = list(
        session.scalars(
            delete(McpGrantModel).where(McpGrantModel.username == username).returning(McpGrantModel.id),
            execution_options={"synchronize_session": False},
        )
    )
    deleted += len(mcp_grant_ids)
    if mcp_grant_ids:
        _run_delete(delete(McpTokenModel).where(McpTokenModel.grant_id.in_(mcp_grant_ids)))
    _run_delete(delete(BillingProviderKeyModel).where(BillingProviderKeyModel.username == username))
    _run_delete(delete(BillingOpenRouterKeyModel).where(BillingOpenRouterKeyModel.username == username))
    _run_delete(delete(UserQuotaOverrideModel).where(UserQuotaOverrideModel.username == username))
    _run_delete(delete(UserStorageQuotaOverrideModel).where(UserStorageQuotaOverrideModel.username == username))
    _run_delete(delete(AgentStagedDatasetModel).where(AgentStagedDatasetModel.username == username))
    _run_delete(delete(SetupDraftModel).where(SetupDraftModel.username == username))
    _run_delete(delete(NotificationPreferenceModel).where(NotificationPreferenceModel.username == username))
    _run_delete(delete(NotificationModel).where(NotificationModel.username == username))
    _run_delete(delete(PushSubscriptionModel).where(PushSubscriptionModel.username == username))
    _run_delete(delete(SlackConnectionModel).where(SlackConnectionModel.username == username))
    _run_delete(delete(UserConnectorModel).where(UserConnectorModel.username == username))
    _run_delete(delete(ModelPrivacyPreferenceModel).where(ModelPrivacyPreferenceModel.username == username))
    _run_delete(delete(AgentEnginePreferenceModel).where(AgentEnginePreferenceModel.username == username))

    _run_delete(delete(PackageRegistryPreferenceModel).where(PackageRegistryPreferenceModel.username == username))

    tombstone = _anonymized_username(username)
    _run_anonymize(update(TelemetryEventModel).where(TelemetryEventModel.username == username).values(username=None))
    # SKY-567: close the wallet instead of deleting it. The row keeps its
    # Stripe customer id so a later refund or dispute webhook still finds it.
    # A zero balance and grant keep it out of the outstanding liability, and
    # the closure ledger row records the forfeited balance for audit.
    forfeited = session.scalar(
        select(BillingCustomerModel.balance_cents).where(BillingCustomerModel.username == username).with_for_update()
    )
    if forfeited:
        session.add(
            WalletLedgerModel(
                username=tombstone,
                delta_cents=-int(forfeited),
                kind="closure",
                description="Account deleted",
                created_at=datetime.now(UTC),
            )
        )
    _run_anonymize(
        update(BillingCustomerModel)
        .where(BillingCustomerModel.username == username)
        .values(username=tombstone, balance_cents=0, grant_remaining=0, updated_at=datetime.now(UTC))
    )
    _run_anonymize(update(WalletLedgerModel).where(WalletLedgerModel.username == username).values(username=tombstone))
    _run_anonymize(update(UserQuotaAuditModel).where(UserQuotaAuditModel.actor == username).values(actor=tombstone))
    _run_anonymize(
        update(UserQuotaAuditModel)
        .where(UserQuotaAuditModel.target_username == username)
        .values(target_username=tombstone)
    )

    _run_delete(delete(UserModel).where(UserModel.email == username))

    return AccountDeletionSummary(deleted_rows=deleted, anonymized_rows=anonymized)
