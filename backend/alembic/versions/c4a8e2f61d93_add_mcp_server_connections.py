"""Keep each user's OAuth sign-ins to remote MCP servers (SKY-361).

Revision ID: c4a8e2f61d93
Revises: 6e7f0a3eb85b
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c4a8e2f61d93"
down_revision: str | Sequence[str] | None = "6e7f0a3eb85b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the MCP server connection table and its indexes idempotently."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS mcp_server_connections (
            id VARCHAR(36) PRIMARY KEY,
            username VARCHAR(255) NOT NULL,
            name VARCHAR(40) NOT NULL,
            server_url TEXT NOT NULL,
            status VARCHAR(16) NOT NULL,
            credentials_encrypted TEXT,
            expires_at TIMESTAMP WITH TIME ZONE,
            pending_state_hash VARCHAR(64),
            pending_encrypted TEXT,
            pending_expires_at TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
            CONSTRAINT uq_mcp_server_connections_owner_name UNIQUE (username, name)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_mcp_server_connections_username ON mcp_server_connections (username)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_mcp_server_connections_pending_state_hash "
        "ON mcp_server_connections (pending_state_hash)"
    )


def downgrade() -> None:
    """Drop the MCP server connection table with its indexes."""
    op.execute("DROP TABLE IF EXISTS mcp_server_connections")
