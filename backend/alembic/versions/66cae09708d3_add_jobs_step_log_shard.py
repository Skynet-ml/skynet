"""Add ``jobs.step_log_shard``, the step log shard of each run (SKY-731).

Revision ID: 66cae09708d3
Revises: c4f1a7e9d2b6

The shard map writes the column once, on the run's first step log use. NULL
means the main database, so existing runs keep their steps where they are.
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
    """Add the nullable shard column to ``jobs``."""
    op.add_column("jobs", sa.Column("step_log_shard", sa.String(64), nullable=True))


def downgrade() -> None:
    """Drop the shard column."""
    op.drop_column("jobs", "step_log_shard")
