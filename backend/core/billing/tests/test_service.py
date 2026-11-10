"""Tests for ``StripeBillingService``: the wallet ledger and the submit gate.

Covers the wallet-ledger backbone — the no-subsidy pricing policy (no free
allowance, packs at par), run debiting (legacy grant before paid balance), the
``spendable_cents`` figure the submit gate reads, the usage rollup, and
webhook idempotency. Each test stands up an
in-memory SQLite engine with the billing tables and patches the ``stripe``
module so no network call is made.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import litellm
import pytest
import stripe
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect
from sqlalchemy.orm import Session

from core.api.errors import DomainError
from core.api.routers.billing import create_billing_router
from core.billing.pricing import ModelUsage, cents_for_usage
from core.billing.service import (
    CUSTOM_CENTS_MAX,
    CUSTOM_CENTS_MIN,
    FREE_GRANT_CENTS,
    PACK_CENTS,
    StripeBillingService,
    committed_spend_cents,
    cost_ceiling_budget,
    platform_fee_cents_for_usage,
    purchase_fee_cents,
    run_cost_cents,
)
from core.config import settings
from core.constants import TOKEN_SOURCE_BYOK, TOKEN_SOURCE_MANAGED
from core.storage.models import (
    Base,
    BillingCustomerModel,
    BillingWebhookEventModel,
    JobModel,
    TelemetryEventModel,
    WalletLedgerModel,
)
from scripts import provision_stripe


@pytest.fixture
def engine() -> Iterator[object]:
    """Yield an in-memory SQLite engine with the billing tables created."""
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)


_TEST_MODEL = "test/priced"


@pytest.fixture(autouse=True)
def _stable_prices(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Price the test model at $1/$3 per M tokens with a 1.15 markup, whatever LiteLLM ships."""
    monkeypatch.setattr(settings, "usage_markup", 1.15)
    with patch.dict(
        litellm.model_cost, {_TEST_MODEL: {"input_cost_per_token": 1e-6, "output_cost_per_token": 3e-6}}, clear=False
    ):
        yield


def _usages(input_tokens: int, output_tokens: int = 0) -> list[ModelUsage]:
    """Single-model usage on a model with a fixed test price.

    ``_usages(100_000)`` costs $0.10, 12 cents with markup; ``_usages(200_000)`` 23.
    """
    return [ModelUsage(model=_TEST_MODEL, input_tokens=input_tokens, output_tokens=output_tokens)]


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set a fake Stripe secret key so mutations are not short-circuited."""
    monkeypatch.setattr(settings, "stripe_secret_key", SecretStr("sk_test_dummy"))


def _seed_customer(engine: object, username: str) -> None:
    """Insert a billing customer row with a real-looking Stripe link.

    Args:
        engine: The SQLite engine to write to.
        username: Account identity to create a Stripe-customer link for.
    """
    with Session(engine) as session:
        session.add(BillingCustomerModel(username=username, stripe_customer_id=f"cus_{username}", balance_cents=0))
        session.commit()


def _grant_remaining(engine: object, username: str) -> int | None:
    """Read the persisted grant_remaining for an account.

    Args:
        engine: The SQLite engine to read from.
        username: Account whose grant column to read.

    Returns:
        The stored ``grant_remaining`` (``None`` when the row or column is unseeded).
    """
    with Session(engine) as session:
        customer = session.get(BillingCustomerModel, username)
        return None if customer is None else customer.grant_remaining


def test_pricing_policy_no_subsidy_packs_at_par() -> None:
    """The no-subsidy policy: no free allowance, and packs grant exactly their price in cents."""
    assert FREE_GRANT_CENTS == 0
    # The balance is kept in cents, so at-par packs must grant exactly the Stripe
    # unit_amount provisioned in scripts/provision_stripe.py ($5 / $20 / $50).
    assert PACK_CENTS == {"starter": 500, "plus": 2000, "pro": 5000}


def test_purchase_fee_cents_covers_card_and_provider_costs() -> None:
    """The platform fee is 12.5% of the top-up amount, rounded up to the cent, plus 35 cents."""
    assert purchase_fee_cents(50) == 42
    assert purchase_fee_cents(500) == 98
    assert purchase_fee_cents(2000) == 285
    assert purchase_fee_cents(5000) == 660
    # 1637 * 12.5% = 204.625c, so the percentage rounds up to 205c.
    assert purchase_fee_cents(1637) == 240


def test_purchase_fee_cents_keeps_a_margin_on_non_us_cards() -> None:
    """After Stripe's non-US card cut and OpenRouter's top-up fee, every purchase nets a gain."""
    for cents in (CUSTOM_CENTS_MIN, 500, 1000, 1637, 2000, 5000, CUSTOM_CENTS_MAX):
        fee = purchase_fee_cents(cents)
        stripe_cut = (cents + fee) * 0.044 + 30
        openrouter_cut = cents * 0.055
        assert fee - stripe_cut - openrouter_cut > 0, cents


def test_wallet_reports_empty_grant_for_new_account(engine: object) -> None:
    """A brand-new account reads a zero grant without a row being created."""
    snapshot = StripeBillingService(engine=engine).get_wallet("new@x.com")
    assert snapshot.free_grant_remaining == 0
    assert snapshot.paid_balance_cents == 0
    with Session(engine) as session:
        assert session.get(BillingCustomerModel, "new@x.com") is None


def test_wallet_seeds_grant_column_on_first_read(engine: object) -> None:
    """Reading a row with a NULL grant seeds it to the (zero) allowance and persists it."""
    _seed_customer(engine, "u@x.com")
    snapshot = StripeBillingService(engine=engine).get_wallet("u@x.com")
    assert snapshot.free_grant_remaining == FREE_GRANT_CENTS
    with Session(engine) as session:
        customer = session.get(BillingCustomerModel, "u@x.com")
    assert customer.grant_remaining == FREE_GRANT_CENTS


@pytest.mark.parametrize(("environment_name", "expected"), [("staging", 0), ("skynet-pr-662", 1000)])
def test_new_account_grant_only_in_preview_environments(
    engine: object, monkeypatch: pytest.MonkeyPatch, environment_name: str, expected: int
) -> None:
    """A new account is seeded with ``PREVIEW_GRANT_CENTS`` only in a PR preview environment."""
    monkeypatch.setattr(settings, "preview_grant_cents", 1000)
    monkeypatch.setattr(settings, "app_environment", environment_name)
    _seed_customer(engine, "u@x.com")
    service = StripeBillingService(engine=engine)
    assert service.spendable_cents("u@x.com") == expected
    assert service.get_wallet("u@x.com").free_grant_total == expected


def test_debit_run_draws_from_legacy_grant_first(engine: object) -> None:
    """A run debit decrements a remaining legacy grant before touching the paid balance."""
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(
                username="u@x.com",
                stripe_customer_id="cus_u",
                balance_cents=0,
                grant_remaining=50,
            )
        )
        session.commit()
    service = StripeBillingService(engine=engine)
    usages = _usages(100_000)
    expected = cents_for_usage(usages)
    cost = service.debit_run("u@x.com", usages, model="openai/gpt-5.5-mini", description="run-a")
    assert cost == expected
    assert 0 < expected < 50
    snapshot = service.get_wallet("u@x.com")
    assert snapshot.free_grant_remaining == 50 - expected
    assert snapshot.paid_balance_cents == 0
    with Session(engine) as session:
        rows = session.query(WalletLedgerModel).filter_by(username="u@x.com").all()
    assert len(rows) == 1
    assert rows[0].delta_cents == -expected
    assert rows[0].kind == "run"
    assert rows[0].model == "openai/gpt-5.5-mini"


def test_debit_run_overflows_grant_into_paid_balance(engine: object) -> None:
    """A debit larger than the remaining grant drains it, then the paid balance."""
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(
                username="u@x.com",
                stripe_customer_id="cus_u",
                balance_cents=100,
                grant_remaining=10,
            )
        )
        session.commit()
    service = StripeBillingService(engine=engine)
    usages = _usages(200_000)
    expected = cents_for_usage(usages)
    cost = service.debit_run("u@x.com", usages, model=None, description="big")
    assert cost == expected
    assert expected > 10  # must exceed the remaining grant to overflow into paid
    snapshot = service.get_wallet("u@x.com")
    assert snapshot.free_grant_remaining == 0
    assert snapshot.paid_balance_cents == 100 - (expected - 10)


def test_debit_run_creates_local_row_for_customerless_account(engine: object) -> None:
    """A run for an account that never touched Stripe seeds a local billing row.

    The balance is prepaid, so with no free allowance and no purchased balance
    there is nothing to draw from: the charge clamps to zero (the shortfall is
    absorbed, never lent) and the balance stays at exactly zero. The absorbed
    cost is still recorded on a zero-delta ledger row.
    """
    service = StripeBillingService(engine=engine)
    usages = _usages(20_000)
    charged = service.debit_run("free@x.com", usages, model=None, description="r")
    assert charged == 0
    with Session(engine) as session:
        customer = session.get(BillingCustomerModel, "free@x.com")
        ledger_rows = session.query(WalletLedgerModel).filter_by(username="free@x.com").all()
    assert customer is not None
    assert customer.stripe_customer_id.startswith("local:")
    assert customer.grant_remaining == 0
    assert customer.balance_cents == 0
    assert [(row.delta_cents, row.uncollected_cents) for row in ledger_rows] == [(0, cents_for_usage(usages))]


def test_debit_run_clamps_charge_to_available_balance(engine: object) -> None:
    """A run costing more than grant + paid drains both to zero, never below.

    The ledger row records the clamped amount actually charged, so the audit
    trail still sums to the stored balance.
    """
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(
                username="u@x.com",
                stripe_customer_id="cus_u",
                balance_cents=3,
                grant_remaining=5,
            )
        )
        session.commit()
    service = StripeBillingService(engine=engine)
    usages = _usages(200_000)
    assert cents_for_usage(usages) > 8
    charged = service.debit_run("u@x.com", usages, model=None, description="big")
    assert charged == 8
    snapshot = service.get_wallet("u@x.com")
    assert snapshot.free_grant_remaining == 0
    assert snapshot.paid_balance_cents == 0
    with Session(engine) as session:
        (row,) = session.query(WalletLedgerModel).filter_by(username="u@x.com").all()
    assert row.delta_cents == -8


def test_debit_run_repeated_overdraw_floors_at_zero(engine: object) -> None:
    """A second overdrawing run charges nothing — the balance can never go negative."""
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(
                username="u@x.com",
                stripe_customer_id="cus_u",
                balance_cents=2,
                grant_remaining=0,
            )
        )
        session.commit()
    service = StripeBillingService(engine=engine)
    usages = _usages(200_000)
    assert service.debit_run("u@x.com", usages, model=None, description="first") == 2
    assert service.debit_run("u@x.com", usages, model=None, description="second") == 0
    assert service.spendable_cents("u@x.com") == 0
    with Session(engine) as session:
        customer = session.get(BillingCustomerModel, "u@x.com")
        ledger_rows = session.query(WalletLedgerModel).filter_by(username="u@x.com").all()
    cost = cents_for_usage(usages)
    assert customer.balance_cents == 0
    assert [(row.delta_cents, row.uncollected_cents) for row in ledger_rows] == [(-2, cost - 2), (0, cost)]


def test_debit_run_settlement_key_charges_once(engine: object) -> None:
    """A retried debit with the same settlement key returns the first charge and debits nothing more."""
    with Session(engine) as session:
        session.add(BillingCustomerModel(username="u@x.com", stripe_customer_id="cus_u", balance_cents=100))
        session.commit()
    service = StripeBillingService(engine=engine)
    usages = _usages(100_000)
    first = service.debit_run("u@x.com", usages, model=None, description="r", settlement_key="legacy:j:g1")
    again = service.debit_run("u@x.com", usages, model=None, description="r", settlement_key="legacy:j:g1")
    other = service.debit_run("u@x.com", usages, model=None, description="r", settlement_key="legacy:j:g2")
    assert first == again == other == cents_for_usage(usages)
    assert _balance(engine, "u@x.com") == 100 - 2 * first
    assert len(_ledger_rows(engine, "u@x.com", "run")) == 2


def test_debit_run_records_job_and_grant_split(engine: object) -> None:
    """A run drawing on grant and paid balance stores the job id and the grant share."""
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username="u@x.com", stripe_customer_id="cus_u", balance_cents=500, grant_remaining=100)
        )
        session.commit()
    service = StripeBillingService(engine=engine)
    with patch("core.billing.service.run_cost_cents", return_value=150):
        charged = service.debit_run("u@x.com", _usages(100_000), model=None, description="r", optimization_id="job-1")
    assert charged == 150
    (row,) = _ledger_rows(engine, "u@x.com", "run")
    assert (row.delta_cents, row.from_grant_cents, row.optimization_id) == (-150, 100, "job-1")
    assert -row.delta_cents - row.from_grant_cents == 50
    snapshot = service.get_wallet("u@x.com")
    assert (snapshot.free_grant_remaining, snapshot.paid_balance_cents) == (0, 450)


def test_debit_run_paid_only_records_zero_from_grant(engine: object) -> None:
    """A run fully covered by the paid balance records ``from_grant_cents = 0``."""
    with Session(engine) as session:
        session.add(BillingCustomerModel(username="u@x.com", stripe_customer_id="cus_u", balance_cents=500))
        session.commit()
    service = StripeBillingService(engine=engine)
    charged = service.debit_run("u@x.com", _usages(100_000), model=None, description="r", optimization_id="job-2")
    (row,) = _ledger_rows(engine, "u@x.com", "run")
    assert row.delta_cents == -charged
    assert row.from_grant_cents == 0
    assert row.optimization_id == "job-2"


def test_debit_run_without_job_leaves_optimization_id_null(engine: object) -> None:
    """An interactive debit (no job) writes a null ``optimization_id``."""
    with Session(engine) as session:
        session.add(BillingCustomerModel(username="u@x.com", stripe_customer_id="cus_u", balance_cents=500))
        session.commit()
    StripeBillingService(engine=engine).debit_run("u@x.com", _usages(100_000), model=None, description="chat")
    (row,) = _ledger_rows(engine, "u@x.com", "run")
    assert row.optimization_id is None


def _load_run_attribution_migration() -> object:
    """Load the wallet-ledger run-attribution migration module from its file.

    Returns:
        The imported migration module.
    """
    path = Path(__file__).resolve().parents[3] / "alembic/versions/c8d9e0f1a2b3_add_wallet_ledger_run_attribution.py"
    spec = importlib.util.spec_from_file_location("run_attribution_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_run_attribution_migration_adds_and_drops_columns() -> None:
    """The migration adds both columns and the index; its downgrade removes them."""
    migration = _load_run_attribution_migration()
    eng = create_engine("sqlite:///:memory:")
    legacy = MetaData()
    Table("wallet_ledger", legacy, Column("id", Integer, primary_key=True), Column("username", String(255)))
    legacy.create_all(eng)
    with eng.begin() as conn:
        conn.exec_driver_sql("INSERT INTO wallet_ledger (id, username) VALUES (1, 'u@x.com')")
        with patch.object(migration, "op", Operations(MigrationContext.configure(conn))):
            migration.upgrade()
        columns = {column["name"] for column in inspect(conn).get_columns("wallet_ledger")}
        indexes = {index["name"] for index in inspect(conn).get_indexes("wallet_ledger")}
        old_row = conn.exec_driver_sql("SELECT optimization_id, from_grant_cents FROM wallet_ledger").one()
        assert {"optimization_id", "from_grant_cents"} <= columns
        assert "ix_wallet_ledger_optimization_id" in indexes
        assert tuple(old_row) == (None, None)
        with patch.object(migration, "op", Operations(MigrationContext.configure(conn))):
            migration.downgrade()
        columns = {column["name"] for column in inspect(conn).get_columns("wallet_ledger")}
        indexes = {index["name"] for index in inspect(conn).get_indexes("wallet_ledger")}
    assert columns == {"id", "username"}
    assert "ix_wallet_ledger_optimization_id" not in indexes


def test_debit_run_records_and_alerts_uncollected_overrun(engine: object) -> None:
    """A clamped charge stores the shortfall on the ledger row and alerts operators."""
    with Session(engine) as session:
        session.add(BillingCustomerModel(username="u@x.com", stripe_customer_id="cus_u", balance_cents=3))
        session.commit()
    service = StripeBillingService(engine=engine)
    usages = _usages(200_000)
    cost = cents_for_usage(usages)
    with patch("core.billing.service.send_alert") as alert:
        assert service.debit_run("u@x.com", usages, model=None, description="big") == 3
    (row,) = _ledger_rows(engine, "u@x.com", "run")
    assert row.uncollected_cents == cost - 3
    alert.assert_called_once()
    assert f"uncollected={cost - 3}" in alert.call_args.kwargs["body"]


def test_debit_run_fully_collected_sends_no_alert(engine: object) -> None:
    """A run the balance covers records no shortfall and sends no alert."""
    with Session(engine) as session:
        session.add(BillingCustomerModel(username="u@x.com", stripe_customer_id="cus_u", balance_cents=100))
        session.commit()
    service = StripeBillingService(engine=engine)
    with patch("core.billing.service.send_alert") as alert:
        service.debit_run("u@x.com", _usages(100_000), model=None, description="r")
    (row,) = _ledger_rows(engine, "u@x.com", "run")
    assert row.uncollected_cents is None
    alert.assert_not_called()


def test_cents_spent_since_counts_only_run_charges(engine: object) -> None:
    """The spend ceiling sums run debits; refunds, disputes and debt repayments are money movements."""
    now = datetime.now(UTC)
    _add_ledger(engine, "u@x.com", delta=-40, kind="run", model="m", when=now)
    _add_ledger(engine, "u@x.com", delta=-500, kind="refund", model=None, when=now)
    _add_ledger(engine, "u@x.com", delta=-300, kind="dispute", model=None, when=now)
    _add_ledger(engine, "u@x.com", delta=-100, kind="debt_repayment", model=None, when=now)
    _add_ledger(engine, "u@x.com", delta=-7, kind="run", model="m", when=now - timedelta(days=2))
    service = StripeBillingService(engine=engine)
    assert service.cents_spent_since(now - timedelta(hours=24)) == 40


def test_debit_run_zero_cost_writes_nothing(engine: object) -> None:
    """A run under one cent's worth of tokens writes no ledger row or debit."""
    _seed_customer(engine, "u@x.com")
    service = StripeBillingService(engine=engine)
    assert service.debit_run("u@x.com", [], model=None, description="r") == 0
    with Session(engine) as session:
        assert session.query(WalletLedgerModel).filter_by(username="u@x.com").count() == 0
        assert session.get(BillingCustomerModel, "u@x.com").grant_remaining in (None, FREE_GRANT_CENTS)


def test_free_grant_is_one_time_and_never_resets(engine: object) -> None:
    """A legacy account's partially-spent grant is honored as-is — never topped up."""
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(
                username="u@x.com",
                stripe_customer_id="cus_u",
                balance_cents=0,
                grant_remaining=40,
            )
        )
        session.commit()
    snapshot = StripeBillingService(engine=engine).get_wallet("u@x.com")
    assert snapshot.free_grant_remaining == 40
    assert _grant_remaining(engine, "u@x.com") == 40


def test_spendable_cents_sums_grant_and_paid_balance(engine: object) -> None:
    """The submit-gate figure is grant remaining plus purchased balance."""
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(
                username="u@x.com",
                stripe_customer_id="cus_u",
                balance_cents=70,
                grant_remaining=30,
            )
        )
        session.commit()
    assert StripeBillingService(engine=engine).spendable_cents("u@x.com") == 100


def test_spendable_cents_zero_when_grant_and_balance_exhausted(engine: object) -> None:
    """A drained grant and zero paid balance reads as no spendable balance (gate trips)."""
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(
                username="u@x.com",
                stripe_customer_id="cus_u",
                balance_cents=0,
                grant_remaining=0,
            )
        )
        session.commit()
    assert StripeBillingService(engine=engine).spendable_cents("u@x.com") == 0


def test_spendable_cents_zero_for_new_account(engine: object) -> None:
    """A brand-new account has no spendable balance — the submit gate trips until it tops up."""
    assert StripeBillingService(engine=engine).spendable_cents("new@x.com") == 0


def test_debit_run_byok_has_no_model_charge(engine: object) -> None:
    """A BYOK model call does not debit the wallet."""
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(
                username="u@x.com",
                stripe_customer_id="cus_u",
                balance_cents=100,
                grant_remaining=0,
            )
        )
        session.commit()
    service = StripeBillingService(engine=engine)
    usages = _usages(200_000)
    cost = service.debit_run(
        "u@x.com",
        usages,
        model="m1",
        description="byok-run",
        token_source=TOKEN_SOURCE_BYOK,
    )
    fee = platform_fee_cents_for_usage(usages)
    assert cost == fee
    assert fee == 0
    snapshot = service.get_wallet("u@x.com")
    assert snapshot.paid_balance_cents == 100 - fee


def test_debit_run_managed_still_charges_full_cost(engine: object) -> None:
    """A managed run is unaffected — it still pays the full per-token cost."""
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(
                username="u@x.com",
                stripe_customer_id="cus_u",
                balance_cents=1000,
                grant_remaining=0,
            )
        )
        session.commit()
    service = StripeBillingService(engine=engine)
    usages = _usages(200_000)
    cost = service.debit_run("u@x.com", usages, model="m1", description="managed-run")
    assert cost == cents_for_usage(usages)


def test_mixed_run_cost_prices_each_model_by_its_source() -> None:
    """Mixed jobs charge managed tokens and not BYOK tokens."""
    managed = ModelUsage(model="managed/model", input_tokens=100_000, output_tokens=0)
    byok = ModelUsage(model="byok/model", input_tokens=200_000, output_tokens=0)

    cost = run_cost_cents(
        [managed, byok],
        TOKEN_SOURCE_MANAGED,
        {
            managed.model: TOKEN_SOURCE_MANAGED,
            byok.model: TOKEN_SOURCE_BYOK,
        },
    )

    assert cost == cents_for_usage([managed])


def test_cost_ceiling_budget_managed_is_the_balance(engine: object) -> None:
    """A managed run's ceiling budget is exactly the spendable balance."""
    assert cost_ceiling_budget(0, TOKEN_SOURCE_MANAGED) == 0
    assert cost_ceiling_budget(100, TOKEN_SOURCE_MANAGED) == 100


def test_cost_ceiling_budget_byok_has_no_model_cap(engine: object) -> None:
    """A BYOK model ceiling does not depend on a fee."""
    assert cost_ceiling_budget(0, TOKEN_SOURCE_BYOK) == 0
    budget = cost_ceiling_budget(100, TOKEN_SOURCE_BYOK)
    assert budget > 100
    assert budget == cost_ceiling_budget(1, TOKEN_SOURCE_BYOK)


def test_committed_spend_cents_managed_is_the_full_budget() -> None:
    """A managed run's committed spend equals its full cost ceiling."""
    assert committed_spend_cents(200, TOKEN_SOURCE_MANAGED) == 200
    assert committed_spend_cents(0, TOKEN_SOURCE_MANAGED) == 0
    assert committed_spend_cents(-5, TOKEN_SOURCE_MANAGED) == 0


def test_committed_spend_cents_byok_is_zero() -> None:
    """A BYOK model commits no wallet balance."""
    assert committed_spend_cents(1000, TOKEN_SOURCE_BYOK) == 0
    assert committed_spend_cents(1, TOKEN_SOURCE_BYOK) == 0
    assert committed_spend_cents(0, TOKEN_SOURCE_BYOK) == 0


def test_committed_spend_cents_inverts_cost_ceiling_budget() -> None:
    """The ceiling granted for a balance never commits more than that balance."""
    for balance in (1, 12, 100, 500):
        for source in (TOKEN_SOURCE_MANAGED, TOKEN_SOURCE_BYOK):
            budget = cost_ceiling_budget(balance, source)
            assert committed_spend_cents(budget, source) <= balance


def _add_ledger(
    engine: object,
    username: str,
    *,
    delta: int,
    kind: str,
    model: str | None,
    when: datetime,
    description: str = "",
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    """Insert one wallet-ledger row at an explicit instant.

    Args:
        engine: SQLite engine to write to.
        username: Account the row belongs to.
        delta: Signed delta in cents (negative for a spend).
        kind: Ledger kind ('run', 'topup', 'grant').
        model: Model id, or None for non-run rows.
        when: ``created_at`` instant the row is stamped with.
        description: Row label; defaults to the kind.
        input_tokens: Measured input tokens, or None for legacy/non-run rows.
        output_tokens: Measured output tokens, or None for legacy/non-run rows.
    """
    with Session(engine) as session:
        session.add(
            WalletLedgerModel(
                username=username,
                delta_cents=delta,
                kind=kind,
                description=description or kind,
                model=model,
                created_at=when,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        )
        session.commit()


def _add_optimization(engine: object, username: str, when: datetime) -> None:
    """Save a synthetic optimization without executing work or charging its owner.

    Args:
        engine: Test database engine.
        username: Optimization owner.
        when: Optimization creation time.
    """
    with Session(engine) as session:
        session.add(JobModel(optimization_id=str(uuid4()), username=username, created_at=when, status="completed"))
        session.commit()


def test_get_usage_aggregates_runs_by_day_and_model(engine: object) -> None:
    """Count optimizations separately from billed spend grouped by day and model.

    Args:
        engine: Test database engine.
    """
    user = "u@x.com"
    now = datetime.now(UTC)
    day1 = now - timedelta(days=1)
    day2 = now - timedelta(days=2)
    for when in (day1, day1, day2):
        _add_optimization(engine, user, when)
    _add_ledger(engine, user, delta=-300, kind="run", model="openai/gpt-5.5", when=day1)
    _add_ledger(engine, user, delta=-50, kind="run", model="anthropic/claude", when=day1)
    # A positive run row (legacy correction) is ignored by the rollups.
    _add_ledger(engine, user, delta=120, kind="run", model="openai/gpt-5.5", when=day1)
    _add_ledger(engine, user, delta=-100, kind="run", model="openai/gpt-5.5", when=day2)
    _add_ledger(engine, user, delta=2200, kind="topup", model=None, when=day1)

    service = StripeBillingService(engine=engine)
    snapshot = service.get_usage(user, now - timedelta(days=3), now)

    assert snapshot.billed_cents == 450
    assert snapshot.runs == 3
    # Top-ups and positive run rows are excluded from the spend rollups but
    # still ride along in entries.
    assert len(snapshot.entries) == 5
    assert [m.model for m in snapshot.by_model] == ["openai/gpt-5.5", "anthropic/claude"]
    assert snapshot.by_model[0].cents == 400
    assert snapshot.by_model[0].runs == 2
    assert [d.date for d in snapshot.by_day] == [day2.date().isoformat(), day1.date().isoformat()]
    day1_row = next(d for d in snapshot.by_day if d.date == day1.date().isoformat())
    assert day1_row.billed_cents == 350
    assert day1_row.runs == 2
    day2_row = next(d for d in snapshot.by_day if d.date == day2.date().isoformat())
    assert (day2_row.billed_cents, day2_row.runs) == (100, 1)


def test_get_usage_rolls_up_token_counts_per_day(engine: object) -> None:
    """Per-day token sums cover only billed runs; legacy rows add zero tokens."""
    user = "u@x.com"
    now = datetime.now(UTC)
    day1 = now - timedelta(days=1)
    day2 = now - timedelta(days=2)
    _add_ledger(engine, user, delta=-30, kind="run", model="m", when=day1, input_tokens=1000, output_tokens=200)
    _add_ledger(engine, user, delta=-10, kind="run", model="m", when=day1)
    _add_ledger(engine, user, delta=-20, kind="run", model="m", when=day2, input_tokens=500, output_tokens=100)
    # A top-up on a billed day adds neither a run nor tokens to that day.
    _add_ledger(engine, user, delta=500, kind="topup", model=None, when=day2, input_tokens=9, output_tokens=9)

    service = StripeBillingService(engine=engine)
    snapshot = service.get_usage(user, now - timedelta(days=3), now)

    rows = {d.date: d for d in snapshot.by_day}
    assert (rows[day1.date().isoformat()].runs, rows[day1.date().isoformat()].input_tokens) == (2, 1000)
    assert rows[day1.date().isoformat()].output_tokens == 200
    assert (rows[day2.date().isoformat()].runs, rows[day2.date().isoformat()].input_tokens) == (1, 500)
    assert rows[day2.date().isoformat()].output_tokens == 100


def test_debit_run_stamps_token_counts(engine: object) -> None:
    """The ledger row records the measured input/output tokens behind the charge."""
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(
                username="u@x.com",
                stripe_customer_id="cus_u",
                balance_cents=1000,
                grant_remaining=0,
            )
        )
        session.commit()
    service = StripeBillingService(engine=engine)
    service.debit_run("u@x.com", _usages(100_000, 40_000), model="m", description="Run")
    with Session(engine) as session:
        row = session.query(WalletLedgerModel).one()
    assert row.input_tokens == 100_000
    assert row.output_tokens == 40_000


def test_get_usage_rolls_up_token_counts_per_model(engine: object) -> None:
    """Per-model token sums cover stamped rows; legacy rows contribute zero."""
    user = "u@x.com"
    now = datetime.now(UTC)
    when = now - timedelta(days=1)
    _add_ledger(engine, user, delta=-30, kind="run", model="m", when=when, input_tokens=1000, output_tokens=200)
    _add_ledger(engine, user, delta=-20, kind="run", model="m", when=when, input_tokens=500, output_tokens=100)
    _add_ledger(engine, user, delta=-10, kind="run", model="m", when=when)

    service = StripeBillingService(engine=engine)
    snapshot = service.get_usage(user, now - timedelta(days=3), now)

    (model_row,) = snapshot.by_model
    assert model_row.input_tokens == 1500
    assert model_row.output_tokens == 300
    assert model_row.runs == 3


def test_get_usage_excludes_rows_outside_window(engine: object) -> None:
    """Exclude another account and dates outside the usage window.

    Args:
        engine: Test database engine.
    """
    user = "u@x.com"
    now = datetime.now(UTC)
    for when in (now - timedelta(days=1), now - timedelta(days=40), now + timedelta(days=1)):
        _add_optimization(engine, user, when)
    _add_optimization(engine, "other@example.test", now - timedelta(days=1))
    _add_ledger(engine, "other@example.test", delta=-999, kind="run", model="m", when=now - timedelta(days=1))
    _add_ledger(engine, user, delta=-999, kind="run", model="m", when=now + timedelta(days=1))
    _add_ledger(engine, user, delta=-40, kind="run", model="m", when=now - timedelta(days=1))
    _add_ledger(engine, user, delta=-999, kind="run", model="m", when=now - timedelta(days=40))

    service = StripeBillingService(engine=engine)
    snapshot = service.get_usage(user, now - timedelta(days=7), now)

    assert snapshot.billed_cents == 40
    assert snapshot.runs == 1
    assert len(snapshot.entries) == 1


@pytest.mark.parametrize("charges", [0, 1, 3])
def test_get_usage_counts_optimizations_independently_of_charges(engine: object, charges: int) -> None:
    """Count every owned optimization even when wallet charge counts differ.

    Args:
        engine: Test database engine.
        charges: Number of billed calls, independent of optimization count.
    """
    user = "owner@example.test"
    now = datetime.now(UTC)
    for _ in range(2):
        _add_optimization(engine, user, now - timedelta(days=1))
    for _ in range(charges):
        _add_ledger(engine, user, delta=-40, kind="run", model="m", when=now - timedelta(days=1))

    snapshot = StripeBillingService(engine=engine).get_usage(user, now - timedelta(days=7), now)

    assert snapshot.runs == 2
    assert snapshot.billed_cents == charges * 40
    assert sum(row.runs for row in snapshot.by_day) == charges
    assert sum(row.runs for row in snapshot.by_model) == charges


def test_get_usage_includes_optimizations_at_both_window_boundaries(engine: object) -> None:
    """Include both boundary instants without including adjacent optimizations.

    Args:
        engine: Test database engine.
    """
    user = "owner@example.test"
    end = datetime.now(UTC)
    start = end - timedelta(days=7)
    for when in (start - timedelta(microseconds=1), start, end, end + timedelta(microseconds=1)):
        _add_optimization(engine, user, when)

    snapshot = StripeBillingService(engine=engine).get_usage(user, start, end)

    assert snapshot.runs == 2
    assert snapshot.billed_cents == 0
    assert snapshot.entries == []


def _checkout_event(
    event_id: str,
    username: str,
    cents: int,
    pack_id: str = "pack_small",
    payment_intent: str | None = None,
) -> dict:
    """Build a minimal ``checkout.session.completed`` event for a paid pack purchase.

    Args:
        event_id: Stripe event id (the idempotency key the handler records).
        username: Buyer the top-up lands on.
        cents: Cents the pack grants.
        pack_id: Pack identifier carried in the session metadata.
        payment_intent: PaymentIntent id stamped on the top-up row, so a later
            refund/dispute can resolve back to it.

    Returns:
        An event dict shaped like the fields ``handle_webhook`` reads.
    """
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "mode": "payment",
                "payment_status": "paid",
                "customer": f"cus_{username}",
                "payment_intent": payment_intent,
                "metadata": {"username": username, "cents": str(cents), "pack_id": pack_id},
            }
        },
    }


def _refund_event(event_id: str, payment_intent: str, amount_refunded: int) -> dict:
    """Build a ``charge.refunded`` event whose charge cumulatively refunded ``amount_refunded``.

    Args:
        event_id: Stripe event id (the idempotency key the handler records).
        payment_intent: The PaymentIntent behind the refunded charge.
        amount_refunded: Cumulative refunded cents on the charge.

    Returns:
        An event dict shaped like the fields the refund handler reads.
    """
    return {
        "id": event_id,
        "type": "charge.refunded",
        "data": {"object": {"payment_intent": payment_intent, "amount_refunded": amount_refunded}},
    }


def _dispute_event(event_id: str, payment_intent: str, amount: int) -> dict:
    """Build a ``charge.dispute.created`` event disputing ``amount`` cents of a charge.

    Args:
        event_id: Stripe event id (the idempotency key the handler records).
        payment_intent: The PaymentIntent behind the disputed charge.
        amount: Disputed cents.

    Returns:
        An event dict shaped like the fields the dispute handler reads.
    """
    return {
        "id": event_id,
        "type": "charge.dispute.created",
        "data": {"object": {"payment_intent": payment_intent, "amount": amount}},
    }


@pytest.fixture
def webhook_ready(configured: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure both the Stripe secret and the webhook signing secret."""
    monkeypatch.setattr(settings, "stripe_webhook_secret", SecretStr("whsec_test"))


def test_webhook_raises_503_when_signing_secret_unconfigured(
    engine: object, configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unconfigured webhook secret is a 503, never a silent accept."""
    monkeypatch.setattr(settings, "stripe_webhook_secret", None)
    service = StripeBillingService(engine=engine)
    with pytest.raises(DomainError) as exc:
        service.handle_webhook(b"{}", "sig")
    assert exc.value.status_code == 503


def test_webhook_rejects_bad_signature_with_400(engine: object, webhook_ready: None) -> None:
    """A signature that fails verification is rejected as a 400, nothing applied."""
    service = StripeBillingService(engine=engine)
    err = stripe.SignatureVerificationError("invalid signature", "t=1,v1=bad")
    with (
        patch("stripe.Webhook.construct_event", side_effect=err),
        pytest.raises(DomainError) as exc,
    ):
        service.handle_webhook(b"{}", "t=1,v1=bad")
    assert exc.value.status_code == 400
    assert exc.value.code == "billing.webhook_invalid"


def test_webhook_tops_up_pack(engine: object, webhook_ready: None) -> None:
    """A verified paid checkout credits the buyer and writes one topup ledger row."""
    _seed_customer(engine, "u@x.com")
    service = StripeBillingService(engine=engine)
    event = _checkout_event("evt_1", "u@x.com", 500)
    with patch("stripe.Webhook.construct_event", return_value=event):
        service.handle_webhook(b"{}", "sig")
    with Session(engine) as session:
        assert session.get(BillingCustomerModel, "u@x.com").balance_cents == 500
        rows = session.query(WalletLedgerModel).filter_by(username="u@x.com", kind="topup").all()
        assert len(rows) == 1
        assert rows[0].delta_cents == 500
        assert rows[0].stripe_event_id == "evt_1"
        assert session.get(BillingWebhookEventModel, "evt_1") is not None


def test_webhook_records_purchase_milestone(
    engine: object, webhook_ready: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A credited top-up leaves one ``purchase_completed`` telemetry row, and only one."""
    monkeypatch.setattr(settings, "telemetry_enabled", True)
    _seed_customer(engine, "u@x.com")
    service = StripeBillingService(engine=engine)
    event = _checkout_event("evt_tel", "u@x.com", 500, pack_id="pack_small")
    with patch("stripe.Webhook.construct_event", return_value=event):
        service.handle_webhook(b"{}", "sig")
        service.handle_webhook(b"{}", "sig")
    with Session(engine) as session:
        rows = session.query(TelemetryEventModel).filter_by(event_name="purchase_completed").all()
    assert len(rows) == 1
    assert rows[0].username == "u@x.com"
    assert rows[0].properties == {"pack_id": "pack_small", "cents": 500}
    assert rows[0].context == {"source": "server"}


def test_webhook_is_idempotent_on_redelivery(engine: object, webhook_ready: None) -> None:
    """A redelivered event (same id) credits exactly once — no double top-up."""
    _seed_customer(engine, "u@x.com")
    service = StripeBillingService(engine=engine)
    event = _checkout_event("evt_dup", "u@x.com", 500)
    with patch("stripe.Webhook.construct_event", return_value=event):
        service.handle_webhook(b"{}", "sig")
        service.handle_webhook(b"{}", "sig")
    with Session(engine) as session:
        assert session.get(BillingCustomerModel, "u@x.com").balance_cents == 500
        rows = session.query(WalletLedgerModel).filter_by(username="u@x.com", kind="topup").all()
        assert len(rows) == 1


def test_custom_checkout_rejects_out_of_bounds_amount(engine: object) -> None:
    """Amounts outside the custom bounds are a 400 before any Stripe call."""
    service = StripeBillingService(engine=engine)
    for cents in (CUSTOM_CENTS_MIN - 1, 0, -5, CUSTOM_CENTS_MAX + 1):
        with pytest.raises(DomainError) as exc:
            service.create_custom_checkout("u@x.com", cents)
        assert exc.value.status_code == 400
        assert exc.value.code == "billing.invalid_amount"


def test_custom_checkout_builds_ad_hoc_price_and_metadata(engine: object, configured: None) -> None:
    """A custom top-up charges the amount in cents and stamps webhook metadata."""
    _seed_customer(engine, "u@x.com")
    service = StripeBillingService(engine=engine)
    captured: dict[str, object] = {}

    def _create(**kwargs: object) -> object:
        captured.update(kwargs)
        return type("Obj", (), {"url": "https://stripe.test/c/cs_123"})()

    with patch("stripe.checkout.Session.create", side_effect=_create):
        url = service.create_custom_checkout("u@x.com", 1234)
    assert url == "https://stripe.test/c/cs_123"
    line_items = captured["line_items"]
    assert isinstance(line_items, list)
    price_data = line_items[0]["price_data"]
    assert price_data["unit_amount"] == 1234
    assert price_data["currency"] == "usd"
    # A second line carries the OpenRouter-style service fee; the top-up line stays
    # at par and the granted cents (metadata) are unchanged by the fee.
    fee_data = line_items[1]["price_data"]
    assert fee_data["unit_amount"] == purchase_fee_cents(1234)
    assert fee_data["currency"] == "usd"
    assert line_items[1]["price_data"]["product_data"]["name"] == "Platform fee"
    metadata = captured["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["cents"] == "1234"
    assert metadata["pack_id"] == "custom"
    assert metadata["username"] == "u@x.com"
    assert captured["billing_address_collection"] == "required"
    assert captured["customer_update"] == {"address": "auto", "name": "auto"}
    assert captured["invoice_creation"] == {"enabled": True}
    assert captured["saved_payment_method_options"] == {
        "payment_method_save": "enabled",
        "payment_method_remove": "enabled",
    }
    assert "payment_method_types" not in captured


def test_billing_profile_is_empty_without_stripe_customer(engine: object, configured: None) -> None:
    """Opening Billing for a customerless account does not provision Stripe state."""
    service = StripeBillingService(engine=engine)
    with patch("stripe.Customer.retrieve") as retrieve:
        snapshot = service.get_billing_profile("new@x.com")
    assert snapshot.available is True
    assert snapshot.has_customer is False
    assert snapshot.payment_methods == []
    retrieve.assert_not_called()


def test_billing_profile_maps_safe_customer_and_payment_fields(engine: object, configured: None) -> None:
    """The profile includes billing contact data and only masked card metadata."""
    _seed_customer(engine, "u@x.com")
    service = StripeBillingService(engine=engine)
    customer = {
        "email": "billing@x.com",
        "name": "Billing Person",
        "phone": "+1 212 555 0100",
        "address": {
            "line1": "12 Mercer St",
            "line2": None,
            "city": "New York",
            "state": "NY",
            "postal_code": "10013",
            "country": "US",
        },
        "invoice_settings": {"default_payment_method": "pm_default"},
    }
    methods = {
        "data": [
            {
                "id": "pm_default",
                "type": "card",
                "card": {"brand": "visa", "last4": "4242", "exp_month": 12, "exp_year": 2030},
            }
        ]
    }
    with (
        patch("stripe.Customer.retrieve", return_value=customer),
        patch("stripe.Customer.list_payment_methods", return_value=methods) as list_methods,
    ):
        snapshot = service.get_billing_profile("u@x.com")
    assert snapshot.available is True
    assert snapshot.has_customer is True
    assert snapshot.email == "billing@x.com"
    assert snapshot.address.city == "New York"
    assert snapshot.payment_methods[0].last4 == "4242"
    assert snapshot.payment_methods[0].is_default is True
    list_methods.assert_called_once_with("cus_u@x.com", limit=20)


def test_transactions_map_invoice_and_refund_status(engine: object, configured: None) -> None:
    """Purchase history exposes totals, top-up metadata, refunds, and hosted documents."""
    _seed_customer(engine, "u@x.com")
    service = StripeBillingService(engine=engine)
    created = int(datetime(2026, 8, 1, tzinfo=UTC).timestamp())
    checkout = {
        "id": "cs_1",
        "created": created,
        "amount_total": 500,
        "currency": "usd",
        "payment_status": "paid",
        "metadata": {"credits": "500", "pack_id": "starter"},
        "payment_intent": {"latest_charge": {"amount_refunded": 200, "receipt_url": "https://receipt"}},
        "invoice": {"hosted_invoice_url": "https://invoice"},
    }
    with patch("stripe.checkout.Session.list", return_value={"data": [checkout]}) as list_sessions:
        snapshot = service.get_transactions(
            "u@x.com",
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 9, 1, tzinfo=UTC),
        )
    (entry,) = snapshot.entries
    assert snapshot.available is True
    assert entry.status == "partially_refunded"
    assert entry.amount == 500
    assert entry.currency == "USD"
    assert entry.cents == 500
    assert entry.document_url == "https://invoice"
    kwargs = list_sessions.call_args.kwargs
    assert kwargs["customer"] == "cus_u@x.com"
    assert kwargs["status"] == "complete"
    assert kwargs["expand"] == ["data.payment_intent.latest_charge", "data.invoice"]


def test_payment_method_portal_deep_links_and_returns_to_billing(
    engine: object,
    configured: None,
) -> None:
    """Adding a card uses Stripe's payment-method flow and returns to Billing."""
    _seed_customer(engine, "u@x.com")
    service = StripeBillingService(engine=engine)
    with patch(
        "stripe.billing_portal.Session.create",
        return_value=SimpleNamespace(url="https://billing.stripe.test/session"),
    ) as create:
        url = service.create_portal_session("u@x.com", payment_method_update=True)
    assert url == "https://billing.stripe.test/session"
    kwargs = create.call_args.kwargs
    assert kwargs["customer"] == "cus_u@x.com"
    assert kwargs["return_url"].endswith("/?settings=billing")
    assert kwargs["flow_data"]["type"] == "payment_method_update"
    assert kwargs["flow_data"]["after_completion"]["redirect"]["return_url"].endswith("/?settings=billing")


def test_update_payment_method_edits_card_and_sets_default(engine: object, configured: None) -> None:
    """Editing a card sends the new expiry and name, then makes it the customer's default."""
    _seed_customer(engine, "u@x.com")
    service = StripeBillingService(engine=engine)
    method = {"id": "pm_1", "type": "card", "customer": "cus_u@x.com"}
    with (
        patch("stripe.PaymentMethod.retrieve", return_value=method),
        patch("stripe.PaymentMethod.modify") as modify,
        patch("stripe.Customer.modify") as customer_modify,
    ):
        service.update_payment_method(
            "u@x.com", "pm_1", exp_month=4, exp_year=2031, holder_name=" Card Holder ", make_default=True
        )
    modify.assert_called_once_with(
        "pm_1", card={"exp_month": 4, "exp_year": 2031}, billing_details={"name": "Card Holder"}
    )
    customer_modify.assert_called_once_with("cus_u@x.com", invoice_settings={"default_payment_method": "pm_1"})


def test_update_payment_method_rejects_expiry_on_non_card(engine: object, configured: None) -> None:
    """Expiry only exists on cards, so sending it for another method type is a 400."""
    _seed_customer(engine, "u@x.com")
    service = StripeBillingService(engine=engine)
    method = {"id": "pm_1", "type": "us_bank_account", "customer": "cus_u@x.com"}
    with (
        patch("stripe.PaymentMethod.retrieve", return_value=method),
        patch("stripe.PaymentMethod.modify") as modify,
        pytest.raises(DomainError) as exc,
    ):
        service.update_payment_method("u@x.com", "pm_1", exp_month=4, exp_year=2031)
    assert exc.value.status_code == 400
    modify.assert_not_called()


def test_update_payment_method_maps_stripe_rejection_to_400(engine: object, configured: None) -> None:
    """A past expiry Stripe refuses surfaces as an invalid-details error, not an outage."""
    _seed_customer(engine, "u@x.com")
    service = StripeBillingService(engine=engine)
    method = {"id": "pm_1", "type": "card", "customer": "cus_u@x.com"}
    with (
        patch("stripe.PaymentMethod.retrieve", return_value=method),
        patch("stripe.PaymentMethod.modify", side_effect=stripe.InvalidRequestError("expired", "exp_year")),
        pytest.raises(DomainError) as exc,
    ):
        service.update_payment_method("u@x.com", "pm_1", exp_month=1, exp_year=2001)
    assert exc.value.code == "billing.payment_method_invalid"
    assert exc.value.status_code == 400


def test_remove_payment_method_detaches_owned_method(engine: object, configured: None) -> None:
    """Removing a card the account owns detaches it from the Stripe customer."""
    _seed_customer(engine, "u@x.com")
    service = StripeBillingService(engine=engine)
    method = {"id": "pm_1", "type": "card", "customer": "cus_u@x.com"}
    with (
        patch("stripe.PaymentMethod.retrieve", return_value=method),
        patch("stripe.PaymentMethod.detach") as detach,
    ):
        service.remove_payment_method("u@x.com", "pm_1")
    detach.assert_called_once_with("pm_1")


@pytest.mark.parametrize("owner", ["cus_other@x.com", None])
def test_payment_method_changes_refuse_methods_of_other_customers(
    engine: object, configured: None, owner: str | None
) -> None:
    """A method attached elsewhere, or to nobody, reads as not found and is never touched."""
    _seed_customer(engine, "u@x.com")
    service = StripeBillingService(engine=engine)
    method = {"id": "pm_1", "type": "card", "customer": owner}
    with (
        patch("stripe.PaymentMethod.retrieve", return_value=method),
        patch("stripe.PaymentMethod.detach") as detach,
        patch("stripe.PaymentMethod.modify") as modify,
    ):
        with pytest.raises(DomainError) as removed:
            service.remove_payment_method("u@x.com", "pm_1")
        with pytest.raises(DomainError) as edited:
            service.update_payment_method("u@x.com", "pm_1", make_default=True)
    assert removed.value.status_code == edited.value.status_code == 404
    detach.assert_not_called()
    modify.assert_not_called()


def test_payment_method_changes_need_a_stripe_customer(engine: object, configured: None) -> None:
    """A customerless account has no saved methods, so any id is not found."""
    service = StripeBillingService(engine=engine)
    with patch("stripe.PaymentMethod.retrieve") as retrieve, pytest.raises(DomainError) as exc:
        service.remove_payment_method("new@x.com", "pm_1")
    assert exc.value.status_code == 404
    retrieve.assert_not_called()


def _deliver(service: StripeBillingService, event: dict) -> None:
    """Deliver a prebuilt event dict through the webhook with signature verification stubbed."""
    with patch("stripe.Webhook.construct_event", return_value=event):
        service.handle_webhook(b"{}", "sig")


def _balance(engine: object, username: str) -> int:
    """Read the persisted purchased balance for an account (0 when it has no row)."""
    with Session(engine) as session:
        customer = session.get(BillingCustomerModel, username)
        return 0 if customer is None else int(customer.balance_cents)


def _ledger_rows(engine: object, username: str, kind: str) -> list[WalletLedgerModel]:
    """Return an account's ledger rows of a given kind, oldest first.

    Args:
        engine: The SQLite engine to read from.
        username: Account whose ledger to read.
        kind: The ``kind`` column value to filter on (e.g. ``"refund"``).

    Returns:
        The matching rows ordered by insertion.
    """
    with Session(engine) as session:
        return (
            session.query(WalletLedgerModel)
            .filter_by(username=username, kind=kind)
            .order_by(WalletLedgerModel.id)
            .all()
        )


def test_webhook_refund_claws_back_cents(engine: object, webhook_ready: None) -> None:
    """A full refund removes the topped-up cents and writes one refund ledger row."""
    _seed_customer(engine, "u@x.com")
    service = StripeBillingService(engine=engine)
    _deliver(service, _checkout_event("evt_pay", "u@x.com", 500, payment_intent="pi_1"))
    assert _balance(engine, "u@x.com") == 500

    _deliver(service, _refund_event("evt_ref", "pi_1", 500))

    assert _balance(engine, "u@x.com") == 0
    rows = _ledger_rows(engine, "u@x.com", "refund")
    assert len(rows) == 1
    assert rows[0].delta_cents == -500
    assert rows[0].stripe_payment_intent_id == "pi_1"
    assert rows[0].stripe_event_id == "evt_ref"


def test_webhook_partial_refunds_are_incremental(engine: object, webhook_ready: None) -> None:
    """Two partial refunds each claw back only the new slice — the cumulative total is never removed twice."""
    _seed_customer(engine, "u@x.com")
    service = StripeBillingService(engine=engine)
    _deliver(service, _checkout_event("evt_pay", "u@x.com", 500, payment_intent="pi_1"))

    _deliver(service, _refund_event("evt_ref1", "pi_1", 200))
    assert _balance(engine, "u@x.com") == 300
    # The second event carries the charge's cumulative refunded total, not just the new slice.
    _deliver(service, _refund_event("evt_ref2", "pi_1", 500))
    assert _balance(engine, "u@x.com") == 0

    rows = _ledger_rows(engine, "u@x.com", "refund")
    assert [r.delta_cents for r in rows] == [-200, -300]


def test_webhook_refund_clamps_to_spent_balance(engine: object, webhook_ready: None) -> None:
    """A refund of cents already spent claws back only what remains, never below zero."""
    _seed_customer(engine, "u@x.com")
    service = StripeBillingService(engine=engine)
    _deliver(service, _checkout_event("evt_pay", "u@x.com", 500, payment_intent="pi_1"))
    # The buyer spent 300 of the 500 before the refund lands.
    with Session(engine) as session:
        session.get(BillingCustomerModel, "u@x.com").balance_cents = 200
        session.commit()

    _deliver(service, _refund_event("evt_ref", "pi_1", 500))

    assert _balance(engine, "u@x.com") == 0
    rows = _ledger_rows(engine, "u@x.com", "refund")
    assert len(rows) == 1
    assert rows[0].delta_cents == -200
    assert rows[0].uncollected_cents == 300
    assert _debt(engine, "u@x.com") == 300


def _debt(engine: object, username: str) -> int:
    """Read the persisted refund/chargeback debt for an account (0 when it has no row)."""
    with Session(engine) as session:
        customer = session.get(BillingCustomerModel, username)
        return 0 if customer is None else int(customer.debt_cents)


def _spend_all_but(engine: object, username: str, remaining: int) -> None:
    """Simulate spending a buyer's purchased balance down to ``remaining``."""
    with Session(engine) as session:
        session.get(BillingCustomerModel, username).balance_cents = remaining
        session.commit()


def test_webhook_refund_of_spent_cents_records_debt_and_blocks_spending(engine: object, webhook_ready: None) -> None:
    """Refunding spent cents leaves debt that blocks every spend gate, and alerts."""
    _seed_customer(engine, "u@x.com")
    service = StripeBillingService(engine=engine)
    _deliver(service, _checkout_event("evt_pay", "u@x.com", 500, payment_intent="pi_1"))
    _spend_all_but(engine, "u@x.com", 0)

    with patch("core.billing.service.send_alert") as alert:
        _deliver(service, _refund_event("evt_ref", "pi_1", 500))

    assert _debt(engine, "u@x.com") == 500
    assert service.spendable_cents("u@x.com") == 0
    (row,) = _ledger_rows(engine, "u@x.com", "refund")
    assert (row.delta_cents, row.uncollected_cents) == (0, 500)
    alert.assert_called_once()
    assert service.debit_run("u@x.com", _usages(100_000), model=None, description="r") == 0


def test_webhook_clawback_drains_free_grant_before_recording_debt(engine: object, webhook_ready: None) -> None:
    """A clawback exceeding the paid balance takes the grant next so it cannot be spent on top of the reversal."""
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username="u@x.com", stripe_customer_id="cus_u", balance_cents=0, grant_remaining=50)
        )
        session.commit()
    service = StripeBillingService(engine=engine)
    _deliver(service, _checkout_event("evt_pay", "u@x.com", 500, payment_intent="pi_1"))
    _spend_all_but(engine, "u@x.com", 100)

    _deliver(service, _dispute_event("evt_dis", "pi_1", 500))

    assert _balance(engine, "u@x.com") == 0
    assert _grant_remaining(engine, "u@x.com") == 0
    assert _debt(engine, "u@x.com") == 350
    (row,) = _ledger_rows(engine, "u@x.com", "dispute")
    assert (row.delta_cents, row.uncollected_cents) == (-150, 350)


def test_webhook_topup_repays_debt_before_crediting(engine: object, webhook_ready: None) -> None:
    """A top-up clears outstanding debt first; only the remainder becomes spendable."""
    _seed_customer(engine, "u@x.com")
    service = StripeBillingService(engine=engine)
    _deliver(service, _checkout_event("evt_pay", "u@x.com", 500, payment_intent="pi_1"))
    _spend_all_but(engine, "u@x.com", 0)
    _deliver(service, _refund_event("evt_ref", "pi_1", 500))

    _deliver(service, _checkout_event("evt_pay2", "u@x.com", 200, payment_intent="pi_2"))
    assert (_debt(engine, "u@x.com"), _balance(engine, "u@x.com")) == (300, 0)
    assert service.spendable_cents("u@x.com") == 0

    _deliver(service, _checkout_event("evt_pay3", "u@x.com", 1000, payment_intent="pi_3"))
    assert (_debt(engine, "u@x.com"), _balance(engine, "u@x.com")) == (0, 700)
    repayments = _ledger_rows(engine, "u@x.com", "debt_repayment")
    assert [row.delta_cents for row in repayments] == [-200, -300]
    assert all(row.stripe_payment_intent_id is None for row in repayments)


def test_webhook_dispute_after_debt_refund_does_not_double_claw(engine: object, webhook_ready: None) -> None:
    """A dispute on an already-refunded charge counts the earlier debt as reversed and adds none."""
    _seed_customer(engine, "u@x.com")
    service = StripeBillingService(engine=engine)
    _deliver(service, _checkout_event("evt_pay", "u@x.com", 500, payment_intent="pi_1"))
    _spend_all_but(engine, "u@x.com", 0)
    _deliver(service, _refund_event("evt_ref", "pi_1", 500))

    _deliver(service, _dispute_event("evt_dis", "pi_1", 500))

    assert _debt(engine, "u@x.com") == 500
    assert _ledger_rows(engine, "u@x.com", "dispute") == []


def test_webhook_refund_is_idempotent_on_redelivery(engine: object, webhook_ready: None) -> None:
    """A redelivered refund event claws back exactly once."""
    _seed_customer(engine, "u@x.com")
    service = StripeBillingService(engine=engine)
    _deliver(service, _checkout_event("evt_pay", "u@x.com", 500, payment_intent="pi_1"))

    event = _refund_event("evt_ref", "pi_1", 500)
    _deliver(service, event)
    _deliver(service, event)

    assert _balance(engine, "u@x.com") == 0
    assert len(_ledger_rows(engine, "u@x.com", "refund")) == 1


def test_webhook_refund_for_unknown_payment_intent_is_noop(engine: object, webhook_ready: None) -> None:
    """A refund for a charge Skynet never credited touches no balance and writes no row."""
    _seed_customer(engine, "u@x.com")
    service = StripeBillingService(engine=engine)

    _deliver(service, _refund_event("evt_ref", "pi_unknown", 500))

    assert _balance(engine, "u@x.com") == 0
    assert _ledger_rows(engine, "u@x.com", "refund") == []


def test_webhook_dispute_claws_back_cents(engine: object, webhook_ready: None) -> None:
    """A chargeback removes the disputed cents and writes one dispute ledger row."""
    _seed_customer(engine, "u@x.com")
    service = StripeBillingService(engine=engine)
    _deliver(service, _checkout_event("evt_pay", "u@x.com", 500, payment_intent="pi_2"))

    _deliver(service, _dispute_event("evt_dis", "pi_2", 500))

    assert _balance(engine, "u@x.com") == 0
    rows = _ledger_rows(engine, "u@x.com", "dispute")
    assert len(rows) == 1
    assert rows[0].delta_cents == -500
    assert rows[0].description == "Chargeback"
    assert rows[0].stripe_payment_intent_id == "pi_2"


def test_webhook_dispute_after_partial_refund_nets(engine: object, webhook_ready: None) -> None:
    """A dispute after a partial refund claws back only the still-unreversed remainder."""
    _seed_customer(engine, "u@x.com")
    service = StripeBillingService(engine=engine)
    _deliver(service, _checkout_event("evt_pay", "u@x.com", 500, payment_intent="pi_1"))
    _deliver(service, _refund_event("evt_ref", "pi_1", 200))
    assert _balance(engine, "u@x.com") == 300

    _deliver(service, _dispute_event("evt_dis", "pi_1", 500))

    assert _balance(engine, "u@x.com") == 0
    dispute_rows = _ledger_rows(engine, "u@x.com", "dispute")
    assert len(dispute_rows) == 1
    assert dispute_rows[0].delta_cents == -300


def test_byok_model_charge_stays_zero_with_usage_markup() -> None:
    """BYOK model usage is free; managed spend is marked up."""
    usages = _usages(2_000_000)
    assert platform_fee_cents_for_usage(usages) == 0
    assert cents_for_usage(usages) == 230


@pytest.mark.parametrize(
    "event_type",
    ["customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"],
)
def test_retired_subscription_events_preserve_history_without_calls(
    engine: object, webhook_ready: None, event_type: str
) -> None:
    """Acknowledge subscription events without changing history, balance, or provider state.

    Args:
        engine: Isolated billing database.
        webhook_ready: Fixture enabling signed-event handling with fake credentials.
        event_type: Retired subscription lifecycle event.
    """
    _seed_customer(engine, "u@x.com")
    with Session(engine) as session:
        customer = session.get(BillingCustomerModel, "u@x.com")
        customer.stripe_subscription_id = "sub_archived"
        customer.subscription_status = "active"
        customer.balance_cents = 125
        session.commit()
    event = {
        "id": "evt_retired",
        "type": event_type,
        "data": {"object": {"id": "sub_archived", "status": "canceled", "metadata": {"username": "u@x.com"}}},
    }
    service = StripeBillingService(engine=engine)
    with (
        patch("stripe.Webhook.construct_event", return_value=event),
        patch("stripe.Subscription.retrieve") as retrieve,
    ):
        service.handle_webhook(b"{}", "sig")
        service.handle_webhook(b"{}", "sig")
    retrieve.assert_not_called()
    with Session(engine) as session:
        customer = session.get(BillingCustomerModel, "u@x.com")
        assert customer.stripe_subscription_id == "sub_archived"
        assert customer.subscription_status == "active"
        assert customer.balance_cents == 125
        assert session.get(BillingWebhookEventModel, "evt_retired") is not None
        assert session.query(WalletLedgerModel).count() == 0
    assert "plan" not in asdict(service.get_wallet("u@x.com"))


def test_retired_subscription_checkout_cannot_credit_wallet(engine: object, webhook_ready: None) -> None:
    """Ignore a completed subscription checkout even if it carries top-up metadata.

    Args:
        engine: Isolated billing database.
        webhook_ready: Fixture enabling signed-event handling with fake credentials.
    """
    _seed_customer(engine, "u@x.com")
    event = _checkout_event("evt_subscription_checkout", "u@x.com", 500)
    event["data"]["object"]["mode"] = "subscription"
    service = StripeBillingService(engine=engine)
    with patch("stripe.Webhook.construct_event", return_value=event):
        service.handle_webhook(b"{}", "sig")
    assert _balance(engine, "u@x.com") == 0
    assert _ledger_rows(engine, "u@x.com", "topup") == []


def test_retired_checkout_route_and_wallet_plan_are_absent(engine: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the subscription endpoint and schema while retaining prepaid billing routes.

    Args:
        engine: Isolated billing database.
        monkeypatch: Build the hosted route set without starting the full app.
    """
    monkeypatch.setattr(settings, "local_mode", False)
    app = FastAPI()
    app.include_router(create_billing_router(job_store=SimpleNamespace(engine=engine)))
    schema = app.openapi()
    assert "/billing/subscription/checkout" not in schema["paths"]
    assert "/billing/checkout" in schema["paths"]
    assert "/billing/portal" in schema["paths"]
    assert "PlanResponse" not in schema["components"]["schemas"]
    assert "plan" not in schema["components"]["schemas"]["WalletResponse"]["properties"]
    with TestClient(app) as client:
        assert client.post("/billing/subscription/checkout").status_code == 404
    assert not hasattr(StripeBillingService, "create_subscription_checkout")


def test_provisioning_keeps_only_prepaid_prices(configured: None, capsys: pytest.CaptureFixture[str]) -> None:
    """Provision the three existing top-up prices without a monthly product.

    Args:
        configured: Fake Stripe key fixture.
        capsys: Captured setup output.
    """
    with (
        patch.object(provision_stripe, "_ensure_price", return_value="price_fixture") as ensure,
        patch.object(stripe, "api_key", None),
    ):
        assert provision_stripe.main() == 0
    assert [(call.args[0], call.args[2]) for call in ensure.call_args_list] == [
        ("skynet_pack_starter", 500),
        ("skynet_pack_plus", 2000),
        ("skynet_pack_pro", 5000),
    ]
    output = capsys.readouterr().out
    assert "STRIPE_PRICE_PACK_PRO=price_fixture" in output
    assert "MONTHLY" not in output


def test_prepaid_price_creation_has_no_recurring_terms() -> None:
    """Create the existing prepaid pack as a one-time price."""
    with (
        patch.object(stripe.Price, "list", return_value=SimpleNamespace(data=[])),
        patch.object(stripe.Price, "create", return_value=SimpleNamespace(id="price_fixture")) as create,
    ):
        assert provision_stripe._ensure_price("skynet_pack_pro", "Prepaid top-up", 5000) == "price_fixture"
    assert create.call_args.kwargs == {
        "currency": "usd",
        "unit_amount": 5000,
        "lookup_key": "skynet_pack_pro",
        "product_data": {"name": "Prepaid top-up"},
    }
