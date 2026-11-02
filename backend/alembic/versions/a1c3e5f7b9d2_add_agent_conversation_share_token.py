"""add share_token and shared_at to agent_conversations

Revision ID: a1c3e5f7b9d2
Revises: f5a1c9d3e7b2
Create Date: 2026-11-01 12:00:00.000000

A nullable, unique ``share_token`` lets the owner of an agent conversation
hand out a public read-only link to it. NULL means the chat is not shared;
revoking a link sets the column back to NULL. ``shared_at`` records when
the current link was made, for the owner's list of shared chats.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "a1c3e5f7b9d2"
down_revision: str | None = "f5a1c9d3e7b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable share_token and shared_at columns and the token's unique index."""
    op.execute("ALTER TABLE agent_conversations ADD COLUMN IF NOT EXISTS share_token VARCHAR(64)")
    op.execute("ALTER TABLE agent_conversations ADD COLUMN IF NOT EXISTS shared_at TIMESTAMP WITH TIME ZONE")
    op.create_index(
        "ix_agent_conversations_share_token",
        "agent_conversations",
        ["share_token"],
        unique=True,
        if_not_exists=True,
    )


def downgrade() -> None:
    """Drop the share_token index and the share_token and shared_at columns."""
    op.drop_index(
        "ix_agent_conversations_share_token",
        table_name="agent_conversations",
        if_exists=True,
    )
    op.execute("ALTER TABLE agent_conversations DROP COLUMN IF EXISTS shared_at")
    op.execute("ALTER TABLE agent_conversations DROP COLUMN IF EXISTS share_token")
