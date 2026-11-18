"""Add a unique index on the progress event key of each job (SKY-808).

Revision ID: a8d3f5b7c9e1
Revises: e3b7d1f5a9c2

An engine that replays its committed steps on a resume sends their progress
events again. The keyed insert uses ``ON CONFLICT DO NOTHING`` on this partial
index, so the database keeps one row per job and string key. Only a key of 1
to 200 characters counts, as ``MAX_PROGRESS_EVENT_KEY_LENGTH`` in the models says.

Built as a plain (transactional) CREATE INDEX, not CONCURRENTLY:
``sync_migration_head`` applies pending migrations inside the schema-bootstrap
lock transaction, where ``autocommit_block`` cannot commit (see
``c7d8e9f0a1b2``). The build scans the table once under a SHARE lock, which
holds progress inserts until it ends. No row has a key before this release, so
the index starts empty.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a8d3f5b7c9e1"
down_revision: str | Sequence[str] | None = "e3b7d1f5a9c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the unique partial index on the job and its progress event key."""
    op.create_index(
        "ux_job_progress_events_event_key",
        "job_progress_events",
        ["optimization_id", sa.text("(metrics ->> 'progress_event_key')")],
        unique=True,
        if_not_exists=True,
        postgresql_where=sa.text(
            "jsonb_typeof(metrics -> 'progress_event_key') = 'string' "
            "AND length(metrics ->> 'progress_event_key') BETWEEN 1 AND 200"
        ),
    )


def downgrade() -> None:
    """Drop the progress event key index."""
    op.drop_index("ux_job_progress_events_event_key", table_name="job_progress_events", if_exists=True)
