"""Tests for self-service account data export and deletion.

Runs the accounts + account-data routers against a shared in-memory SQLite
engine holding the full ORM schema, so the deletion sweep exercises real
DELETE/UPDATE statements across every owned table. The bearer dependency is
overridden to a fixed identity, and a second account is seeded alongside to
prove deletion never reaches another user's rows.
"""

from __future__ import annotations

import csv
import io
import json
import pickle
import runpy
import threading
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pyarrow.feather as feather
import pyarrow.parquet as pq
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, event, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ...billing.budgets import BudgetService
from ...billing.service import LOCAL_CUSTOMER_PREFIX, StripeBillingService, local_customer_id
from ...config import app_env, settings
from ...storage.models import (
    AgentConversationModel,
    AgentFeedbackReportModel,
    ApiTokenModel,
    Base,
    BillingCustomerModel,
    ChatMediaModel,
    DatasetModel,
    ExecutionBudgetModel,
    ExecutionOperationModel,
    JobModel,
    LogEntryModel,
    NotificationModel,
    NotificationPreferenceModel,
    ProgressEventModel,
    PushSubscriptionModel,
    SetupDraftModel,
    SlackConnectionModel,
    StoragePackModel,
    TelemetryEventModel,
    TreeObjectModel,
    UserConnectorModel,
    UserModel,
    WalletLedgerModel,
    WorkspaceModel,
)
from ...storage.remote import RemoteDBJobStore
from ...storage.tree_purge import TreePurgeError, mark_run_packs
from ..account_data_service import _anonymized_username, delete_account
from ..account_export_formats import EXPORT_FORMATS
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..routers import account_data as account_data_mod
from ..routers.account_data import create_account_data_router
from ..routers.accounts import create_accounts_router

_SECRET = "test-internal-secret"
_AUTH_HEADER = {"X-Internal-Auth": _SECRET}
_EMAIL = "owner@example.com"
_PASSWORD = "correct horse battery"
_OTHER = "bystander@example.com"
_PG_URL = app_env("APP_BUDGET_TEST_DB_URL")


def _seed_user_data(engine: Any, username: str, stripe_id: str) -> None:
    """Insert one owned row into a representative spread of tables.

    Args:
        engine: The SQLite engine holding the full schema.
        username: The identity every seeded row is owned by.
        stripe_id: A unique Stripe customer id for the billing row.
    """
    now = datetime.now(UTC)
    with Session(engine) as session:
        session.add_all(
            [
                JobModel(optimization_id=f"job-{username}", username=username, status="completed"),
                DatasetModel(
                    id=f"ds-{username}",
                    owner_username=username,
                    name="my dataset",
                    source="upload",
                    row_count=3,
                    column_count=2,
                    byte_size=100,
                    stored_bytes=80,
                    content_hash="deadbeef",
                ),
                WalletLedgerModel(username=username, delta_cents=500, kind="grant", description="welcome"),
                ApiTokenModel(username=username, token_hash=f"hash-{username}", last4="abcd"),
                TelemetryEventModel(event_name=f"view-{username}", username=username, received_at=now),
                BillingCustomerModel(username=username, stripe_customer_id=stripe_id),
                NotificationPreferenceModel(
                    username=username,
                    channel_grid={"finished": {"email": False}},
                    hold_minutes=5,
                ),
                NotificationModel(
                    username=username,
                    event_type="finished",
                    title="Run finished",
                    facts={"score": 0.9},
                ),
                PushSubscriptionModel(
                    username=username,
                    endpoint=f"https://fcm.googleapis.com/fcm/send/secret-{username}",
                    p256dh="p256dh-key",
                    auth="auth-secret",
                ),
                SlackConnectionModel(
                    username=username,
                    team_id="T1",
                    team_name="NOVA",
                    bot_user_id="B1",
                    slack_user_id="U1",
                    bot_token_encrypted="encrypted-bot-token",
                    channel_name="alerts",
                ),
                UserConnectorModel(
                    username=username,
                    provider="github",
                    auth_method="oauth",
                    account_label="octocat",
                    secret_ciphertext=b"encrypted-token",
                ),
            ]
        )
        session.commit()


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Build a client serving the accounts + account-data routers over SQLite.

    Registers the primary local account and seeds owned data for it and for a
    second bystander account. The bearer dependency resolves to the primary
    account so the data routes act on a known identity without minting JWTs.

    Args:
        monkeypatch: Pytest fixture used to set the shared internal secret.

    Returns:
        A namespace exposing ``client``, ``app``, and ``engine``.
    """
    monkeypatch.setattr(settings, "backend_auth_secret", SecretStr(_SECRET))
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    store = SimpleNamespace(engine=engine)
    app = FastAPI()
    app.state.job_store = store

    @app.exception_handler(DomainError)
    async def _domain_error(request: Request, exc: DomainError) -> JSONResponse:
        """Render a domain error as the app does.

        Args:
            request: The request.
            exc: The error.

        Returns:
            The error response.
        """
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "code": exc.code, "params": exc.params},
        )

    app.include_router(create_accounts_router(job_store=store))
    app.include_router(create_account_data_router(job_store=store))
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser(
        username=_EMAIL, role="user", groups=()
    )
    client = TestClient(app)
    resp = client.post("/auth/register", json={"email": _EMAIL, "password": _PASSWORD}, headers=_AUTH_HEADER)
    assert resp.status_code == 201
    _seed_user_data(engine, _EMAIL, "cus_owner")
    _seed_user_data(engine, _OTHER, "cus_other")
    return SimpleNamespace(client=client, app=app, engine=engine)


def test_export_returns_owned_data_without_secrets(harness: SimpleNamespace) -> None:
    """Export carries the account's own rows but never any secret material."""
    resp = harness.client.get("/account/export")
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    body = resp.json()

    assert body["account"]["email"] == _EMAIL
    assert "password_hash" not in body["account"]
    assert "totp_secret" not in body["account"]
    assert body["account"]["two_factor"]["totp_enabled"] is False

    assert [job["optimization_id"] for job in body["optimizations"]] == [f"job-{_EMAIL}"]
    assert [ds["name"] for ds in body["datasets"]] == ["my dataset"]
    assert body["billing"]["ledger"][0]["kind"] == "grant"
    assert body["api_token"]["last4"] == "abcd"
    assert "token_hash" not in body["api_token"]
    notification_preferences = body["notification_preferences"]
    assert notification_preferences["grid"]["finished"]["email"] is False
    assert notification_preferences["grid"]["failed"]["email"] is True
    assert notification_preferences["hold_minutes"] == 5
    assert notification_preferences["held_channels"] == ["email"]
    assert notification_preferences["stuck_fraction"] == 0.25
    assert body["account"]["intake_completed_at"] is None
    assert body["account"]["intake_profile"] is None
    # No other account's rows leak into this export.
    assert all(job["optimization_id"] != f"job-{_OTHER}" for job in body["optimizations"])


def test_export_carries_the_saved_setup_draft_until_it_is_deleted(harness: SimpleNamespace) -> None:
    """Export returns the account's saved setup and chat, and nothing once the draft is deleted."""
    assert harness.client.get("/account/export").json()["setup_draft"] is None

    with Session(harness.engine) as session:
        session.add(
            SetupDraftModel(
                username=_EMAIL,
                left_at=1700000000000,
                draft_values={"goal": "summarize tickets"},
                chat={"messages": [{"role": "user", "content": "help me set up"}]},
                revision=3,
            )
        )
        session.commit()
    draft = harness.client.get("/account/export").json()["setup_draft"]
    assert draft["values"] == {"goal": "summarize tickets"}
    assert draft["chat"]["messages"][0]["content"] == "help me set up"
    assert draft["left_at"] == 1700000000000
    assert draft["revision"] == 3

    with Session(harness.engine) as session:
        session.get(SetupDraftModel, _EMAIL).deleted = True
        session.commit()
    assert harness.client.get("/account/export").json()["setup_draft"] is None


def _seed_feedback_report(engine: Engine, username: str) -> None:
    """Store one chat with one agent feedback report for ``username`` (SKY-580).

    Args:
        engine: The SQLite engine holding the full schema.
        username: The chat owner.
    """
    with Session(engine) as session:
        session.add(AgentConversationModel(id=f"chat-{username}", username=username, title="t"))
        session.add(
            AgentFeedbackReportModel(
                conversation_id=f"chat-{username}",
                user_message_id=1,
                username=username,
                agent="generalist",
                tool_name="validate_code",
                user_quote="the score is wrong",
                summary="Expected 1, got 0.",
            )
        )
        session.commit()


def test_export_carries_each_chat_feedback_reports(harness: SimpleNamespace) -> None:
    """Each exported chat lists the agent's feedback reports about it, without internal keys."""
    _seed_feedback_report(harness.engine, _EMAIL)
    _seed_feedback_report(harness.engine, _OTHER)

    [chat] = harness.client.get("/account/export").json()["agent_conversations"]

    [report] = chat["feedback_reports"]
    assert set(report) == {"tool_name", "user_quote", "summary", "created_at"}
    assert (report["tool_name"], report["user_quote"], report["summary"]) == (
        "validate_code",
        "the score is wrong",
        "Expected 1, got 0.",
    )


def test_delete_removes_and_counts_the_feedback_reports(harness: SimpleNamespace) -> None:
    """Deletion removes the account's feedback reports, counts them, and keeps another account's."""
    _seed_feedback_report(harness.engine, _OTHER)
    with Session(harness.engine) as session:
        baseline = delete_account(session, _EMAIL)
        session.rollback()
    _seed_feedback_report(harness.engine, _EMAIL)

    resp = harness.client.post("/account/delete", json={"password": _PASSWORD})

    assert resp.status_code == 200
    assert resp.json()["deleted_rows"] == baseline.deleted_rows + 2
    with Session(harness.engine) as session:
        owners = session.scalars(select(AgentFeedbackReportModel.username)).all()
    assert owners == [_OTHER]


@pytest.mark.parametrize(
    ("filename", "kind", "mime"),
    [("shot.png", "image", "image/png"), ("brief.pdf", "document", "application/pdf")],
)
def test_export_lists_and_delete_removes_the_chat_media(
    harness: SimpleNamespace, filename: str, kind: str, mime: str
) -> None:
    """Export lists only the account's chat media, documents too, without bytes; deletion removes only that media."""
    with Session(harness.engine) as session:
        for owner in (_EMAIL, _OTHER):
            session.add(
                ChatMediaModel(
                    id=str(uuid4()),
                    username=owner,
                    filename=filename,
                    kind=kind,
                    mime_type=mime,
                    byte_size=4,
                    data=b"\x89PNG",
                )
            )
        session.commit()

    exported = harness.client.get("/account/export").json()["chat_media"]
    assert [(m["filename"], m["kind"], m["byte_size"]) for m in exported] == [(filename, kind, 4)]
    assert "data" not in exported[0]

    resp = harness.client.post("/account/delete", json={"password": _PASSWORD})

    assert resp.status_code == 200
    with Session(harness.engine) as session:
        owners = session.scalars(select(ChatMediaModel.username)).all()
    assert owners == [_OTHER]


def test_delete_stops_only_the_account_chat_sandboxes(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting an account stops each of its chats' file sandboxes and leaves other accounts' sandboxes."""
    stopped: list[str] = []
    monkeypatch.setattr(account_data_mod, "stop_chat_runner", stopped.append)
    with Session(harness.engine) as session:
        for owner in (_EMAIL, _EMAIL, _OTHER):
            session.add(AgentConversationModel(id=str(uuid4()), username=owner, title="t"))
        session.commit()
        owned = sorted(
            session.scalars(select(AgentConversationModel.id).where(AgentConversationModel.username == _EMAIL))
        )
    assert len(owned) == 2

    resp = harness.client.post("/account/delete", json={"password": _PASSWORD})

    assert resp.status_code == 200
    assert sorted(stopped) == owned


def test_delete_rejects_wrong_password_for_local_account(harness: SimpleNamespace) -> None:
    """A local account keeps all its data when the confirm password is wrong."""
    resp = harness.client.post("/account/delete", json={"password": "not my password"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "accounts.invalid_credentials"
    with Session(harness.engine) as session:
        assert session.get(UserModel, _EMAIL) is not None
        assert session.get(JobModel, f"job-{_EMAIL}") is not None


def test_delete_answers_503_and_keeps_the_account_when_the_tree_purge_fails(harness: SimpleNamespace) -> None:
    """A failed tree purge is a 503 with its own code, and the account and its rows stay (SKY-733)."""
    with patch("core.api.account_data_service.tree_purge.purge_user", side_effect=TreePurgeError("packs/w1/p1 failed")):
        resp = harness.client.post("/account/delete", json={"password": _PASSWORD})

    assert resp.status_code == 503
    assert resp.json()["code"] == "accounts.deletion_storage_unavailable"
    with Session(harness.engine) as session:
        assert session.get(UserModel, _EMAIL) is not None
        assert session.get(JobModel, f"job-{_EMAIL}") is not None


def test_delete_purges_owned_data_and_anonymizes_records(harness: SimpleNamespace) -> None:
    """Deletion removes owned rows, anonymizes financial/telemetry rows, spares others."""
    resp = harness.client.post("/account/delete", json={"password": _PASSWORD})
    assert resp.status_code == 200
    assert resp.json()["deleted_rows"] > 0

    with Session(harness.engine) as session:
        assert session.get(UserModel, _EMAIL) is None
        assert session.get(JobModel, f"job-{_EMAIL}") is None
        assert session.get(DatasetModel, f"ds-{_EMAIL}") is None
        assert session.get(ApiTokenModel, _EMAIL) is None
        assert session.get(NotificationPreferenceModel, _EMAIL) is None

        tombstone = _wallet_tombstone(session)
        ledger = session.scalars(select(WalletLedgerModel).where(WalletLedgerModel.username == tombstone)).all()
        assert len(ledger) == 1
        assert not session.scalars(select(WalletLedgerModel).where(WalletLedgerModel.username == _EMAIL)).all()

        telemetry = session.scalars(
            select(TelemetryEventModel).where(TelemetryEventModel.event_name == f"view-{_EMAIL}")
        ).one()
        assert telemetry.username is None

        # The bystander account is entirely untouched.
        assert session.get(JobModel, f"job-{_OTHER}") is not None
        assert session.get(NotificationPreferenceModel, _OTHER) is not None
        assert session.scalars(select(WalletLedgerModel).where(WalletLedgerModel.username == _OTHER)).all()


def test_delete_oauth_account_needs_no_password(harness: SimpleNamespace) -> None:
    """An OAuth account (no users row, no password) deletes on bearer auth alone."""
    oauth_email = "sso@example.com"
    _seed_user_data(harness.engine, oauth_email, "cus_sso")
    harness.app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser(
        username=oauth_email, role="user", groups=()
    )

    resp = harness.client.post("/account/delete", json={"password": ""})
    assert resp.status_code == 200

    with Session(harness.engine) as session:
        assert session.get(JobModel, f"job-{oauth_email}") is None
        assert session.get(BillingCustomerModel, oauth_email) is None


def test_delete_provisioned_oauth_account_needs_no_password(harness: SimpleNamespace) -> None:
    """An OAuth identity mirrored into ``users`` still deletes on bearer auth alone."""
    oauth_email = "mirrored@example.com"
    provisioned = harness.client.post(
        "/auth/oauth/provision",
        json={"email": oauth_email, "name": "Mirrored"},
        headers=_AUTH_HEADER,
    )
    assert provisioned.status_code == 200
    _seed_user_data(harness.engine, oauth_email, "cus_mirrored")
    harness.app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser(
        username=oauth_email, role="user", groups=()
    )

    resp = harness.client.post("/account/delete", json={"password": ""})
    assert resp.status_code == 200

    with Session(harness.engine) as session:
        assert session.get(UserModel, oauth_email) is None
        assert session.get(JobModel, f"job-{oauth_email}") is None


def test_export_includes_channels_and_connectors_without_secrets(harness: SimpleNamespace) -> None:
    """Inbox, Chrome alerts, Slack and connectors are exported, their tokens are not."""
    body = harness.client.get("/account/export").json()
    raw = json.dumps(body)

    assert body["export_format"] == "skynet-account-export/v2"
    assert [item["title"] for item in body["notifications"]] == ["Run finished"]
    assert body["push_subscriptions"][0]["push_service"] == "fcm.googleapis.com"
    assert body["slack"]["team_name"] == "NOVA"
    assert body["connectors"][0]["account_label"] == "octocat"
    for secret in ("secret-", "auth-secret", "p256dh-key", "encrypted-bot-token", "encrypted-token"):
        assert secret not in raw


@pytest.mark.parametrize("export_format", EXPORT_FORMATS)
def test_export_renders_every_format(harness: SimpleNamespace, export_format: str) -> None:
    """Each format downloads as a readable file holding the account's runs."""
    resp = harness.client.get("/account/export", params={"format": export_format})
    assert resp.status_code == 200
    disposition = resp.headers["content-disposition"]
    assert "attachment" in disposition
    job_id = f"job-{_EMAIL}"

    if export_format == "json":
        assert resp.json()["optimizations"][0]["optimization_id"] == job_id
    elif export_format == "pkl":
        assert disposition.endswith('.pkl"')
        assert pickle.loads(resp.content)["optimizations"][0]["optimization_id"] == job_id
    elif export_format == "xlsx":
        with zipfile.ZipFile(io.BytesIO(resp.content)) as book:
            assert "xl/workbook.xml" in book.namelist()
            assert 'name="optimizations"' in book.read("xl/workbook.xml").decode()
            sheets = "".join(book.read(n).decode() for n in book.namelist() if n.startswith("xl/worksheets/"))
            assert job_id in sheets
    else:
        with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
            names = archive.namelist()
            runs = next(n for n in names if n.endswith(f"/optimizations.{export_format}"))
            assert any(n.endswith(f"/billing_ledger.{export_format}") for n in names)
            data = archive.read(runs)
        if export_format == "csv":
            rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
            assert rows[0]["optimization_id"] == job_id
        else:
            reader = pq.read_table if export_format == "parquet" else feather.read_table
            assert reader(io.BytesIO(data)).column("optimization_id").to_pylist() == [job_id]


def test_export_rejects_unknown_format(harness: SimpleNamespace) -> None:
    """An unsupported format is a validation error, not a silent JSON fallback."""
    assert harness.client.get("/account/export", params={"format": "docx"}).status_code == 422


def test_delete_removes_channels_and_connectors(harness: SimpleNamespace) -> None:
    """Deletion clears the inbox, Chrome alerts, Slack link and connectors, sparing others."""
    assert harness.client.post("/account/delete", json={"password": _PASSWORD}).status_code == 200
    with Session(harness.engine) as session:
        for model in (NotificationModel, PushSubscriptionModel, SlackConnectionModel, UserConnectorModel):
            owners = set(session.scalars(select(model.username)))
            assert _EMAIL not in owners
            assert _OTHER in owners


def _wallet_tombstone(session: Session) -> str:
    """Return the tombstone name of the primary account's one closed wallet.

    Args:
        session: Open session on the harness engine.

    Returns:
        The closed wallet's ``deleted-user-<hash>-<random>`` username.
    """
    return session.scalars(
        select(BillingCustomerModel.username).where(
            BillingCustomerModel.username.startswith(f"{_anonymized_username(_EMAIL)}-")
        )
    ).one()


def _open_wallets(engine: Any) -> None:
    """Give both seeded wallets a spendable balance and an unspent free grant.

    Args:
        engine: The SQLite engine holding the seeded wallets.
    """
    with Session(engine) as session:
        for customer in session.scalars(select(BillingCustomerModel)):
            customer.balance_cents = 200
            customer.grant_remaining = 300
        session.commit()


def test_delete_closes_the_wallet_and_records_the_forfeited_balance(harness: SimpleNamespace) -> None:
    """Deletion leaves no open wallet: balance and grant go to zero, and a closure row explains it."""
    _open_wallets(harness.engine)
    assert harness.client.post("/account/delete", json={"password": _PASSWORD}).status_code == 200

    with Session(harness.engine) as session:
        assert session.get(BillingCustomerModel, _EMAIL) is None
        tombstone = _wallet_tombstone(session)
        wallet = session.get(BillingCustomerModel, tombstone)
        assert (wallet.balance_cents, wallet.grant_remaining) == (0, 0)
        assert wallet.stripe_customer_id == "cus_owner"
        closure = session.scalars(
            select(WalletLedgerModel).where(
                WalletLedgerModel.username == tombstone, WalletLedgerModel.kind == "closure"
            )
        ).one()
        assert closure.delta_cents == -200

        other = session.get(BillingCustomerModel, _OTHER)
        assert (other.balance_cents, other.grant_remaining) == (200, 300)


def test_migration_closes_wallets_deleted_before_the_fix(harness: SimpleNamespace) -> None:
    """The one-time migration closes old tombstone wallets and spares live ones."""
    _open_wallets(harness.engine)
    tombstone = _anonymized_username(_EMAIL)
    with Session(harness.engine) as session:
        session.get(BillingCustomerModel, _EMAIL).username = tombstone
        session.add(
            BillingCustomerModel(
                username="deleted-user-look@example.com", stripe_customer_id="cus_look", balance_cents=50
            )
        )
        session.commit()
    migration = runpy.run_path(
        str(Path(__file__).resolve().parents[3] / "alembic/versions/1b5b259642df_close_deleted_account_wallets.py")
    )
    for _ in range(2):
        with harness.engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            migration["upgrade"]()

    with Session(harness.engine) as session:
        wallet = session.get(BillingCustomerModel, tombstone)
        assert (wallet.balance_cents, wallet.grant_remaining) == (0, 0)
        closures = session.scalars(
            select(WalletLedgerModel.delta_cents).where(WalletLedgerModel.kind == "closure")
        ).all()
        assert closures == [-200]
        other = session.get(BillingCustomerModel, _OTHER)
        assert (other.balance_cents, other.grant_remaining) == (200, 300)
        assert session.get(BillingCustomerModel, "deleted-user-look@example.com").balance_cents == 50


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param(
            "postgresql",
            marks=pytest.mark.skipif(
                not _PG_URL or not _PG_URL.startswith("postgresql"),
                reason="Set APP_BUDGET_TEST_DB_URL to an explicitly disposable PostgreSQL database.",
            ),
        ),
    ]
)
def full_schema_engine(request: pytest.FixtureRequest) -> Iterator[Engine]:
    """Yield SQLite, and PostgreSQL when supplied, with the full ORM schema.

    The PostgreSQL engine works in its own schema, which it drops at the end.

    Args:
        request: Selects the database dialect.

    Yields:
        Engine holding every table that account deletion touches.
    """
    if request.param == "sqlite":
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        yield engine
        engine.dispose()
        return
    schema = f"account_delete_test_{uuid4().hex}"
    admin = create_engine(_PG_URL)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(_PG_URL, connect_args={"options": f"-csearch_path={schema},public"})
    try:
        Base.metadata.create_all(engine)
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def _sign_up_with_local_wallet(engine: Engine, balance_cents: int) -> None:
    """Create the user row, and the wallet and grant row that a first local credit writes.

    Args:
        engine: Engine holding the full schema.
        balance_cents: Spendable balance of the new wallet.
    """
    with Session(engine) as session:
        session.add(UserModel(email=_EMAIL, name="Owner", password_hash="x"))
        session.add(
            BillingCustomerModel(
                username=_EMAIL, stripe_customer_id=local_customer_id(_EMAIL), balance_cents=balance_cents
            )
        )
        session.add(WalletLedgerModel(username=_EMAIL, delta_cents=balance_cents, kind="grant", description="g"))
        session.commit()


def _delete(engine: Engine) -> None:
    """Delete the account in one committed transaction.

    Args:
        engine: Engine holding the full schema.
    """
    with Session(engine) as session:
        delete_account(session, _EMAIL)
        session.commit()


def test_repeat_deletion_of_one_email_keeps_each_closed_wallet(full_schema_engine: Engine) -> None:
    """Delete, sign up again and delete again: each deletion keeps its own closed wallet (SKY-602)."""
    _sign_up_with_local_wallet(full_schema_engine, 200)
    _delete(full_schema_engine)
    _sign_up_with_local_wallet(full_schema_engine, 70)
    _delete(full_schema_engine)

    with Session(full_schema_engine) as session:
        assert session.get(BillingCustomerModel, _EMAIL) is None
        wallets = session.scalars(select(BillingCustomerModel)).all()
        assert len(wallets) == 2
        for wallet in wallets:
            assert wallet.username.startswith(_anonymized_username(_EMAIL))
            assert "@" not in wallet.username
            assert (wallet.balance_cents, wallet.grant_remaining) == (0, 0)
            assert wallet.stripe_customer_id.startswith(LOCAL_CUSTOMER_PREFIX)
            assert wallet.stripe_customer_id != local_customer_id(_EMAIL)
            # Each closed wallet keeps its own audit trail: its grant and its closure.
            ledger = session.scalars(
                select(WalletLedgerModel.delta_cents).where(WalletLedgerModel.username == wallet.username)
            ).all()
            assert sorted(ledger) in ([-200, 200], [-70, 70])
        assert len({wallet.username for wallet in wallets}) == 2


def test_migration_frees_the_local_ids_of_wallets_closed_before_the_fix(full_schema_engine: Engine) -> None:
    """A wallet closed before SKY-602 gives up the email's local id, so the account can sign up again."""
    old_tombstone = _anonymized_username(_EMAIL)
    with Session(full_schema_engine) as session:
        session.add_all(
            [
                BillingCustomerModel(username=old_tombstone, stripe_customer_id=local_customer_id(_EMAIL)),
                BillingCustomerModel(username=f"{old_tombstone}-real", stripe_customer_id="cus_kept"),
                BillingCustomerModel(username=_OTHER, stripe_customer_id=local_customer_id(_OTHER)),
            ]
        )
        session.commit()
    migration = runpy.run_path(
        str(Path(__file__).resolve().parents[3] / "alembic/versions/71fa5f622536_free_local_ids_of_closed_wallets.py")
    )
    for _ in range(2):
        with full_schema_engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            migration["upgrade"]()

    _sign_up_with_local_wallet(full_schema_engine, 70)
    with Session(full_schema_engine) as session:
        assert session.get(BillingCustomerModel, old_tombstone).stripe_customer_id == local_customer_id(old_tombstone)
        assert session.get(BillingCustomerModel, f"{old_tombstone}-real").stripe_customer_id == "cus_kept"
        assert session.get(BillingCustomerModel, _OTHER).stripe_customer_id == local_customer_id(_OTHER)
        assert session.get(BillingCustomerModel, _EMAIL).stripe_customer_id == local_customer_id(_EMAIL)


def test_deletion_keeps_a_real_stripe_customer_id(full_schema_engine: Engine) -> None:
    """A closed wallet keeps its real Stripe customer id, so a later webhook still finds it."""
    with Session(full_schema_engine) as session:
        session.add(BillingCustomerModel(username=_EMAIL, stripe_customer_id="cus_real"))
        session.commit()
    _delete(full_schema_engine)
    with Session(full_schema_engine) as session:
        wallet = session.scalars(select(BillingCustomerModel)).one()
        assert wallet.stripe_customer_id == "cus_real"
        assert wallet.username.startswith(_anonymized_username(_EMAIL))


def _seed_budget_with_holds(engine: Engine, username: str) -> None:
    """Give an account a funded wallet and a budget with one operation in each ledger state.

    The reserved hold is unstarted work, the dispatched hold is a Vercel
    sandbox, the pending hold is a partly settled model call, and the settled
    operation is finished work.

    Args:
        engine: Engine holding the full schema.
        username: Owner of the wallet and the budget.
    """
    now = datetime.now(UTC)
    budget_id = f"budget-{username}"
    holds = {
        "reserved": ("model", "openrouter", 0),
        "dispatched": ("sandbox", "vercel", 0),
        "pending": ("model", "openrouter", 30),
        "settled": ("model", "openrouter", 100),
    }
    with Session(engine) as session:
        session.add(BillingCustomerModel(username=username, stripe_customer_id=f"cus-{username}", balance_cents=500))
        session.add(
            ExecutionBudgetModel(
                id=budget_id,
                username=username,
                creation_key=budget_id,
                creation_fingerprint="f",
                total_cents=100,
                state="attached",
                settled_units=130,
                wallet_settled_units=130,
                reserved_units=270,
                wallet_reserved_units=270,
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        for state, (cost_kind, provider, actual) in holds.items():
            session.add(
                ExecutionOperationModel(
                    id=f"{state}-{username}",
                    budget_id=budget_id,
                    operation_key=state,
                    attempt=0,
                    generation=0,
                    phase="run",
                    cost_kind=cost_kind,
                    request_fingerprint="r",
                    admission_fingerprint="a",
                    price_snapshot={"provider": provider},
                    state=state,
                    max_units=100,
                    max_wallet_units=100,
                    actual_units=actual,
                    actual_wallet_units=actual,
                    created_at=now,
                    dispatched_at=None if state == "reserved" else now,
                    updated_at=now,
                )
            )
        session.commit()


def _swept(engine: Engine) -> set[str]:
    """Return the operations that the model and Vercel reconciliation sweeps select.

    Args:
        engine: Engine holding the full schema.

    Returns:
        Operation ids the two sweeps would read on their next pass.
    """
    service = BudgetService(engine=engine)
    return {
        operation_id
        for provider, cost_kind in (("openrouter", "model"), ("vercel", "sandbox"))
        for operation_id, _ in service.unsettled_operations(provider=provider, cost_kind=cost_kind)
    }


def test_deletion_releases_the_open_holds_so_the_sweep_skips_them(full_schema_engine: Engine) -> None:
    """A deleted account leaves no hold for the reconciliation sweep, and a live account keeps its holds."""
    _seed_budget_with_holds(full_schema_engine, _EMAIL)
    _seed_budget_with_holds(full_schema_engine, _OTHER)
    assert _swept(full_schema_engine) == {
        f"{state}-{username}" for state in ("dispatched", "pending") for username in (_EMAIL, _OTHER)
    }

    _delete(full_schema_engine)

    assert _swept(full_schema_engine) == {f"dispatched-{_OTHER}", f"pending-{_OTHER}"}
    with Session(full_schema_engine) as session:
        states = {
            operation.id: operation.state
            for operation in session.scalars(select(ExecutionOperationModel).order_by(ExecutionOperationModel.id))
        }
        assert {key: value for key, value in states.items() if key.endswith(_EMAIL)} == {
            f"reserved-{_EMAIL}": "released",
            f"dispatched-{_EMAIL}": "released",
            f"pending-{_EMAIL}": "released",
            f"settled-{_EMAIL}": "settled",
        }
        assert {key: value for key, value in states.items() if key.endswith(_OTHER)} == {
            f"reserved-{_OTHER}": "reserved",
            f"dispatched-{_OTHER}": "dispatched",
            f"pending-{_OTHER}": "pending",
            f"settled-{_OTHER}": "settled",
        }
        closed = session.get(ExecutionBudgetModel, f"budget-{_EMAIL}")
        assert (closed.state, closed.blocked_reason) == ("closed", "account_deleted")
        assert (closed.reserved_units, closed.wallet_reserved_units) == (0, 0)
        assert (closed.settled_units, closed.wallet_settled_units) == (130, 130)
        live = session.get(ExecutionBudgetModel, f"budget-{_OTHER}")
        assert (live.state, live.blocked_reason, live.reserved_units) == ("attached", None, 270)


def test_refund_that_resolved_the_email_before_deletion_reverses_the_closure(full_schema_engine: Engine) -> None:
    """A refund webhook that resolved the email before the deletion finds the closed wallet (SKY-601)."""
    with Session(full_schema_engine) as session:
        session.add(BillingCustomerModel(username=_EMAIL, stripe_customer_id="cus_real", balance_cents=300))
        session.add(
            WalletLedgerModel(
                username=_EMAIL,
                delta_cents=500,
                kind="topup",
                description="Top-up",
                stripe_payment_intent_id="pi_race",
            )
        )
        session.commit()
    service = StripeBillingService(engine=full_schema_engine)
    resolve = service._pi_clawback_context
    deleted: list[bool] = []

    def _resolve_then_delete(session: Session, payment_intent: str) -> tuple[str | None, int, int]:
        """Resolve the owner, then let the deletion commit once before the webhook locks the wallet.

        Args:
            session: The webhook's open session.
            payment_intent: The refunded PaymentIntent id.

        Returns:
            The owner, granted and clawed cents that the ledger holds at the call.
        """
        context = resolve(session, payment_intent)
        if not deleted:
            deleted.append(True)
            _delete(full_schema_engine)
        return context

    with (
        patch.object(service, "_pi_clawback_context", _resolve_then_delete),
        patch("core.billing.service.send_alert"),
        Session(full_schema_engine) as session,
    ):
        service._on_charge_refunded(session, "evt_race", {"payment_intent": "pi_race", "amount_refunded": 500})
        session.commit()

    with Session(full_schema_engine) as session:
        assert session.get(BillingCustomerModel, _EMAIL) is None
        wallet = session.scalars(select(BillingCustomerModel)).one()
        assert wallet.username.startswith(_anonymized_username(_EMAIL))
        # The refund returns the 300 forfeited cents; only the 200 spent cents stay owed.
        assert (wallet.balance_cents, wallet.debt_cents) == (0, 200)
        ledger = session.scalars(
            select(WalletLedgerModel)
            .where(WalletLedgerModel.kind.in_(("closure", "refund")))
            .order_by(WalletLedgerModel.id)
        ).all()
        assert {row.username for row in ledger} == {wallet.username}
        assert [(row.kind, row.delta_cents, row.uncollected_cents) for row in ledger] == [
            ("closure", -300, None),
            ("closure", 300, None),
            ("refund", -300, 200),
        ]


def test_deletion_purges_the_tree_rows_and_fails_when_a_pack_delete_fails(full_schema_engine: Engine) -> None:
    """Account deletion purges the trees, workspaces and own packs; a failed pack delete fails it (SKY-733)."""
    with Session(full_schema_engine) as session:
        session.add(UserModel(email=_EMAIL, name="Owner", password_hash="x"))
        session.add(WorkspaceModel(id="ws-1", username=_EMAIL))
        # Older than the grace time, so the purge deletes the pack at once.
        session.add(
            StoragePackModel(
                pack_key="packs/w1/p1",
                worker_id="w1",
                byte_size=10,
                created_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        session.add(
            TreeObjectModel(
                username=_EMAIL, object_id="o1", byte_size=10, pack_key="packs/w1/p1", pack_offset=0, pack_length=10
            )
        )
        session.commit()

    def _fail(keys: list[str]) -> None:
        """Fail like an S3 outage.

        Args:
            keys: The object keys.

        Raises:
            OSError: Always.
        """
        raise OSError("S3 is down")

    with patch("core.storage.tree_purge.bucket_deleter", return_value=_fail), pytest.raises(TreePurgeError):
        _delete(full_schema_engine)
    with Session(full_schema_engine) as session:
        assert session.get(UserModel, _EMAIL) is not None
        assert session.scalars(select(TreeObjectModel)).one().pack_key == "packs/w1/p1"

    deleted: list[str] = []
    with patch("core.storage.tree_purge.bucket_deleter", return_value=deleted.extend):
        _delete(full_schema_engine)

    assert deleted == ["packs/w1/p1"]
    with Session(full_schema_engine) as session:
        assert session.get(UserModel, _EMAIL) is None
        assert session.scalars(select(TreeObjectModel)).all() == []
        assert session.scalars(select(WorkspaceModel)).all() == []
        assert session.scalars(select(StoragePackModel)).all() == []


def test_an_account_delete_and_a_run_delete_at_once_do_not_deadlock(full_schema_engine: Engine) -> None:
    """A run delete paused between its log rows and its job lock lets a concurrent account delete finish (SKY-733).

    The run delete deletes its log and progress rows, then waits until the
    account delete is about to delete those rows. If the account delete locked
    the job first, each would wait for the other and PostgreSQL would abort one.

    Args:
        full_schema_engine: Engine holding the full schema.
    """
    if full_schema_engine.dialect.name != "postgresql":
        pytest.skip("Row locks need PostgreSQL.")
    run_id = "job-race"
    with Session(full_schema_engine) as session:
        session.add(UserModel(email=_EMAIL, name="Owner", password_hash="x"))
        session.add(JobModel(optimization_id=run_id, username=_EMAIL))
        session.add(LogEntryModel(optimization_id=run_id, level="INFO", logger="x", message="m"))
        session.add(ProgressEventModel(optimization_id=run_id, event="e", metrics={}))
        session.commit()
    # The full constructor opens its own engine from a URL; delete_job needs only the session factory and engine.
    store = object.__new__(RemoteDBJobStore)
    store._engine = full_schema_engine
    store._session_factory = sessionmaker(bind=full_schema_engine)

    run_rows_deleted = threading.Event()
    account_at_child_rows = threading.Event()
    errors: list[BaseException] = []
    account_thread_name = "account-delete"
    real_mark = mark_run_packs

    def _mark_after_the_account_delete_starts(session: Session, run_ids: list[str]) -> None:
        """Hold the run delete after its child row deletes until the account delete reaches its own.

        Args:
            session: The run delete's open session.
            run_ids: The runs being deleted.

        Raises:
            AssertionError: When the account delete never reaches its child row deletes.
        """
        run_rows_deleted.set()
        if not account_at_child_rows.wait(timeout=30):
            raise AssertionError("the account delete never reached the run's child rows")
        real_mark(session, run_ids)

    def _flag_child_row_delete(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        """Set the event when the account delete sends its first log or progress row delete.

        Args:
            conn: The connection.
            cursor: The DBAPI cursor.
            statement: The SQL text.
            *args: The other listener arguments.
        """
        if threading.current_thread().name == account_thread_name and statement.startswith(
            ("DELETE FROM job_logs", "DELETE FROM job_progress_events")
        ):
            account_at_child_rows.set()

    def _delete_run() -> None:
        """Delete the run and keep any error."""
        try:
            store.delete_job(run_id)
        except BaseException as exc:
            errors.append(exc)

    def _delete_owner() -> None:
        """Delete the account and keep any error."""
        try:
            _delete(full_schema_engine)
        except BaseException as exc:
            errors.append(exc)

    event.listen(full_schema_engine, "before_cursor_execute", _flag_child_row_delete)
    try:
        with patch("core.storage.remote.mark_run_packs", _mark_after_the_account_delete_starts):
            run = threading.Thread(target=_delete_run)
            run.start()
            assert run_rows_deleted.wait(timeout=30)
            owner = threading.Thread(target=_delete_owner, name=account_thread_name)
            owner.start()
            run.join(timeout=60)
            owner.join(timeout=60)
    finally:
        event.remove(full_schema_engine, "before_cursor_execute", _flag_child_row_delete)

    assert not run.is_alive()
    assert not owner.is_alive()
    assert errors == []
    with Session(full_schema_engine) as session:
        assert session.get(UserModel, _EMAIL) is None
        assert session.get(JobModel, run_id) is None
        assert session.scalars(select(LogEntryModel)).all() == []
        assert session.scalars(select(ProgressEventModel)).all() == []
