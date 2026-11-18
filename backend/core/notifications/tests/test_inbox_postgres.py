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

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine, delete, make_url, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from core.config import app_env
from core.notifications import news as news_module
from core.notifications.inbox import list_page, mark_all_read, mark_read, unread_counts
from core.notifications.news import NewsEntry, ensure_news
from core.storage.models import NotificationModel
from core.storage.tests.test_remote_jobstore_postgres import migrate_test_db

TEST_DB_URL = app_env("APP_TEST_DB_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DB_URL or not TEST_DB_URL.startswith("postgresql"),
    reason="APP_TEST_DB_URL not set to a postgresql:// URL — skipping live-DB tests.",
)

_NEWS = NewsEntry("2026-10-09-pg", "PG", date(2026, 10, 9), "Body")


def _migrated_engine(db_url: str) -> Engine:
    """Upgrade ``db_url`` to Alembic head and open an engine on it.

    Args:
        db_url: SQLAlchemy URL of the live Postgres test target.

    Returns:
        The engine. The caller disposes it.
    """
    migrate_test_db(db_url)
    return create_engine(db_url)


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    """Upgrade the live database to Alembic head.

    Yields:
        The engine.
    """
    engine = _migrated_engine(TEST_DB_URL or "")
    try:
        yield engine
    finally:
        engine.dispose()


def test_engine_setup_uses_the_test_url_when_remote_db_url_points_elsewhere(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Migrate the ``APP_TEST_DB_URL`` database even when ``REMOTE_DB_URL`` names another database.

    The ``engine`` fixture migrates the database first, so the upgrade here has no migration to run.
    An upgrade that went to the other database still raises, because it cannot connect.

    Args:
        engine: The live engine. Only its migration is used.
        monkeypatch: pytest's ``MonkeyPatch`` fixture.
    """
    other_url = make_url(TEST_DB_URL or "").set(database=f"sky824_missing_{uuid.uuid4().hex[:8]}")
    other = other_url.render_as_string(hide_password=False)
    monkeypatch.setenv("REMOTE_DB_URL", other)

    # The other database does not exist, so an upgrade that went to it would raise.
    _migrated_engine(TEST_DB_URL or "").dispose()

    assert os.environ["REMOTE_DB_URL"] == other


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
