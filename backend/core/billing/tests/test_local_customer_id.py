"""Verify placeholder billing customer ids fit the column for any username length.

``billing_customers.stripe_customer_id`` is ``String(64)``. SQLite does not
enforce that width, so each test asserts the stored length directly; with
``APP_BUDGET_TEST_DB_URL`` set, the same tests also run on PostgreSQL, which
rejects a longer value with ``StringDataRightTruncation``. The checkout
webhook tests check that a customerless session never stores an empty id.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from core.billing.budgets import BudgetService
from core.billing.pricing import ModelUsage
from core.billing.service import LOCAL_CUSTOMER_PREFIX, StripeBillingService, local_customer_id
from core.config import app_env, settings
from core.storage.models import (
    AgentConversationModel,
    Base,
    BillingCustomerModel,
    ExecutionBudgetModel,
    ExecutionOperationModel,
    ExecutionUsageEvidenceModel,
    JobModel,
    WalletLedgerModel,
)

COLUMN_WIDTH = 64
DOMAIN = "@eegeupelae.resend.app"
USERNAME_61 = "a" * (61 - len(DOMAIN)) + DOMAIN
USERNAME_255 = "b" * (255 - len(DOMAIN)) + DOMAIN
LONG_USERNAMES = [pytest.param(USERNAME_61, id="61-chars"), pytest.param(USERNAME_255, id="255-chars")]


PG_URL = app_env("APP_BUDGET_TEST_DB_URL")
BILLING_TABLES = [
    model.__table__
    for model in (
        BillingCustomerModel,
        WalletLedgerModel,
        ExecutionBudgetModel,
        ExecutionOperationModel,
        ExecutionUsageEvidenceModel,
        AgentConversationModel,
        JobModel,
    )
]


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
    """Yield SQLite, and PostgreSQL when supplied, since only PostgreSQL enforces the column width.

    Args:
        request: Selects the database dialect.

    Yields:
        Engine with the billing tables created in an isolated database or schema.
    """
    if request.param == "sqlite":
        database = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(database, tables=BILLING_TABLES)
        yield database
        database.dispose()
        return
    schema = f"local_customer_test_{uuid4().hex}"
    admin = create_engine(PG_URL)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    database = create_engine(PG_URL, connect_args={"options": f"-csearch_path={schema}"})
    try:
        Base.metadata.create_all(database, tables=BILLING_TABLES)
        yield database
    finally:
        database.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def preview_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give new accounts the PR-preview grant so a budget can be funded without a top-up."""
    monkeypatch.setattr(settings, "app_environment", "skynet-pr-107")
    monkeypatch.setattr(settings, "preview_grant_cents", 1000)


def _stored_id(engine: Engine, username: str) -> str:
    """Read the persisted ``stripe_customer_id`` of an account.

    Args:
        engine: Database holding the billing row.
        username: Account whose row to read.

    Returns:
        The stored customer id.
    """
    with Session(engine) as session:
        row = session.get(BillingCustomerModel, username)
        assert row is not None
        return row.stripe_customer_id


def _assert_fits(customer_id: str) -> None:
    """Check that a placeholder id keeps the local prefix and fits the column.

    Args:
        customer_id: Stored ``stripe_customer_id`` value.
    """
    assert customer_id.startswith(LOCAL_CUSTOMER_PREFIX)
    assert len(customer_id) <= COLUMN_WIDTH


def test_fixture_usernames_exceed_the_old_limit() -> None:
    """The fixtures are long enough that ``local:`` plus the raw username overflowed."""
    assert [len(USERNAME_61), len(USERNAME_255)] == [61, 255]
    assert len(f"{LOCAL_CUSTOMER_PREFIX}{USERNAME_61}") > COLUMN_WIDTH


@pytest.mark.parametrize("username", LONG_USERNAMES)
def test_budget_creation_seeds_a_wallet_for_a_long_username(engine: Engine, preview_grant: None, username: str) -> None:
    """``BudgetService._wallet`` creates a fitting row when a new account funds a budget."""
    budget = BudgetService(engine=engine).create(username, 100, idempotency_key="first")

    assert budget.total_cents == 100
    _assert_fits(_stored_id(engine, username))


@pytest.mark.parametrize("username", LONG_USERNAMES)
def test_run_debit_seeds_a_wallet_for_a_long_username(engine: Engine, username: str) -> None:
    """``debit_run`` creates a fitting row for an account that never reached Stripe."""
    StripeBillingService(engine=engine).debit_run(
        username,
        [ModelUsage(model="unpriced/model", input_tokens=1, output_tokens=1)],
        model=None,
        description="run",
    )

    _assert_fits(_stored_id(engine, username))


@pytest.mark.parametrize("username", LONG_USERNAMES)
def test_clawback_seeds_a_wallet_for_a_long_username(engine: Engine, username: str) -> None:
    """``_write_clawback`` creates a fitting row when a refund names an unknown account."""
    service = StripeBillingService(engine=engine)
    with Session(engine) as session:
        service._write_clawback(
            session,
            event_id="evt_test",
            payment_intent="pi_test",
            username=username,
            cents=50,
            kind="refund",
            description="Refund",
        )
        session.commit()

    _assert_fits(_stored_id(engine, username))


def test_long_usernames_sharing_a_prefix_get_different_ids() -> None:
    """Usernames equal in their first 58 characters still map to distinct ids."""
    stem = "c" * 58
    first, second = f"{stem}-one{DOMAIN}", f"{stem}-two{DOMAIN}"

    assert local_customer_id(first) != local_customer_id(second)
    assert local_customer_id(first) == local_customer_id(first)


def test_two_long_accounts_each_get_a_row(engine: Engine, preview_grant: None) -> None:
    """The unique index accepts two long accounts that share a 58-character prefix."""
    stem = "d" * 58
    names = [f"{stem}-one{DOMAIN}", f"{stem}-two{DOMAIN}"]
    service = BudgetService(engine=engine)
    for name in names:
        service.create(name, 100, idempotency_key="first")

    ids = [_stored_id(engine, name) for name in names]
    assert ids[0] != ids[1]


def test_existing_short_local_row_still_counts_as_customerless(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """A legacy ``local:<username>`` row passes the prefix checks and upgrades to a real id."""
    with Session(engine) as session:
        session.add(BillingCustomerModel(username="old@x.com", stripe_customer_id="local:old@x.com", balance_cents=7))
        session.commit()
    service = StripeBillingService(engine=engine)
    customer_api = SimpleNamespace(create=lambda **_: SimpleNamespace(id="cus_real"))
    monkeypatch.setattr(service, "_stripe", lambda: SimpleNamespace(Customer=customer_api))

    assert service._existing_customer_id("old@x.com") is None
    assert service.get_or_create_customer("old@x.com") == "cus_real"
    with Session(engine) as session:
        row = session.get(BillingCustomerModel, "old@x.com")
        assert row is not None
        assert (row.stripe_customer_id, row.balance_cents) == ("cus_real", 7)


def _complete_checkout(engine: Engine, username: str, customer: object) -> None:
    """Apply a paid one-time Checkout Session for ``username`` and commit it.

    Args:
        engine: Database holding the billing tables.
        username: Buyer identity stamped into the session metadata.
        customer: The session's ``customer`` field (id, expanded object, or ``None``).
    """
    obj = {
        "mode": "payment",
        "payment_status": "paid",
        "customer": customer,
        "payment_intent": f"pi_{uuid4().hex}",
        "metadata": {"username": username, "cents": "500", "pack_id": "starter"},
    }
    with Session(engine) as session:
        StripeBillingService(engine=engine)._on_checkout_completed(session, f"evt_{uuid4().hex}", obj)
        session.commit()


def test_customerless_checkouts_for_two_new_accounts_each_get_a_row(engine: Engine) -> None:
    """A Checkout Session without a customer must not store an empty, colliding id."""
    names = ["first@x.com", "second@x.com"]
    for name in names:
        _complete_checkout(engine, name, None)

    ids = [_stored_id(engine, name) for name in names]
    assert ids == [local_customer_id(name) for name in names]
    with Session(engine) as session:
        assert [session.get(BillingCustomerModel, name).balance_cents for name in names] == [500, 500]


def test_checkout_stores_the_session_customer_id(engine: Engine) -> None:
    """A new account keeps the real Stripe id, also when the field is expanded."""
    _complete_checkout(engine, "plain@x.com", "cus_plain")
    _complete_checkout(engine, "expanded@x.com", {"id": "cus_expanded", "object": "customer"})

    assert _stored_id(engine, "plain@x.com") == "cus_plain"
    assert _stored_id(engine, "expanded@x.com") == "cus_expanded"


def test_existing_empty_customer_id_counts_as_customerless(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """A row an older checkout stored with an empty id upgrades to a real id."""
    with Session(engine) as session:
        session.add(BillingCustomerModel(username="empty@x.com", stripe_customer_id="", balance_cents=500))
        session.commit()
    service = StripeBillingService(engine=engine)
    customer_api = SimpleNamespace(create=lambda **_: SimpleNamespace(id="cus_new"))
    monkeypatch.setattr(service, "_stripe", lambda: SimpleNamespace(Customer=customer_api))

    assert service._existing_customer_id("empty@x.com") is None
    assert service.get_or_create_customer("empty@x.com") == "cus_new"
    with Session(engine) as session:
        row = session.get(BillingCustomerModel, "empty@x.com")
        assert row is not None
        assert (row.stripe_customer_id, row.balance_cents) == ("cus_new", 500)
