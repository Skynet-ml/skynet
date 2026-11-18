"""Add ``storage_packs.needs_rewrite`` (SKY-733).

Revision ID: e3b7d1f5a9c2
Revises: a7c3e5f9b1d4

A pack gets the mark when an entry in it has no row: an account deletion,
a stale step, a duplicate, or an entry of a job that is gone. The compactor
rewrites a marked pack at any size, so those bytes leave the tree bucket.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e3b7d1f5a9c2"
down_revision: str | Sequence[str] | None = "a7c3e5f9b1d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the rewrite mark; every existing pack starts unmarked."""
    op.add_column(
        "storage_packs",
        sa.Column("needs_rewrite", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    """Drop the rewrite mark."""
    with op.batch_alter_table("storage_packs") as batch:
        batch.drop_column("needs_rewrite")
