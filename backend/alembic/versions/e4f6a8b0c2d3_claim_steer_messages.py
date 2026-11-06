"""Separate claimed steering messages from delivered messages.

Revision ID: e4f6a8b0c2d3
Revises: d3e5f7a9b1c2
"""

import sqlalchemy as sa

from alembic import op

revision = "e4f6a8b0c2d3"
down_revision = "d3e5f7a9b1c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add a cancellable claim timestamp without changing delivery proof."""
    op.add_column("agent_steer_messages", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Remove the claim timestamp."""
    op.drop_column("agent_steer_messages", "claimed_at")
