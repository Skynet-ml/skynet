"""add agent feedback reports

Revision ID: 33a823c490ff
Revises: 882f3bdd5fd9
Create Date: 2026-11-12 12:00:00.000000

SKY-580 adds ``agent_feedback_reports``. The agent's ``send_feedback`` tool
writes one row when a user is not satisfied with a tool result. The unique
key on ``(conversation_id, turn)`` keeps it to one report for each complaint.
The admin feedback endpoint reads the table, newest first.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "33a823c490ff"
down_revision: str | Sequence[str] | None = "882f3bdd5fd9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the agent feedback report table."""
    op.create_table(
        "agent_feedback_reports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("agent_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("turn", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(255), nullable=False),
        sa.Column("agent", sa.String(64), nullable=False),
        sa.Column("tool_name", sa.String(100), nullable=False),
        sa.Column("user_quote", sa.String(300), nullable=False),
        sa.Column("summary", sa.String(1000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("conversation_id", "turn", name="uq_agent_feedback_reports_turn"),
    )
    op.create_index("ix_agent_feedback_reports_created_at", "agent_feedback_reports", ["created_at"])


def downgrade() -> None:
    """Drop the agent feedback report table."""
    op.drop_index("ix_agent_feedback_reports_created_at", table_name="agent_feedback_reports")
    op.drop_table("agent_feedback_reports")
