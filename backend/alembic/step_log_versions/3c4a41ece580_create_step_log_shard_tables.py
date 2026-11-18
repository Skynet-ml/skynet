"""Create the step log tables on a step log shard database (SKY-731).

Revision ID: 3c4a41ece580
Revises:

This chain runs only on the shard databases in ``STEP_LOG_SHARDS``; the
main database gets the same tables from ``c4f1a7e9d2b6``. It lives outside
``alembic/versions`` so the main chain keeps one head and ``upgrade head``
stays one target. The tables match the main ones except for the foreign key
to ``jobs``: a shard has no ``jobs`` table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "3c4a41ece580"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _now() -> sa.TextClause:
    """Return the server default for a creation timestamp."""
    return sa.text("now()")


def upgrade() -> None:
    """Create ``storage_packs``, ``agent_steps`` and ``tree_objects``."""
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
        sa.Column("step_id", sa.String(112), primary_key=True),
        # No foreign key: the job row is on the main database, so a job or account delete does not
        # cascade to these rows. The purge of shard rows comes with SKY-733 (tree retention and purge).
        sa.Column("run_id", sa.String(36), nullable=False),
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


def downgrade() -> None:
    """Drop the step log tables of the shard."""
    op.drop_table("tree_objects")
    op.drop_table("agent_steps")
    op.drop_table("storage_packs")
