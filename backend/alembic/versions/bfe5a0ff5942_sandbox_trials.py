"""Add the sandbox_trials table: one row for each sandbox trial of a job (SKY-420).

Revision ID: bfe5a0ff5942
Revises: fe9d384913a7
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "bfe5a0ff5942"
down_revision: str | Sequence[str] | None = "fe9d384913a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the trials table, its interrupted column and its claim index idempotently."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS sandbox_trials (
            optimization_id VARCHAR(36) NOT NULL REFERENCES jobs(optimization_id) ON DELETE CASCADE,
            trial_key VARCHAR(255) NOT NULL,
            fingerprint VARCHAR(128) NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'pending',
            attempt INTEGER NOT NULL DEFAULT 0,
            interrupted INTEGER NOT NULL DEFAULT 0,
            result_ref JSONB,
            error TEXT,
            claimed_by VARCHAR(255),
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            PRIMARY KEY (optimization_id, trial_key),
            CONSTRAINT ck_sandbox_trials_status
                CHECK (status IN ('pending', 'running', 'succeeded', 'retryable_failed', 'failed'))
        )
        """
    )
    # A preview database can hold the table from an earlier build of this
    # revision, without the interrupted column.
    op.execute("ALTER TABLE sandbox_trials ADD COLUMN IF NOT EXISTS interrupted INTEGER NOT NULL DEFAULT 0")
    op.execute("CREATE INDEX IF NOT EXISTS ix_sandbox_trials_job_status ON sandbox_trials (optimization_id, status)")


def downgrade() -> None:
    """Drop the trials table and its index."""
    op.execute("DROP TABLE IF EXISTS sandbox_trials")
