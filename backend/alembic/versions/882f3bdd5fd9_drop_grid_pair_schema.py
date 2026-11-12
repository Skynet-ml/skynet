"""Drop the grid pair table and the pair columns (SKY-676).

Revision ID: 882f3bdd5fd9
Revises: f3a9c2d81b56

SKY-656 (revision f3a9c2d81b56) deleted the DSPy, grid and pair runs, but
kept their schema for the rollout, because old pods still used it. Nothing
maps or reads it now, so this revision drops:

- the ``grid_pair_results`` table,
- ``jobs.parent_optimization_id`` with its self FK and the
  ``ix_jobs_parent_optimization_id`` index,
- ``jobs.pair_index``,
- ``job_logs.pair_index``.

``gepa_checkpoints.pair_index`` stays: it is part of that table's primary
key and a repository run uses it.

Merge this revision only after production runs SKY-656: an old pod deletes
from ``grid_pair_results`` on every job delete.

Every step uses ``IF EXISTS``, so the revision is idempotent. Postgres only,
like the revisions that added this schema: the SQLite test schema comes from
the ORM models. The downgrade recreates the empty table and the nullable
columns with the old FK and index. It cannot bring back the dropped data.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "882f3bdd5fd9"
down_revision: str | Sequence[str] | None = "f3a9c2d81b56"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop the grid pair table, the jobs pair columns and ``job_logs.pair_index``."""
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP TABLE IF EXISTS grid_pair_results")
    # Postgres drops the self FK and the ix_jobs_parent_optimization_id index
    # with the column.
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS parent_optimization_id, DROP COLUMN IF EXISTS pair_index")
    op.execute("ALTER TABLE job_logs DROP COLUMN IF EXISTS pair_index")


def downgrade() -> None:
    """Recreate the empty grid pair table and the nullable pair columns, with no data."""
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE job_logs ADD COLUMN IF NOT EXISTS pair_index INTEGER")
    op.execute(
        """
        ALTER TABLE jobs
            ADD COLUMN IF NOT EXISTS parent_optimization_id VARCHAR(36)
                REFERENCES jobs (optimization_id) ON DELETE CASCADE,
            ADD COLUMN IF NOT EXISTS pair_index INTEGER
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_jobs_parent_optimization_id ON jobs (parent_optimization_id)")
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
