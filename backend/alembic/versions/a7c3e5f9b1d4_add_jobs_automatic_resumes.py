"""Add the automatic resume count to jobs (SKY-801).

Revision ID: a7c3e5f9b1d4
Revises: 1c02f9da1d9f

``jobs.automatic_resumes`` counts only the orphan sweep's requeues, so a
manual resume no longer uses up the step log run's cap of automatic resumes.
Existing rows start at 0: the old count mixed manual and automatic resumes,
so a copy of ``attempts`` would keep the bug for runs already in flight.

During a rolling deploy, an old pod's rerun resets ``attempts`` but not this
column, and an old pod's sweep does not add 1 to it. The window is short; the
worst case is one run that fails its first automatic resume after such a rerun.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a7c3e5f9b1d4"
down_revision: str | Sequence[str] | None = "1c02f9da1d9f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``jobs.automatic_resumes``, not null with a server default of 0."""
    op.add_column("jobs", sa.Column("automatic_resumes", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    """Drop ``jobs.automatic_resumes``."""
    op.drop_column("jobs", "automatic_resumes")
