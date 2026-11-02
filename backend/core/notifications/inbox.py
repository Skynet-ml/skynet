"""Inbox reads, read-state writes and removal over the ``notifications`` table."""

from __future__ import annotations

import base64
import binascii
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from ..storage.models import NotificationModel
from .catalog import FOCUSED_EVENTS, PRODUCT_NEWS
from .news import live_news_keys

InboxTab = Literal["focused", "everything"]


def _tab_filter(username: str, tab: InboxTab) -> list[ColumnElement[bool]]:
    """Return the WHERE clauses selecting one user's visible items on a tab.

    Args:
        username: Inbox owner.
        tab: ``focused`` or ``everything``.

    Returns:
        SQL conditions to AND together. Product news shows only while its
        file is shipped and not expired.
    """
    clauses: list[ColumnElement[bool]] = [
        NotificationModel.username == username,
        NotificationModel.in_inbox.is_(True),
        or_(NotificationModel.event_type != PRODUCT_NEWS, NotificationModel.dedupe_key.in_(live_news_keys())),
    ]
    if tab == "focused":
        clauses.append(NotificationModel.event_type.in_(sorted(FOCUSED_EVENTS)))
    return clauses


def _withdrawn(row: NotificationModel) -> bool:
    """Tell whether a row is product news whose file is gone or expired.

    Args:
        row: The item.

    Returns:
        Whether the inbox hides the row; see :func:`_tab_filter`.
    """
    return row.event_type == PRODUCT_NEWS and row.dedupe_key not in live_news_keys()


def encode_cursor(row: NotificationModel) -> str:
    """Encode a row's position as an opaque pagination cursor.

    Args:
        row: The last row of a page.

    Returns:
        A URL-safe cursor string.
    """
    raw = f"{row.created_at.isoformat()}|{row.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, str] | None:
    """Decode a cursor from :func:`encode_cursor`.

    Args:
        cursor: The opaque cursor.

    Returns:
        ``(created_at, id)``, or ``None`` when the cursor is malformed.
    """
    try:
        created, _, row_id = base64.urlsafe_b64decode(cursor.encode()).decode().partition("|")
        parsed = datetime.fromisoformat(created)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    if not row_id:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)), row_id


def list_page(
    session: Session, username: str, tab: InboxTab, cursor: str | None, limit: int
) -> tuple[list[NotificationModel], str | None]:
    """Return one page of a tab, newest first.

    Args:
        session: Open session.
        username: Inbox owner.
        tab: ``focused`` or ``everything``.
        cursor: Cursor from the previous page, or ``None`` for the first page.
        limit: Page size.

    Returns:
        ``(rows, next_cursor)``; ``next_cursor`` is ``None`` on the last page.
    """
    query = select(NotificationModel).where(*_tab_filter(username, tab))
    position = decode_cursor(cursor) if cursor else None
    if position is not None:
        created, row_id = position
        query = query.where(
            or_(
                NotificationModel.created_at < created,
                and_(NotificationModel.created_at == created, NotificationModel.id < row_id),
            )
        )
    rows = list(
        session.scalars(
            query.order_by(NotificationModel.created_at.desc(), NotificationModel.id.desc()).limit(limit + 1)
        )
    )
    if len(rows) > limit:
        return rows[:limit], encode_cursor(rows[limit - 1])
    return rows, None


def unread_counts(session: Session, username: str) -> dict[str, int]:
    """Count unread items per tab.

    Args:
        session: Open session.
        username: Inbox owner.

    Returns:
        ``{"focused": n, "everything": m}``.
    """
    counts = {}
    for tab in ("focused", "everything"):
        counts[tab] = int(
            session.scalar(
                select(func.count())
                .select_from(NotificationModel)
                .where(*_tab_filter(username, tab), NotificationModel.read_at.is_(None))
            )
            or 0
        )
    return counts


def mark_read(session: Session, username: str, notification_id: str, now: datetime | None = None) -> bool:
    """Mark one of the user's items read (idempotent).

    Args:
        session: Open session; committed here.
        username: Inbox owner.
        notification_id: The item.
        now: Clock override for tests.

    Returns:
        Whether the item exists and belongs to ``username``. Withdrawn
        product news counts as handled but is left unchanged.
    """
    row = session.get(NotificationModel, notification_id)
    if row is None or row.username != username:
        return False
    # A tab opened before a deploy can still show withdrawn news; succeed without
    # a write so the client drops the item instead of showing an error.
    if _withdrawn(row):
        return True
    if row.read_at is None:
        row.read_at = now or datetime.now(UTC)
        session.commit()
    return True


def mark_all_read(session: Session, username: str, tab: InboxTab, now: datetime | None = None) -> int:
    """Mark every unread item on a tab read.

    Args:
        session: Open session; committed here.
        username: Inbox owner.
        tab: ``focused`` or ``everything``.
        now: Clock override for tests.

    Returns:
        The number of items marked.
    """
    result = session.execute(
        update(NotificationModel)
        .where(*_tab_filter(username, tab), NotificationModel.read_at.is_(None))
        .values(read_at=now or datetime.now(UTC))
    )
    session.commit()
    return int(result.rowcount or 0)


def remove(session: Session, username: str, notification_id: str) -> bool:
    """Take one of the user's items out of the inbox (idempotent).

    The row is hidden, not deleted: a held email, Chrome or Slack send is
    still released by the hold sweeper, read state is left alone so removal
    never cancels that send, and a product-news row stays as the user's
    dismissal so :func:`~core.notifications.news.ensure_news` won't re-add it.

    Args:
        session: Open session; committed here.
        username: Inbox owner.
        notification_id: The item.

    Returns:
        Whether the item exists and belongs to ``username``. Withdrawn
        product news counts as handled but is left unchanged.
    """
    row = session.get(NotificationModel, notification_id)
    if row is None or row.username != username:
        return False
    # A tab opened before a deploy can still show withdrawn news; succeed without
    # a write so the client drops the item instead of showing an error.
    if _withdrawn(row):
        return True
    if row.in_inbox:
        row.in_inbox = False
        session.commit()
    return True


def remove_all(session: Session, username: str, tab: InboxTab) -> int:
    """Take every item on a tab out of the user's inbox; see :func:`remove`.

    Args:
        session: Open session; committed here.
        username: Inbox owner.
        tab: ``focused`` or ``everything``.

    Returns:
        The number of items removed.
    """
    result = session.execute(update(NotificationModel).where(*_tab_filter(username, tab)).values(in_inbox=False))
    session.commit()
    return int(result.rowcount or 0)
