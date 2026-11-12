"""Run the SKY-656 migration on seeded rows in a real PostgreSQL database.

The migration deletes the retired DSPy, grid and pair jobs with their rows,
and drops the grid columns and table. Only PostgreSQL runs it, so this module
needs a disposable database in ``APP_BUDGET_TEST_DB_URL`` and skips without
one. Each test works in its own schema and drops it afterwards. Never point
the URL at staging or production.
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

from ...config import app_env
from ..models import Base

TEST_DB_URL = app_env("APP_BUDGET_TEST_DB_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL or not TEST_DB_URL.startswith("postgresql"),
    reason="Set APP_BUDGET_TEST_DB_URL to an explicitly disposable PostgreSQL database.",
)

_MIGRATION = Path(__file__).resolve().parents[3] / "alembic" / "versions" / "f3a9c2d81b56_drop_dspy_runs.py"


def _load_migration() -> ModuleType:
    """Load the migration module from its file.

    Returns:
        The migration module.
    """
    spec = importlib.util.spec_from_file_location("sky656_drop_dspy_runs", _MIGRATION)
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load_migration()

_DEAD = ("dspy-run", "grid-parent", "grid-pair", "unified-run")
_LIVE = ("repo-run", "tagging-run", "untyped-run")


@pytest.fixture
def conn() -> Iterator[Connection]:
    """Yield a connection to a fresh schema with the pre-migration grid shape, dropped after."""
    schema = f"sky656_test_{uuid4().hex}"
    admin = create_engine(TEST_DB_URL)
    with admin.begin() as setup:
        setup.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(TEST_DB_URL, connect_args={"options": f"-csearch_path={schema},public"})
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
        with engine.connect() as connection:
            yield connection
    finally:
        engine.dispose()
        with admin.begin() as teardown:
            teardown.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def _seed(connection: Connection) -> None:
    """Write dead and live jobs, each with rows in every table that names a job.

    Args:
        connection: The test schema connection.
    """
    tables = Base.metadata.tables
    jobs = [
        {"optimization_id": "dspy-run", "optimization_type": "run", "payload": {}},
        {"optimization_id": "grid-parent", "optimization_type": "grid_search", "payload": {}},
        {"optimization_id": "grid-pair", "optimization_type": "run", "payload": {}},
        {"optimization_id": "unified-run", "optimization_type": "blackbox", "payload": {"payload_kind": "unified_v1"}},
        {"optimization_id": "repo-run", "optimization_type": "blackbox", "payload": {"objective": "x"}},
        {"optimization_id": "tagging-run", "optimization_type": "tagging_autotag", "payload": {}},
        {"optimization_id": "untyped-run", "optimization_type": None, "payload": None},
    ]
    connection.execute(tables["jobs"].insert(), jobs)
    connection.execute(
        text(
            "UPDATE jobs SET parent_optimization_id = 'grid-parent', pair_index = 0 WHERE optimization_id = 'grid-pair'"
        )
    )
    ids = [job["optimization_id"] for job in jobs]
    connection.execute(
        tables["job_logs"].insert(),
        [{"optimization_id": i, "level": "INFO", "logger": "t", "message": "m"} for i in ids],
    )
    connection.execute(tables["job_progress_events"].insert(), [{"optimization_id": i} for i in ids])
    connection.execute(tables["job_embeddings"].insert(), [{"optimization_id": i} for i in ids])
    connection.execute(
        tables["notifications"].insert(),
        [{"username": "u", "event_type": "done", "optimization_id": i} for i in ids],
    )
    connection.execute(
        tables["optimization_share_links"].insert(),
        [{"token": f"tok-{i}", "optimization_id": i, "created_by": "u"} for i in ids],
    )
    connection.execute(
        tables["optimization_share_grants"].insert(),
        [{"optimization_id": i, "grantee_username": "g", "role": "viewer", "created_by": "u"} for i in ids],
    )
    connection.execute(
        tables["run_folder_items"].insert(),
        [{"optimization_id": i, "folder_id": "f", "added_by": "u"} for i in ids],
    )
    connection.execute(tables["gepa_checkpoints"].insert(), [{"optimization_id": i, "data": b"x"} for i in ids])
    connection.execute(
        tables["wallet_ledger"].insert(),
        [{"id": n, "username": "u", "delta_cents": -1, "kind": "run", "optimization_id": i} for n, i in enumerate(ids)],
    )
    connection.execute(
        text("INSERT INTO grid_pair_results (optimization_id, pair_index, result) VALUES ('grid-parent', 0, '{}')")
    )
    connection.execute(
        tables["continuous_plans"].insert(),
        [
            {
                "id": "plan-dead",
                "username": "u",
                "source_optimization_id": "dspy-run",
                "incumbent_optimization_id": "dspy-run",
                "schedule": "monthly",
                "cycle_budget_cents": 1,
            },
            {
                "id": "plan-live",
                "username": "u",
                "source_optimization_id": "repo-run",
                "incumbent_optimization_id": "repo-run",
                "schedule": "monthly",
                "cycle_budget_cents": 1,
            },
        ],
    )
    connection.execute(
        tables["continuous_cycles"].insert(),
        [
            {"id": "cycle-dead-plan", "plan_id": "plan-dead", "slot": "s", "month": "2026-10", "status": "done"},
            {
                "id": "cycle-live",
                "plan_id": "plan-live",
                "slot": "s",
                "month": "2026-10",
                "status": "done",
                "optimization_id": "repo-run",
            },
        ],
    )
    connection.commit()


def _upgrade(connection: Connection) -> None:
    """Run the migration's upgrade on ``connection`` and commit.

    Args:
        connection: The test schema connection.
    """
    with Operations.context(MigrationContext.configure(connection)):
        migration.upgrade()
    connection.commit()


def _ids(connection: Connection, table: str, column: str = "optimization_id") -> set[str]:
    """Return the distinct values of ``column`` in ``table``.

    Args:
        connection: The test schema connection.
        table: The table name.
        column: The column to read.

    Returns:
        The distinct values.
    """
    return {row[0] for row in connection.execute(text(f"SELECT DISTINCT {column} FROM {table}"))}


def test_upgrade_deletes_only_the_dspy_grid_and_pair_rows(conn: Connection) -> None:
    """Delete every dead job row and keep the live jobs, their rows and the billing rows."""
    _seed(conn)

    _upgrade(conn)

    live = set(_LIVE)
    assert _ids(conn, "jobs") == live
    for table in (
        "job_logs",
        "job_progress_events",
        "job_embeddings",
        "notifications",
        "optimization_share_links",
        "optimization_share_grants",
        "run_folder_items",
        "gepa_checkpoints",
    ):
        assert _ids(conn, table) == live, table
    assert _ids(conn, "wallet_ledger") == live | set(_DEAD)
    assert _ids(conn, "continuous_plans", "id") == {"plan-live"}
    assert _ids(conn, "continuous_cycles", "id") == {"cycle-live"}


def test_upgrade_drops_the_grid_schema_and_keeps_the_checkpoint_pair_index(conn: Connection) -> None:
    """Drop the grid table, columns, FK and index, and leave the checkpoint key alone."""
    _seed(conn)

    _upgrade(conn)

    inspector = inspect(conn)
    assert not inspector.has_table("grid_pair_results")
    job_columns = {column["name"] for column in inspector.get_columns("jobs")}
    assert not {"parent_optimization_id", "pair_index"} & job_columns
    assert "pair_index" not in {column["name"] for column in inspector.get_columns("job_logs")}
    assert not inspector.get_foreign_keys("jobs")
    assert "ix_jobs_parent_optimization_id" not in {index["name"] for index in inspector.get_indexes("jobs")}
    assert "pair_index" in {column["name"] for column in inspector.get_columns("gepa_checkpoints")}


def test_upgrade_runs_twice_and_downgrade_restores_the_empty_schema(conn: Connection) -> None:
    """Run the upgrade again with no change, then restore the empty grid schema."""
    _seed(conn)
    _upgrade(conn)

    _upgrade(conn)
    assert _ids(conn, "jobs") == set(_LIVE)

    with Operations.context(MigrationContext.configure(conn)):
        migration.downgrade()
    conn.commit()
    inspector = inspect(conn)
    assert inspector.has_table("grid_pair_results")
    assert {"parent_optimization_id", "pair_index"} <= {column["name"] for column in inspector.get_columns("jobs")}
    assert "pair_index" in {column["name"] for column in inspector.get_columns("job_logs")}
    assert [fk["referred_table"] for fk in inspector.get_foreign_keys("jobs")] == ["jobs"]
    assert "ix_jobs_parent_optimization_id" in {index["name"] for index in inspector.get_indexes("jobs")}
    assert _ids(conn, "jobs") == set(_LIVE)
