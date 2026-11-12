"""Keep the repository runs that MCP clients build step by step.

Revision ID: b6760e5bb290
Revises: 2cd2a1289484
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b6760e5bb290"
down_revision: str | Sequence[str] | None = "2cd2a1289484"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the MCP run draft table idempotently."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS mcp_run_drafts (
            id VARCHAR(36) PRIMARY KEY,
            username VARCHAR(255) NOT NULL,
            spec JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_mcp_run_drafts_username ON mcp_run_drafts (username)")


def downgrade() -> None:
    """Drop the MCP run draft table."""
    op.execute("DROP TABLE IF EXISTS mcp_run_drafts")
