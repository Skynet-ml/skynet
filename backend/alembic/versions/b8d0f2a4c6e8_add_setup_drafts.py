"""Keep one left run-wizard setup per account on the server.

Revision ID: b8d0f2a4c6e8
Revises: a7c9e1f3b5d7
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b8d0f2a4c6e8"
down_revision: str | Sequence[str] | None = "a7c9e1f3b5d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the per-account setup draft table idempotently."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS setup_drafts (
            username VARCHAR(255) PRIMARY KEY,
            left_at BIGINT NOT NULL,
            draft_values JSONB NOT NULL,
            chat JSONB,
            staged_dataset_id VARCHAR(36),
            revision BIGINT NOT NULL DEFAULT 0,
            deleted BOOLEAN NOT NULL DEFAULT false,
            saves JSONB NOT NULL DEFAULT '[]'::jsonb,
            evicted_revision BIGINT NOT NULL DEFAULT 0,
            deleted_revision BIGINT NOT NULL DEFAULT 0,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("ALTER TABLE setup_drafts ADD COLUMN IF NOT EXISTS revision BIGINT NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE setup_drafts ADD COLUMN IF NOT EXISTS deleted BOOLEAN NOT NULL DEFAULT false")
    op.execute("ALTER TABLE setup_drafts ADD COLUMN IF NOT EXISTS saves JSONB NOT NULL DEFAULT '[]'::jsonb")
    op.execute("ALTER TABLE setup_drafts ADD COLUMN IF NOT EXISTS evicted_revision BIGINT NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE setup_drafts ADD COLUMN IF NOT EXISTS deleted_revision BIGINT NOT NULL DEFAULT 0")
    op.execute("CREATE INDEX IF NOT EXISTS ix_setup_drafts_staged_dataset_id ON setup_drafts (staged_dataset_id)")


def downgrade() -> None:
    """Drop the setup draft table."""
    op.execute("DROP TABLE IF EXISTS setup_drafts")
