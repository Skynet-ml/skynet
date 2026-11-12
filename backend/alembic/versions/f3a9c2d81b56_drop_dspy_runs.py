"""Delete the retired DSPy, grid search and pair runs, and drop their columns (SKY-656).

Revision ID: f3a9c2d81b56
Revises: c4d1e8a2f6b3

Since SKY-316 every run optimizes a repository, and the code that ran, read
and served DSPy runs is gone. This revision deletes every job row that only
that code could read:

- ``optimization_type = 'run'``: a DSPy program run.
- ``optimization_type = 'grid_search'``: a grid search parent.
- a row with ``parent_optimization_id``: a grid pair child.
- a ``blackbox`` row whose payload has ``payload_kind = 'unified_v1'``: a
  DSPy program run through the unified engine.

The rows of those jobs go too. The tables with a cascading FK to
``jobs.optimization_id`` lose them with the job: ``gepa_checkpoints``,
``grid_pair_results``, ``blackbox_agent_runs``, ``run_calls``,
``run_candidates``, ``sandbox_trials`` and the pair children. The tables
that keep a job id with no FK are cleared here: ``job_logs``,
``job_progress_events``, ``job_embeddings``, ``notifications``,
``optimization_share_links``, ``optimization_share_grants``,
``run_folder_items``, ``continuous_cycles`` and ``continuous_plans`` (with the
cycles of a deleted plan). ``wallet_ledger`` and ``execution_budgets`` are
billing records, so they keep their rows.

Then it drops ``grid_pair_results``, the self FK and index on
``jobs.parent_optimization_id``, that column, ``jobs.pair_index`` and
``job_logs.pair_index``. ``gepa_checkpoints.pair_index`` stays: it is part of
that table's primary key and a repository run uses it.

Every step uses ``IF EXISTS`` or matches no rows on a second run, so the
revision is idempotent. Postgres only, like the revisions that added these
columns: the SQLite test schema comes from the ORM models. The downgrade
restores the empty columns, FK, index and table. It cannot bring the deleted
rows back.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f3a9c2d81b56"
down_revision: str | Sequence[str] | None = "c4d1e8a2f6b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEAD_JOBS = "_sky656_dead_jobs"

_JOB_ID_TABLES = (
    "job_logs",
    "job_progress_events",
    "job_embeddings",
    "notifications",
    "optimization_share_links",
    "optimization_share_grants",
    "run_folder_items",
    "continuous_cycles",
)


def _has_column(table: str, column: str) -> bool:
    """Return whether ``table`` exists and has ``column``.

    Args:
        table: The table name.
        column: The column name.

    Returns:
        True when the column exists.
    """
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return False
    return any(found["name"] == column for found in inspector.get_columns(table))


def _has_table(table: str) -> bool:
    """Return whether ``table`` exists.

    Args:
        table: The table name.

    Returns:
        True when the table exists.
    """
    return sa.inspect(op.get_bind()).has_table(table)


def _collect_dead_jobs() -> None:
    """Fill a temporary table with the id of every retired DSPy, grid or pair job."""
    op.execute(f"DROP TABLE IF EXISTS {_DEAD_JOBS}")
    op.execute(
        f"""
        CREATE TEMPORARY TABLE {_DEAD_JOBS} AS
        SELECT optimization_id FROM jobs
        WHERE optimization_type IN ('run', 'grid_search')
           OR (optimization_type = 'blackbox' AND payload ->> 'payload_kind' = 'unified_v1')
        """
    )
    if _has_column("jobs", "parent_optimization_id"):
        op.execute(
            f"""
            INSERT INTO {_DEAD_JOBS}
            SELECT optimization_id FROM jobs
            WHERE parent_optimization_id IS NOT NULL
              AND optimization_id NOT IN (SELECT optimization_id FROM {_DEAD_JOBS})
            """
        )


def _delete_dead_rows() -> None:
    """Delete the dead jobs and every row that names one of them."""
    if _has_table("continuous_plans"):
        plans = f"""
            SELECT id FROM continuous_plans
            WHERE source_optimization_id IN (SELECT optimization_id FROM {_DEAD_JOBS})
               OR incumbent_optimization_id IN (SELECT optimization_id FROM {_DEAD_JOBS})
        """
        if _has_table("continuous_cycles"):
            op.execute(f"DELETE FROM continuous_cycles WHERE plan_id IN ({plans})")
        op.execute(f"DELETE FROM continuous_plans WHERE id IN ({plans})")
    for table in _JOB_ID_TABLES:
        if _has_table(table):
            op.execute(f"DELETE FROM {table} WHERE optimization_id IN (SELECT optimization_id FROM {_DEAD_JOBS})")
    # The pair children go in the same statement as their parents, so the
    # self FK cascade has nothing left to do.
    op.execute(f"DELETE FROM jobs WHERE optimization_id IN (SELECT optimization_id FROM {_DEAD_JOBS})")
    op.execute(f"DROP TABLE IF EXISTS {_DEAD_JOBS}")


def upgrade() -> None:
    """Delete the retired DSPy, grid and pair jobs and drop their schema."""
    if op.get_bind().dialect.name != "postgresql":
        return
    _collect_dead_jobs()
    _delete_dead_rows()
    op.execute("DROP TABLE IF EXISTS grid_pair_results")
    # The FK name depends on how the column was made (inline REFERENCES or the
    # ORM), so drop every FK on the column by its catalog name.
    op.execute(
        """
        DO $$
        DECLARE fk record;
        BEGIN
            FOR fk IN
                SELECT con.conname FROM pg_constraint con
                JOIN pg_attribute att
                  ON att.attrelid = con.conrelid AND att.attnum = ANY (con.conkey)
                WHERE con.conrelid = 'jobs'::regclass AND con.contype = 'f'
                  AND att.attname = 'parent_optimization_id'
            LOOP
                EXECUTE format('ALTER TABLE jobs DROP CONSTRAINT %I', fk.conname);
            END LOOP;
        END $$
        """
    )
    op.execute("DROP INDEX IF EXISTS ix_jobs_parent_optimization_id")
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS parent_optimization_id, DROP COLUMN IF EXISTS pair_index")
    op.execute("ALTER TABLE job_logs DROP COLUMN IF EXISTS pair_index")


def downgrade() -> None:
    """Restore the empty grid columns, FK, index and table; the deleted rows stay gone."""
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        ALTER TABLE jobs
            ADD COLUMN IF NOT EXISTS parent_optimization_id VARCHAR(36)
                REFERENCES jobs (optimization_id) ON DELETE CASCADE,
            ADD COLUMN IF NOT EXISTS pair_index INTEGER
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_jobs_parent_optimization_id ON jobs (parent_optimization_id)")
    op.execute("ALTER TABLE job_logs ADD COLUMN IF NOT EXISTS pair_index INTEGER")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS grid_pair_results (
            optimization_id VARCHAR(36) NOT NULL REFERENCES jobs(optimization_id) ON DELETE CASCADE,
            pair_index INTEGER NOT NULL,
            result JSONB NOT NULL,
            stored_bytes BIGINT NOT NULL DEFAULT 0,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            PRIMARY KEY (optimization_id, pair_index)
        )
        """
    )
