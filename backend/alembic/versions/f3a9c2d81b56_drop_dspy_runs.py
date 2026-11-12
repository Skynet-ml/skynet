"""Delete the retired DSPy, grid search and pair runs (SKY-656).

Revision ID: f3a9c2d81b56
Revises: c4d1e8a2f6b3

Since SKY-316 every run optimizes a repository, and the code that ran, read
and served DSPy runs is gone. This revision deletes every job row that only
that code could read:

- ``optimization_type = 'run'``: a DSPy program run.
- ``optimization_type = 'grid_search'``: a grid search parent.
- a row with ``parent_optimization_id``: a grid pair child.
- a ``blackbox`` row whose payload has ``payload_kind = 'unified_v1'``: a
  DSPy program run through the unified engine.

The rows of those jobs go too. The tables with a cascading FK to
``jobs.optimization_id`` lose them with the job: ``gepa_checkpoints``,
``grid_pair_results``, ``blackbox_agent_runs``, ``run_calls``,
``run_candidates``, ``sandbox_trials`` and the pair children. The tables
that keep a job id with no FK are cleared here: ``job_logs``,
``job_progress_events``, ``job_embeddings``, ``notifications``,
``optimization_share_links``, ``optimization_share_grants``,
``run_folder_items``, ``continuous_cycles`` and ``continuous_plans`` (with the
cycles of a deleted plan). ``wallet_ledger`` and ``execution_budgets`` are
billing records, so they keep their rows. Each budget of a deleted job is
closed and detached the same way ``BudgetService.close_for_deleted_jobs``
does it: its unstarted reservations are released, so no money stays held.

It keeps the ``grid_pair_results`` table, ``jobs.parent_optimization_id``
(with its self FK and index), ``jobs.pair_index`` and ``job_logs.pair_index``:
aws-deploy migrates before the rollout, and the old pods still use them. An
old pod deletes from ``grid_pair_results`` on every job delete. The new code
does not read or write the table; its cascading FK clears it when a job goes.
The new ORM does not map the pair columns, and they are nullable, so new rows
leave them empty. A later revision drops the table and the columns after this
deploy.
``gepa_checkpoints.pair_index`` stays for good: it is part of that table's
primary key and a repository run uses it.

Every step uses ``IF EXISTS`` or matches no rows on a second run, so the
revision is idempotent. Postgres only, like the revisions that added these
columns: the SQLite test schema comes from the ORM models. The downgrade
does nothing: it cannot bring the deleted rows back.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f3a9c2d81b56"
down_revision: str | Sequence[str] | None = "c4d1e8a2f6b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEAD_JOBS = "_sky656_dead_jobs"

_JOB_ID_TABLES = (
    "job_logs",
    "job_progress_events",
    "job_embeddings",
    "notifications",
    "optimization_share_links",
    "optimization_share_grants",
    "run_folder_items",
    "continuous_cycles",
)


def _has_column(table: str, column: str) -> bool:
    """Return whether ``table`` exists and has ``column``.

    Args:
        table: The table name.
        column: The column name.

    Returns:
        True when the column exists.
    """
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return False
    return any(found["name"] == column for found in inspector.get_columns(table))


def _has_table(table: str) -> bool:
    """Return whether ``table`` exists.

    Args:
        table: The table name.

    Returns:
        True when the table exists.
    """
    return sa.inspect(op.get_bind()).has_table(table)


def _collect_dead_jobs() -> None:
    """Fill a temporary table with the id of every retired DSPy, grid or pair job."""
    op.execute(f"DROP TABLE IF EXISTS {_DEAD_JOBS}")
    op.execute(
        f"""
        CREATE TEMPORARY TABLE {_DEAD_JOBS} AS
        SELECT optimization_id FROM jobs
        WHERE optimization_type IN ('run', 'grid_search')
           OR (optimization_type = 'blackbox' AND payload ->> 'payload_kind' = 'unified_v1')
        """
    )
    if _has_column("jobs", "parent_optimization_id"):
        op.execute(
            f"""
            INSERT INTO {_DEAD_JOBS}
            SELECT optimization_id FROM jobs
            WHERE parent_optimization_id IS NOT NULL
              AND optimization_id NOT IN (SELECT optimization_id FROM {_DEAD_JOBS})
            """
        )


def _close_dead_budgets() -> None:
    """Release the holds of the dead jobs' budgets, then close and detach those budgets.

    This is ``BudgetService.close_for_deleted_jobs`` in SQL. A reserved
    operation never started, so its units go back to the budget and the
    wallet reserve. Dispatched work keeps its coverage until its usage
    reconciles. A blocked budget keeps its state and reason.
    """
    if not (_has_table("execution_budgets") and _has_table("execution_operations")):
        return
    dead_budgets = f"SELECT id FROM execution_budgets WHERE job_id IN (SELECT optimization_id FROM {_DEAD_JOBS})"
    op.execute(
        f"""
        UPDATE execution_budgets AS budget
        SET reserved_units = budget.reserved_units - held.units,
            wallet_reserved_units = budget.wallet_reserved_units - held.wallet_units
        FROM (
            SELECT budget_id, SUM(max_units) AS units, SUM(max_wallet_units) AS wallet_units
            FROM execution_operations
            WHERE state = 'reserved' AND budget_id IN ({dead_budgets})
            GROUP BY budget_id
        ) AS held
        WHERE budget.id = held.budget_id
        """
    )
    op.execute(
        f"""
        UPDATE execution_operations SET state = 'released', updated_at = now()
        WHERE state = 'reserved' AND budget_id IN ({dead_budgets})
        """
    )
    op.execute(
        f"""
        UPDATE execution_budgets
        SET blocked_reason = CASE WHEN state = 'blocked' THEN blocked_reason ELSE 'job_deleted' END,
            state = CASE WHEN state = 'blocked' THEN state ELSE 'closed' END,
            job_id = NULL,
            updated_at = now()
        WHERE id IN ({dead_budgets})
        """
    )


def _delete_dead_rows() -> None:
    """Delete the dead jobs and every row that names one of them."""
    if _has_table("continuous_plans"):
        plans = f"""
            SELECT id FROM continuous_plans
            WHERE source_optimization_id IN (SELECT optimization_id FROM {_DEAD_JOBS})
               OR incumbent_optimization_id IN (SELECT optimization_id FROM {_DEAD_JOBS})
        """
        if _has_table("continuous_cycles"):
            op.execute(f"DELETE FROM continuous_cycles WHERE plan_id IN ({plans})")
        op.execute(f"DELETE FROM continuous_plans WHERE id IN ({plans})")
    for table in _JOB_ID_TABLES:
        if _has_table(table):
            op.execute(f"DELETE FROM {table} WHERE optimization_id IN (SELECT optimization_id FROM {_DEAD_JOBS})")
    # The pair children go in the same statement as their parents, so the
    # self FK cascade has nothing left to do.
    op.execute(f"DELETE FROM jobs WHERE optimization_id IN (SELECT optimization_id FROM {_DEAD_JOBS})")
    op.execute(f"DROP TABLE IF EXISTS {_DEAD_JOBS}")


def upgrade() -> None:
    """Delete the retired DSPy, grid and pair jobs and the rows that name them."""
    if op.get_bind().dialect.name != "postgresql":
        return
    _collect_dead_jobs()
    _close_dead_budgets()
    _delete_dead_rows()
    # Old pods still use grid_pair_results, jobs.parent_optimization_id,
    # jobs.pair_index and job_logs.pair_index during the rollout. A later
    # revision drops them after this deploy.


def downgrade() -> None:
    """Do nothing: the schema is unchanged and the deleted rows stay gone."""
