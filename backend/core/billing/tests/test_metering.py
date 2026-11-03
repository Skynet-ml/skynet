"""Tests for ``core.billing.metering.meter_llm_run`` — the interactive-run seam.

Each test stands up an in-memory SQLite engine with the billing tables and
drives the helper with fake LMs shaped like ``dspy.LM`` history carriers, the
same contract ``usage_by_model_from_history`` reads in production.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from core.billing.metering import estimate_run_cents, meter_llm_run, meter_llm_usage
from core.billing.pricing import ModelUsage, cents_for_usage, fallback_priced_usages
from core.billing.service import StripeBillingService
from core.storage.models import Base, BillingCustomerModel, WalletLedgerModel


@pytest.fixture
def engine() -> Iterator[object]:
    """Yield an in-memory SQLite engine with the billing tables created."""
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)


class _FakeLm:
    """History-carrying LM double, optionally with a served-model stash."""

    def __init__(
        self,
        history: list[dict[str, Any]],
        model: str = "openrouter/test/unpriced",
        served: str | None = None,
    ) -> None:
        """Store the canned history, model id, and optional served-model stash.

        Args:
            history: Entries shaped like ``dspy.LM.history`` rows.
            model: Model id the LM reports.
            served: Concrete model to stash as ``last_response_model``.
        """
        self.history = history
        self.model = model
        if served is not None:
            self.last_request_model = model
            self.last_response_model = served


def _ledger_rows(engine: object) -> list[WalletLedgerModel]:
    """Return every wallet-ledger row, oldest first."""
    with Session(engine) as session:
        return session.query(WalletLedgerModel).order_by(WalletLedgerModel.id).all()


def _fund(engine: object, username: str, cents: int = 100_000) -> None:
    """Seed a billing row with a paid balance so a debit has something to draw.

    The clamped debit charges at most what the account holds, so a test that
    asserts a real charge landed must start from a funded balance.

    Args:
        engine: The SQLite engine to write to.
        username: Account to fund.
        cents: Paid balance to seed.
    """
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(
                username=username,
                stripe_customer_id=f"cus_{username}",
                balance_cents=cents,
                grant_remaining=0,
            )
        )
        session.commit()


def test_meter_llm_run_debits_and_stamps_tokens(engine: object) -> None:
    """A tracked run writes one run row with the measured token counts."""
    _fund(engine, "alice@x.io")
    lm = _FakeLm([{"usage": {"prompt_tokens": 100_000, "completion_tokens": 40_000}}])
    cents = meter_llm_run(engine, "alice@x.io", [lm], description="Agent chat")
    assert cents > 0
    rows = _ledger_rows(engine)
    assert len(rows) == 1
    row = rows[0]
    assert row.kind == "run"
    assert row.description == "Agent chat"
    assert row.model == "openrouter/test/unpriced"
    assert row.delta_cents == -cents
    assert row.input_tokens == 100_000
    assert row.output_tokens == 40_000
    with Session(engine) as session:
        customer = session.get(BillingCustomerModel, "alice@x.io")
        assert customer is not None
        assert int(customer.balance_cents) == 100_000 - cents


def test_meter_llm_run_rekeys_auto_route_to_served_model(engine: object) -> None:
    """An auto-routed run's charge lands on the concrete model the router served."""
    _fund(engine, "alice@x.io")
    lm = _FakeLm(
        [{"usage": {"prompt_tokens": 50_000, "completion_tokens": 10_000}}],
        model="litellm_proxy/openrouter/auto-beta",
        served="google/gemini-2.5-flash-lite",
    )
    assert meter_llm_run(engine, "alice@x.io", [lm], description="Agent chat") > 0
    (row,) = _ledger_rows(engine)
    assert row.model == "google/gemini-2.5-flash-lite"


def test_meter_llm_run_strips_proxy_prefix(engine: object) -> None:
    """An explicit pick behind the managed proxy books under its catalog id."""
    _fund(engine, "alice@x.io")
    lm = _FakeLm(
        [{"usage": {"prompt_tokens": 50_000, "completion_tokens": 10_000}}],
        model="litellm_proxy/openrouter/test/unpriced",
    )
    assert meter_llm_run(engine, "alice@x.io", [lm], description="Code interview") > 0
    (row,) = _ledger_rows(engine)
    assert row.model == "openrouter/test/unpriced"


def test_meter_llm_run_skips_untracked_usage(engine: object) -> None:
    """No usage info anywhere bills nothing and writes no row."""
    assert meter_llm_run(engine, "alice@x.io", [_FakeLm([{"response": "hi"}])], description="x") == 0
    assert meter_llm_run(engine, "alice@x.io", [], description="x") == 0
    assert meter_llm_run(engine, "alice@x.io", [None], description="x") == 0
    assert _ledger_rows(engine) == []


def test_meter_llm_run_skips_without_engine_or_username(engine: object) -> None:
    """A store with no engine, or an anonymous caller, meters nothing."""
    lm = _FakeLm([{"usage": {"prompt_tokens": 10, "completion_tokens": 5}}])
    assert meter_llm_run(None, "alice@x.io", [lm], description="x") == 0
    assert meter_llm_run(engine, "", [lm], description="x") == 0
    assert _ledger_rows(engine) == []


def test_estimate_run_cents_prices_without_debiting(engine: object) -> None:
    """The in-flight estimate matches what a debit would charge and writes nothing."""
    _fund(engine, "alice@x.io")
    history = [{"usage": {"prompt_tokens": 100_000, "completion_tokens": 40_000}}]
    estimate = estimate_run_cents([_FakeLm(history)])
    assert estimate > 0
    assert _ledger_rows(engine) == []
    assert estimate == meter_llm_run(engine, "alice@x.io", [_FakeLm(history)], description="x")


def test_estimate_run_cents_excludes_byok_model_charge() -> None:
    """The live balance watch does not charge BYOK model usage."""
    history = [{"usage": {"prompt_tokens": 100_000, "completion_tokens": 40_000}}]
    managed = estimate_run_cents([_FakeLm(history)], "managed")
    byok = estimate_run_cents([_FakeLm(history)], "byok")
    assert byok == 0
    assert managed > 0


def test_estimate_run_cents_handles_empty_and_untracked_lms() -> None:
    """No LMs or no tracked usage estimates to zero instead of raising."""
    assert estimate_run_cents([]) == 0
    assert estimate_run_cents([None]) == 0
    assert estimate_run_cents([_FakeLm([{"response": "hi"}])]) == 0


def test_meter_llm_run_never_raises_on_billing_failure(engine: object) -> None:
    """A broken billing backend logs and returns 0 — the user's turn already succeeded."""
    lm = _FakeLm([{"usage": {"prompt_tokens": 10, "completion_tokens": 5}}])
    assert meter_llm_run(object(), "alice@x.io", [lm], description="x") == 0


def test_meter_llm_usage_debits_a_token_breakdown(engine: object) -> None:
    """A per-model breakdown (a scorer dry run's ``llm()`` calls) debits like a run, keyed by the bare model."""
    _fund(engine, "alice@x.io")
    cents = meter_llm_usage(
        engine,
        "alice@x.io",
        {"litellm_proxy/openrouter/test/unpriced": (100_000, 40_000)},
        description="Scorer dry run",
    )
    assert cents > 0
    [row] = _ledger_rows(engine)
    assert row.kind == "run"
    assert row.description == "Scorer dry run"
    assert row.model == "openrouter/test/unpriced"
    assert row.delta_cents == -cents
    assert row.input_tokens == 100_000
    assert row.output_tokens == 40_000


def test_meter_llm_usage_skips_empty_or_unbound(engine: object) -> None:
    """Nothing is written without an engine, a user, or any usage."""
    assert meter_llm_usage(engine, "alice@x.io", {}, description="Scorer dry run") == 0
    assert meter_llm_usage(None, "alice@x.io", {"m": (1, 1)}, description="Scorer dry run") == 0
    assert meter_llm_usage(engine, "", {"m": (1, 1)}, description="Scorer dry run") == 0
    assert _ledger_rows(engine) == []


def _fallback_cost(model: str, input_tokens: int, output_tokens: int) -> int:
    """Cents a single-model usage costs at the conservative fallback rates."""
    return cents_for_usage(fallback_priced_usages([ModelUsage(model, input_tokens, output_tokens)]))


def test_meter_llm_run_charges_fallback_price_when_harvest_fails(
    engine: object, caplog: pytest.LogCaptureFixture
) -> None:
    """A metering fault logs at ERROR and still bills the raw tokens at the fallback rate."""
    _fund(engine, "alice@x.io")
    lm = _FakeLm([{"usage": {"prompt_tokens": 100_000, "completion_tokens": 40_000}}])
    with patch("core.billing.metering._harvest_usages", side_effect=RuntimeError("boom")):
        cents = meter_llm_run(engine, "alice@x.io", [lm], description="Agent chat")
    assert cents == _fallback_cost("openrouter/test/unpriced", 100_000, 40_000)
    (row,) = _ledger_rows(engine)
    assert row.delta_cents == -cents
    assert (row.input_tokens, row.output_tokens) == (100_000, 40_000)
    assert any(r.levelname == "ERROR" and "fallback price" in r.getMessage() for r in caplog.records)


def test_meter_llm_usage_charges_fallback_price_when_debit_fails(engine: object) -> None:
    """A failed exact debit is retried once at the fallback rate rather than billing zero."""
    _fund(engine, "alice@x.io")
    real_debit = StripeBillingService.debit_run
    calls: list[int] = []

    def flaky(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("price lookup failed")
        return real_debit(self, *args, **kwargs)

    with patch.object(StripeBillingService, "debit_run", flaky):
        cents = meter_llm_usage(engine, "alice@x.io", {"test/unpriced": (100_000, 40_000)}, description="Scorer")
    assert len(calls) == 2
    assert cents == _fallback_cost("test/unpriced", 100_000, 40_000)
    (row,) = _ledger_rows(engine)
    assert row.delta_cents == -cents
