"""Drop the lifted-in-use metric view.

Revision ID: 6db6fa226aca
Revises: f2a4c6e8b0d1
Create Date: 2026-11-27 12:00:00.000000

The view counted ``serve_inference`` events. Skynet no longer serves optimized
programs, so nothing emits that event and the admin north-star endpoint no
longer reads the view. Old ``telemetry_events`` rows stay. Postgres-only;
other dialects skip it.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "6db6fa226aca"
down_revision: str | None = "f2a4c6e8b0d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A frozen copy of the view as revision d1e2f3a4b5c6 created it, so the
# downgrade restores the same schema.
_QUALIFYING_RUN = """
    CASE WHEN jsonb_typeof({p} -> 'lift') = 'number' THEN ({p} ->> 'lift')::numeric END >= 0.02
    AND {p} -> 'held_out' = 'true'::jsonb
    AND CASE WHEN jsonb_typeof({p} -> 'held_out_rows') = 'number'
        THEN ({p} ->> 'held_out_rows')::numeric END >= 20
    AND {p} -> 'from_sample' = 'false'::jsonb
"""

_LIFTED_IN_USE_DDL = f"""
CREATE OR REPLACE VIEW metric_lifted_in_use_weekly AS
WITH program_days AS (
    SELECT
        j.username,
        date_trunc('week', e.received_at AT TIME ZONE 'UTC')::date AS week,
        count(DISTINCT (e.received_at AT TIME ZONE 'UTC')::date) AS days
    FROM telemetry_events e
    JOIN jobs j ON j.optimization_id = e.properties ->> 'optimization_id'
    JOIN users u ON u.email = j.username AND NOT u.is_internal
    WHERE e.event_name = 'serve_inference'
    AND e.properties ->> 'channel' IN ('api_token', 'share_link')
    GROUP BY j.optimization_id, j.username, 2
)
SELECT d.week, count(DISTINCT d.username) AS users_in_use
FROM program_days d
WHERE d.days >= 2
AND EXISTS (
    SELECT 1 FROM telemetry_events r
    WHERE r.event_name = 'run_completed'
    AND r.username = d.username
    AND r.received_at < ((d.week + 7)::timestamp AT TIME ZONE 'UTC')
    AND {_QUALIFYING_RUN.format(p="r.properties")}
)
GROUP BY d.week
"""


def upgrade() -> None:
    """Drop the lifted-in-use view on Postgres."""
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP VIEW IF EXISTS metric_lifted_in_use_weekly")


def downgrade() -> None:
    """Re-create the lifted-in-use view on Postgres exactly as d1e2f3a4b5c6 created it."""
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(_LIFTED_IN_USE_DDL)
