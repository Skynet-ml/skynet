"""Verify the staging test-credit grant: its guards, its daily cap and its idempotency.

The grant funds a temporary staging check account through
:meth:`StripeBillingService.grant_test_credit`. Each test checks one guard or
one effect. With ``APP_BUDGET_TEST_DB_URL`` set, the same tests also run on
PostgreSQL, where the grant takes the advisory lock that serializes the cap,
and a PostgreSQL-only test proves that lock blocks a grant on another account.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, func, make_url, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from core.billing import service as billing_service
from core.billing.grant_test_credit import dollars_to_cents, main
from core.billing.service import (
    TEST_GRANT_DAILY_CAP_CENTS,
    GrantRefusedError,
    GrantResult,
    StripeBillingService,
    check_account_email,
)
from core.config import app_env, settings
from core.storage.models import Base, BillingCustomerModel, UserModel, WalletLedgerModel

PG_URL = app_env("APP_BUDGET_TEST_DB_URL")
TABLES = [model.__table__ for model in (BillingCustomerModel, WalletLedgerModel, UserModel)]
STAGING_ID = "staging-credit-check-a1b2c3"
PREVIEW_ID = "pr-107-credit-check-d4e5f6"
STAGING_RDS_URL = "postgresql://app@skynet-staging.abc123.us-east-2.rds.amazonaws.com:5432/skynet"
PRODUCTION_RDS_URL = "postgresql://app@skynet-production.abc123.us-east-2.rds.amazonaws.com:5432/skynet"


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
    yield from _postgres_engine()


@pytest.fixture
def pg_engine() -> Iterator[Engine]:
    """Yield only PostgreSQL, for behavior that SQLite does not have.

    Yields:
        Engine with the billing and user tables in an isolated schema.
    """
    if not PG_URL or not PG_URL.startswith("postgresql"):
        pytest.skip("Set APP_BUDGET_TEST_DB_URL to an explicitly disposable PostgreSQL database.")
    yield from _postgres_engine()


@pytest.fixture(autouse=True)
def test_database_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Accept the local test PostgreSQL host as the staging database host.

    The grant refuses any PostgreSQL host but the staging RDS instance. Tests
    that check that guard set the real label back.

    Args:
        monkeypatch: Replaces the staging host label for this test.
    """
    host = make_url(PG_URL).host if PG_URL and PG_URL.startswith("postgresql") else None
    monkeypatch.setattr(billing_service, "_STAGING_DB_HOST_LABEL", (host or "localhost").lower().split(".")[0])


def _postgres_engine() -> Iterator[Engine]:
    """Yield a PostgreSQL engine on a new schema, and drop the schema after.

    Yields:
        Engine with the billing and user tables in an isolated schema.
    """
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


def _grant(
    engine: Engine,
    account_id: str = STAGING_ID,
    cents: int = 500,
    key: str = "k1",
    *,
    reason: str = "Run a paid optimization check",
    approval: str = "Gilad approved $5 in chat",
) -> GrantResult:
    """Call the grant with valid defaults, overridden per test.

    Args:
        engine: Database to grant against.
        account_id: Test-account id from the account tool.
        cents: Amount to grant, in cents.
        key: Idempotency key.
        reason: Reason text for the ledger description.
        approval: Approval text for the operator log.

    Returns:
        The grant result.
    """
    return StripeBillingService(engine=engine).grant_test_credit(
        check_account_email(account_id),
        cents,
        reason=reason,
        approval=approval,
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
def test_refuses_outside_staging(engine: Engine, monkeypatch: pytest.MonkeyPatch, environment: str) -> None:
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


@pytest.mark.parametrize("account_id", [PREVIEW_ID, STAGING_ID])
def test_refuses_a_preview_environment(engine: Engine, monkeypatch: pytest.MonkeyPatch, account_id: str) -> None:
    """A PR preview refuses every grant: its own database would get a second $50 cap."""
    monkeypatch.setattr(settings, "app_environment", "skynet-pr-107")
    _add_user(engine, check_account_email(account_id))
    with pytest.raises(GrantRefusedError, match="PR preview environments are refused"):
        _grant(engine, account_id)
    assert _ledger(engine) == []


@pytest.mark.parametrize("field", ["approval", "reason"])
@pytest.mark.parametrize("value", ["", "   "])
def test_refuses_missing_approval_or_reason(engine: Engine, staging: None, field: str, value: str) -> None:
    """Approval and reason text are both required and recorded."""
    _add_user(engine, check_account_email(STAGING_ID))
    with pytest.raises(GrantRefusedError, match=field):
        if field == "approval":
            _grant(engine, approval=value)
        else:
            _grant(engine, reason=value)
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
    assert row.description == "Test grant: Run a paid optimization check"
    assert row.stripe_payment_intent_id is None


def test_approval_goes_to_the_operator_log_not_the_ledger(
    engine: Engine, staging: None, caplog: pytest.LogCaptureFixture
) -> None:
    """The account sees the ledger description, so the approval text is only in the operator log."""
    _add_user(engine, check_account_email(STAGING_ID))
    with caplog.at_level(logging.WARNING, logger="app.billing.service"):
        _grant(engine)
    [row] = _ledger(engine)
    assert "Gilad approved" not in row.description
    assert "approval" not in row.description
    assert "Gilad approved $5 in chat" in caplog.text
    assert "testgrant:k1" in caplog.text


def test_retry_with_the_same_key_credits_once(engine: Engine, staging: None) -> None:
    """A repeated idempotency key returns the first grant and adds nothing."""
    email = check_account_email(STAGING_ID)
    _add_user(engine, email)
    _grant(engine, cents=500)
    again = _grant(engine, cents=500)
    assert (again.credited_cents, again.balance_cents, again.replayed) == (500, 500, True)
    assert _balance(engine, email) == 500
    assert len(_ledger(engine)) == 1


@pytest.mark.parametrize(("account_id", "cents"), [("staging-other-check-0f0f0f", 500), (STAGING_ID, 999)])
def test_refuses_a_reused_key_for_another_account_or_amount(
    engine: Engine, staging: None, account_id: str, cents: int
) -> None:
    """A key reused with a different account or amount is refused, not reported as a replay."""
    _add_user(engine, check_account_email(STAGING_ID))
    _add_user(engine, check_account_email("staging-other-check-0f0f0f"))
    _grant(engine, cents=500)
    with pytest.raises(GrantRefusedError, match="different account or amount"):
        _grant(engine, account_id, cents=cents)
    assert _balance(engine, check_account_email(STAGING_ID)) == 500
    assert _balance(engine, check_account_email("staging-other-check-0f0f0f")) is None
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


def test_refuses_a_reason_longer_than_the_column(engine: Engine, staging: None) -> None:
    """The reason is never cut: a reason that does not fit the description is refused."""
    _add_user(engine, check_account_email(STAGING_ID))
    with pytest.raises(GrantRefusedError, match="at most 243 characters"):
        _grant(engine, reason="r" * 244)
    _grant(engine, reason="r" * 243, approval="a" * 1000)
    [row] = _ledger(engine)
    assert len(row.description) == 255


@pytest.mark.parametrize(
    ("url", "staging_database"),
    [
        (STAGING_RDS_URL, True),
        ("postgresql://app@SKYNET-STAGING.abc123.us-east-2.rds.amazonaws.com/skynet_pr_107", True),
        (PRODUCTION_RDS_URL, False),
        ("postgresql://app@skynet-staging-copy.abc123.us-east-2.rds.amazonaws.com/skynet", False),
        ("postgresql://app@localhost:5432/skynet", False),
        ("postgresql:///skynet", False),
        ("sqlite:///:memory:", True),
        (f"{STAGING_RDS_URL}?host=skynet-production.abc123.us-east-2.rds.amazonaws.com", False),
        (f"{STAGING_RDS_URL}?hostaddr=10.0.0.5", False),
    ],
)
def test_staging_database_host_check(monkeypatch: pytest.MonkeyPatch, url: str, staging_database: bool) -> None:
    """Only the staging RDS host passes; production, copies, tunnels, socket URLs and query host overrides do not."""
    monkeypatch.setattr(billing_service, "_STAGING_DB_HOST_LABEL", "skynet-staging")
    assert billing_service._is_staging_database(make_url(url)) is staging_database


def test_refuses_the_production_database_even_with_a_staging_environment(
    staging: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An overridden ``APP_ENVIRONMENT`` cannot grant against the production RDS host."""
    monkeypatch.setattr(billing_service, "_STAGING_DB_HOST_LABEL", "skynet-staging")
    production = create_engine(PRODUCTION_RDS_URL)
    with pytest.raises(GrantRefusedError, match="not the staging database") as refused:
        _grant(production)
    assert "skynet-production" not in str(refused.value)
    production.dispose()


def test_a_billing_row_created_concurrently_is_reread_once(
    engine: Engine, staging: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another writer creates the billing row before the grant inserts it; the grant retries and adds to it."""
    email = check_account_email(STAGING_ID)
    _add_user(engine, email)
    original = billing_service.local_customer_id
    calls: list[str] = []

    def create_row_first(username: str) -> str:
        """Commit the billing row from another connection, as a racing writer would.

        Args:
            username: Account the grant is about to create a billing row for.

        Returns:
            The local customer id, as the real function returns it.
        """
        calls.append(username)
        if len(calls) == 1:
            now = datetime.now(UTC)
            with engine.begin() as connection:
                connection.execute(
                    BillingCustomerModel.__table__.insert().values(
                        username=username,
                        stripe_customer_id=original(username),
                        balance_cents=100,
                        created_at=now,
                        updated_at=now,
                    )
                )
        return original(username)

    monkeypatch.setattr(billing_service, "local_customer_id", create_row_first)
    result = _grant(engine, cents=500)
    assert (result.credited_cents, result.balance_cents, result.replayed) == (500, 600, False)
    assert _balance(engine, email) == 600
    assert len(_ledger(engine)) == 1
    assert len(calls) == 1


def test_refuses_after_a_second_conflict(engine: Engine, staging: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two conflicts in a row refuse the grant instead of retrying forever."""
    _add_user(engine, check_account_email(STAGING_ID))
    attempts: list[int] = []

    def always_conflict(*_args: object) -> None:
        """Fail as a concurrent insert would.

        Args:
            *_args: The grant arguments, ignored.

        Raises:
            IntegrityError: Always.
        """
        attempts.append(1)
        raise IntegrityError("INSERT", {}, Exception("duplicate key"))

    monkeypatch.setattr(StripeBillingService, "_apply_test_grant", always_conflict)
    with pytest.raises(GrantRefusedError, match="conflicted twice"):
        _grant(engine)
    assert len(attempts) == 2
    assert _ledger(engine) == []


def test_advisory_lock_blocks_a_grant_on_another_account_until_the_first_commits(
    pg_engine: Engine, staging: None
) -> None:
    """While one grant holds the lock, a grant on another account waits, then sees the total and hits the cap."""
    first, second = STAGING_ID, "staging-other-check-0f0f0f"
    _add_user(pg_engine, check_account_email(first))
    _add_user(pg_engine, check_account_email(second))
    with ThreadPoolExecutor(max_workers=1) as pool, Session(pg_engine) as holder:
        holder.execute(select(func.pg_advisory_xact_lock(billing_service._TEST_GRANT_LOCK_ID)))
        holder.add(
            WalletLedgerModel(
                username=check_account_email(first),
                delta_cents=3000,
                kind="grant",
                settlement_key="testgrant:held",
                created_at=datetime.now(UTC),
            )
        )
        holder.flush()
        waiting = pool.submit(_grant, pg_engine, second, 3000, "second")
        deadline = time.monotonic() + 10
        while not _advisory_lock_waiters(pg_engine):
            assert not waiting.done(), "the second grant finished without waiting for the lock"
            assert time.monotonic() < deadline, "the second grant never waited for the lock"
            time.sleep(0.05)
        assert not waiting.done()
        holder.commit()
        with pytest.raises(GrantRefusedError, match=r"\$30\.00 already granted"):
            waiting.result(timeout=10)
    assert _balance(pg_engine, check_account_email(second)) is None
    assert [row.settlement_key for row in _ledger(pg_engine)] == ["testgrant:held"]


def _advisory_lock_waiters(engine: Engine) -> int:
    """Count sessions in this database that wait for the grant's advisory lock.

    Args:
        engine: PostgreSQL engine on the test database.

    Returns:
        The number of ungranted advisory locks on the grant lock id.
    """
    # PostgreSQL splits a bigint advisory key into classid (high 32 bits) and
    # objid (low 32 bits), with objsubid 1; the key does not fit objid alone.
    with engine.connect() as connection:
        return int(
            connection.scalar(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
                    "AND ((classid::bigint << 32) | objid::bigint) = :lock_id AND objsubid = 1 "
                    "AND database = (SELECT oid FROM pg_database "
                    "WHERE datname = current_database())"
                ),
                {"lock_id": billing_service._TEST_GRANT_LOCK_ID},
            )
        )


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


def test_cli_hides_a_database_error_and_exits_1(
    engine: Engine, staging: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A database error prints only its class, never its text, which can hold the email."""
    email = check_account_email(STAGING_ID)

    def fail(*_args: object, **_kwargs: object) -> None:
        """Fail as a lost database connection would, with the email in the error text.

        Args:
            *_args: The grant arguments, ignored.
            **_kwargs: The grant keyword arguments, ignored.

        Raises:
            OperationalError: Always.
        """
        raise OperationalError(f"SELECT users WHERE email = '{email}'", {"email": email}, Exception(email))

    monkeypatch.setattr("core.billing.grant_test_credit._engine", lambda: engine)
    monkeypatch.setattr(StripeBillingService, "grant_test_credit", fail)
    argv = ["--account-id", STAGING_ID, "--dollars", "1", "--reason", "r", "--approval", "a", "--idempotency-key", "k"]
    assert main(argv) == 1
    captured = capsys.readouterr()
    assert captured.err.strip() == "Grant failed: OperationalError"
    assert "@" not in captured.err + captured.out
