"""merge setup drafts and agent chat shares heads

Revision ID: 2cd2a1289484
Revises: b8d0f2a4c6e8, c4e6a8b0d2f4
Create Date: 2026-11-05 19:45:00.613815

"""
from __future__ import annotations

from collections.abc import Sequence

revision: str = '2cd2a1289484'
down_revision: str | None = ('b8d0f2a4c6e8', 'c4e6a8b0d2f4')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
