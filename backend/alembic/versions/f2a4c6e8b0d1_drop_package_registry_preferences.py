"""Drop the per-account Python package index table (SKY-966).

Revision ID: f2a4c6e8b0d1
Revises: e7f9a1b3c5d8
Create Date: 2026-11-25 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f2a4c6e8b0d1"
down_revision: str | None = "e7f9a1b3c5d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop the table; every scorer lock and setup box now uses PyPI."""
    op.execute("DROP TABLE IF EXISTS package_registry_preferences")


def downgrade() -> None:
    """Re-create the table exactly as revision b95ea4618c02 created it, with no rows."""
    if sa.inspect(op.get_bind()).has_table("package_registry_preferences"):
        return
    op.create_table(
        "package_registry_preferences",
        sa.Column("username", sa.String(255), primary_key=True),
        sa.Column("index_url", sa.String(2048), nullable=False),
    )
