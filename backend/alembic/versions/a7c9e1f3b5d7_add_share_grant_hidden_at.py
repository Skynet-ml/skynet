"""Let a grantee hide a shared item from their own "Shared with me" view.

Revision ID: a7c9e1f3b5d7
Revises: e4f6a8b0c2d3
"""

import sqlalchemy as sa

from alembic import op

revision = "a7c9e1f3b5d7"
down_revision = "e4f6a8b0c2d3"
branch_labels = None
depends_on = None

_GRANT_TABLES = (
    "optimization_share_grants",
    "dataset_share_grants",
    "tagging_session_share_grants",
    "run_folder_share_grants",
)


def upgrade() -> None:
    """Add a nullable hidden timestamp to every share-grant table."""
    for table in _GRANT_TABLES:
        op.add_column(table, sa.Column("hidden_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Remove the hidden timestamp from every share-grant table."""
    for table in _GRANT_TABLES:
        op.drop_column(table, "hidden_at")
