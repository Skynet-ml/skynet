"""Keep the metric-hardening jobs of each setup draft (SKY-355).

Revision ID: 6e7f0a3eb85b
Revises: b6760e5bb290
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "6e7f0a3eb85b"
down_revision: str | Sequence[str] | None = "b6760e5bb290"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the hardening job table and its indexes idempotently."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS hardening_jobs (
            id VARCHAR(36) PRIMARY KEY,
            username VARCHAR(255) NOT NULL,
            draft_id VARCHAR(255) NOT NULL,
            status VARCHAR(32) NOT NULL,
            job JSONB NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_hardening_jobs_username ON hardening_jobs (username)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_hardening_jobs_status ON hardening_jobs (status)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_hardening_jobs_owner_draft_created "
        "ON hardening_jobs (username, draft_id, created_at)"
    )
    # One unfinished job per draft: the router's SELECT check alone races
    # when two creates arrive at the same time.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_hardening_jobs_active_draft "
        "ON hardening_jobs (username, draft_id) "
        "WHERE status IN ('preflight', 'running', 'waiting_review', 'waiting_fix')"
    )


def downgrade() -> None:
    """Drop the hardening job table with its indexes."""
    op.execute("DROP TABLE IF EXISTS hardening_jobs")
