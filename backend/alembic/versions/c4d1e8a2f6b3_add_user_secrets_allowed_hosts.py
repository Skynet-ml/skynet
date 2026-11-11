"""Store the hosts a user approved for each saved secret (SKY-641).

A run may send a saved secret only to a host in this list. Every secret saved
before this revision gets an empty list, so it goes to no host until the user
saves it again with a host.

Revision ID: c4d1e8a2f6b3
Revises: 71fa5f622536
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "c4d1e8a2f6b3"
down_revision = "71fa5f622536"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add ``user_secrets.allowed_hosts`` unless startup already created it."""
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("user_secrets")}
    if "allowed_hosts" in columns:
        return
    op.add_column(
        "user_secrets",
        sa.Column(
            "allowed_hosts",
            sa.JSON().with_variant(JSONB(), "postgresql"),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )


def downgrade() -> None:
    """Drop ``user_secrets.allowed_hosts``."""
    op.drop_column("user_secrets", "allowed_hosts")
