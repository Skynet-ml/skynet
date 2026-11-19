"""Tests for boot-time Alembic version sync (:func:`sync_migration_head`).

The non-Postgres no-op guard runs in ordinary (SQLite) CI. The adopt-vs-upgrade
behaviour needs a real Postgres and is gated on ``APP_TEST_DB_URL`` — a
dedicated opt-in, **never** ``REMOTE_DB_URL``, because these cases wipe a
database (``DROP SCHEMA public CASCADE``) and ``tests/conftest.py`` loads
``backend/.env`` into the environment: a developer's ordinary config must not
be able to unlock a wipe of their local database. Point it at a throwaway
database to exercise the live cases. The cases wipe only a new database
that the module creates on the same server and drops at the end, so the
other ``APP_TEST_DB_URL`` modules keep their tables in a parallel run::

    APP_TEST_DB_URL=postgresql://postgres:test@localhost:5432/testdb \
        pytest backend/core/storage/tests/test_migrate.py

Without that env var the live-DB cases are skipped, so unit-test CI is unaffected.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.orm import Session

from alembic import command
from core.config import app_env, settings
from core.storage import remote
from core.storage.migrate import stamp_if_unadopted, sync_migration_head, upgrade_if_adopted
from core.storage.models import (
    Base,
    BillingCustomerModel,
    ConversationEmbeddingModel,
    JobEmbeddingModel,
)
from core.storage.tests.test_remote_jobstore_postgres import own_database

_BACKEND_DIR = Path(__file__).resolve().parents[3]
# Resolved from the migration scripts, not pinned: every new migration moves the
# head, and a stale pin fails all live-DB cases (which plain CI never runs).
_HEAD = ScriptDirectory.from_config(Config(str(_BACKEND_DIR / "alembic.ini"))).get_current_head()
# The schema state just before the one-time-500 grant migration.
_PRE_500 = "f2b3c4d5e6a7"
# The schema state just before credits were renamed to cents (#502).
_PRE_CENTS_RENAME = "c3e5a7b9d1f2"
TEST_DB_URL = app_env("APP_TEST_DB_URL")

_needs_pg = pytest.mark.skipif(
    not TEST_DB_URL or not TEST_DB_URL.startswith("postgresql"),
    reason="APP_TEST_DB_URL not set to a postgresql:// URL — skipping live-DB migration tests.",
)


def test_sync_migration_head_is_noop_off_postgres() -> None:
    """On SQLite the lock helper yields ``None`` and Alembic is left untouched."""
    engine = create_engine("sqlite://")
    sync_migration_head(engine)
    assert not inspect(engine).has_table("alembic_version")


def _build_schema_like_prod(engine: Engine) -> None:
    """Create the full ORM schema minus the pgvector tables, as prod boot does.

    Args:
        engine: Engine on the wiped live-Postgres target.
    """
    embedding = {JobEmbeddingModel.__table__, ConversationEmbeddingModel.__table__}
    tables = [table for table in Base.metadata.sorted_tables if table not in embedding]
    Base.metadata.create_all(engine, tables=tables)


def _version(engine: Engine) -> str | None:
    """Return the stamped Alembic revision, or ``None`` when the DB is unadopted."""
    with engine.connect() as conn:
        if not inspect(conn).has_table("alembic_version"):
            return None
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


def _grant(engine: Engine) -> int:
    """Return the seeded account's remaining free-grant cents."""
    with engine.connect() as conn:
        return conn.execute(text("SELECT grant_remaining FROM billing_customers WHERE username = 'a@x.com'")).scalar()


def _seed(engine: Engine, grant: int) -> None:
    """Insert one billing row with the given remaining grant.

    Args:
        engine: Engine on the live-Postgres target.
        grant: Remaining free-grant cents to seed.
    """
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(
                username="a@x.com",
                stripe_customer_id="local:test",
                balance_cents=0,
                grant_remaining=grant,
            )
        )
        session.commit()


@pytest.fixture(autouse=True)
def _no_step_log_shards(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty the step log shard map for each test.

    Each boot sync and store here migrates every ``STEP_LOG_SHARDS`` database, and
    ``backend/.env`` can name real ones (SKY-831).

    Args:
        monkeypatch: pytest's ``MonkeyPatch`` fixture.
    """
    monkeypatch.setattr(settings, "step_log_shards", {})


@pytest.fixture(scope="module")
def migrate_db_url() -> Iterator[str]:
    """Yield the URL of a new database that only this module uses, on the ``APP_TEST_DB_URL`` server.

    The cases wipe ``public``. In ``APP_TEST_DB_URL`` itself, that deleted the tables
    of the modules that other xdist workers ran at the same time (SKY-831).

    Guards against catastrophe: the cases ``DROP SCHEMA public CASCADE``, and
    ``own_database`` refuses any non-local host — a stray ``APP_TEST_DB_URL`` pointing
    at a managed Postgres (e.g. a proxy domain) skips instead of touching its server.

    Yields:
        The SQLAlchemy URL of the new database.
    """
    with own_database(TEST_DB_URL or "", "migrate") as url:
        yield url


@pytest.fixture
def fresh_pg(migrate_db_url: str) -> Iterator[Engine]:
    """Yield an engine over a freshly wiped ``public`` schema of the module's own database.

    Args:
        migrate_db_url: The module's own database.
    """
    engine = create_engine(migrate_db_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    try:
        yield engine
    finally:
        engine.dispose()


@_needs_pg
def test_unadopted_database_is_stamped_not_replayed(fresh_pg: Engine) -> None:
    """A create_all-built, unstamped DB is stamped at head without running migrations."""
    _build_schema_like_prod(fresh_pg)
    _seed(fresh_pg, 200)
    assert _version(fresh_pg) is None
    sync_migration_head(fresh_pg)
    assert _version(fresh_pg) == _HEAD
    # Stamping must adopt in place, never replay the 500-grant migration onto rows.
    assert _grant(fresh_pg) == 200


def _replay_to(engine: Engine, revision: str) -> None:
    """Build the schema as it stood at ``revision`` by replaying migration history.

    Runs Alembic standalone, as the CLI does: a migration that builds an index
    concurrently can't run inside a caller-owned transaction. A call from code
    also upgrades every ``STEP_LOG_SHARDS`` database, so the shard map is empty
    for the length of the upgrade (SKY-831).

    Args:
        engine: Engine on the throwaway target. env.py's ``REMOTE_DB_URL`` points at it.
        revision: The Alembic revision to stop at.
    """
    cfg = Config(str(_BACKEND_DIR / "alembic.ini"))
    cfg.attributes["configure_logger"] = False
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("REMOTE_DB_URL", engine.url.render_as_string(hide_password=False))
        mp.setattr(settings, "step_log_shards", {})
        command.upgrade(cfg, revision)


@_needs_pg
def test_replay_skips_the_step_log_shards_from_the_settings(fresh_pg: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replay only the test database, even when ``STEP_LOG_SHARDS`` names another database (SKY-831).

    Args:
        fresh_pg: The wiped test database.
        monkeypatch: Sets ``settings.step_log_shards`` for this test.
    """
    shard_url = fresh_pg.url.set(database=f"sky831_missing_shard_{fresh_pg.url.database}")
    shards = {"proof": SecretStr(shard_url.render_as_string(hide_password=False))}
    monkeypatch.setattr(settings, "step_log_shards", shards)

    # The shard database does not exist, so a shard upgrade would raise.
    _replay_to(fresh_pg, _PRE_500)

    assert _version(fresh_pg) == _PRE_500
    assert settings.step_log_shards is shards


@_needs_pg
def test_adopted_database_applies_pending_migrations(fresh_pg: Engine) -> None:
    """A DB at an older revision is upgraded, actually running the pending migrations."""
    _replay_to(fresh_pg, _PRE_500)
    with fresh_pg.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO billing_customers (username, stripe_customer_id, grant_remaining) "
                "VALUES ('a@x.com', 'local:test', 200)"
            )
        )
    sync_migration_head(fresh_pg)
    assert _version(fresh_pg) == _HEAD
    # The pending chain really ran: the one-time-500 migration tops the 200
    # grant up to 500, then c4f1a7d9e2b5 clears every leftover grant to 0.
    assert _grant(fresh_pg) == 0


@_needs_pg
def test_sync_at_head_is_idempotent(fresh_pg: Engine) -> None:
    """Running the sync twice against a head DB leaves it at head."""
    _build_schema_like_prod(fresh_pg)
    sync_migration_head(fresh_pg)
    sync_migration_head(fresh_pg)
    assert _version(fresh_pg) == _HEAD


@_needs_pg
def test_sync_preserves_root_logging_config(fresh_pg: Engine) -> None:
    """Boot-time sync must not let env.py's fileConfig hijack root logging.

    ``fileConfig(alembic.ini)`` replaces the root handlers and raises the root
    level to WARN, which silenced every app INFO log (and dropped the JSON
    format) for the rest of the process lifetime on both API and worker pods.
    """
    _build_schema_like_prod(fresh_pg)
    root = logging.getLogger()
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)
    level_before = root.level
    try:
        sync_migration_head(fresh_pg)
        assert sentinel in root.handlers
        assert root.level == level_before
    finally:
        root.removeHandler(sentinel)


@_needs_pg
def test_boot_migrates_before_create_all(fresh_pg: Engine) -> None:
    """A table-renaming migration lands when boot upgrades before ``create_all``.

    Upgrading after ``create_all`` failed the #502 deploy: ``create_all`` had
    already made the empty renamed table, so the rename collided with it.
    """
    _replay_to(fresh_pg, _PRE_CENTS_RENAME)
    upgrade_if_adopted(fresh_pg)
    _build_schema_like_prod(fresh_pg)
    stamp_if_unadopted(fresh_pg)
    assert _version(fresh_pg) == _HEAD


@_needs_pg
def test_store_boot_upgrades_before_create_all(fresh_pg: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Boot applies pending migrations before ``create_all`` and stamps after it.

    The migration history guards itself against the reverse order (``IF NOT
    EXISTS`` and similar), so only the call order itself shows the regression.
    """
    calls: list[str] = []
    create_all = Base.metadata.create_all
    monkeypatch.setattr(remote, "upgrade_if_adopted", lambda engine: calls.append("upgrade"))
    monkeypatch.setattr(remote, "stamp_if_unadopted", lambda engine: calls.append("stamp"))
    monkeypatch.setattr(
        Base.metadata,
        "create_all",
        lambda *args, **kwargs: (calls.append("create_all"), create_all(*args, **kwargs))[1],
    )
    store = remote.RemoteDBJobStore(fresh_pg.url.render_as_string(hide_password=False))
    store.engine.dispose()
    assert calls == ["upgrade", "create_all", "stamp"]
