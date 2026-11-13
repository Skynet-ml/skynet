"""Remove chat share links and bind each run to its setup chat.

Revision ID: b8d2e4f6a1c3
Revises: 33a823c490ff

The "Share chat" feature (SKY-294) goes away. A run now names the setup chat
that created it, and the run owner and admins can read that
chat. The upgrade drops ``agent_chat_shares`` and the continued-copy column
``agent_conversations.source_share_token`` with its index, then adds
``jobs.conversation_id``. The new column is nullable and its FK sets it to
NULL when the chat is deleted, so a run never blocks a chat delete.

Postgres only, like the revisions that added these objects: the SQLite test
schema comes from the ORM models. The downgrade restores the empty share
table and column; the deleted share links do not come back.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "b8d2e4f6a1c3"
down_revision: str | Sequence[str] | None = "33a823c490ff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop the chat share links and add the run-to-chat binding."""
    op.drop_index("ix_agent_conversations_source_share", table_name="agent_conversations", if_exists=True)
    op.execute("ALTER TABLE agent_conversations DROP COLUMN IF EXISTS source_share_token")
    op.drop_index("ix_agent_chat_shares_username", table_name="agent_chat_shares", if_exists=True)
    op.execute("DROP TABLE IF EXISTS agent_chat_shares")
    op.add_column("jobs", sa.Column("conversation_id", sa.String(36), nullable=True))
    op.create_foreign_key(
        "fk_jobs_conversation_id",
        "jobs",
        "agent_conversations",
        ["conversation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_jobs_conversation_id", "jobs", ["conversation_id"])


def downgrade() -> None:
    """Drop the run-to-chat binding and restore the empty chat share links."""
    op.drop_index("ix_jobs_conversation_id", table_name="jobs", if_exists=True)
    op.drop_constraint("fk_jobs_conversation_id", "jobs", type_="foreignkey", if_exists=True)
    op.drop_column("jobs", "conversation_id")
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
