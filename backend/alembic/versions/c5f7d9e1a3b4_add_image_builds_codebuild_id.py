"""Add the CodeBuild build id of an image build that may still run (SKY-896).

Revision ID: c5f7d9e1a3b4
Revises: b4e6c8d0f2a3

``image_builds.codebuild_id`` holds the CodeBuild build id from the build
start until the build succeeds; a failed or cancelled build keeps it. Account
deletion stops each recent build that still has an id, so a build whose worker
was hard-killed cannot push after the account's repository delete.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c5f7d9e1a3b4"
down_revision: str | Sequence[str] | None = "b4e6c8d0f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable CodeBuild build id column."""
    op.add_column("image_builds", sa.Column("codebuild_id", sa.String(length=255), nullable=True))


def downgrade() -> None:
    """Drop the CodeBuild build id column."""
    op.drop_column("image_builds", "codebuild_id")
