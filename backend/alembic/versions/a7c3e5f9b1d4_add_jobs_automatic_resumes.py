"""Add the automatic resume count to jobs (SKY-801).

Revision ID: a7c3e5f9b1d4
Revises: e8b2d4f6a9c1

Only automatic resumes count against the resume cap of a step log run, so
a manual resume no longer uses up the run's automatic retries.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a7c3e5f9b1d4"
down_revision: str | Sequence[str] | None = "e8b2d4f6a9c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``jobs.automatic_resumes``, zero for every existing run."""
    op.add_column("jobs", sa.Column("automatic_resumes", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    """Drop ``jobs.automatic_resumes``."""
    op.drop_column("jobs", "automatic_resumes")
