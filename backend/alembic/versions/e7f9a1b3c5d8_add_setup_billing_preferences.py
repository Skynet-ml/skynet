"""Add the per-account choice of whether setup checks bill the wallet only.

Revision ID: e7f9a1b3c5d8
Revises: d6e8f0a2b4c7
Create Date: 2026-11-23 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "e7f9a1b3c5d8"
down_revision: str | None = "d6e8f0a2b4c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the setup billing preference table idempotently (SKY-925)."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS setup_billing_preferences (
            username VARCHAR(255) PRIMARY KEY,
            from_wallet BOOLEAN NOT NULL DEFAULT TRUE,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    """Drop the setup billing preference table; every account bills setup to the wallet."""
    op.execute("DROP TABLE IF EXISTS setup_billing_preferences")
