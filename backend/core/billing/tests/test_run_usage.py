"""Verify the per-run usage breakdown adds up to the run's charges for every optimizer shape."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from core.api.routers.optimizations.usage import run_usage
from core.billing import pricing
from core.billing.budget_amounts import cent_units
from core.billing.openrouter_prices import TokenPrices
from core.billing.pricing import ModelUsage, known_token_prices, usage_cost_usd
from core.billing.run_usage import (
    BILLING_BYOK,
    BILLING_DIRECT,
    BILLING_SKYNET,
    BILLING_SUBSCRIPTION,
    ROLE_PROPOSER,
    ROLE_REFLECTION,
    ROLE_ROUNDING,
    ROLE_SANDBOX,
    ROLE_SCORER,
    ROLE_SETUP,
    ROLE_TASK,
    OperationRecord,
    aggregate,
    billed_cents,
    from_result,
    load_records,
    serialize,
    with_rounding,
)
from core.storage.models import Base, ExecutionBudgetModel, ExecutionOperationModel, ExecutionUsageEvidenceModel

_MANAGED = {"kind": "managed_model", "byok_fee_fraction": "0.05"}
_BYOK = {"kind": "byok_model", "byok_fee_fraction": "0"}
_SUBSCRIPTION = {"kind": "subscription_model"}


def _op(
    *,
    role: str | None = "task",
    cost_kind: str = "model",
    phase: str = "run",
    state: str = "settled",
    policy: dict[str, Any] = _MANAGED,
    cents: str = "1",
    evidence: dict[str, Any] | None = None,
    quoted_model: str | None = None,
) -> OperationRecord:
    """Build one operation record with sensible defaults."""
    return OperationRecord(phase, cost_kind, role, state, policy, cent_units(cents), evidence or {}, quoted_model)


def _call(model: str = "openai/gpt-4o-mini", **tags: str) -> dict[str, Any]:
    """Build model-call evidence with OpenAI-style usage."""
    evidence: dict[str, Any] = {
        "model": model,
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        "latency_ms": 400,
    }
    if tags:
        evidence["tags"] = tags
    return evidence


def test_dspy_run_splits_task_and_reflection_by_stage_and_candidate() -> None:
    """Group DSPy calls by role, stage and candidate while summing tokens and latency."""
    rows = aggregate(
        [
            _op(evidence=_call(stage="baseline")),
            _op(evidence=_call(stage="baseline")),
            _op(role="optimization", cents="3", evidence=_call("openai/gpt-5", stage="training", candidate="2")),
        ],
        proposer=False,
    )
    assert [(row.role, row.stage, row.candidate, row.calls) for row in rows] == [
        (ROLE_TASK, "baseline", None, 2),
        (ROLE_REFLECTION, "training", "2", 1),
    ]
    assert rows[0].input_tokens == 200
    assert rows[0].output_tokens == 40
    assert rows[0].latency_ms_total == 800
    assert rows[0].latency_calls == 2


def test_rows_add_up_to_every_charge_including_setup_and_sandbox() -> None:
    """Keep compute and setup as their own rows so the total equals what the run charged."""
    records = [
        _op(phase="setup", cents="2", evidence=_call()),
        _op(role="runtime", cost_kind="sandbox", cents="7.5", evidence={"provider": "vercel"}),
        _op(role="judge", cents="0.25", evidence=_call(candidate="1", case="ex-3")),
        _op(role="optimization", cents="4", evidence=_call("anthropic/claude-sonnet-4.5")),
    ]
    rows = aggregate(records, proposer=True)
    assert {row.role for row in rows} == {ROLE_SETUP, ROLE_SANDBOX, ROLE_SCORER, ROLE_PROPOSER}
    assert sum(row.charged_units for row in rows) == sum(record.wallet_units for record in records)
    sandbox = next(row for row in rows if row.role == ROLE_SANDBOX)
    assert sandbox.model is None
    assert sandbox.calls == 0


def test_caller_tag_decides_proposer_or_reflection_on_a_mixed_route() -> None:
    """Split an auto run's optimization route into proposer and reflection rows by the caller tag."""
    rows = aggregate(
        [
            _op(role="optimization", evidence=_call(caller="reflection")),
            _op(role="optimization", evidence=_call(caller="proposer")),
            _op(role="optimization", evidence=_call()),
        ],
        proposer=True,
    )
    assert sorted((row.role, row.calls) for row in rows) == [(ROLE_PROPOSER, 2), (ROLE_REFLECTION, 1)]


def test_batched_calls_count_without_a_response_time() -> None:
    """Leave economy-batch calls out of the average, since their wait is the batch window."""
    batched = {**_call(), "batched": True}
    del batched["latency_ms"]
    (row,) = aggregate([_op(evidence=_call()), _op(evidence=batched)], proposer=False)
    assert (row.calls, row.latency_calls, row.latency_ms_total) == (2, 1, 400)


def test_byok_calls_report_the_provider_charge_from_usage() -> None:
    """Read provider cost from usage without charging the user's wallet."""
    evidence = {**_call(), "usage": {"cost": "0.10", "prompt_tokens": 3}}
    (row,) = aggregate([_op(policy=_BYOK, cents="0", evidence=evidence)], proposer=False)
    assert row.billing == BILLING_BYOK
    assert row.provider_cents == Decimal(10)


@pytest.mark.parametrize(
    ("model", "usage"),
    [
        ("openai/gpt-4o-mini", {"prompt_tokens": 100, "completion_tokens": 20}),
        ("anthropic/claude-sonnet-4.5", {"input_tokens": 100, "output_tokens": 20}),
    ],
)
def test_native_byok_estimates_missing_cost_from_tokens(
    model: str, usage: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Estimate native provider cost without changing wallet charges."""
    monkeypatch.setattr(
        "core.billing.run_usage.cached_token_prices", lambda _model: TokenPrices(prompt=0.001, completion=0.002)
    )
    (row,) = aggregate([_op(policy=_BYOK, cents="0", evidence={"model": model, "usage": usage})], proposer=False)
    assert row.provider_cents == Decimal(14)
    assert row.unpriced_calls == 0
    assert row.charged_units == 0


def test_byok_missing_provider_cost_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Leave an unknown model unpriced when the provider gives no cost."""
    monkeypatch.setattr("core.billing.run_usage.cached_token_prices", lambda _model: None)
    evidence = {"model": "unknown/model", "usage": {"input_tokens": 100, "output_tokens": 20}}
    (row,) = aggregate([_op(policy=_BYOK, cents="0", evidence=evidence)], proposer=False)
    assert row.provider_cents is None
    assert row.unpriced_calls == 1


@pytest.mark.parametrize(
    ("state", "evidence"), [("settled", {"failure": "provider"}), ("reserved", {}), ("dispatched", {})]
)
def test_byok_missing_usage_is_unpriced(state: str, evidence: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not estimate a provider cost without usage."""
    monkeypatch.setattr(
        "core.billing.run_usage.cached_token_prices", lambda _model: TokenPrices(prompt=0.001, completion=0.002)
    )
    (row,) = aggregate(
        [_op(policy=_BYOK, cents="0", state=state, quoted_model="openai/model", evidence=evidence)], proposer=False
    )
    assert row.provider_cents is None
    assert row.unpriced_calls == 1


def test_byok_anthropic_cache_tokens_use_cache_price(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore the provider prefix and use the cache-read token price."""

    def prices(model: str) -> TokenPrices | None:
        """Return a price only for the fully qualified model."""
        return (
            TokenPrices(prompt=0.000003, completion=0.000015, cache_read=0.0000003)
            if model == "anthropic/claude"
            else None
        )

    monkeypatch.setattr("core.billing.run_usage.cached_token_prices", prices)
    evidence = {
        "provider": "anthropic",
        "model": "claude",
        "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 1000000},
    }
    (row,) = aggregate([_op(policy=_BYOK, cents="0", evidence=evidence)], proposer=False)
    assert row.provider_cents == Decimal(30)


@pytest.mark.parametrize(
    ("details", "expected"),
    [({"cached_tokens": 1000000}, Decimal(330)), ({}, Decimal(600)), ("invalid", Decimal(600))],
)
def test_byok_openai_cached_tokens_use_cache_price(
    details: object, expected: Decimal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Price cached OpenAI prompt tokens at the cache-read rate."""
    monkeypatch.setattr(
        "core.billing.run_usage.cached_token_prices",
        lambda _model: TokenPrices(prompt=0.000003, completion=0.000015, cache_read=0.0000003),
    )
    evidence = {
        "provider": "openai",
        "model": "gpt-4o",
        "usage": {"prompt_tokens": 2000000, "completion_tokens": 0, "prompt_tokens_details": details},
    }
    (row,) = aggregate([_op(policy=_BYOK, cents="0", evidence=evidence)], proposer=False)
    assert row.provider_cents == expected
    assert row.input_tokens == 2000000
    assert row.charged_units == 0


def test_byok_custom_provider_is_unpriced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not use a catalog price for an unknown custom endpoint."""
    monkeypatch.setattr(
        "core.billing.run_usage.cached_token_prices", lambda _model: TokenPrices(prompt=1, completion=1)
    )
    evidence = {"provider": "custom", "model": "openai/model", "usage": {"prompt_tokens": 1}}
    (row,) = aggregate([_op(policy=_BYOK, cents="0", evidence=evidence)], proposer=False)
    assert row.provider_cents is None


@pytest.mark.parametrize("usage", [None, "invalid", {}, {"input_tokens": None}, {"prompt_tokens": "invalid"}])
def test_byok_usage_without_token_counts_is_unpriced(usage: Any) -> None:
    """Leave missing and malformed token evidence unpriced."""
    (row,) = aggregate(
        [_op(policy=_BYOK, cents="0", evidence={"model": "openai/gpt-4o-mini", "usage": usage})], proposer=False
    )
    assert row.provider_cents is None
    assert row.unpriced_calls == 1


def test_byok_estimate_does_not_refresh_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read token prices without refreshing the live catalog."""

    def fail() -> None:
        """Reject any refresh."""
        raise AssertionError("catalog refreshed")

    monkeypatch.setattr("core.billing.openrouter_prices._refresh_if_stale", fail)
    evidence = {"provider": "openai", "model": "gpt-4o-mini", "usage": {"prompt_tokens": 100}}
    (row,) = aggregate([_op(policy=_BYOK, cents="0", evidence=evidence)], proposer=False)
    assert row.provider_cents is not None


@pytest.mark.parametrize("cost", ["invalid", "-1", "NaN"])
def test_byok_malformed_provider_cost_is_unavailable(cost: str) -> None:
    """Do not replace malformed provider cost with an estimate."""
    (row,) = aggregate([_op(policy=_BYOK, cents="0", evidence={"usage": {"cost": cost}})], proposer=False)
    assert row.provider_cents is None
    assert row.unpriced_calls == 1


def test_openrouter_byok_provider_cost_includes_upstream_cost() -> None:
    """Include both OpenRouter and upstream cost in provider telemetry."""
    usage = {"cost": "0.001", "is_byok": True, "cost_details": {"upstream_inference_cost": "0.02"}}
    (row,) = aggregate([_op(policy=_BYOK, cents="0", evidence={"usage": usage})], proposer=False)
    assert row.provider_cents == Decimal("2.1")


def test_openrouter_byok_invalid_cost_details_are_unavailable() -> None:
    """Keep malformed upstream evidence from blocking run usage."""
    usage = {"cost": "0.001", "is_byok": True, "cost_details": "invalid"}
    (row,) = aggregate([_op(policy=_BYOK, cents="0", evidence={"usage": usage})], proposer=False)
    assert row.provider_cents is None


def test_historical_byok_provider_cost_from_wallet_without_usage() -> None:
    """Recover provider spend from an old fee-only operation."""
    policy = {"kind": "byok_model", "byok_fee_fraction": "0.05"}
    (row,) = aggregate([_op(policy=policy, cents="1", evidence={})], proposer=False)
    assert row.provider_cents == Decimal(20)


def test_subscription_calls_show_their_api_rate_without_a_charge() -> None:
    """Price subscription tokens at the model's API rate, apart from any charge or provider spend."""
    evidence = {**_call(), "provider": "subscription", "cli": "codex"}
    rows = aggregate(
        [
            _op(policy=_SUBSCRIPTION, cents="0", evidence=evidence),
            _op(policy=_SUBSCRIPTION, cents="0", evidence=evidence),
        ],
        proposer=False,
    )
    (row,) = rows
    assert (row.billing, row.calls, row.charged_units, row.provider_cents) == (BILLING_SUBSCRIPTION, 2, 0, None)
    expected = Decimal(str(usage_cost_usd(ModelUsage("openai/gpt-4o-mini", 100, 20)))) / Decimal("0.01")
    assert row.api_rate_cents == 2 * expected
    assert row.api_rate_cents > 0
    assert row.unpriced_calls == 0
    assert with_rounding(rows, 0) == rows


def test_subscription_call_on_a_model_without_an_api_price_gets_no_amount() -> None:
    """Show no API-rate amount, and not the fallback rate, for a model no price table knows."""
    (row,) = aggregate([_op(policy=_SUBSCRIPTION, cents="0", evidence=_call("acme/no-such-model-xyz"))], proposer=False)
    assert (row.billing, row.calls, row.api_rate_cents, row.unpriced_calls) == (BILLING_SUBSCRIPTION, 1, None, 0)


def _subscription_call(model: str = "openai/gpt-4o-mini", cost: Any = None) -> dict[str, Any]:
    """Build subscription call evidence whose usage block carries the cost recorded at call time."""
    evidence = {**_call(model), "provider": "subscription", "cli": "codex"}
    evidence["usage"] = {**evidence["usage"], "cost": cost}
    return evidence


def test_subscription_call_uses_the_cost_recorded_at_call_time() -> None:
    """Prefer the cost the CLI dispatcher recorded over today's price table."""
    table = Decimal(str(usage_cost_usd(ModelUsage("openai/gpt-4o-mini", 100, 20)))) / Decimal("0.01")
    (row,) = aggregate([_op(policy=_SUBSCRIPTION, cents="0", evidence=_subscription_call(cost=0.5))], proposer=False)
    assert row.api_rate_cents == Decimal(50)
    assert row.api_rate_cents != table


def test_recorded_cost_prices_a_model_no_price_table_knows() -> None:
    """Show the recorded cost even when no price table knows the model today."""
    evidence = _subscription_call("acme/no-such-model-xyz", cost=0.02)
    (row,) = aggregate([_op(policy=_SUBSCRIPTION, cents="0", evidence=evidence)], proposer=False)
    assert row.api_rate_cents == Decimal(2)


@pytest.mark.parametrize("cost", [None, True, -1, "0.5"])
def test_missing_or_invalid_recorded_cost_falls_back_to_the_price_table(cost: Any) -> None:
    """Use the current price table when the recorded cost is missing, a bool, negative or not a number."""
    expected = Decimal(str(usage_cost_usd(ModelUsage("openai/gpt-4o-mini", 100, 20)))) / Decimal("0.01")
    (row,) = aggregate([_op(policy=_SUBSCRIPTION, cents="0", evidence=_subscription_call(cost=cost))], proposer=False)
    assert row.api_rate_cents == expected


def test_fallback_prices_a_call_with_one_price_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Price with the one lookup that decided the model is priced, even if a second lookup would miss."""
    answers = iter([TokenPrices(prompt=1e-6, completion=2e-6)])
    monkeypatch.setattr(pricing, "live_prices", lambda _model: next(answers, None))
    evidence = _subscription_call("acme/no-such-model-xyz")
    (row,) = aggregate([_op(policy=_SUBSCRIPTION, cents="0", evidence=evidence)], proposer=False)
    assert row.api_rate_cents == (Decimal(100) * Decimal("1e-6") + Decimal(20) * Decimal("2e-6")) / Decimal("0.01")


def test_usage_cost_uses_the_given_prices_without_a_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the price lookup when the caller passes the prices."""

    def fail(_model: str) -> None:
        """Fail if a lookup happens."""
        raise AssertionError("unexpected price lookup")

    monkeypatch.setattr(pricing, "live_prices", fail)
    prices = TokenPrices(prompt=1e-6, completion=2e-6)
    assert usage_cost_usd(ModelUsage("acme/no-such-model-xyz", 100, 20), prices=prices) == pytest.approx(1.4e-4)


def test_only_subscription_calls_get_an_api_rate() -> None:
    """Leave managed and BYOK rows without an API-rate amount."""
    rows = aggregate([_op(evidence=_call()), _op(policy=_BYOK, cents="0.5", evidence=_call())], proposer=False)
    assert [row.api_rate_cents for row in rows] == [None, None]


def test_unsettled_and_undispatched_calls() -> None:
    """Count in-flight calls as pending, and never count a reservation that sent nothing as a call."""
    (row,) = aggregate(
        [
            _op(state="pending", cents="0", evidence=_call()),
            _op(state="reserved", cents="0", quoted_model="openai/gpt-4o-mini"),
        ],
        proposer=False,
    )
    assert row.calls == 1
    assert row.pending_calls == 2


def test_anthropic_usage_counts_cached_input() -> None:
    """Count Anthropic's separately reported cache tokens as input."""
    usage = {"input_tokens": 10, "cache_read_input_tokens": 90, "cache_creation_input_tokens": 5, "output_tokens": 7}
    (row,) = aggregate([_op(evidence={"model": "anthropic/claude", "usage": usage})], proposer=False)
    assert (row.input_tokens, row.output_tokens) == (105, 7)


def test_pair_filter_keeps_only_that_pairs_calls() -> None:
    """Narrow a grid run to one pair's tagged calls."""
    rows = aggregate(
        [_op(evidence=_call(pair="0")), _op(evidence=_call(pair="1")), _op(role="runtime", cost_kind="sandbox")],
        proposer=False,
        pair="1",
    )
    assert [(row.pair, row.calls) for row in rows] == [("1", 1)]


def test_direct_proposer_usage_is_a_row_with_an_estimate_and_no_charge() -> None:
    """Show calls paid on the owner's own Anthropic key, estimated, with nothing charged by Skynet."""
    usage = {"model": "claude-sonnet-4-5", "calls": 4, "prompt_tokens": 1000, "completion_tokens": 200}
    (row,) = aggregate([], proposer=True, direct_usage=[usage])
    assert (row.role, row.billing, row.calls, row.charged_units) == (ROLE_PROPOSER, BILLING_DIRECT, 4, 0)
    assert row.provider_cents is not None
    assert row.provider_cents > 0
    assert aggregate([], proposer=True, direct_usage=[usage], pair="0") == []


def test_rounding_row_brings_the_rows_up_to_the_billed_charge() -> None:
    """Add the gap to whole billed cents as its own row, so the rows equal the charge."""
    rows = aggregate(
        [_op(cents="0.4", evidence=_call()), _op(role="runtime", cost_kind="sandbox", cents="0.3")], proposer=False
    )
    rounded = with_rounding(rows, 1)
    assert rounded[-1].role == ROLE_ROUNDING
    assert sum(row.charged_units for row in rounded) == cent_units(1)
    assert with_rounding(rows, None) == rows


def test_no_rounding_row_when_rows_already_match() -> None:
    """Leave whole-cent rows alone."""
    rows = aggregate([_op(cents="2", evidence=_call())], proposer=False)
    assert with_rounding(rows, 2) == rows


def test_serialize_renders_cents() -> None:
    """Render units as cents for JSON."""
    (document,) = serialize(aggregate([_op(cents="1.23456789", evidence=_call())], proposer=False))
    assert document["charged_cents"] == 1.2346
    assert document["provider_cents"] is None
    assert document["api_rate_cents"] is None
    assert document["billing"] == BILLING_SKYNET
    (subscription,) = serialize(aggregate([_op(policy=_SUBSCRIPTION, cents="0", evidence=_call())], proposer=False))
    assert subscription["charged_cents"] == 0
    assert isinstance(subscription["api_rate_cents"], float)
    assert subscription["api_rate_cents"] > 0


_LEGACY_RESULT: dict[str, Any] = {
    "usage_by_model": [
        {"model": "litellm_proxy/openai/gpt-4.1-nano", "input_tokens": 15465, "output_tokens": 923},
        {"model": "litellm_proxy/openai/gpt-4.1", "input_tokens": 4476, "output_tokens": 311},
    ],
    "lm_activity": {
        "generation": {
            "baseline": {"calls": 5, "avg_response_time_ms": 1000.0},
            "training": {"calls": 61, "avg_response_time_ms": 500.0},
        },
        "reflection": {"training": {"calls": 1, "avg_response_time_ms": 7000.0}},
    },
    "details": {"billing": {"outcome": "billed", "cents": 3}},
}


def test_budgetless_run_is_rebuilt_from_its_result_and_adds_up_to_the_stamp() -> None:
    """Split a pre-budget run by model and role, with the billed stamp spread across the rows."""
    rows = from_result(
        _LEGACY_RESULT,
        task_model="openrouter/openai/gpt-4.1-nano",
        reflection_model="openrouter/openai/gpt-4.1",
        proposer=False,
        byok=False,
    )
    assert [(row.role, row.model, row.calls, row.input_tokens) for row in rows] == [
        (ROLE_TASK, "litellm_proxy/openai/gpt-4.1-nano", 66, 15465),
        (ROLE_REFLECTION, "litellm_proxy/openai/gpt-4.1", 1, 4476),
    ]
    assert rows[0].latency_ms_total == 5 * 1000 + 61 * 500
    assert all(row.billing == BILLING_SKYNET for row in rows)
    assert sum(row.charged_units for row in rows) == cent_units(3)
    assert (
        from_result(_LEGACY_RESULT, task_model=None, reflection_model=None, proposer=False, byok=False, pair="0") == []
    )


def test_budgetless_charge_without_usage_keeps_its_own_row() -> None:
    """Keep a stamped charge visible even when the result recorded no model usage."""
    (row,) = from_result(
        {"details": {"billing": {"outcome": "billed", "cents": 2}}},
        task_model=None,
        reflection_model=None,
        proposer=False,
        byok=False,
    )
    assert row.charged_units == cent_units(2)


def test_budgetless_grid_pairs_carry_their_own_models() -> None:
    """Read each grid pair's usage under its pair tag and narrow to one pair."""
    pair = {
        "pair_index": 1,
        "generation_model": "openai/gpt-4o-mini",
        "reflection_model": "openai/gpt-5",
        "usage_by_model": [{"model": "openai/gpt-5", "input_tokens": 10, "output_tokens": 5}],
    }
    result = {"pair_results": [{**pair, "pair_index": 0}, pair]}
    rows = from_result(result, task_model=None, reflection_model=None, proposer=False, byok=False, pair="1")
    assert [(row.pair, row.role, row.input_tokens) for row in rows] == [("1", ROLE_REFLECTION, 10)]


def test_usage_route_falls_back_to_the_result_when_the_run_has_no_budget() -> None:
    """Show an older run's recorded usage instead of an empty tab when no budget is linked."""
    job = {
        "status": "success",
        "execution_budget_id": None,
        "payload_overview": {
            "model_name": "openrouter/openai/gpt-4.1-nano",
            "reflection_model_name": "openrouter/openai/gpt-4.1",
        },
        "result": _LEGACY_RESULT,
    }
    usage = run_usage(object(), job, None)
    assert sum(row["calls"] for row in usage["rows"]) == 67
    assert sum(row["input_tokens"] + row["output_tokens"] for row in usage["rows"]) == 21175
    assert sum(row["charged_cents"] for row in usage["rows"]) == pytest.approx(3)
    assert usage["settling"] is False


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Create a private billing database."""
    database = create_engine(f"sqlite:///{tmp_path / 'usage.db'}")
    Base.metadata.create_all(database)
    yield database
    database.dispose()


def test_load_records_merges_evidence_per_operation(engine: Engine) -> None:
    """Read each operation with its evidence rows merged in order, and ignore other budgets."""
    now = datetime.now(UTC)
    with Session(engine) as session:
        for budget_id in ("b1", "b2"):
            session.add(
                ExecutionBudgetModel(
                    id=budget_id,
                    username="alice",
                    creation_key=budget_id,
                    creation_fingerprint="f",
                    total_cents=100,
                    created_at=now,
                    updated_at=now,
                )
            )
        for index, budget_id in enumerate(("b1", "b2")):
            session.add(
                ExecutionOperationModel(
                    id=f"op{index}",
                    budget_id=budget_id,
                    operation_key="k",
                    attempt=0,
                    generation=0,
                    phase="run",
                    cost_kind="model",
                    role="task",
                    request_fingerprint="r",
                    admission_fingerprint="a",
                    price_snapshot={"policy": _MANAGED},
                    state="settled",
                    max_units=cent_units(5),
                    max_wallet_units=cent_units(5),
                    actual_units=cent_units(2),
                    actual_wallet_units=cent_units(2),
                    created_at=now,
                    updated_at=now,
                )
            )
        for offset, evidence in enumerate((_call(stage="training"), {"generation": {"total_cost": 0.02}})):
            session.add(
                ExecutionUsageEvidenceModel(
                    id=f"ev{offset}",
                    operation_id="op0",
                    evidence_key=f"e{offset}",
                    fingerprint="f",
                    actual_units=0,
                    actual_wallet_units=0,
                    billed_cents=0,
                    final=True,
                    evidence=evidence,
                    created_at=now + timedelta(seconds=offset),
                )
            )
        session.commit()
    (record,) = load_records(engine, "b1")
    assert billed_cents(engine, "b1") == 0
    assert billed_cents(engine, "missing") is None
    assert record.policy == _MANAGED
    assert record.wallet_units == cent_units(2)
    assert record.evidence["tags"] == {"stage": "training"}
    assert "generation" in record.evidence


def test_known_token_prices_skips_the_fallback_rate() -> None:
    """Return a priced model's rates, and ``None`` where pricing would fall back to the default."""
    assert known_token_prices("openai/gpt-4o-mini") is not None
    assert known_token_prices("acme/no-such-model-xyz") is None
