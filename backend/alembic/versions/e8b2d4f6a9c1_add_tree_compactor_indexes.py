"""Add the indexes that the tree compactor queries need (SKY-802).

Revision ID: e8b2d4f6a9c1
Revises: c4f1a7e9d2b6

The compactor's candidate and retired-pack queries filter ``storage_packs`` on
``created_at`` and sort by ``(created_at, pack_key)`` with a LIMIT, so one
composite index serves the filter, the order and the early stop. Both queries
also check if any ``agent_steps`` or ``tree_objects`` row points at a pack, and
a merge reads the rows of each pack, so each table gets a ``pack_key`` index.
The query for unrecorded packs reads ``storage_packs`` by its primary key and
needs no new index.

Built as a plain (transactional) CREATE INDEX, not CONCURRENTLY: the boot-time
upgrade runs inside a transaction, where a concurrent build fails (see
``c7d8e9f0a1b2``).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "e8b2d4f6a9c1"
down_revision: str | Sequence[str] | None = "c4f1a7e9d2b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the storage_packs (created_at, pack_key) index and the two pack_key indexes."""
    op.create_index(
        "ix_storage_packs_created_at_pack_key", "storage_packs", ["created_at", "pack_key"], if_not_exists=True
    )
    op.create_index("ix_agent_steps_pack_key", "agent_steps", ["pack_key"], if_not_exists=True)
    op.create_index("ix_tree_objects_pack_key", "tree_objects", ["pack_key"], if_not_exists=True)


def downgrade() -> None:
    """Drop the three indexes."""
    op.drop_index("ix_tree_objects_pack_key", table_name="tree_objects", if_exists=True)
    op.drop_index("ix_agent_steps_pack_key", table_name="agent_steps", if_exists=True)
    op.drop_index("ix_storage_packs_created_at_pack_key", table_name="storage_packs", if_exists=True)
