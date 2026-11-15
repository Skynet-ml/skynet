"""Add chat media: images, video and audio attached in agent chats.

Revision ID: c3d5e7f9a2b4
Revises: b8d2e4f6a1c3

SKY-707. Adds ``chat_media``: one row for each file a user attaches in an
agent chat, with its bytes in BYTEA like ``dataset_blobs``. Only the owning
``username`` reads a row. The ``(username, created_at)`` index serves the
owner reads, the storage meter and the sweep of unsent files.
``conversation_id`` names the stored chat that sent the file, and deleting
that chat deletes the file through the foreign key.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c3d5e7f9a2b4"
down_revision: str | Sequence[str] | None = "b8d2e4f6a1c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the chat media table."""
    op.create_table(
        "chat_media",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("username", sa.String(255), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("mime_type", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("agent_conversations.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index("ix_chat_media_user_created", "chat_media", ["username", "created_at"])
    op.create_index("ix_chat_media_conversation_id", "chat_media", ["conversation_id"])


def downgrade() -> None:
    """Drop the chat media table."""
    op.drop_index("ix_chat_media_conversation_id", table_name="chat_media")
    op.drop_index("ix_chat_media_user_created", table_name="chat_media")
    op.drop_table("chat_media")
