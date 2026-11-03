"""Index owner-wide pending steers and bounded expiry scans.

Revision ID: c2d4e6f8a0b1
Revises: a1c3e5f7b9d2
"""

from alembic import op

revision = "c2d4e6f8a0b1"
down_revision = "a1c3e5f7b9d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add indexes for owner caps and indexed stale-batch selection."""
    op.create_index("ix_agent_steer_messages_owner_pending", "agent_steer_messages", ["owner", "taken_at"])
    op.create_index("ix_agent_steer_messages_created_at", "agent_steer_messages", ["created_at"])


def downgrade() -> None:
    """Remove the owner and expiry indexes."""
    op.drop_index("ix_agent_steer_messages_created_at", table_name="agent_steer_messages")
    op.drop_index("ix_agent_steer_messages_owner_pending", table_name="agent_steer_messages")
