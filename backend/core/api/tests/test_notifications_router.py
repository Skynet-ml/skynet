"""Tests for the caller-scoped notification inbox endpoints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ...notifications import channels as channels_module
from ...storage.models import Base, NotificationModel, PushSubscriptionModel
from ..auth import AuthenticatedUser, get_authenticated_user
from ..routers import notifications as notifications_router
from ..routers.notifications import create_notifications_router

_USER = "oauth-user@example.com"
_T0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Build an authenticated inbox client over an in-memory database without news.

    Args:
        monkeypatch: pytest's ``MonkeyPatch`` fixture.

    Returns:
        ``client`` and ``engine``.
    """
    monkeypatch.setattr(notifications_router, "ensure_news", lambda session, username: None)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    app = FastAPI()
    app.include_router(create_notifications_router(job_store=SimpleNamespace(engine=engine)))
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser(username=_USER, role="user", groups=())
    return SimpleNamespace(client=TestClient(app), engine=engine)


def _seed(engine, events: list[str], user: str = _USER) -> list[str]:
    """Store one notification per event, a minute apart.

    Args:
        engine: Database engine.
        events: Event types, oldest first.
        user: Owner.

    Returns:
        The row ids, oldest first.
    """
    ids = []
    with Session(engine) as session:
        for minutes, event in enumerate(events):
            row = NotificationModel(
                username=user,
                event_type=event,
                optimization_id="run1",
                title="Bot",
                facts={"score": 0.9},
                link="/optimizations/run1",
                created_at=_T0 + timedelta(minutes=minutes),
                deliveries={},
            )
            session.add(row)
            session.flush()
            ids.append(row.id)
        session.commit()
    return ids


def test_list_pages_and_tabs(harness: SimpleNamespace) -> None:
    """The list is newest first, paginates by cursor and filters the Focused tab."""
    _seed(harness.engine, ["finished", "new_best", "budget_near"])
    _seed(harness.engine, ["finished"], user="someone-else@example.com")

    first = harness.client.get("/notifications", params={"limit": 2}).json()
    second = harness.client.get("/notifications", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    focused = harness.client.get("/notifications", params={"tab": "focused"}).json()

    assert [item["event_type"] for item in first["items"]] == ["budget_near", "new_best"]
    assert first["items"][0]["urgent"] is True
    assert first["items"][0]["focused"] is True
    assert [item["event_type"] for item in second["items"]] == ["finished"]
    assert second["next_cursor"] is None
    assert [item["event_type"] for item in focused["items"]] == ["budget_near", "finished"]


def test_unread_count_and_mark_read(harness: SimpleNamespace) -> None:
    """Marking one item and then a whole tab read updates the counts."""
    ids = _seed(harness.engine, ["finished", "new_best", "failed"])

    assert harness.client.get("/notifications/unread-count").json() == {"focused": 2, "everything": 3}
    assert harness.client.post(f"/notifications/{ids[0]}/read").json() == {"marked": 1}
    assert harness.client.get("/notifications/unread-count").json() == {"focused": 1, "everything": 2}
    assert harness.client.post("/notifications/read-all", params={"tab": "focused"}).json() == {"marked": 1}
    assert harness.client.get("/notifications/unread-count").json() == {"focused": 0, "everything": 1}


def test_mark_read_of_someone_elses_item_is_404(harness: SimpleNamespace) -> None:
    """A caller cannot touch another identity's notifications."""
    (other_id,) = _seed(harness.engine, ["finished"], user="someone-else@example.com")

    assert harness.client.post(f"/notifications/{other_id}/read").status_code == 404


def test_remove_one_hides_it_and_drops_the_unread_count(harness: SimpleNamespace) -> None:
    """Removing an item takes it off both tabs and the badge, and repeating it is harmless."""
    ids = _seed(harness.engine, ["finished", "new_best"])

    for _ in range(2):
        assert harness.client.delete(f"/notifications/{ids[0]}").json() == {"removed": 1}
    items = harness.client.get("/notifications").json()["items"]

    assert [item["id"] for item in items] == [ids[1]]
    assert harness.client.get("/notifications", params={"tab": "focused"}).json()["items"] == []
    assert harness.client.get("/notifications/unread-count").json() == {"focused": 0, "everything": 1}


def test_remove_of_someone_elses_or_a_missing_item_is_404(harness: SimpleNamespace) -> None:
    """A caller cannot remove another identity's notification, and it stays in their inbox."""
    (other_id,) = _seed(harness.engine, ["finished"], user="someone-else@example.com")

    assert harness.client.delete(f"/notifications/{other_id}").status_code == 404
    assert harness.client.delete("/notifications/does-not-exist").status_code == 404
    with Session(harness.engine) as session:
        assert session.get(NotificationModel, other_id).in_inbox is True


def test_clear_all_is_per_tab_and_caller_scoped(harness: SimpleNamespace) -> None:
    """Clearing Focused leaves Everything-only items; clearing Everything empties the inbox."""
    _seed(harness.engine, ["finished", "new_best", "failed"])
    (other_id,) = _seed(harness.engine, ["finished"], user="someone-else@example.com")

    assert harness.client.delete("/notifications", params={"tab": "focused"}).json() == {"removed": 2}
    assert harness.client.get("/notifications/unread-count").json() == {"focused": 0, "everything": 1}
    assert harness.client.delete("/notifications").json() == {"removed": 1}
    assert harness.client.delete("/notifications").json() == {"removed": 0}
    assert harness.client.get("/notifications").json()["items"] == []
    with Session(harness.engine) as session:
        assert session.get(NotificationModel, other_id).in_inbox is True


def test_delete_slack_still_reaches_the_slack_route(harness: SimpleNamespace) -> None:
    """The single-item delete route does not swallow ``DELETE /notifications/slack``."""
    assert harness.client.delete("/notifications/slack").json()["connected"] is False


def test_bad_tab_is_rejected(harness: SimpleNamespace) -> None:
    """Only the two tabs exist."""
    assert harness.client.get("/notifications", params={"tab": "urgent"}).status_code == 422


_SUBSCRIPTION = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "key", "auth": "secret"}}


def _subscriptions(engine) -> list[tuple[str, str, str]]:
    """Return the stored subscriptions.

    Args:
        engine: Database engine.

    Returns:
        ``(username, endpoint, p256dh)`` per row.
    """
    with Session(engine) as session:
        rows = session.scalars(select(PushSubscriptionModel)).all()
        return [(row.username, row.endpoint, row.p256dh) for row in rows]


def test_vapid_key_is_null_until_configured(harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """The browser learns the key only when both halves of the pair are set."""
    monkeypatch.setattr(channels_module.settings, "vapid_public_key", "public-key")
    monkeypatch.setattr(channels_module.settings, "vapid_private_key", None)
    assert harness.client.get("/notifications/push/vapid-key").json() == {"public_key": None}

    monkeypatch.setattr(channels_module.settings, "vapid_private_key", SecretStr("private-key"))
    assert harness.client.get("/notifications/push/vapid-key").json() == {"public_key": "public-key"}


def test_subscribe_upserts_by_endpoint(harness: SimpleNamespace) -> None:
    """Subscribing twice keeps one row with the newest keys and the browser's user agent."""
    first = harness.client.post("/notifications/push/subscriptions", json=_SUBSCRIPTION)
    again = harness.client.post(
        "/notifications/push/subscriptions",
        json={**_SUBSCRIPTION, "keys": {"p256dh": "new-key", "auth": "secret"}},
        headers={"User-Agent": "Chrome/140"},
    )

    assert first.json() == {"subscribed": True}
    assert again.json() == {"subscribed": True}
    assert _subscriptions(harness.engine) == [(_USER, _SUBSCRIPTION["endpoint"], "new-key")]
    with Session(harness.engine) as session:
        assert session.scalars(select(PushSubscriptionModel.user_agent)).one() == "Chrome/140"


def test_subscribe_moves_an_endpoint_to_the_new_identity(harness: SimpleNamespace) -> None:
    """A browser another identity subscribed now pushes to the caller only."""
    with Session(harness.engine) as session:
        session.add(
            PushSubscriptionModel(
                username="someone-else@example.com", endpoint=_SUBSCRIPTION["endpoint"], p256dh="old", auth="old"
            )
        )
        session.commit()

    harness.client.post("/notifications/push/subscriptions", json=_SUBSCRIPTION)

    assert _subscriptions(harness.engine) == [(_USER, _SUBSCRIPTION["endpoint"], "key")]


def test_subscribe_rejects_a_non_https_endpoint(harness: SimpleNamespace) -> None:
    """Push services are always https; anything else is not a real subscription."""
    response = harness.client.post(
        "/notifications/push/subscriptions", json={**_SUBSCRIPTION, "endpoint": "http://localhost/x"}
    )

    assert response.status_code == 422


def test_unsubscribe_removes_only_the_callers_endpoint(harness: SimpleNamespace) -> None:
    """Unsubscribing is idempotent and never touches another identity's browser."""
    harness.client.post("/notifications/push/subscriptions", json=_SUBSCRIPTION)
    with Session(harness.engine) as session:
        session.add(
            PushSubscriptionModel(
                username="someone-else@example.com", endpoint="https://push.example/other", p256dh="k", auth="a"
            )
        )
        session.commit()

    for _ in range(2):
        response = harness.client.request(
            "DELETE", "/notifications/push/subscriptions", json={"endpoint": _SUBSCRIPTION["endpoint"]}
        )
        assert response.json() == {"subscribed": False}
    harness.client.request(
        "DELETE", "/notifications/push/subscriptions", json={"endpoint": "https://push.example/other"}
    )

    assert _subscriptions(harness.engine) == [("someone-else@example.com", "https://push.example/other", "k")]
