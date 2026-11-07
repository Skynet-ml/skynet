"""Let one worker pod at a time claim a hardening job with a lease, and keep its budget hold (SKY-369).

Revision ID: c1d3e5f7a9b2
Revises: c4a8e2f61d93
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c1d3e5f7a9b2"
down_revision: str | Sequence[str] | None = "c4a8e2f61d93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the claim owner, lease expiry and budget operation columns idempotently."""
    op.execute("ALTER TABLE hardening_jobs ADD COLUMN IF NOT EXISTS claimed_by VARCHAR(255)")
    op.execute("ALTER TABLE hardening_jobs ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMP WITH TIME ZONE")
    op.execute("ALTER TABLE hardening_jobs ADD COLUMN IF NOT EXISTS budget_operation_id VARCHAR(64)")


def downgrade() -> None:
    """Drop the claim and budget operation columns."""
    op.execute("ALTER TABLE hardening_jobs DROP COLUMN IF EXISTS budget_operation_id")
    op.execute("ALTER TABLE hardening_jobs DROP COLUMN IF EXISTS lease_expires_at")
    op.execute("ALTER TABLE hardening_jobs DROP COLUMN IF EXISTS claimed_by")
