"""Grant test credit to a staging check account (SKY-573).

Agents run this command through ECS RunTask on the staging migrate task, which
has ``APP_ENVIRONMENT`` and ``REMOTE_DB_URL``. The grant goes through
:meth:`StripeBillingService.grant_test_credit`, so every guard (environment,
test account, approval text, $50 UTC-day cap, idempotency) applies. Amounts
are in dollars on the command line and in the output. The output never prints
the account email.

Usage:
    python -m core.billing.grant_test_credit --account-id staging-label-a1b2c3 \\
        --dollars 2.50 --reason "..." --approval "..." --idempotency-key KEY
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation

from sqlalchemy import Engine, create_engine

from core.billing.service import GrantRefusedError, StripeBillingService, check_account_email
from core.config import settings

EXIT_REFUSED = 2


def dollars_to_cents(dollars: str) -> int:
    """Convert a dollar amount to exact cents.

    Args:
        dollars: Amount text such as ``"2.50"``.

    Returns:
        The amount in cents.

    Raises:
        GrantRefusedError: The text is not a positive amount in whole cents.
    """
    try:
        amount = Decimal(dollars.strip())
    except InvalidOperation as error:
        raise GrantRefusedError(f"dollars must be a number, got {dollars!r}") from error
    if not amount.is_finite() or amount <= 0:
        raise GrantRefusedError("dollars must be a positive amount")
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise GrantRefusedError("dollars must have at most 2 decimal places")
    return int(cents)


def _engine() -> Engine:
    """Build the engine for the deployment database.

    Returns:
        An engine on ``REMOTE_DB_URL``.

    Raises:
        GrantRefusedError: ``REMOTE_DB_URL`` is not set.
    """
    if not settings.remote_db_url:
        raise GrantRefusedError("REMOTE_DB_URL is not set")
    return create_engine(settings.remote_db_url.get_secret_value(), pool_pre_ping=True)


def _parser() -> argparse.ArgumentParser:
    """Build the command-line parser.

    Returns:
        The parser for the grant arguments.
    """
    parser = argparse.ArgumentParser(
        prog="python -m core.billing.grant_test_credit",
        description="Grant test credit to a staging check account. Needs Gilad's approval in chat first.",
    )
    parser.add_argument("--account-id", required=True, help="Account id from the test-account tool.")
    parser.add_argument("--dollars", required=True, help="Amount in dollars, for example 2.50.")
    parser.add_argument("--reason", required=True, help="Why the check needs the money.")
    parser.add_argument("--approval", required=True, help="Gilad's approval text from chat.")
    parser.add_argument("--idempotency-key", required=True, help="Key that makes a retry credit nothing.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one grant and print the result in dollars.

    Args:
        argv: Arguments without the program name; ``None`` reads ``sys.argv``.

    Returns:
        ``0`` on a grant or a replay, ``2`` on a refusal.
    """
    args = _parser().parse_args(argv)
    try:
        cents = dollars_to_cents(args.dollars)
        result = StripeBillingService(engine=_engine()).grant_test_credit(
            check_account_email(args.account_id),
            cents,
            reason=args.reason,
            approval=args.approval,
            idempotency_key=args.idempotency_key,
        )
    except GrantRefusedError as error:
        print(f"Grant refused: {error}", file=sys.stderr)
        return EXIT_REFUSED
    verb = "Already granted" if result.replayed else "Granted"
    print(
        f"{verb} ${result.credited_cents / 100:.2f} to {args.account_id}. "
        f"Balance: ${result.balance_cents / 100:.2f}. Key: {args.idempotency_key}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
