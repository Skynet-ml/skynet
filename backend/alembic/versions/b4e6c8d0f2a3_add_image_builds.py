"""Add the image build table and the per-box image pin of a job (SKY-737).

Revision ID: b4e6c8d0f2a3
Revises: a8d3f5b7c9e1

``image_builds`` keeps each build of a user's ``[run]`` or ``[metric]``
Dockerfile with its full log. A later run of the same user reuses a finished
build whose input hash matches. ``jobs.box_images`` pins the user image of
each box at the first start, so a resume boots the same two digests.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b4e6c8d0f2a3"
down_revision: str | Sequence[str] | None = "a8d3f5b7c9e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    """Create the image build table and add the job's box image pin."""
    op.create_table(
        "image_builds",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("input_hash", sa.String(length=80), nullable=False),
        sa.Column("builder", sa.String(length=32), nullable=False),
        sa.Column("image", sa.String(length=512), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("log", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_image_builds_username_input_hash",
        "image_builds",
        ["username", "input_hash"],
    )
    op.add_column("jobs", sa.Column("box_images", _JSON, nullable=True))


def downgrade() -> None:
    """Drop the job's box image pin and the image build table."""
    op.drop_column("jobs", "box_images")
    op.drop_index("ix_image_builds_username_input_hash", table_name="image_builds")
    op.drop_table("image_builds")
