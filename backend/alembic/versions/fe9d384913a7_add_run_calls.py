"""Add the run record: one row for each relay call, and each candidate's state (SKY-364).

Revision ID: fe9d384913a7
Revises: c1d3e5f7a9b2
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "fe9d384913a7"
down_revision: str | Sequence[str] | None = "c1d3e5f7a9b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the call and candidate tables idempotently."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS run_calls (
            id BIGSERIAL PRIMARY KEY,
            optimization_id VARCHAR(36) NOT NULL REFERENCES jobs(optimization_id) ON DELETE CASCADE,
            candidate_id VARCHAR(64) NOT NULL,
            role VARCHAR(64) NOT NULL DEFAULT '',
            kind VARCHAR(16) NOT NULL,
            target VARCHAR(512) NOT NULL DEFAULT '',
            started_at TIMESTAMP WITH TIME ZONE NOT NULL,
            duration_ms INTEGER,
            status VARCHAR(16) NOT NULL,
            failure_reason TEXT,
            cost_usd DOUBLE PRECISION,
            request TEXT NOT NULL DEFAULT '',
            request_bytes BIGINT NOT NULL DEFAULT 0,
            request_truncated BOOLEAN NOT NULL DEFAULT FALSE,
            response TEXT NOT NULL DEFAULT '',
            response_bytes BIGINT NOT NULL DEFAULT 0,
            response_truncated BOOLEAN NOT NULL DEFAULT FALSE
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_run_calls_job_candidate_id ON run_calls (optimization_id, candidate_id, id)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS run_candidates (
            optimization_id VARCHAR(36) NOT NULL REFERENCES jobs(optimization_id) ON DELETE CASCADE,
            candidate_id VARCHAR(64) NOT NULL,
            role VARCHAR(64) NOT NULL DEFAULT '',
            state VARCHAR(16) NOT NULL,
            started_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            PRIMARY KEY (optimization_id, candidate_id)
        )
        """
    )


def downgrade() -> None:
    """Drop the call and candidate tables."""
    op.execute("DROP TABLE IF EXISTS run_candidates")
    op.execute("DROP TABLE IF EXISTS run_calls")
