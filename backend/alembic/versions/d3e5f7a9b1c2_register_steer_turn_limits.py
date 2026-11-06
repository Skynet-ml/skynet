"""Store shared per-turn steering input limits.

Revision ID: d3e5f7a9b1c2
Revises: c2d4e6f8a0b1
"""

import sqlalchemy as sa

from alembic import op

revision = "d3e5f7a9b1c2"
down_revision = "c2d4e6f8a0b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the per-owner and key input bound."""
    op.create_table(
        "agent_steer_turns",
        sa.Column("owner", sa.String(255), primary_key=True),
        sa.Column("steer_key", sa.String(64), primary_key=True),
        sa.Column("max_chars", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_agent_steer_turns_created_at", "agent_steer_turns", ["created_at"])


def downgrade() -> None:
    """Remove the shared turn limits."""
    op.drop_index("ix_agent_steer_turns_created_at", table_name="agent_steer_turns")
    op.drop_table("agent_steer_turns")
