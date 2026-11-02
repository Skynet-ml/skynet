"""Tests for ``core.billing.pricing`` — per-model, per-token run pricing."""

from __future__ import annotations

import logging
import math
from unittest.mock import patch

import litellm
import pytest

from core.billing import openrouter_prices
from core.billing.pricing import (
    CENT_USD_VALUE,
    FALLBACK_INPUT_COST_PER_TOKEN,
    FALLBACK_OUTPUT_COST_PER_TOKEN,
    ModelUsage,
    cents_for_cost_usd,
    cents_for_usage,
    combine_usages,
    fallback_priced_usages,
    model_token_costs,
    raw_cost_usd,
    usage_markup,
    usages_from_breakdown,
)
from core.config import settings

# Controlled price table so assertions don't drift when LiteLLM updates its real
# numbers. ``test/bare`` is keyed without a provider prefix to exercise the
# prefix-stripping fallback in ``model_token_costs``.
_FAKE_COSTS = {
    "test/cheap": {"input_cost_per_token": 1e-7, "output_cost_per_token": 4e-7, "mode": "chat"},
    "test/frontier": {"input_cost_per_token": 5e-6, "output_cost_per_token": 3e-5, "mode": "chat"},
    "bare": {"input_cost_per_token": 2e-7, "output_cost_per_token": 6e-7, "mode": "chat"},
    "test/zerofield": {"input_cost_per_token": 0, "output_cost_per_token": 6e-7, "mode": "chat"},
}


def _expected_cents(raw_usd: float) -> int:
    """Mirror ``cents_for_usage`` arithmetic so tests track a tuned markup."""
    cost = raw_usd * usage_markup()
    return 0 if cost <= 0 else max(1, math.ceil(cost / CENT_USD_VALUE))


def test_model_token_costs_reads_known_model() -> None:
    """A priced model returns its LiteLLM input/output per-token costs."""
    with patch.dict(litellm.model_cost, _FAKE_COSTS, clear=False):
        assert model_token_costs("test/cheap") == (1e-7, 4e-7)


def test_model_token_costs_strips_prefix_fallback() -> None:
    """A prefixed id resolves against a bare-keyed table entry."""
    with patch.dict(litellm.model_cost, _FAKE_COSTS, clear=False):
        assert model_token_costs("someprovider/bare") == (2e-7, 6e-7)


def test_model_token_costs_unknown_model_uses_high_fallback_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    """An unpriced model bills at the conservative fallback rate and logs a warning."""
    with patch.dict(litellm.model_cost, _FAKE_COSTS, clear=False), caplog.at_level(logging.WARNING):
        assert model_token_costs("nope/not-real") == (
            FALLBACK_INPUT_COST_PER_TOKEN,
            FALLBACK_OUTPUT_COST_PER_TOKEN,
        )
    assert "nope/not-real" in caplog.text


def test_model_token_costs_missing_field_uses_fallback() -> None:
    """A zero/absent static cost field is not trusted; the model bills at the fallback rate."""
    with patch.dict(litellm.model_cost, _FAKE_COSTS, clear=False):
        assert model_token_costs("test/zerofield") == (FALLBACK_INPUT_COST_PER_TOKEN, FALLBACK_OUTPUT_COST_PER_TOKEN)


def test_live_openrouter_prices_win_over_static_table() -> None:
    """A model in the live OpenRouter listing is priced from it, with the provider prefix stripped."""
    openrouter_prices.remember([{"id": "test/cheap", "pricing": {"prompt": "0.000002", "completion": "0.000008"}}])
    with patch.dict(litellm.model_cost, _FAKE_COSTS, clear=False):
        assert model_token_costs("openrouter/test/cheap") == (2e-6, 8e-6)


def test_offline_offered_model_bills_at_its_openrouter_list_price(caplog: pytest.LogCaptureFixture) -> None:
    """The offline sandbox prices an offered model from the listed rates, not the fallback."""
    with caplog.at_level(logging.WARNING):
        assert model_token_costs("z-ai/glm-5.3-flash") == (2.85e-8, 9.23e-8)
        assert model_token_costs("openrouter/z-ai/glm-5.3-flash") == (2.85e-8, 9.23e-8)
    assert "No price known" not in caplog.text


def test_live_openrouter_prices_win_over_listed_snapshot() -> None:
    """A fresh listing replaces the stored rates of an offered model."""
    openrouter_prices.remember(
        [{"id": "z-ai/glm-5.3-flash", "pricing": {"prompt": "0.000001", "completion": "0.000002"}}]
    )
    assert model_token_costs("z-ai/glm-5.3-flash") == (1e-6, 2e-6)


def test_cache_and_reasoning_tokens_bill_at_their_own_rates() -> None:
    """Cache reads, cache writes and reasoning tokens use their published rates."""
    openrouter_prices.remember(
        [
            {
                "id": "v/m",
                "pricing": {
                    "prompt": "0.000001",
                    "completion": "0.000004",
                    "input_cache_read": "0.0000001",
                    "input_cache_write": "0.00000125",
                    "internal_reasoning": "0.000005",
                },
            }
        ]
    )
    usage = ModelUsage(
        "openrouter/v/m",
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=600,
        cache_write_tokens=100,
        reasoning_tokens=200,
    )
    expected = 300 * 1e-6 + 600 * 1e-7 + 100 * 1.25e-6 + 200 * 5e-6 + 300 * 4e-6
    assert raw_cost_usd([usage]) == pytest.approx(expected)


def test_reported_cost_is_used_for_the_tokens_it_covers() -> None:
    """Tokens covered by a provider-reported cost are not priced again."""
    with patch.dict(litellm.model_cost, _FAKE_COSTS, clear=False):
        covered = ModelUsage(
            "test/cheap",
            input_tokens=1000,
            output_tokens=100,
            reported_cost_usd=0.02,
            reported_input_tokens=1000,
            reported_output_tokens=100,
        )
        assert raw_cost_usd([covered]) == 0.02
        partial = ModelUsage(
            "test/cheap",
            input_tokens=3000,
            output_tokens=100,
            reported_cost_usd=0.02,
            reported_input_tokens=1000,
            reported_output_tokens=100,
        )
        assert raw_cost_usd([partial]) == pytest.approx(0.02 + 2000 * 1e-7)


def test_markup_multiplies_platform_paid_cents(monkeypatch: pytest.MonkeyPatch) -> None:
    """The configured usage markup scales the cost; ``markup=1.0`` prices at cost."""
    monkeypatch.setattr(settings, "usage_markup", 1.15)
    usage = [ModelUsage("x/y", 0, 0, reported_cost_usd=1.0)]
    assert cents_for_usage(usage) == 115
    assert cents_for_usage(usage, markup=1.0) == 100


def test_combine_usages_sums_rows_per_model() -> None:
    """Rows sharing a model id fold into one, summing every counter and reported cost."""
    rows = combine_usages(
        [
            ModelUsage("a", 10, 1, reported_cost_usd=0.5, reported_input_tokens=10, reported_output_tokens=1),
            ModelUsage("a", 5, 2, cache_read_tokens=3),
            ModelUsage("b", 1, 1),
        ]
    )
    assert rows == [
        ModelUsage(
            "a",
            15,
            3,
            reported_cost_usd=0.5,
            reported_input_tokens=10,
            reported_output_tokens=1,
            cache_read_tokens=3,
        ),
        ModelUsage("b", 1, 1),
    ]


def test_cents_for_usage_prices_input_and_output_separately() -> None:
    """A single-model run is priced from its split token volume, marked up."""
    with patch.dict(litellm.model_cost, _FAKE_COSTS, clear=False):
        usage = [ModelUsage("test/cheap", input_tokens=1_000_000, output_tokens=1_000_000)]
        raw = 1_000_000 * 1e-7 + 1_000_000 * 4e-7  # 0.5 USD
        assert raw_cost_usd(usage) == raw
        assert cents_for_usage(usage) == _expected_cents(raw)


def test_cents_for_usage_aggregates_multiple_models() -> None:
    """A run spanning two models sums each model's marked-up cost."""
    with patch.dict(litellm.model_cost, _FAKE_COSTS, clear=False):
        usage = [
            ModelUsage("test/cheap", input_tokens=500_000, output_tokens=200_000),
            ModelUsage("test/frontier", input_tokens=100_000, output_tokens=50_000),
        ]
        raw = (500_000 * 1e-7 + 200_000 * 4e-7) + (100_000 * 5e-6 + 50_000 * 3e-5)
        assert cents_for_usage(usage) == _expected_cents(raw)


def test_cents_for_usage_any_usage_costs_at_least_one_cent() -> None:
    """A sliver of usage rounds up to one cent, never billed zero."""
    with patch.dict(litellm.model_cost, _FAKE_COSTS, clear=False):
        assert cents_for_usage([ModelUsage("test/cheap", input_tokens=1, output_tokens=0)]) == 1


def test_cents_for_usage_zero_usage_is_zero() -> None:
    """No tokens cost nothing."""
    assert cents_for_usage([]) == 0
    with patch.dict(litellm.model_cost, _FAKE_COSTS, clear=False):
        assert cents_for_usage([ModelUsage("test/cheap", input_tokens=0, output_tokens=0)]) == 0


def test_frontier_costs_more_than_mini_for_same_volume() -> None:
    """The whole point: identical token volume prices higher on a frontier model."""
    with patch.dict(litellm.model_cost, _FAKE_COSTS, clear=False):
        mini = cents_for_usage([ModelUsage("test/cheap", 200_000, 200_000)])
        frontier = cents_for_usage([ModelUsage("test/frontier", 200_000, 200_000)])
        assert frontier > mini


def test_usages_from_breakdown_builds_model_usage_rows() -> None:
    """A ``model → (input, output)`` mapping becomes ModelUsage rows."""
    rows = usages_from_breakdown({"a/m": (10, 20), "b/n": (30, 40)})
    assert ModelUsage("a/m", 10, 20) in rows
    assert ModelUsage("b/n", 30, 40) in rows
    assert len(rows) == 2


def test_fallback_priced_usages_prices_every_token_at_frontier_rates() -> None:
    """Fallback repricing carries the whole cost as reported, so no price lookup is needed."""
    usages = [
        ModelUsage("cheap/model", 1_000_000, 100_000, reported_cost_usd=0.01, reported_input_tokens=10),
        ModelUsage("idle/model", 0, 0),
    ]
    (row,) = fallback_priced_usages(usages)
    expected_usd = 1_000_000 * FALLBACK_INPUT_COST_PER_TOKEN + 100_000 * FALLBACK_OUTPUT_COST_PER_TOKEN
    assert row.reported_cost_usd == pytest.approx(expected_usd)
    assert (row.reported_input_tokens, row.reported_output_tokens) == (1_000_000, 100_000)
    with patch("core.billing.pricing.model_token_costs", side_effect=AssertionError("no lookup")):
        assert cents_for_usage([row]) == cents_for_cost_usd(expected_usd * usage_markup())
