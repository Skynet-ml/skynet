"""replace agent chat share tokens with typed share links

Revision ID: c4e6a8b0d2f4
Revises: a1c3e5f7b9d2
Create Date: 2026-11-02 12:00:00.000000

SKY-294 replaces the read-only chat link with a typed link: view only,
continue the chat, or continue the chat with live read access to ticked
data. There is no compatibility path. The upgrade deletes the chats that
the Mac app published as snapshots, drops the old ``share_token`` and
``shared_at`` columns (so every old link stops working), and adds the
``agent_chat_shares`` table plus ``source_share_token`` on the
continued copies.

A Mac snapshot is a shared chat with a user turn that carries
``router_metadata``: the import wrote it on every turn, and a hosted chat
never writes it on a user turn.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "c4e6a8b0d2f4"
down_revision: str | None = "a1c3e5f7b9d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Delete the Mac snapshot chats, drop the old share columns and add the typed share links."""
    op.execute(
        "DELETE FROM agent_conversations c WHERE c.share_token IS NOT NULL AND EXISTS ("
        "SELECT 1 FROM agent_messages m WHERE m.conversation_id = c.id "
        "AND m.role = 'user' AND m.router_metadata IS NOT NULL "
        "AND jsonb_typeof(m.router_metadata::jsonb) = 'object')"
    )
    op.drop_index("ix_agent_conversations_share_token", table_name="agent_conversations", if_exists=True)
    op.execute("ALTER TABLE agent_conversations DROP COLUMN IF EXISTS shared_at")
    op.execute("ALTER TABLE agent_conversations DROP COLUMN IF EXISTS share_token")
    op.create_table(
        "agent_chat_shares",
        sa.Column("token", sa.String(48), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("agent_conversations.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("username", sa.String(255), nullable=False),
        sa.Column("link_type", sa.String(16), nullable=False),
        sa.Column("items", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_agent_chat_shares_username", "agent_chat_shares", ["username"])
    op.add_column("agent_conversations", sa.Column("source_share_token", sa.String(48), nullable=True))
    op.create_index(
        "ix_agent_conversations_source_share",
        "agent_conversations",
        ["source_share_token", "username"],
        unique=True,
    )


def downgrade() -> None:
    """Drop the typed share links and restore the empty old share columns.

    The deleted Mac snapshot chats and the old tokens do not come back.
    """
    op.drop_index("ix_agent_conversations_source_share", table_name="agent_conversations", if_exists=True)
    op.execute("ALTER TABLE agent_conversations DROP COLUMN IF EXISTS source_share_token")
    op.drop_index("ix_agent_chat_shares_username", table_name="agent_chat_shares", if_exists=True)
    op.drop_table("agent_chat_shares")
    op.execute("ALTER TABLE agent_conversations ADD COLUMN IF NOT EXISTS share_token VARCHAR(64)")
    op.execute("ALTER TABLE agent_conversations ADD COLUMN IF NOT EXISTS shared_at TIMESTAMP WITH TIME ZONE")
    op.create_index(
        "ix_agent_conversations_share_token",
        "agent_conversations",
        ["share_token"],
        unique=True,
        if_not_exists=True,
    )
