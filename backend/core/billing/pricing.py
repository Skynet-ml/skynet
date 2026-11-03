"""Per-model, per-token run pricing — the shared basis for estimate and charge.

A run's cost in cents is the provider cost of its tokens times the configured
usage markup (``settings.usage_markup``, env ``USAGE_MARKUP``), converted to
cents at :data:`CENT_USD_VALUE`. The markup applies to
every platform-paid metered charge. BYOK model calls carry no Skynet charge.

Provider cost comes, in order of preference, from:

1. the ``usage.cost`` OpenRouter reported on the response itself;
2. OpenRouter's live per-model prices (:mod:`.openrouter_prices`), splitting
   prompt, cache-read, cache-write, completion and reasoning tokens;
3. LiteLLM's static ``model_cost`` table;
4. a deliberately high fallback rate, logged as a warning, so an unpriced model
   is over- rather than under-charged.

The *same* function prices a projected token volume (the pre-run estimate) and a
measured one (the post-run charge), so the two reconcile by construction.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields, replace

import litellm

from ..config import settings
from .openrouter_prices import TokenPrices, live_prices

logger = logging.getLogger(__name__)

# The balance is kept in US cents. The markup is applied to the cost, never
# to the balance.
CENT_USD_VALUE = 0.01

# Retain the prior fraction for historical snapshots and the frontend transition.
PLATFORM_FEE_FRACTION = 0.05
BYOK_FEE_FRACTION = 0.0

# Rates for a model neither OpenRouter nor LiteLLM prices, set at a frontier
# model's list price so an unknown model can only be over-charged, never given
# away below cost.
FALLBACK_INPUT_COST_PER_TOKEN = 15e-6
FALLBACK_OUTPUT_COST_PER_TOKEN = 75e-6

_warned_unpriced: set[str] = set()


def usage_markup() -> float:
    """Return the multiplier applied to platform-paid provider cost.

    Returns:
        ``settings.usage_markup`` (``1.15`` unless ``USAGE_MARKUP`` overrides it).
    """
    return settings.usage_markup


@dataclass(frozen=True)
class ModelUsage:
    """Token usage attributed to one model in a run — measured or projected.

    ``input_tokens`` and ``output_tokens`` are the full totals. When some calls
    reported their own provider cost, ``reported_cost_usd`` is that cost and the
    ``reported_*_tokens`` fields are the tokens it already covers; only the
    remainder is priced per token. ``cache_read_tokens``, ``cache_write_tokens``
    (subsets of the unreported input) and ``reasoning_tokens`` (a subset of the
    unreported output) are priced at their own rates when the model has them.
    """

    model: str
    input_tokens: int
    output_tokens: int
    reported_cost_usd: float = 0.0
    reported_input_tokens: int = 0
    reported_output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0


def _positive(value: object) -> float | None:
    """Return a strictly positive numeric rate, else ``None``.

    Args:
        value: A raw price-table value.

    Returns:
        The rate as a float, or ``None`` when missing, non-numeric or not positive.
    """
    return float(value) if isinstance(value, (int, float)) and value > 0 else None


def _static_prices(model_id: str) -> TokenPrices | None:
    """Look a model up in LiteLLM's static ``model_cost`` table.

    Retries without the provider prefix (``openai/gpt-4o-mini`` →
    ``gpt-4o-mini``) since the table keys both shapes inconsistently.

    Args:
        model_id: Fully-qualified or bare model id.

    Returns:
        The model's prices, or ``None`` when the table lacks an input or output rate.
    """
    meta = litellm.model_cost.get(model_id)
    if meta is None and "/" in model_id:
        meta = litellm.model_cost.get(model_id.split("/", 1)[1])
    if not isinstance(meta, Mapping):
        return None
    prompt = _positive(meta.get("input_cost_per_token"))
    completion = _positive(meta.get("output_cost_per_token"))
    if prompt is None or completion is None:
        return None
    return TokenPrices(
        prompt=prompt,
        completion=completion,
        cache_read=_positive(meta.get("cache_read_input_token_cost")),
        cache_write=_positive(meta.get("cache_creation_input_token_cost")),
        reasoning=_positive(meta.get("output_cost_per_reasoning_token")),
    )


def model_token_prices(model_id: str) -> TokenPrices:
    """Return a model's per-token USD prices, never zero.

    Args:
        model_id: The model id as billed.

    Returns:
        Live OpenRouter prices, else LiteLLM's static prices, else the high
        fallback rates (logged once per model as a warning).
    """
    prices = live_prices(model_id) or _static_prices(model_id)
    if prices is not None:
        return prices
    if model_id not in _warned_unpriced:
        _warned_unpriced.add(model_id)
        logger.warning(
            "No price known for model %r; billing at the fallback rate of $%.2f/$%.2f per M tokens",
            model_id,
            FALLBACK_INPUT_COST_PER_TOKEN * 1e6,
            FALLBACK_OUTPUT_COST_PER_TOKEN * 1e6,
        )
    return TokenPrices(prompt=FALLBACK_INPUT_COST_PER_TOKEN, completion=FALLBACK_OUTPUT_COST_PER_TOKEN)


def known_token_prices(model_id: str) -> TokenPrices | None:
    """Return a model's per-token USD prices only when a price table knows the model.

    Args:
        model_id: The model id as recorded.

    Returns:
        Live OpenRouter prices, else LiteLLM's static prices, else ``None``
        (never the fallback rates, which exist to bill safely, not to estimate).
    """
    return live_prices(model_id) or _static_prices(model_id)


def model_token_costs(model_id: str) -> tuple[float, float]:
    """Return a model's ``(input, output)`` per-token cost in USD, at cost.

    Args:
        model_id: Fully-qualified or bare model id.

    Returns:
        The ``(prompt, completion)`` pair from :func:`model_token_prices`.
    """
    prices = model_token_prices(model_id)
    return prices.prompt, prices.completion


def usage_cost_usd(usage: ModelUsage, prices: TokenPrices | None = None) -> float:
    """Price one model's usage at provider cost (USD, before markup).

    Args:
        usage: The model's token usage and any cost the provider reported.
        prices: Per-token prices the caller already looked up; ``None`` looks them up.

    Returns:
        The reported cost plus the per-token price of the tokens it does not cover.
    """
    unreported_in = max(0, usage.input_tokens - usage.reported_input_tokens)
    unreported_out = max(0, usage.output_tokens - usage.reported_output_tokens)
    cost = max(0.0, usage.reported_cost_usd)
    if unreported_in == 0 and unreported_out == 0:
        return cost
    if prices is None:
        prices = model_token_prices(usage.model)
    cache_read = min(max(0, usage.cache_read_tokens), unreported_in)
    cache_write = min(max(0, usage.cache_write_tokens), unreported_in - cache_read)
    reasoning = min(max(0, usage.reasoning_tokens), unreported_out)
    cost += (unreported_in - cache_read - cache_write) * prices.prompt
    cost += cache_read * (prices.cache_read if prices.cache_read is not None else prices.prompt)
    cost += cache_write * (prices.cache_write if prices.cache_write is not None else prices.prompt)
    cost += reasoning * (prices.reasoning if prices.reasoning is not None else prices.completion)
    cost += (unreported_out - reasoning) * prices.completion
    return cost


def raw_cost_usd(usages: Iterable[ModelUsage]) -> float:
    """Sum the provider cost (USD, before markup) of per-model usage.

    Args:
        usages: Per-model usage rows.

    Returns:
        The total at-cost provider spend in USD.
    """
    return sum(usage_cost_usd(usage) for usage in usages)


def cents_for_cost_usd(cost_usd: float) -> int:
    """Convert a USD amount to cents, rounding any non-zero amount up.

    Args:
        cost_usd: The amount to bill, already marked up (or fee-scaled).

    Returns:
        The non-negative cost in cents; a positive amount costs at least one cent.
    """
    if cost_usd <= 0:
        return 0
    return max(1, math.ceil(cost_usd / CENT_USD_VALUE))


def cents_for_usage(usages: Iterable[ModelUsage], *, markup: float | None = None) -> int:
    """Convert per-model usage to the cents a platform-paid run costs.

    Args:
        usages: Per-model usage (measured or projected).
        markup: Multiplier on provider cost; ``None`` uses :func:`usage_markup`.
            Pass ``1.0`` for the at-cost value.

    Returns:
        The non-negative cost in cents; any non-zero usage costs at least one cent.
    """
    factor = usage_markup() if markup is None else markup
    return cents_for_cost_usd(raw_cost_usd(usages) * factor)


def usages_from_breakdown(breakdown: Mapping[str, tuple[int, int]]) -> list[ModelUsage]:
    """Build :class:`ModelUsage` rows from a ``model → (input, output)`` mapping.

    Args:
        breakdown: Per-model ``(input_tokens, output_tokens)`` pairs.

    Returns:
        One :class:`ModelUsage` per model, priced wholly per token.
    """
    return [
        ModelUsage(model=model, input_tokens=in_out[0], output_tokens=in_out[1]) for model, in_out in breakdown.items()
    ]


def fallback_priced_usages(usages: Iterable[ModelUsage]) -> list[ModelUsage]:
    """Reprice usage rows wholly at the fallback frontier rates.

    Used when exact pricing failed: the tokens are known but the charge could
    not be computed, so it is recomputed at :data:`FALLBACK_INPUT_COST_PER_TOKEN`
    / :data:`FALLBACK_OUTPUT_COST_PER_TOKEN` — conservative, so a metering fault
    over-charges rather than giving the run away. The cost is carried as a
    reported cost covering every token, so pricing it needs no price lookup
    (the step most likely to have failed).

    Args:
        usages: Per-model usage whose token counts are trusted.

    Returns:
        One row per input row, priced entirely at the fallback rates.
    """
    return [
        ModelUsage(
            model=usage.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            reported_cost_usd=usage.input_tokens * FALLBACK_INPUT_COST_PER_TOKEN
            + usage.output_tokens * FALLBACK_OUTPUT_COST_PER_TOKEN,
            reported_input_tokens=usage.input_tokens,
            reported_output_tokens=usage.output_tokens,
        )
        for usage in usages
        if usage.input_tokens > 0 or usage.output_tokens > 0
    ]


def combine_usages(usages: Iterable[ModelUsage]) -> list[ModelUsage]:
    """Sum usage rows that share a model id into one row per model.

    Args:
        usages: Rows that may repeat a model (e.g. after rekeying ids).

    Returns:
        One row per distinct model, every counter and reported cost summed.
    """
    merged: dict[str, ModelUsage] = {}
    for usage in usages:
        prior = merged.get(usage.model)
        if prior is None:
            merged[usage.model] = usage
            continue
        merged[usage.model] = replace(
            prior,
            **{
                f.name: getattr(prior, f.name) + getattr(usage, f.name) for f in fields(ModelUsage) if f.name != "model"
            },
        )
    return list(merged.values())
