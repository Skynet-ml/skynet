"""Verify the staging test-credit grant: its guards, its daily cap and its idempotency.

The grant funds a temporary staging or PR-preview check account through
:meth:`StripeBillingService.grant_test_credit`. Each test checks one guard or
one effect. With ``APP_BUDGET_TEST_DB_URL`` set, the same tests also run on
PostgreSQL, where the grant takes the advisory lock that serializes the cap.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.orm import Session

from core.billing.grant_test_credit import dollars_to_cents, main
from core.billing.service import (
    TEST_GRANT_DAILY_CAP_CENTS,
    GrantRefusedError,
    StripeBillingService,
    check_account_email,
)
from core.config import app_env, settings
from core.storage.models import Base, BillingCustomerModel, UserModel, WalletLedgerModel

PG_URL = app_env("APP_BUDGET_TEST_DB_URL")
TABLES = [model.__table__ for model in (BillingCustomerModel, WalletLedgerModel, UserModel)]
STAGING_ID = "staging-credit-check-a1b2c3"
PREVIEW_ID = "pr-107-credit-check-d4e5f6"


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param(
            "postgresql",
            marks=pytest.mark.skipif(
                not PG_URL or not PG_URL.startswith("postgresql"),
                reason="Set APP_BUDGET_TEST_DB_URL to an explicitly disposable PostgreSQL database.",
            ),
        ),
    ]
)
def engine(request: pytest.FixtureRequest) -> Iterator[Engine]:
    """Yield SQLite, and PostgreSQL when supplied, with the grant tables created.

    Args:
        request: Selects the database dialect.

    Yields:
        Engine with the billing and user tables in an isolated database or schema.
    """
    if request.param == "sqlite":
        database = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(database, tables=TABLES)
        yield database
        database.dispose()
        return
    schema = f"grant_test_credit_{uuid4().hex}"
    admin = create_engine(PG_URL)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    database = create_engine(PG_URL, connect_args={"options": f"-csearch_path={schema}"})
    try:
        Base.metadata.create_all(database, tables=TABLES)
        yield database
    finally:
        database.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def staging(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the test as the staging deployment."""
    monkeypatch.setattr(settings, "app_environment", "staging")


def _add_user(engine: Engine, email: str) -> None:
    """Create a ``users`` row, as the test-account sign-up does.

    Args:
        engine: Database holding the users table.
        email: Account email (the username).
    """
    with Session(engine) as session:
        session.add(UserModel(email=email, name="Check", password_hash=""))
        session.commit()


def _grant(engine: Engine, account_id: str = STAGING_ID, cents: int = 500, key: str = "k1", **overrides: str):
    """Call the grant with valid defaults, overridden per test.

    Args:
        engine: Database to grant against.
        account_id: Test-account id from the account tool.
        cents: Amount to grant, in cents.
        key: Idempotency key.
        **overrides: Replacement ``reason`` or ``approval`` text.

    Returns:
        The grant result.
    """
    return StripeBillingService(engine=engine).grant_test_credit(
        check_account_email(account_id),
        cents,
        reason=overrides.get("reason", "Run a paid optimization check"),
        approval=overrides.get("approval", "Gilad approved $5 in chat"),
        idempotency_key=key,
    )


def _balance(engine: Engine, email: str) -> int | None:
    """Read the account's purchased balance.

    Args:
        engine: Database holding the billing row.
        email: Account email.

    Returns:
        The balance in cents, or ``None`` when the account has no billing row.
    """
    with Session(engine) as session:
        row = session.get(BillingCustomerModel, email)
        return None if row is None else int(row.balance_cents)


def _ledger(engine: Engine) -> list[WalletLedgerModel]:
    """Read every ledger row.

    Args:
        engine: Database holding the ledger.

    Returns:
        All ledger rows, detached from the session.
    """
    with Session(engine, expire_on_commit=False) as session:
        return list(session.scalars(select(WalletLedgerModel)))


@pytest.mark.parametrize("environment", ["production", "", "dev", "skynet-staging", "staging-pr-x"])
def test_refuses_outside_staging_and_previews(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    """A production, empty or unknown environment refuses before any write."""
    monkeypatch.setattr(settings, "app_environment", environment)
    _add_user(engine, check_account_email(STAGING_ID))
    with pytest.raises(GrantRefusedError, match="environment"):
        _grant(engine)
    assert _ledger(engine) == []


@pytest.mark.parametrize(
    "email",
    [
        "someone@example.com",
        "staging-check-a1b2c3@example.com",
        "staging-check-zzzzzz@eegeupelae.resend.app",
        "pr-107-check-a1b2c3@eegeupelae.resend.app",
    ],
)
def test_refuses_an_account_the_test_tool_did_not_create(engine: Engine, staging: None, email: str) -> None:
    """Only a staging test-account email qualifies on staging, even when the user row exists."""
    _add_user(engine, email)
    with pytest.raises(GrantRefusedError, match="test account"):
        StripeBillingService(engine=engine).grant_test_credit(
            email, 500, reason="r", approval="a", idempotency_key="k1"
        )
    assert _ledger(engine) == []


def test_refuses_a_test_account_with_no_user_row(engine: Engine, staging: None) -> None:
    """A well-formed id that never signed up is refused, so no wallet appears for nobody."""
    with pytest.raises(GrantRefusedError, match="no user"):
        _grant(engine)
    assert _balance(engine, check_account_email(STAGING_ID)) is None


def test_preview_accepts_only_its_own_pr_accounts(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """A PR preview grants to ``pr-<n>-`` accounts of that PR and refuses staging ones."""
    monkeypatch.setattr(settings, "app_environment", "skynet-pr-107")
    _add_user(engine, check_account_email(PREVIEW_ID))
    _add_user(engine, check_account_email(STAGING_ID))
    assert _grant(engine, PREVIEW_ID).credited_cents == 500
    with pytest.raises(GrantRefusedError, match="test account"):
        _grant(engine, STAGING_ID, key="k2")


@pytest.mark.parametrize("field", ["approval", "reason"])
@pytest.mark.parametrize("value", ["", "   "])
def test_refuses_missing_approval_or_reason(engine: Engine, staging: None, field: str, value: str) -> None:
    """Approval and reason text are both required and recorded."""
    _add_user(engine, check_account_email(STAGING_ID))
    with pytest.raises(GrantRefusedError, match=field):
        _grant(engine, **{field: value})
    assert _ledger(engine) == []


def test_writes_balance_and_grant_row_together(engine: Engine, staging: None) -> None:
    """One grant raises the balance and adds one ``grant`` row with the audit text."""
    email = check_account_email(STAGING_ID)
    _add_user(engine, email)
    result = _grant(engine, cents=725)
    assert (result.credited_cents, result.balance_cents, result.replayed) == (725, 725, False)
    assert _balance(engine, email) == 725
    [row] = _ledger(engine)
    assert (row.username, row.delta_cents, row.kind, row.settlement_key) == (email, 725, "grant", "testgrant:k1")
    assert "Run a paid optimization check" in row.description
    assert "Gilad approved $5 in chat" in row.description
    assert row.stripe_payment_intent_id is None


def test_retry_with_the_same_key_credits_once(engine: Engine, staging: None) -> None:
    """A repeated idempotency key returns the first grant and adds nothing."""
    email = check_account_email(STAGING_ID)
    _add_user(engine, email)
    _grant(engine, cents=500)
    again = _grant(engine, cents=500)
    assert (again.credited_cents, again.balance_cents, again.replayed) == (500, 500, True)
    assert _balance(engine, email) == 500
    assert len(_ledger(engine)) == 1


def test_daily_cap_counts_every_account(engine: Engine, staging: None) -> None:
    """Grants on all accounts share one $50 UTC-day cap; the grant that would pass it is refused."""
    other = "staging-other-check-0f0f0f"
    _add_user(engine, check_account_email(STAGING_ID))
    _add_user(engine, check_account_email(other))
    _grant(engine, cents=3000, key="a")
    _grant(engine, other, cents=TEST_GRANT_DAILY_CAP_CENTS - 3000, key="b")
    with pytest.raises(GrantRefusedError, match=r"\$50\.00"):
        _grant(engine, cents=1, key="c")
    assert len(_ledger(engine)) == 2


def test_daily_cap_ignores_earlier_days_and_other_kinds(engine: Engine, staging: None) -> None:
    """Yesterday's grants and today's top-ups do not count toward today's cap."""
    email = check_account_email(STAGING_ID)
    _add_user(engine, email)
    with Session(engine) as session:
        session.add(
            WalletLedgerModel(
                username=email,
                delta_cents=TEST_GRANT_DAILY_CAP_CENTS,
                kind="grant",
                settlement_key="testgrant:yesterday",
                created_at=datetime.now(UTC) - timedelta(days=1, minutes=1),
            )
        )
        session.add(WalletLedgerModel(username=email, delta_cents=9000, kind="topup"))
        session.commit()
    assert _grant(engine, cents=TEST_GRANT_DAILY_CAP_CENTS).credited_cents == TEST_GRANT_DAILY_CAP_CENTS


@pytest.mark.parametrize("cents", [0, -100])
def test_refuses_a_non_positive_amount(engine: Engine, staging: None, cents: int) -> None:
    """Only a positive amount is a grant."""
    _add_user(engine, check_account_email(STAGING_ID))
    with pytest.raises(GrantRefusedError, match="amount"):
        _grant(engine, cents=cents)


@pytest.mark.parametrize("key", ["", "x" * 55])
def test_refuses_a_key_that_does_not_fit(engine: Engine, staging: None, key: str) -> None:
    """The key must be present and fit ``settlement_key`` after the ``testgrant:`` prefix."""
    _add_user(engine, check_account_email(STAGING_ID))
    with pytest.raises(GrantRefusedError, match="idempotency key"):
        _grant(engine, key=key)


def test_refuses_audit_text_longer_than_the_column(engine: Engine, staging: None) -> None:
    """The audit text is never cut: too long a reason plus approval is refused."""
    _add_user(engine, check_account_email(STAGING_ID))
    with pytest.raises(GrantRefusedError, match="too long"):
        _grant(engine, reason="r" * 200, approval="a" * 100)


@pytest.mark.parametrize(
    ("dollars", "cents"),
    [("5", 500), ("0.01", 1), ("0.29", 29), ("12.5", 1250), ("12.34", 1234), ("50.00", 5000)],
)
def test_dollars_to_cents(dollars: str, cents: int) -> None:
    """Dollar text converts to exact cents."""
    assert dollars_to_cents(dollars) == cents


@pytest.mark.parametrize("dollars", ["0", "-1", "1.234", "abc", "nan", "inf", ""])
def test_dollars_to_cents_refuses_bad_amounts(dollars: str) -> None:
    """Zero, negative, sub-cent and non-numeric amounts are refused."""
    with pytest.raises(GrantRefusedError, match="dollars"):
        dollars_to_cents(dollars)


def test_cli_grants_and_prints_dollars_without_the_email(
    engine: Engine, staging: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command converts dollars, grants once, and prints dollars but never the email."""
    email = check_account_email(STAGING_ID)
    _add_user(engine, email)
    monkeypatch.setattr("core.billing.grant_test_credit._engine", lambda: engine)
    argv = [
        "--account-id",
        STAGING_ID,
        "--dollars",
        "7.25",
        "--reason",
        "Run a paid optimization check",
        "--approval",
        "Gilad approved $7.25 in chat",
        "--idempotency-key",
        "cli-1",
    ]
    assert main(argv) == 0
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert "$7.25" in out
    assert "@" not in out
    assert _balance(engine, email) == 725


def test_cli_reports_a_refusal_with_exit_code_2(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refused grant exits 2 with the reason on stderr and writes nothing."""
    monkeypatch.setattr(settings, "app_environment", "production")
    monkeypatch.setattr("core.billing.grant_test_credit._engine", lambda: engine)
    argv = ["--account-id", STAGING_ID, "--dollars", "1", "--reason", "r", "--approval", "a", "--idempotency-key", "k"]
    assert main(argv) == 2
    assert "refused" in capsys.readouterr().err
    assert _ledger(engine) == []
