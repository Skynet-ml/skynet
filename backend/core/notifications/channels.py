"""Delivery channels a notification can be routed to beyond the inbox.

Each channel is one :class:`NotificationChannel` subclass registered under its
catalog name. :func:`core.notifications.dispatch.notify` and the hold sweeper
only ever call :meth:`NotificationChannel.send`, so a new channel means
implementing that one method and registering the instance.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal

from cryptography.fernet import InvalidToken
from pywebpush import WebPushException, webpush
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..i18n import t_locale
from ..storage.models import PushSubscriptionModel, SlackConnectionModel
from . import slack
from .catalog import CHROME, EMAIL, MACOS, SLACK
from .comms import mailable, resolve_email, send_mail
from .email_render import EVENT_TYPES as EMAIL_EVENT_TYPES
from .email_render import FRONTEND_URL, render_notification_email
from .preferences import notification_engine

logger = logging.getLogger(__name__)

# ``queued`` waits for the Mac app to claim it; see :mod:`.native`.
DeliveryState = Literal["sent", "failed", "unavailable", "queued"]

# Keys of ``Delivery.facts`` the email fact row understands; the rest
# (``detail``, ``body``) are carried as text, not as facts.
_EMAIL_FACT_KEYS = frozenset({"score", "delta", "spent", "limit", "duration"})

NOTIFICATION_SETTINGS_URL = f"{FRONTEND_URL}/?settings=notifications"

# A push the browser cannot take now (offline, asleep) waits at the push
# service this long; a day covers an overnight run without piling up stale ones.
PUSH_TTL_SECONDS = 24 * 3600
PUSH_TIMEOUT_SECONDS = 10.0
# The push service's answer for a subscription the browser dropped for good.
_GONE_STATUSES = frozenset({404, 410})
_BIDI_ISOLATES = dict.fromkeys(map(ord, "\u2066\u2067\u2068\u2069"))


@dataclass(frozen=True)
class Delivery:
    """Everything a channel needs to deliver one notification."""

    notification_id: str
    username: str
    event_type: str
    optimization_id: str | None
    title: str
    link: str | None
    facts: Mapping[str, Any] = field(default_factory=dict)

    @property
    def detail(self) -> str | None:
        """Return the optional sentence under the headline (error, question, sharer)."""
        value = self.facts.get("detail")
        return value if isinstance(value, str) and value else None


class NotificationChannel(ABC):
    """One outbound channel (email, browser push, Slack)."""

    name: ClassVar[str]

    @abstractmethod
    def send(self, delivery: Delivery) -> DeliveryState:
        """Deliver one notification; must never raise.

        Args:
            delivery: The notification to deliver.

        Returns:
            ``"sent"`` on success, ``"failed"`` when the transport refused it,
            ``"unavailable"`` when the user has no destination on this channel.
        """


class EmailChannel(NotificationChannel):
    """Email through the shared SMTP transport, rendered by :mod:`.email_render`."""

    name = EMAIL

    def send(self, delivery: Delivery) -> DeliveryState:
        """Render and send the notification email.

        Args:
            delivery: The notification to deliver.

        Returns:
            The delivery state.
        """
        address = resolve_email(delivery.username)
        if address is None or delivery.event_type not in EMAIL_EVENT_TYPES:
            return "unavailable"
        if settings.local_mode and not mailable(address):
            return "unavailable"
        try:
            facts = {key: value for key, value in delivery.facts.items() if key in _EMAIL_FACT_KEYS}
            rendered = render_notification_email(
                delivery.event_type,
                delivery.title,
                facts,
                delivery.link or "/",
                detail=delivery.detail,
                settings_url=NOTIFICATION_SETTINGS_URL,
            )
        except Exception:
            logger.warning("Rendering notification %s failed", delivery.notification_id, exc_info=True)
            return "failed"
        sent = send_mail(address, rendered.subject, rendered.html, text_body=rendered.text)
        return "sent" if sent else "failed"


def vapid_public_key() -> str | None:
    """Return the VAPID public key browsers subscribe with, when push is configured.

    Returns:
        The urlsafe-base64 public key, or ``None`` while either key is unset.
    """
    if not settings.vapid_public_key or settings.vapid_private_key is None:
        return None
    return settings.vapid_public_key


def _vapid_subject() -> str:
    """Return the VAPID ``sub`` claim.

    Returns:
        ``VAPID_SUBJECT``, or the app's public URL when it is unset.
    """
    return settings.vapid_subject or settings.app_public_url


def push_payload(delivery: Delivery, locale: str | None = None) -> dict[str, str]:
    """Build the JSON the service worker turns into a system notification.

    Args:
        delivery: The notification to deliver.
        locale: Display locale; ``None`` keeps the Hebrew base that Web Push uses.

    Returns:
        ``title``, ``body``, ``url`` (an app path or absolute URL) and ``tag``
        (one per run and event, so a repeat replaces the shown one).
    """
    if delivery.event_type in EMAIL_EVENT_TYPES:
        title = t_locale(locale, f"email.event.{delivery.event_type}.headline", title=delivery.title)
    else:
        title = delivery.title
    return {
        "title": title.translate(_BIDI_ISOLATES),
        "body": delivery.detail or "",
        "url": delivery.link or "/",
        "tag": f"{delivery.event_type}:{delivery.optimization_id or delivery.notification_id}",
    }


def _status_code(exc: WebPushException) -> int | None:
    """Return the push service's HTTP status from a failed push.

    Args:
        exc: The pywebpush failure.

    Returns:
        The status code, or ``None`` when the request never got an answer.
    """
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


class ChromeChannel(NotificationChannel):
    """Browser Web Push (VAPID) to every browser the user subscribed."""

    name = CHROME

    def send(self, delivery: Delivery) -> DeliveryState:
        """Push the notification to each of the user's subscriptions.

        A subscription the push service reports gone (404/410) is deleted.

        Args:
            delivery: The notification to deliver.

        Returns:
            ``"sent"`` when at least one browser accepted it, ``"unavailable"``
            when push is not configured or the user has no live subscription,
            ``"failed"`` otherwise.
        """
        private_key = settings.vapid_private_key
        engine = notification_engine()
        if vapid_public_key() is None or private_key is None or engine is None:
            return "unavailable"
        data = json.dumps(push_payload(delivery), ensure_ascii=False)
        sent = False
        live = 0
        with Session(engine) as session:
            subscriptions = list(
                session.scalars(
                    select(PushSubscriptionModel).where(PushSubscriptionModel.username == delivery.username)
                )
            )
            for subscription in subscriptions:
                try:
                    webpush(
                        subscription_info={
                            "endpoint": subscription.endpoint,
                            "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
                        },
                        data=data,
                        vapid_private_key=private_key.get_secret_value(),
                        # pywebpush writes ``aud`` and ``exp`` into the claims, so each call gets its own dict.
                        vapid_claims={"sub": _vapid_subject()},
                        ttl=PUSH_TTL_SECONDS,
                        timeout=PUSH_TIMEOUT_SECONDS,
                    )
                except WebPushException as exc:
                    if _status_code(exc) in _GONE_STATUSES:
                        session.delete(subscription)
                        continue
                    live += 1
                    logger.warning(
                        "Web push for notification %s failed: %s", delivery.notification_id, _status_code(exc)
                    )
                    continue
                except Exception:
                    live += 1
                    logger.warning("Web push for notification %s failed", delivery.notification_id, exc_info=True)
                    continue
                live += 1
                sent = True
                subscription.last_used_at = datetime.now(UTC)
            session.commit()
        if sent:
            return "sent"
        return "failed" if live else "unavailable"


class SlackChannel(NotificationChannel):
    """Slack messages from the Skynet app, to a DM with the installer or a chosen channel."""

    name = SLACK

    def send(self, delivery: Delivery) -> DeliveryState:
        """Post the notification with the user's Slack install.

        A Slack error that won't go away by retrying (a revoked token, a
        channel the bot left) marks the connection broken, so later sends are
        ``"unavailable"`` until the user reconnects or picks another target.

        Args:
            delivery: The notification to deliver.

        Returns:
            ``"sent"``; ``"unavailable"`` when Slack is not configured or the
            user has no working connection; ``"failed"`` otherwise.
        """
        engine = notification_engine()
        if not slack.slack_configured() or engine is None:
            return "unavailable"
        with Session(engine) as session:
            row = session.get(SlackConnectionModel, delivery.username)
            if row is None or row.status != slack.STATUS_CONNECTED:
                return "unavailable"
            try:
                token = slack.decrypt_token(row.bot_token_encrypted)
            except InvalidToken:
                slack.mark_broken(row, "invalid_auth")
                session.commit()
                return "unavailable"
            try:
                facts = {key: value for key, value in delivery.facts.items() if key in _EMAIL_FACT_KEYS}
                text, blocks = slack.message_blocks(
                    delivery.event_type, delivery.title, facts, delivery.link, delivery.detail
                )
                target = row.channel_id
                if target is None:
                    if row.dm_channel_id is None:
                        row.dm_channel_id = slack.open_dm(token, row.slack_user_id)
                    target = row.dm_channel_id
                slack.post_message(token, target, text, blocks)
            except slack.SlackApiError as exc:
                logger.warning("Slack post for notification %s failed: %s", delivery.notification_id, exc.code)
                if exc.code in slack.BROKEN_ERRORS:
                    slack.mark_broken(row, exc.code)
                session.commit()
                return "failed"
            except Exception:
                logger.warning("Slack post for notification %s failed", delivery.notification_id, exc_info=True)
                session.commit()
                return "failed"
            session.commit()
        return "sent"


class MacOSChannel(NotificationChannel):
    """Native macOS notifications, shown by the Mac app (local mode only).

    The backend cannot reach the Mac's notification center itself, so a send
    only marks the delivery ``queued``; the open app claims it through
    ``POST /local/notifications/native/claim`` and shows it.
    """

    name = MACOS

    def send(self, delivery: Delivery) -> DeliveryState:
        """Queue the notification for the Mac app.

        Args:
            delivery: The notification to deliver.

        Returns:
            ``"queued"`` in local mode, ``"unavailable"`` on a hosted server.
        """
        return "queued" if settings.local_mode else "unavailable"


_REGISTRY: dict[str, NotificationChannel] = {}


def register_channel(channel: NotificationChannel) -> None:
    """Register (or replace) the channel instance used for ``channel.name``.

    Args:
        channel: The channel implementation.
    """
    _REGISTRY[channel.name] = channel


def get_channel(name: str) -> NotificationChannel | None:
    """Return the registered channel for ``name``.

    Args:
        name: Catalog channel name.

    Returns:
        The channel, or ``None`` when nothing is registered under that name.
    """
    return _REGISTRY.get(name)


for _channel in (EmailChannel(), ChromeChannel(), SlackChannel(), MacOSChannel()):
    register_channel(_channel)
