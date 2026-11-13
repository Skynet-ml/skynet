"""Tests for ``notify`` routing, the hold and the hold sweeper."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import core.notifications.channels as channels_module
from core.notifications.channels import (
    Delivery,
    EmailChannel,
    NotificationChannel,
    SlackChannel,
)
from core.notifications.dispatch import notify, release_held
from core.notifications.inbox import remove, remove_all
from core.notifications.preferences import configure_notification_preferences

from ...storage.models import Base, NotificationModel, NotificationPreferenceModel
from .conftest import FakeMail

_USER = "alice@example.com"
_T0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


class _FakeChannel(NotificationChannel):
    """Channel that records what it was asked to send."""

    def __init__(self, name: str) -> None:
        """Bind the channel name.

        Args:
            name: Catalog channel name.
        """
        self.name = name
        self.sent: list[Delivery] = []

    def send(self, delivery: Delivery) -> str:
        """Record the delivery.

        Args:
            delivery: The notification.

        Returns:
            Always ``"sent"``.
        """
        self.sent.append(delivery)
        return "sent"


@pytest.fixture
def engine():
    """Return an in-memory engine wired as the notification store.

    Returns:
        The engine.
    """
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[NotificationModel.__table__, NotificationPreferenceModel.__table__])
    configure_notification_preferences(engine)
    return engine


@pytest.fixture
def channels(monkeypatch: pytest.MonkeyPatch) -> dict[str, _FakeChannel]:
    """Replace every delivery channel with a recorder.

    Args:
        monkeypatch: pytest's ``MonkeyPatch`` fixture.

    Returns:
        The recorders by channel name.
    """
    fakes = {name: _FakeChannel(name) for name in ("email", "chrome", "slack")}
    monkeypatch.setattr(channels_module, "_REGISTRY", dict(fakes))
    return fakes


def _prefs(engine, **fields) -> None:
    """Store a preference row for the test user.

    Args:
        engine: Database engine.
        **fields: Column values.
    """
    with Session(engine) as session:
        session.add(NotificationPreferenceModel(username=_USER, **fields))
        session.commit()


def _row(engine, notification_id: str) -> NotificationModel:
    """Load a stored notification.

    Args:
        engine: Database engine.
        notification_id: The row id.

    Returns:
        The detached row.
    """
    with Session(engine, expire_on_commit=False) as session:
        row = session.get(NotificationModel, notification_id)
        assert row is not None
        return row


def _states(row: NotificationModel) -> dict[str, str]:
    """Return each channel's delivery state.

    Args:
        row: Stored notification.

    Returns:
        ``{channel: state}``.
    """
    return {channel: entry["state"] for channel, entry in row.deliveries.items()}


def test_urgent_event_goes_out_at_once_on_every_enabled_channel(engine, channels) -> None:
    """``budget_near`` skips the hold on email, Chrome and Slack."""
    _prefs(engine, channel_grid={"budget_near": {"slack": True}})
    notification_id = notify(_USER, "budget_near", "run1", {"spent": 12.0}, title="Bot", now=_T0)

    row = _row(engine, notification_id)
    assert row.in_inbox is True
    assert row.link == "/optimizations/run1"
    assert row.hold_until is None
    assert _states(row) == {"email": "sent", "chrome": "sent", "slack": "sent"}
    assert [len(channel.sent) for channel in channels.values()] == [1, 1, 1]


def test_non_urgent_email_is_held_then_sent_when_unread(engine, channels) -> None:
    """``finished`` waits the hold on email and goes out if the item stays unread."""
    notification_id = notify(_USER, "finished", "run1", {"score": 0.9}, title="Bot", now=_T0)

    row = _row(engine, notification_id)
    assert _states(row) == {"email": "held", "chrome": "sent"}
    assert row.hold_until is not None
    assert channels["email"].sent == []

    assert release_held(engine, now=_T0 + timedelta(minutes=5)) == 0
    assert release_held(engine, now=_T0 + timedelta(minutes=10)) == 1

    assert _states(_row(engine, notification_id)) == {"email": "sent", "chrome": "sent"}
    assert len(channels["email"].sent) == 1
    assert release_held(engine, now=_T0 + timedelta(hours=1)) == 0


def test_held_send_is_dropped_when_the_inbox_item_was_read(engine, channels) -> None:
    """Reading the inbox item during the hold cancels the held email."""
    notification_id = notify(_USER, "failed", "run1", {"detail": "boom"}, title="Bot", now=_T0)
    with Session(engine) as session:
        session.get(NotificationModel, notification_id).read_at = _T0 + timedelta(minutes=1)
        session.commit()

    release_held(engine, now=_T0 + timedelta(minutes=11))

    assert _states(_row(engine, notification_id)) == {"email": "dropped", "chrome": "sent"}
    assert channels["email"].sent == []


def test_held_send_still_goes_out_after_the_item_is_removed(engine, channels) -> None:
    """Removing an item from the inbox during the hold does not cancel its held email."""
    removed_one = notify(_USER, "finished", "run1", {"score": 0.9}, title="Bot", now=_T0)
    cleared = notify(_USER, "failed", "run2", {"detail": "boom"}, title="Bot", now=_T0)
    with Session(engine) as session:
        assert remove(session, _USER, removed_one) is True
        assert remove_all(session, _USER, "everything") == 1

    assert release_held(engine, now=_T0 + timedelta(minutes=11)) == 2

    for notification_id in (removed_one, cleared):
        row = _row(engine, notification_id)
        assert row.in_inbox is False
        assert _states(row) == {"email": "sent", "chrome": "sent"}
    assert len(channels["email"].sent) == 2


@pytest.mark.parametrize("event", ["finished", "failed"])
def test_run_end_pushes_to_chrome_by_default(engine, channels, event: str) -> None:
    """With no saved preferences, a finished or failed run pushes to Chrome at once."""
    notification_id = notify(_USER, event, "run1", {}, title="Bot", now=_T0)

    assert _states(_row(engine, notification_id)) == {"email": "held", "chrome": "sent"}
    assert len(channels["chrome"].sent) == 1
    assert channels["slack"].sent == []


@pytest.mark.parametrize("event", ["finished", "failed"])
def test_stored_chrome_opt_out_still_wins(engine, channels, event: str) -> None:
    """A saved Chrome off for a run-end event sends nothing to Chrome."""
    _prefs(engine, channel_grid={event: {"chrome": False}})

    notification_id = notify(_USER, event, "run1", {}, title="Bot", now=_T0)

    assert _states(_row(engine, notification_id)) == {"email": "held"}
    assert channels["chrome"].sent == []


def test_only_the_selected_channels_hold(engine, channels) -> None:
    """A channel the user did not pick to hold sends at once, even for non-urgent events."""
    _prefs(engine, channel_grid={"finished": {"chrome": True}}, held_channels=["chrome"])

    notification_id = notify(_USER, "finished", "run1", {}, title="Bot", now=_T0)

    assert _states(_row(engine, notification_id)) == {"email": "sent", "chrome": "held"}


def test_zero_hold_sends_at_once(engine, channels) -> None:
    """A zero-minute hold means nothing waits."""
    _prefs(engine, hold_minutes=0)

    notification_id = notify(_USER, "finished", "run1", {}, title="Bot", now=_T0)

    assert _states(_row(engine, notification_id)) == {"email": "sent", "chrome": "sent"}


def test_inbox_off_still_routes_other_channels(engine, channels) -> None:
    """Switching the inbox cell off hides the item but keeps the email."""
    _prefs(engine, channel_grid={"budget_near": {"inbox": False}})

    notification_id = notify(_USER, "budget_near", "run1", {}, title="Bot", now=_T0)

    assert _row(engine, notification_id).in_inbox is False
    assert len(channels["email"].sent) == 1


def test_event_off_everywhere_stores_nothing(engine, channels) -> None:
    """An event with every cell off is not recorded at all."""
    _prefs(engine, channel_grid={"stuck": {"inbox": False}})

    assert notify(_USER, "stuck", "run1", {}, title="Bot", now=_T0) is None
    with Session(engine) as session:
        assert session.query(NotificationModel).count() == 0


def test_dedupe_key_makes_a_repeat_a_no_op(engine, channels) -> None:
    """A resumed run re-emitting the same event records and sends it once."""
    first = notify(_USER, "budget_near", "run1", {}, title="Bot", dedupe_key="budget_near:run1", now=_T0)
    second = notify(_USER, "budget_near", "run1", {}, title="Bot", dedupe_key="budget_near:run1", now=_T0)

    assert first is not None
    assert second is None
    assert len(channels["email"].sent) == 1


def test_switching_a_channel_off_during_the_hold_drops_it(engine, channels) -> None:
    """The release re-reads the grid, so turning email off cancels a held email."""
    notification_id = notify(_USER, "finished", "run1", {}, title="Bot", now=_T0)
    _prefs(engine, channel_grid={"finished": {"email": False}})

    release_held(engine, now=_T0 + timedelta(minutes=10))

    assert _states(_row(engine, notification_id)) == {"email": "dropped", "chrome": "sent"}


def test_notify_without_storage_or_recipient_is_a_no_op(channels) -> None:
    """No engine or no recipient never raises and records nothing."""
    configure_notification_preferences(None)

    assert notify(_USER, "finished", "run1", {}) is None
    assert notify("", "finished", "run1", {}) is None


def test_email_channel_renders_and_sends(monkeypatch: pytest.MonkeyPatch, fake_mail: FakeMail) -> None:
    """The email channel sends one rendered email to the resolved address."""
    monkeypatch.setattr(channels_module, "send_mail", fake_mail.send_mail)
    monkeypatch.setattr(channels_module, "resolve_email", lambda username: username)

    state = EmailChannel().send(
        Delivery("n1", _USER, "finished", "run1", "Bot", "/optimizations/run1", {"score": 0.9, "detail": None})
    )

    assert state == "sent"
    call = fake_mail.last()
    assert call["to"] == _USER
    assert "/optimizations/run1" in call["html"]
    assert call["text"]


def test_email_channel_reports_unavailable_without_an_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """An identity with no email address is ``unavailable``, not a failure."""
    monkeypatch.setattr(channels_module, "resolve_email", lambda username: None)

    assert EmailChannel().send(Delivery("n1", _USER, "finished", "run1", "Bot", None)) == "unavailable"


def test_slack_is_an_inert_slot() -> None:
    """Until a later step connects it, Slack records ``unavailable``."""
    delivery = Delivery("n1", _USER, "budget_near", "run1", "Bot", None)

    assert SlackChannel().send(delivery) == "unavailable"


@pytest.fixture
def real_email(monkeypatch: pytest.MonkeyPatch, channels, fake_mail: FakeMail) -> FakeMail:
    """Route email through the real :class:`EmailChannel` with a recording transport.

    Args:
        monkeypatch: pytest's ``MonkeyPatch`` fixture.
        channels: The recorder channels (email is replaced here).
        fake_mail: Recorder for ``send_mail``.

    Returns:
        The ``send_mail`` recorder.
    """
    monkeypatch.setitem(channels_module._REGISTRY, "email", EmailChannel())
    monkeypatch.setattr(channels_module, "send_mail", fake_mail.send_mail)
    monkeypatch.setattr(channels_module, "resolve_email", lambda username: username)
    return fake_mail


def test_released_held_email_goes_through_the_renderer(engine, real_email: FakeMail) -> None:
    """A held email is rendered with its facts and both parts when the hold ends."""
    facts = {"score": 0.91, "delta": 0.12, "spent": 3.5, "duration": 125}
    notification_id = notify(_USER, "finished", "run1", facts, title="Bot", now=_T0)
    assert real_email.call_count == 0

    release_held(engine, now=_T0 + timedelta(minutes=10))

    assert _states(_row(engine, notification_id)) == {"email": "sent", "chrome": "sent"}
    call = real_email.last()
    assert call["to"] == _USER
    assert "Bot" in call["subject"]
    assert "/optimizations/run1" in call["html"]
    assert "?settings=notifications" in call["html"]
    assert "?settings=notifications" in call["text"]
    assert "$3.50" in call["text"]
    assert "0.91" in call["text"]


def test_released_held_email_is_dropped_when_read_first(engine, real_email: FakeMail) -> None:
    """Reading the item during the hold means the real email channel is never called."""
    notification_id = notify(_USER, "finished", "run1", {"score": 0.9}, title="Bot", now=_T0)
    with Session(engine) as session:
        session.get(NotificationModel, notification_id).read_at = _T0 + timedelta(minutes=2)
        session.commit()

    release_held(engine, now=_T0 + timedelta(minutes=10))

    assert _states(_row(engine, notification_id)) == {"email": "dropped", "chrome": "sent"}
    assert real_email.call_count == 0
