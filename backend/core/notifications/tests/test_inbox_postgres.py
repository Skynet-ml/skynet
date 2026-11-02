"""Live-Postgres tests for the product-news filter in ``core.notifications.inbox``.

The in-memory tests in ``test_inbox.py`` run on SQLite. They cannot prove that
the retraction filter, and its empty ``IN ()`` when no news file is shipped,
work on the database the product uses. These tests do, and are gated on
``APP_TEST_DB_URL`` like ``test_native_postgres.py``. Use a throwaway database::

    APP_TEST_DB_URL=postgresql://skynet:skynet@localhost:5432/testdb \\
        pytest backend/core/notifications/tests/test_inbox_postgres.py

Without that env var the module is skipped.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alembic import command
from core.config import app_env
from core.notifications import news as news_module
from core.notifications.inbox import list_page, mark_all_read, mark_read, unread_counts
from core.notifications.news import NewsEntry, ensure_news
from core.storage.models import NotificationModel
from core.storage.tests.test_remote_jobstore_postgres import _reset_legacy_schema

TEST_DB_URL = app_env("APP_TEST_DB_URL")
BACKEND_DIR = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.skipif(
    not TEST_DB_URL or not TEST_DB_URL.startswith("postgresql"),
    reason="APP_TEST_DB_URL not set to a postgresql:// URL — skipping live-DB tests.",
)

_NEWS = NewsEntry("2026-10-09-pg", "PG", date(2026, 10, 9), "Body")


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    """Upgrade the live database to Alembic head.

    Returns:
        The engine.
    """
    _reset_legacy_schema(TEST_DB_URL or "")
    alembic_cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", TEST_DB_URL or "")
    command.upgrade(alembic_cfg, "head")

    engine = create_engine(TEST_DB_URL or "")
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """Open a session for one unique user and delete that user's rows afterwards.

    Args:
        engine: The live engine.

    Yields:
        An open session; ``session.info["user"]`` holds the username.
    """
    username = f"inbox-{uuid.uuid4().hex[:8]}@skynet.test"
    with Session(engine) as session:
        session.info["user"] = username
        try:
            yield session
        finally:
            session.rollback()
            session.execute(delete(NotificationModel).where(NotificationModel.username == username))
            session.commit()


def test_withdrawn_news_is_hidden_when_no_news_is_shipped(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """With zero shipped files (an empty ``IN ()``), copied news is hidden and other items still show."""
    user = session.info["user"]
    shipped = [_NEWS]
    monkeypatch.setattr(news_module, "load_news", lambda: tuple(shipped))
    ensure_news(session, user)
    session.add(
        NotificationModel(
            username=user,
            event_type="finished",
            title="Run",
            facts={},
            created_at=datetime.now(UTC),
            deliveries={},
            in_inbox=True,
        )
    )
    session.commit()
    assert unread_counts(session, user)["everything"] == 2

    shipped.clear()
    rows, _ = list_page(session, user, "everything", None, 10)
    assert [row.event_type for row in rows] == ["finished"]
    assert unread_counts(session, user)["everything"] == 1
    assert mark_all_read(session, user, "everything") == 1

    news_row = session.scalars(
        select(NotificationModel).where(
            NotificationModel.username == user, NotificationModel.event_type == "product_news"
        )
    ).one()
    assert mark_read(session, user, news_row.id) is True
    assert news_row.read_at is None
