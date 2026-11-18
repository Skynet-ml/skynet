"""Add ``jobs.step_log_shard``, the step log shard of each run (SKY-731).

Revision ID: 66cae09708d3
Revises: c4f1a7e9d2b6

The shard map writes the column once, on the run's first step log use. NULL
means the main database, so existing runs keep their steps where they are.
A partial index on the set values lets a shard map with no extra shard find a
recorded shard with no scan of ``jobs``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "66cae09708d3"
down_revision: str | Sequence[str] | None = "c4f1a7e9d2b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable shard column to ``jobs`` and its partial index."""
    op.add_column("jobs", sa.Column("step_log_shard", sa.String(64), nullable=True))
    op.create_index(
        "ix_jobs_step_log_shard",
        "jobs",
        ["step_log_shard"],
        postgresql_where=sa.text("step_log_shard IS NOT NULL"),
        sqlite_where=sa.text("step_log_shard IS NOT NULL"),
    )


def downgrade() -> None:
    """Drop the partial index and the shard column."""
    op.drop_index("ix_jobs_step_log_shard", table_name="jobs")
    op.drop_column("jobs", "step_log_shard")
