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
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow.feather as feather
import pyarrow.parquet as pq
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ...config import settings
from ...storage.models import (
    ApiTokenModel,
    Base,
    BillingCustomerModel,
    DatasetModel,
    JobModel,
    NotificationModel,
    NotificationPreferenceModel,
    PushSubscriptionModel,
    SetupDraftModel,
    SlackConnectionModel,
    TelemetryEventModel,
    UserConnectorModel,
    UserModel,
    WalletLedgerModel,
)
from ..account_data_service import _anonymized_username
from ..account_export_formats import EXPORT_FORMATS
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..routers.account_data import create_account_data_router
from ..routers.accounts import create_accounts_router

_SECRET = "test-internal-secret"
_AUTH_HEADER = {"X-Internal-Auth": _SECRET}
_EMAIL = "owner@example.com"
_PASSWORD = "correct horse battery"
_OTHER = "bystander@example.com"


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


def test_delete_rejects_wrong_password_for_local_account(harness: SimpleNamespace) -> None:
    """A local account keeps all its data when the confirm password is wrong."""
    resp = harness.client.post("/account/delete", json={"password": "not my password"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "accounts.invalid_credentials"
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

        tombstone = _anonymized_username(_EMAIL)
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

    tombstone = _anonymized_username(_EMAIL)
    with Session(harness.engine) as session:
        assert session.get(BillingCustomerModel, _EMAIL) is None
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
