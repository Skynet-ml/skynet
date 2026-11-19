"""Tests for the Mac app's notification routes and the local-mode preference grid."""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ...config import app_env, settings
from ...i18n import t_locale
from ...notifications.catalog import DEFAULT_GRID, MACOS
from ...notifications.dispatch import notify
from ...notifications.preferences import clean_held_channels, configure_notification_preferences, lock_preferences
from ...storage.models import Base, NotificationModel, NotificationPreferenceModel
from ...storage.tests.test_remote_jobstore_postgres import migrate_test_db
from ..auth import AuthenticatedUser, get_authenticated_user
from ..routers.local_notifications import create_local_notifications_router
from ..routers.notification_preferences import create_notification_preferences_router

_USER = "local@skynet.local"
_PATHS = (
    ("get", "/local/notifications/channels"),
    ("patch", "/local/notifications/macos"),
    ("post", "/local/notifications/native/claim"),
    ("post", "/local/notifications/native/confirm"),
)


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch):
    """Build a client over both notification routers and an in-memory database.

    Args:
        monkeypatch: pytest's ``MonkeyPatch`` fixture, used to clear the transports.

    Yields:
        ``(client, engine)``. Local mode stays off until a test turns it on.
    """
    for name in ("smtp_host", "vapid_public_key", "vapid_private_key", "slack_client_id"):
        monkeypatch.setattr(settings, name, None)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    configure_notification_preferences(engine)
    store = SimpleNamespace(engine=engine)
    app = FastAPI()
    app.include_router(create_notification_preferences_router(job_store=store))
    app.include_router(create_local_notifications_router(job_store=store))
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser(username=_USER, role="user", groups=())
    yield TestClient(app), engine
    configure_notification_preferences(None)


@pytest.fixture
def local_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn local mode on for one test.

    Args:
        monkeypatch: pytest's ``MonkeyPatch`` fixture.
    """
    monkeypatch.setattr(settings, "local_mode", True)


def test_local_routes_are_404_and_hidden_outside_local_mode(harness) -> None:
    """Hosted deployments answer 404 and keep the routes out of the schema."""
    client, _ = harness
    for method, path in _PATHS:
        assert getattr(client, method)(path, **({"json": {}} if method == "patch" else {})).status_code == 404
    schema_paths = client.app.openapi()["paths"]
    assert not any(path.startswith("/local/") for path in schema_paths)


def test_hosted_preferences_are_unchanged(harness) -> None:
    """Without local mode the preference grid is the hosted grid."""
    client, _ = harness
    assert client.get("/account/notification-preferences").json()["grid"] == DEFAULT_GRID


def test_local_preferences_keep_the_hosted_shape(harness, local_mode: None) -> None:
    """The shared preference API never returns the macOS column, so its contract stays the same."""
    client, _ = harness
    response = client.get("/account/notification-preferences")
    assert response.status_code == 200
    assert response.json()["grid"] == DEFAULT_GRID


def test_channels_report_an_unset_hosted_channel_as_off(harness, local_mode: None) -> None:
    """Without the transports, email, push and Slack stay listed and report off."""
    client, _ = harness
    body = client.get("/local/notifications/channels").json()
    assert body["channels"] == {
        "email": False,
        "chrome": False,
        "slack": False,
        "macos": True,
    }
    assert body["macos"] == {event: row["chrome"] for event, row in DEFAULT_GRID.items()}


def test_channels_report_a_configured_mail_relay_as_available(
    harness, local_mode: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Mac with its own SMTP relay keeps hosted-style email for a user with a real address."""
    monkeypatch.setattr(settings, "smtp_host", "127.0.0.1")
    monkeypatch.setattr(settings, "vapid_public_key", "pub")
    monkeypatch.setattr(settings, "vapid_private_key", SecretStr("priv"))
    client, _ = harness
    client.app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser(
        username="qa@example.com", role="user", groups=()
    )
    channels = client.get("/local/notifications/channels").json()["channels"]
    assert channels["email"] is True
    assert channels["chrome"] is True


def test_channels_report_email_off_for_the_default_local_user(
    harness, local_mode: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SMTP alone is not enough: the default ``local@skynet.local`` address cannot receive mail."""
    monkeypatch.setattr(settings, "smtp_host", "127.0.0.1")
    client, _ = harness
    assert client.get("/local/notifications/channels").json()["channels"]["email"] is False


def test_patch_macos_cells_saves_and_keeps_news_inbox_only(harness, local_mode: None) -> None:
    """Switching macOS cells saves them; product news cannot leave the inbox."""
    client, _ = harness
    response = client.patch(
        "/local/notifications/macos", json={"grid": {"finished": False, "stuck": True, "product_news": True}}
    )
    assert response.status_code == 200
    macos = response.json()["macos"]
    assert macos["finished"] is False
    assert macos["stuck"] is True
    assert macos["product_news"] is False
    assert client.get("/local/notifications/channels").json()["macos"] == macos
    assert client.get("/account/notification-preferences").json()["grid"]["finished"] == DEFAULT_GRID["finished"]


def test_macos_is_not_held_by_default(harness, local_mode: None) -> None:
    """macOS starts outside the held channels, like the hosted push channel."""
    client, _ = harness
    assert client.get("/local/notifications/channels").json()["macos_held"] is False


def test_patch_holds_macos_without_touching_the_hosted_contract(harness, local_mode: None) -> None:
    """Holding macOS saves; the shared preference API keeps listing only hosted channels."""
    client, _ = harness
    response = client.patch("/local/notifications/macos", json={"held": True})
    assert response.status_code == 200
    assert response.json()["macos_held"] is True
    assert client.get("/local/notifications/channels").json()["macos_held"] is True
    assert client.get("/account/notification-preferences").json()["held_channels"] == ["email"]

    assert client.patch("/local/notifications/macos", json={"held": False}).json()["macos_held"] is False
    assert client.get("/account/notification-preferences").json()["held_channels"] == ["email"]


def test_hosted_hold_change_keeps_the_macos_hold(harness, local_mode: None) -> None:
    """Changing the hosted held channels does not drop a macOS hold the hosted contract cannot name."""
    client, _ = harness
    client.patch("/local/notifications/macos", json={"held": True})
    response = client.patch("/account/notification-preferences", json={"held_channels": ["chrome"]})
    assert response.status_code == 200
    assert response.json()["held_channels"] == ["chrome"]
    assert client.get("/local/notifications/channels").json()["macos_held"] is True


def test_hosted_contract_rejects_a_macos_hold(harness) -> None:
    """A hosted server has no macOS channel to hold."""
    client, _ = harness
    response = client.patch("/account/notification-preferences", json={"held_channels": ["macos"]})
    assert response.status_code == 422


def test_patch_rejects_an_unknown_event(harness, local_mode: None) -> None:
    """Unknown event names are a validation error."""
    client, _ = harness
    assert client.patch("/local/notifications/macos", json={"grid": {"bogus": True}}).status_code == 422


def test_claim_returns_display_payloads_once(harness, local_mode: None) -> None:
    """The Mac app claims each queued notification once, with display text only."""
    client, engine = harness
    notify(_USER, "finished", "opt-1", {"score": 0.91}, title="Ticket triage")

    first = client.post("/local/notifications/native/claim").json()
    second = client.post("/local/notifications/native/claim").json()

    assert len(first["items"]) == 1
    item = first["items"][0]
    assert set(item) == {"id", "title", "body", "url", "tag"}
    assert item["url"] == "/optimizations/opt-1"
    assert second == {"items": []}
    with Session(engine) as session:
        row = session.get(NotificationModel, item["id"])
        assert row is not None
        assert row.deliveries["macos"]["state"] == "claimed"

    confirmed = client.post("/local/notifications/native/confirm", json={"ids": [item["id"]]})
    assert confirmed.status_code == 200
    assert confirmed.json() == {"confirmed": 1}
    with Session(engine) as session:
        row = session.get(NotificationModel, item["id"])
        assert row is not None
        assert row.deliveries["macos"]["state"] == "sent"


def test_claim_renders_in_the_app_locale(harness, local_mode: None) -> None:
    """The banner uses the Mac app's UI locale, then Accept-Language, not the Hebrew base."""
    client, _ = harness
    notify(_USER, "finished", "opt-en", {}, title="Ticket triage", dedupe_key="en")
    notify(_USER, "finished", "opt-he", {}, title="Ticket triage", dedupe_key="he")

    english = client.post("/local/notifications/native/claim", json={"locale": "en"}).json()["items"]
    assert [i["title"] for i in english] == [
        t_locale("en", "email.event.finished.headline", title="Ticket triage"),
        t_locale("en", "email.event.finished.headline", title="Ticket triage"),
    ]
    assert english[0]["title"] != t_locale(None, "email.event.finished.headline", title="Ticket triage")

    notify(_USER, "finished", "opt-x", {}, title="Ticket triage", dedupe_key="x")
    header = client.post("/local/notifications/native/claim", headers={"Accept-Language": "de"}).json()["items"]
    assert header[0]["title"] == t_locale("de", "email.event.finished.headline", title="Ticket triage")


@pytest.fixture(params=["sqlite", "postgresql"])
def shared_store(request: pytest.FixtureRequest, tmp_path, local_mode: None):
    """Build a client over both preference routes on a database that more than one connection can open.

    The SQLite case uses a file, so each connection takes real database locks.
    The Postgres case runs only when ``APP_TEST_DB_URL`` names a throwaway
    ``postgresql://`` database, like ``test_native_postgres.py``.

    Args:
        request: Selects the database.
        tmp_path: Folder for the SQLite file.
        local_mode: Local mode, so both routes and the macOS hold exist.

    Yields:
        ``(client, engine, username)``.
    """
    if request.param == "sqlite":
        engine = create_engine(f"sqlite:///{tmp_path / 'preferences.db'}")
        NotificationPreferenceModel.__table__.create(engine)
    else:
        url = app_env("APP_TEST_DB_URL")
        if not url or not url.startswith("postgresql"):
            pytest.skip("APP_TEST_DB_URL not set to a postgresql:// URL")
        # A table made outside the shared migration raced the migration of other xdist workers, and
        # its lone table without ``alembic_version`` made their setup drop ``public`` (SKY-831).
        migrate_test_db(url)
        engine = create_engine(url)
    username = f"race-{uuid.uuid4().hex[:8]}@skynet.test"
    store = SimpleNamespace(engine=engine)
    app = FastAPI()
    app.include_router(create_notification_preferences_router(job_store=store))
    app.include_router(create_local_notifications_router(job_store=store))
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser(
        username=username, role="user", groups=()
    )
    yield TestClient(app), engine, username
    with Session(engine) as session:
        session.execute(delete(NotificationPreferenceModel).where(NotificationPreferenceModel.username == username))
        session.commit()
    engine.dispose()


@pytest.mark.parametrize(
    ("path", "body", "other_change"),
    [
        ("/account/notification-preferences", {"held_channels": ["email", "chrome"]}, [MACOS]),
        ("/local/notifications/macos", {"held": True}, ["email", "chrome"]),
    ],
)
@pytest.mark.parametrize("saved_before", [True, False])
def test_a_save_waits_for_a_concurrent_save_and_keeps_its_change(
    shared_store, path: str, body: dict, other_change: list[str], saved_before: bool
) -> None:
    """Both routes lock the row before they read it, so a save during another save never drops that change.

    Without a stored row the two saves also race to create it; the second must not fail.
    """
    client, engine, username = shared_store
    if saved_before:
        with Session(engine) as session:
            session.add(NotificationPreferenceModel(username=username, held_channels=["email"]))
            session.commit()
    with Session(engine) as holder, ThreadPoolExecutor(max_workers=1) as pool:
        row = lock_preferences(holder, username)
        pending = pool.submit(client.patch, path, json=body)
        time.sleep(0.5)
        assert not pending.done()
        row.held_channels = list(clean_held_channels([*clean_held_channels(row.held_channels), *other_change]))
        holder.commit()
        assert pending.result(timeout=10).status_code == 200

    with Session(engine) as session:
        stored = session.get(NotificationPreferenceModel, username)
        assert stored is not None
        assert clean_held_channels(stored.held_channels) == ("email", "chrome", MACOS)
