"""Close the wallets of accounts that were deleted before SKY-567.

Account deletion renames the wallet to a ``deleted-user-<hash>`` tombstone.
Before SKY-567 it left the balance and the free grant in place, so those
orphan rows still counted as outstanding liability. This migration closes
them the same way account deletion now does: a ``closure`` ledger row
records the forfeited balance, and the balance and grant go to zero.

Only a positive balance gets a closure row. The
``ck_billing_customers_balance_cents_non_negative`` CHECK constraint makes a
negative balance impossible, so the UPDATE never zeroes one without a row.

Revision ID: 1b5b259642df
Revises: bfe5a0ff5942
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "1b5b259642df"
down_revision: str | Sequence[str] | None = "bfe5a0ff5942"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A real account username is an email, so it always has an "@"; a tombstone never does.
_TOMBSTONES = "username LIKE 'deleted-user-%' AND username NOT LIKE '%@%'"


def upgrade() -> None:
    """Write a closure row for each open tombstone balance, then zero the tombstone wallets."""
    op.execute(
        f"""
        INSERT INTO wallet_ledger (username, delta_cents, kind, description, created_at)
        SELECT username, -balance_cents, 'closure', 'Account deleted', CURRENT_TIMESTAMP
        FROM billing_customers
        WHERE {_TOMBSTONES} AND balance_cents > 0
        """
    )
    op.execute(
        f"""
        UPDATE billing_customers
        SET balance_cents = 0, grant_remaining = 0, updated_at = CURRENT_TIMESTAMP
        WHERE {_TOMBSTONES} AND (balance_cents <> 0 OR grant_remaining IS NULL OR grant_remaining <> 0)
        """
    )


def downgrade() -> None:
    """Keep the closed wallets closed; the forfeited balance belongs to no account."""
