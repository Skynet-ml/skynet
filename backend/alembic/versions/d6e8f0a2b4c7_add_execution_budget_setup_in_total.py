"""Let a continuous-plan budget keep counting setup spend against its total.

Revision ID: d6e8f0a2b4c7
Revises: c5f7d9e1a3b4
Create Date: 2026-11-23 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d6e8f0a2b4c7"
down_revision: str | None = "c5f7d9e1a3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the flag that makes a budget's total cap and count its setup spend (SKY-925)."""
    op.execute("ALTER TABLE execution_budgets ADD COLUMN IF NOT EXISTS setup_in_total BOOLEAN NOT NULL DEFAULT FALSE")


def downgrade() -> None:
    """Drop the flag; every budget bills setup to the balance only."""
    op.execute("ALTER TABLE execution_budgets DROP COLUMN IF EXISTS setup_in_total")
