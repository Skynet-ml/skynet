"""Run the SKY-676 migration in a real PostgreSQL database.

The migration drops the grid pair table, the jobs pair columns with their FK
and index, and ``job_logs.pair_index``. Only PostgreSQL runs it, so this
module needs a disposable database in ``APP_BUDGET_TEST_DB_URL`` and skips
without one. Each test works in its own schema and drops it afterwards.
Never point the URL at staging or production.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, create_engine, inspect, text
from sqlalchemy.engine.interfaces import ReflectedForeignKeyConstraint

from ...config import app_env
from ..models import Base

TEST_DB_URL = app_env("APP_BUDGET_TEST_DB_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL or not TEST_DB_URL.startswith("postgresql"),
    reason="Set APP_BUDGET_TEST_DB_URL to an explicitly disposable PostgreSQL database.",
)

_MIGRATION = Path(__file__).resolve().parents[3] / "alembic" / "versions" / "882f3bdd5fd9_drop_grid_pair_schema.py"


def _load_migration() -> ModuleType:
    """Load the migration module from its file.

    Returns:
        The migration module.
    """
    spec = importlib.util.spec_from_file_location("sky676_drop_grid_pair_schema", _MIGRATION)
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load_migration()


@pytest.fixture
def conn() -> Iterator[Connection]:
    """Yield a connection to a fresh schema with the pre-migration grid shape, dropped after."""
    url = TEST_DB_URL
    assert url
    schema = f"sky676_test_{uuid4().hex}"
    admin = create_engine(url)
    with admin.begin() as setup:
        setup.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema},public"})
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            # The ORM no longer has the grid shape, so add it as the old revisions did.
            connection.execute(
                text(
                    """
                    ALTER TABLE jobs
                        ADD COLUMN parent_optimization_id VARCHAR(36)
                            REFERENCES jobs (optimization_id) ON DELETE CASCADE,
                        ADD COLUMN pair_index INTEGER
                    """
                )
            )
            connection.execute(text("CREATE INDEX ix_jobs_parent_optimization_id ON jobs (parent_optimization_id)"))
            connection.execute(text("ALTER TABLE job_logs ADD COLUMN pair_index INTEGER"))
            connection.execute(
                text(
                    """
                    CREATE TABLE grid_pair_results (
                        optimization_id VARCHAR(36) NOT NULL REFERENCES jobs(optimization_id) ON DELETE CASCADE,
                        pair_index INTEGER NOT NULL,
                        result JSONB NOT NULL,
                        stored_bytes BIGINT NOT NULL DEFAULT 0,
                        updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
                        PRIMARY KEY (optimization_id, pair_index)
                    )
                    """
                )
            )
            connection.execute(
                Base.metadata.tables["jobs"].insert(),
                [{"optimization_id": "repo-run", "optimization_type": "blackbox", "payload": {}}],
            )
            connection.execute(
                Base.metadata.tables["job_logs"].insert(),
                [{"optimization_id": "repo-run", "level": "INFO", "logger": "t", "message": "m"}],
            )
            connection.execute(
                Base.metadata.tables["gepa_checkpoints"].insert(), [{"optimization_id": "repo-run", "data": b"x"}]
            )
        with engine.connect() as connection:
            yield connection
    finally:
        engine.dispose()
        with admin.begin() as teardown:
            teardown.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def _run(connection: Connection, step: str) -> None:
    """Run the migration's ``upgrade`` or ``downgrade`` on ``connection`` and commit.

    Args:
        connection: The test schema connection.
        step: ``"upgrade"`` or ``"downgrade"``.
    """
    with Operations.context(MigrationContext.configure(connection)):
        getattr(migration, step)()
    connection.commit()


def _columns(connection: Connection, table: str) -> set[str]:
    """Return the column names of ``table``.

    Args:
        connection: The test schema connection.
        table: The table name.

    Returns:
        The column names.
    """
    return {column["name"] for column in inspect(connection).get_columns(table)}


def _parent_fks(connection: Connection) -> list[ReflectedForeignKeyConstraint]:
    """Return the ``jobs`` FKs on ``parent_optimization_id``.

    Args:
        connection: The test schema connection.

    Returns:
        The matching foreign keys.
    """
    return [
        fk
        for fk in inspect(connection).get_foreign_keys("jobs")
        if fk["constrained_columns"] == ["parent_optimization_id"]
    ]


def _assert_dropped(connection: Connection) -> None:
    """Assert that the grid schema is gone and the live rows and checkpoint key stay.

    Args:
        connection: The test schema connection.
    """
    inspector = inspect(connection)
    assert not inspector.has_table("grid_pair_results")
    assert not {"parent_optimization_id", "pair_index"} & _columns(connection, "jobs")
    assert "pair_index" not in _columns(connection, "job_logs")
    assert _parent_fks(connection) == []
    assert "ix_jobs_parent_optimization_id" not in {index["name"] for index in inspector.get_indexes("jobs")}
    assert "pair_index" in _columns(connection, "gepa_checkpoints")
    for table in ("jobs", "job_logs", "gepa_checkpoints"):
        assert connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 1, table


def test_upgrade_drops_the_grid_table_and_pair_columns(conn: Connection) -> None:
    """Drop the grid table, the pair columns, the FK and the index, and keep the live rows."""
    _run(conn, "upgrade")

    _assert_dropped(conn)


def test_upgrade_runs_twice(conn: Connection) -> None:
    """Run the upgrade again with no error and no change."""
    _run(conn, "upgrade")

    _run(conn, "upgrade")

    _assert_dropped(conn)


def test_downgrade_recreates_the_empty_schema(conn: Connection) -> None:
    """Recreate the empty grid table and the nullable pair columns with the FK and index."""
    _run(conn, "upgrade")

    _run(conn, "downgrade")

    inspector = inspect(conn)
    assert inspector.has_table("grid_pair_results")
    assert conn.execute(text("SELECT count(*) FROM grid_pair_results")).scalar_one() == 0
    assert {"parent_optimization_id", "pair_index"} <= _columns(conn, "jobs")
    assert "pair_index" in _columns(conn, "job_logs")
    [parent_fk] = _parent_fks(conn)
    assert parent_fk["referred_table"] == "jobs"
    assert parent_fk.get("options", {}).get("ondelete") == "CASCADE"
    assert "ix_jobs_parent_optimization_id" in {index["name"] for index in inspector.get_indexes("jobs")}
    assert conn.execute(text("SELECT parent_optimization_id, pair_index FROM jobs")).one() == (None, None)
