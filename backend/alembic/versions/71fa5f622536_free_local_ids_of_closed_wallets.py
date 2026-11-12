"""Free the local customer ids that wallets closed before SKY-602 still hold.

A wallet created by a local debit gets the placeholder id
``local:<sha256(email)>``, and ``stripe_customer_id`` is unique. Before
SKY-602, account deletion kept that id on the closed wallet. A new account of
the same email then needs the same id, so its first local debit, budget or
test grant fails. This migration gives each closed wallet the placeholder id
of its own tombstone name, the same id that account deletion now writes.
Real Stripe customer ids stay, so refund and dispute webhooks still find them.

Revision ID: 71fa5f622536
Revises: 1b5b259642df
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "71fa5f622536"
down_revision: str | Sequence[str] | None = "1b5b259642df"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A real account username is an email, so it always has an "@"; a tombstone never does.
_TOMBSTONES = "username LIKE 'deleted-user-%' AND username NOT LIKE '%@%'"
# Copied from ``core.billing.service.local_customer_id`` so a later change there
# cannot change what this migration wrote.
_LOCAL_PREFIX = "local:"
_CUSTOMER_ID_MAX_LENGTH = 64


def _local_customer_id(username: str) -> str:
    """Build the placeholder customer id for ``username``.

    Args:
        username: The wallet's username.

    Returns:
        ``local:`` followed by a hex digest, at most 64 characters long.
    """
    digest = hashlib.sha256(username.encode("utf-8")).hexdigest()
    return f"{_LOCAL_PREFIX}{digest[: _CUSTOMER_ID_MAX_LENGTH - len(_LOCAL_PREFIX)]}"


def upgrade() -> None:
    """Give each closed wallet with a local placeholder id the id of its tombstone name."""
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            f"SELECT username, stripe_customer_id FROM billing_customers "
            f"WHERE {_TOMBSTONES} AND stripe_customer_id LIKE 'local:%'"
        )
    ).all()
    for username, customer_id in rows:
        new_id = _local_customer_id(username)
        if customer_id != new_id:
            bind.execute(
                sa.text("UPDATE billing_customers SET stripe_customer_id = :new_id WHERE username = :username"),
                {"new_id": new_id, "username": username},
            )


def downgrade() -> None:
    """Keep the new ids; the old ids belong to the live account of the same email."""
