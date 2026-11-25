"""Widen ``chat_media.mime_type`` so it holds the Office Open XML types.

Revision ID: d4e6f8a1b3c5
Revises: c3d5e7f9a2b4

SKY-735. Chat media now keeps the original of an attached DOCX or XLSX. Their
content types have 71 and 65 characters, longer than the 64 the column held,
so Postgres refused the insert.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d4e6f8a1b3c5"
down_revision: str | Sequence[str] | None = "c3d5e7f9a2b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Widen the content type column to 128 characters."""
    op.alter_column(
        "chat_media",
        "mime_type",
        existing_type=sa.String(64),
        type_=sa.String(128),
        existing_nullable=False,
    )


def downgrade() -> None:
    """Narrow the content type column back to 64 characters.

    A downgrade fails while a stored DOCX or XLSX original remains.
    """
    op.alter_column(
        "chat_media",
        "mime_type",
        existing_type=sa.String(128),
        type_=sa.String(64),
        existing_nullable=False,
    )
