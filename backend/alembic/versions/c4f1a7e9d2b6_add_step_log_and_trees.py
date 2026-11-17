"""Add the agent step log, the tree store, the effect log and workspaces (SKY-710).

Revision ID: c4f1a7e9d2b6
Revises: d4e6f8a1b3c5

Every table and column of the Agent OS port lands here, so later tracks need
no migration: ``agent_steps``, ``tree_objects``, ``storage_packs``,
``agent_effects``, ``workspaces``, and ``workspace_id`` on ``jobs`` and
``execution_budgets``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c4f1a7e9d2b6"
down_revision: str | Sequence[str] | None = "d4e6f8a1b3c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _now() -> sa.TextClause:
    """Return the server default for a creation timestamp."""
    return sa.text("now()")


def upgrade() -> None:
    """Create the step log, tree, pack, effect and workspace tables, and the workspace columns."""
    op.create_table(
        "workspaces",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("username", sa.String(255), nullable=False),
        sa.Column("policy", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("execution_budget_id", sa.String(36), nullable=True),
        sa.Column("storage_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
    )
    op.create_index("ix_workspaces_username", "workspaces", ["username"])
    op.create_table(
        "storage_packs",
        sa.Column("pack_key", sa.String(512), primary_key=True),
        sa.Column("worker_id", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
    )
    op.create_index("ix_storage_packs_worker_id", "storage_packs", ["worker_id"])
    op.create_table(
        "agent_steps",
        # run_id 36 + ":" + agent_id 64 + ":" + 10 digits of the largest Integer seq = 112.
        sa.Column("step_id", sa.String(112), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("jobs.optimization_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_id", sa.String(64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("execution_generation", sa.Integer(), nullable=False),
        sa.Column("grant_list_id", sa.String(64), nullable=False),
        sa.Column("tree_id", sa.String(64), nullable=True),
        sa.Column("pack_key", sa.String(512), nullable=False),
        sa.Column("pack_offset", sa.BigInteger(), nullable=False),
        sa.Column("pack_length", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
    )
    op.create_index("ux_agent_steps_run_agent_seq", "agent_steps", ["run_id", "agent_id", "seq"], unique=True)
    op.create_table(
        "tree_objects",
        sa.Column("username", sa.String(255), primary_key=True),
        sa.Column("object_id", sa.String(64), primary_key=True),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("pack_key", sa.String(512), nullable=False),
        sa.Column("pack_offset", sa.BigInteger(), nullable=False),
        sa.Column("pack_length", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
    )
    op.create_table(
        "agent_effects",
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("jobs.optimization_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("effect_key", sa.String(255), primary_key=True),
        sa.Column("step_id", sa.String(112), nullable=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
    )
    op.add_column("jobs", sa.Column("workspace_id", sa.String(36), nullable=True))
    op.add_column("execution_budgets", sa.Column("workspace_id", sa.String(36), nullable=True))


def downgrade() -> None:
    """Drop the workspace columns and the new tables."""
    op.drop_column("execution_budgets", "workspace_id")
    op.drop_column("jobs", "workspace_id")
    op.drop_table("agent_effects")
    op.drop_table("tree_objects")
    op.drop_table("agent_steps")
    op.drop_table("storage_packs")
    op.drop_table("workspaces")
