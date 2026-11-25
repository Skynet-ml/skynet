"""Add ``storage_packs.needs_rewrite`` on a step log shard database (SKY-733).

Revision ID: 96693ab0b019
Revises: 3c4a41ece580

The main database gets the column from ``e3b7d1f5a9c2``. A flush that
records a pack on a shard inserts the column and sets the mark there too.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "96693ab0b019"
down_revision: str | Sequence[str] | None = "3c4a41ece580"
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
    op.drop_column("storage_packs", "needs_rewrite")
