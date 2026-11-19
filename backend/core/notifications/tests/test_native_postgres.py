"""Live-Postgres tests for the Mac app's notification claim (``core.notifications.native``).

SQLite compiles ``FOR UPDATE SKIP LOCKED`` away and stores JSON as text, so
the in-memory tests in ``test_local_channels.py`` cannot prove that two
windows never claim the same row, or that the JSONB lease and expiry filters
work on the database the product uses. These tests do, and are gated on
``APP_TEST_DB_URL`` like ``core/storage/tests/test_remote_jobstore_postgres.py``.
Use a throwaway database::

    APP_TEST_DB_URL=postgresql://skynet:skynet@localhost:5432/testdb \\
        pytest backend/core/notifications/tests/test_native_postgres.py

Without that env var the module is skipped.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, delete, make_url, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from core.config import app_env, settings
from core.notifications.catalog import MACOS
from core.notifications.dispatch import notify, release_held
from core.notifications.native import CLAIM_BATCH, CLAIM_LEASE, EXPIRE_BATCH, claim_native, confirm_native
from core.notifications.preferences import configure_notification_preferences
from core.storage.models import NotificationModel, NotificationPreferenceModel
from core.storage.tests.test_remote_jobstore_postgres import migrate_test_db

TEST_DB_URL = app_env("APP_TEST_DB_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DB_URL or not TEST_DB_URL.startswith("postgresql"),
    reason="APP_TEST_DB_URL not set to a postgresql:// URL — skipping live-DB tests.",
)

_T0 = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def _migrated_engine(db_url: str) -> Engine:
    """Upgrade ``db_url`` to Alembic head and open an engine on it.

    Args:
        db_url: SQLAlchemy URL of the live Postgres test target.

    Returns:
        The engine. The caller disposes it.
    """
    migrate_test_db(db_url)
    return create_engine(db_url, pool_size=12)


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
def user(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Turn local mode on, wire the live notification store, and give the test its own user.

    The conftest resets the store before each test, so it is wired here. The
    user's rows are deleted after the test.

    Args:
        engine: The live engine.
        monkeypatch: pytest's ``MonkeyPatch`` fixture.

    Returns:
        A unique username.
    """
    monkeypatch.setattr(settings, "local_mode", True)
    configure_notification_preferences(engine)
    username = f"native-{uuid.uuid4().hex[:8]}@skynet.test"
    try:
        yield username
    finally:
        with Session(engine) as session:
            session.execute(delete(NotificationModel).where(NotificationModel.username == username))
            session.commit()


def _seed(username: str, count: int, now: datetime, prefix: str = "n") -> list[str]:
    """Queue ``count`` macOS notifications for ``username``.

    Args:
        username: The recipient.
        count: How many to queue.
        now: Their creation time.
        prefix: Dedupe-key prefix, unique per call in one test.

    Returns:
        The notification ids.
    """
    ids = [
        notify(username, "finished", f"opt-{i}", title="Run", dedupe_key=f"{prefix}{i}", now=now) for i in range(count)
    ]
    assert all(ids)
    return [notification_id for notification_id in ids if notification_id]


def _state(engine: Engine, notification_id: str) -> str:
    """Read one row's macOS delivery state.

    Args:
        engine: The live engine.
        notification_id: The notification id.

    Returns:
        The ``deliveries["macos"]["state"]`` value.
    """
    with Session(engine) as session:
        row = session.get(NotificationModel, notification_id)
        assert row is not None
        return row.deliveries[MACOS]["state"]


def test_concurrent_claims_never_hand_one_row_to_two_windows(engine: Engine, user: str) -> None:
    """Eight windows poll at once; every queued row goes to exactly one of them."""
    now = datetime.now(UTC)
    seeded = set(_seed(user, CLAIM_BATCH * 4, now))
    windows = 8
    start = threading.Barrier(windows)

    def window(_: int) -> list[str]:
        """Claim until several polls in a row come back empty.

        Returns:
            The ids this window claimed.
        """
        start.wait()
        got: list[str] = []
        empty = 0
        while empty < 5:
            batch = claim_native(engine, user, now=now)
            if not batch:
                empty += 1
                time.sleep(0.01)
                continue
            empty = 0
            got.extend(item.id for item in batch)
        return got

    with ThreadPoolExecutor(max_workers=windows) as pool:
        results = list(pool.map(window, range(windows)))

    claimed = [notification_id for batch in results for notification_id in batch]
    assert len(claimed) == len(set(claimed)), "A row was handed to two windows."
    assert set(claimed) == seeded
    assert sum(1 for batch in results if batch) > 1, "Only one window claimed; the test did not run concurrently."


def test_claim_skips_a_locked_row_without_waiting(engine: Engine, user: str) -> None:
    """A row that another transaction holds is skipped at once, not waited on."""
    locked, free = _seed(user, 2, _T0)
    with Session(engine) as holder, ThreadPoolExecutor(max_workers=1) as pool:
        holder.scalars(select(NotificationModel).where(NotificationModel.id == locked).with_for_update()).one()
        pending = pool.submit(claim_native, engine, user, None, _T0)
        try:
            claimed = pending.result(timeout=5)
        except TimeoutError:
            claimed = None
        finally:
            # Releasing the lock lets a blocked claim finish, so a failure here never hangs the run.
            holder.rollback()

    assert claimed is not None, "The claim waited on a locked row instead of skipping it."
    assert [item.id for item in claimed] == [free]
    assert [item.id for item in claim_native(engine, user, now=_T0)] == [locked]


def test_lease_filter_on_postgres(engine: Engine, user: str) -> None:
    """A leased row is not handed out until its lease ends; a confirm marks it sent."""
    (notification_id,) = _seed(user, 1, _T0)

    assert [item.id for item in claim_native(engine, user, now=_T0)] == [notification_id]
    assert claim_native(engine, user, now=_T0 + CLAIM_LEASE / 2) == []
    later = _T0 + CLAIM_LEASE + timedelta(seconds=1)
    assert [item.id for item in claim_native(engine, user, now=later)] == [notification_id]
    assert confirm_native(engine, user, [notification_id], now=later) == 1
    assert _state(engine, notification_id) == "sent"
    assert claim_native(engine, user, now=later + CLAIM_LEASE * 2) == []


def test_expiry_filters_on_postgres(engine: Engine, user: str) -> None:
    """Stale rows expire, but not while a lease on them is active, and at most one batch per poll."""
    (on_screen,) = _seed(user, 1, _T0, prefix="screen")
    shown_at = _T0 + timedelta(days=1) - timedelta(seconds=10)
    assert [item.id for item in claim_native(engine, user, now=shown_at)] == [on_screen]
    stale = _seed(user, EXPIRE_BATCH + 3, shown_at - timedelta(days=1), prefix="stale")
    (fresh,) = _seed(user, 1, shown_at, prefix="fresh")

    poll = shown_at + timedelta(seconds=11)
    assert [item.id for item in claim_native(engine, user, now=poll)] == [fresh]
    assert _state(engine, on_screen) == "claimed"
    states = [_state(engine, notification_id) for notification_id in stale]
    assert states.count("expired") == EXPIRE_BATCH
    assert states.count("queued") == 3

    assert confirm_native(engine, user, [on_screen], now=poll) == 1
    assert _state(engine, on_screen) == "sent"
    claim_native(engine, user, now=poll)
    assert {_state(engine, notification_id) for notification_id in stale} == {"expired"}


def test_claim_window_counts_from_the_hold_release_on_postgres(engine: Engine, user: str) -> None:
    """A row held for a full day is claimable after release; a row queued without ``since`` still expires by age."""
    with Session(engine) as session:
        session.add(NotificationPreferenceModel(username=user, hold_minutes=1440, held_channels=[MACOS]))
        session.add(
            NotificationModel(
                username=user,
                event_type="finished",
                title="Old run",
                created_at=_T0 - timedelta(days=2),
                deliveries={MACOS: {"state": "queued", "at": (_T0 - timedelta(days=2)).isoformat()}},
            )
        )
        session.commit()
        legacy = session.scalars(select(NotificationModel.id).where(NotificationModel.username == user)).one()
    try:
        (held,) = _seed(user, 1, _T0, prefix="held")
        released_at = _T0 + timedelta(minutes=1440)
        assert release_held(engine, now=released_at) >= 1

        assert [item.id for item in claim_native(engine, user, now=released_at + timedelta(seconds=2))] == [held]
        assert _state(engine, legacy) == "expired"
    finally:
        with Session(engine) as session:
            session.execute(delete(NotificationPreferenceModel).where(NotificationPreferenceModel.username == user))
            session.commit()
