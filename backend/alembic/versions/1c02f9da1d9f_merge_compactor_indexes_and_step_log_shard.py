"""Join the tree compactor index and step-log shard histories (SKY-819).

Revision ID: 1c02f9da1d9f
Revises: 66cae09708d3, e8b2d4f6a9c1

SKY-802 and SKY-731 each added a revision on ``c4f1a7e9d2b6``. With two heads,
``alembic upgrade head`` stops, so the staging migrate job cannot run.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "1c02f9da1d9f"
down_revision: str | Sequence[str] | None = ("66cae09708d3", "e8b2d4f6a9c1")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Keep both schema changes at one migration head."""


def downgrade() -> None:
    """Return to both parent revisions without changing schema objects."""
