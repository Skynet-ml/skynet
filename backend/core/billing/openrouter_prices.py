"""Live per-model OpenRouter token prices, cached in-process.

OpenRouter is the platform's only managed provider, and its public model
listing (``GET /api/v1/models``) publishes the per-token USD price of every
category a text request can bill: prompt, completion, cache read, cache write
and internal reasoning. LiteLLM's static table lags those prices and has no
cache-discount or reasoning rows for most OpenRouter slugs, so the metered
charge prices from this listing whenever a response did not report its own
``usage.cost``.

The listing is fetched lazily, refreshed hourly, and seeded by the model
catalog build (which already downloads it). A failed fetch is retried at most
once a minute so an OpenRouter outage never adds a network timeout to every
charge. When the listing is out of reach (the offline optimizer sandbox), the
stored list prices of the offered models stand in for it. The module is a
leaf: it imports nothing from the rest of ``core``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

MODELS_URL = "https://openrouter.ai/api/v1/models"
_TTL_SECONDS = 3600.0
_RETRY_SECONDS = 60.0
_TIMEOUT_SECONDS = 3.0


@dataclass(frozen=True)
class TokenPrices:
    """Per-token USD prices of one model's text billing categories.

    A cache or reasoning price of ``None`` means OpenRouter publishes no
    separate rate, so those tokens bill at the plain prompt or completion rate.
    """

    prompt: float
    completion: float
    cache_read: float | None = None
    cache_write: float | None = None
    reasoning: float | None = None


_lock = threading.Lock()
_prices: dict[str, TokenPrices] = {}
_fetched_at = 0.0
_failed_at = 0.0
# Tests flip this off (see core/conftest.py) so pricing never depends on the network.
fetch_enabled = True

# The optimizer sandbox has no network, so it cannot fetch the listing, and
# LiteLLM's bundled table lacks most offered slugs: without these rows every
# proposer cap and cost there used the fallback rate (SKY-269). Copied from
# MODELS_URL on 2026-11-02 for every model the catalog offers; a fresh
# listing always wins.
_LISTED: dict[str, dict[str, str]] = {
    "anthropic/claude-haiku-5.5": {
        "prompt": "0.0000001",
        "completion": "0.0000005",
        "input_cache_read": "0.00000001",
        "input_cache_write": "0.000000125",
    },
    "anthropic/claude-opus-5.5": {
        "prompt": "0.000004",
        "completion": "0.00002",
        "input_cache_read": "0.0000002",
        "input_cache_write": "0.000005",
    },
    "anthropic/claude-sonnet-5.5": {
        "prompt": "0.000002",
        "completion": "0.00001",
        "input_cache_read": "0.0000001",
        "input_cache_write": "0.0000025",
    },
    "cohere/command-a-plus": {"prompt": "0.0000003", "completion": "0.0000015", "input_cache_read": "0.00000015"},
    "deepseek/deepseek-v4-pro-0813": {
        "prompt": "0.000000115",
        "completion": "0.000004",
        "input_cache_read": "0.00000011",
    },
    "google/gemini-3.5-flash-lite": {
        "prompt": "0.0000003",
        "completion": "0.0000025",
        "input_cache_read": "0.00000003",
        "input_cache_write": "0.0000000833333333333333",
        "internal_reasoning": "0.0000025",
    },
    "google/gemini-3.8-flash": {
        "prompt": "0.000000825",
        "completion": "0.000004125",
        "input_cache_read": "0.0000000825",
        "input_cache_write": "0.0000000416666666666667",
        "internal_reasoning": "0.000004125",
    },
    "google/gemini-4-argon": {
        "prompt": "0.000002",
        "completion": "0.00001",
        "input_cache_read": "0.0000001",
        "input_cache_write": "0.0000001875",
        "internal_reasoning": "0.00001",
    },
    "meta/muse-glimmer-30b": {"prompt": "0.0000003", "completion": "0.0000012", "input_cache_read": "0.00000004"},
    "meta/muse-spark-1.3": {"prompt": "0.00000125", "completion": "0.00000425", "input_cache_read": "0.00000015"},
    "minimax/minimax-m3": {"prompt": "0.0000003", "completion": "0.0000012", "input_cache_read": "0.00000006"},
    "minimax/minimax-m3.1-flash": {
        "prompt": "0.000000075",
        "completion": "0.0000003",
        "input_cache_read": "0.0000000075",
    },
    "mistralai/mistral-large-4-0": {"prompt": "0.0000007", "completion": "0.0000021", "input_cache_read": "0.00000007"},
    "mistralai/mistral-small-2603": {
        "prompt": "0.00000015",
        "completion": "0.0000006",
        "input_cache_read": "0.000000015",
    },
    "moonshotai/kimi-k3": {"prompt": "0.000000192", "completion": "0.000015", "input_cache_read": "0.000000182"},
    "moonshotai/kimi-k3.1-pro": {"prompt": "0.000002", "completion": "0.00001", "input_cache_read": "0.0000002"},
    "nvidia/nemotron-3-ultra-550b-a55b": {
        "prompt": "0.0000006",
        "completion": "0.0000024",
        "input_cache_read": "0.00000012",
    },
    "nvidia/nemotron-3.5-lightning": {
        "prompt": "0.00000007",
        "completion": "0.0000002",
        "input_cache_read": "0.00000004",
    },
    "nvidia/nemotron-3.5-super-vl": {
        "prompt": "0.00000015",
        "completion": "0.0000005",
        "input_cache_read": "0.00000002",
    },
    "openai/gpt-6-astra": {
        "prompt": "0.00001",
        "completion": "0.00005",
        "input_cache_read": "0.000001",
        "input_cache_write": "0.0000125",
    },
    "openai/gpt-6-luna": {
        "prompt": "0.0000001",
        "completion": "0.0000005",
        "input_cache_read": "0.00000001",
        "input_cache_write": "0.000000125",
    },
    "openai/gpt-6.1-sol": {
        "prompt": "0.000002",
        "completion": "0.00001",
        "input_cache_read": "0.0000001",
        "input_cache_write": "0.0000025",
    },
    "qwen/qwen3.8-27b": {"prompt": "0.00000015", "completion": "0.000001875", "input_cache_read": "0.0000000375"},
    "qwen/qwen3.8-max-0902": {"prompt": "0.000002", "completion": "0.000006", "input_cache_read": "0.00000025"},
    "stepfun/step-5-preview": {"prompt": "0.000001", "completion": "0.0000027", "input_cache_read": "0.0000001"},
    "thinkingmachines/inkling": {"prompt": "0.000001", "completion": "0.00000405", "input_cache_read": "0.00000017"},
    "thinkingmachines/inkling-small": {
        "prompt": "0.00000045",
        "completion": "0.0000012",
        "input_cache_read": "0.0000001",
    },
    "upstage/solar-pro4": {"prompt": "0.00000015", "completion": "0.0000006", "input_cache_read": "0.00000003"},
    "x-ai/grok-4.7": {"prompt": "0.000002", "completion": "0.000006", "input_cache_read": "0.0000005"},
    "xiaomi/mimo-v2.6-flash": {"prompt": "0.00000014", "completion": "0.00000028", "input_cache_read": "0.0000000028"},
    "xiaomi/mimo-v2.6-pro": {"prompt": "0.000000435", "completion": "0.00000087", "input_cache_read": "0.0000000036"},
    "z-ai/glm-5.3": {"prompt": "0.00000008", "completion": "0.0000036", "input_cache_read": "0.000000076"},
    "z-ai/glm-5.3-flash": {"prompt": "0.0000000285", "completion": "0.0000000923", "input_cache_read": "0.0000000285"},
}


def _rate(pricing: Mapping[str, Any], field: str) -> float | None:
    """Parse one non-negative per-token rate from an OpenRouter pricing dict.

    Args:
        pricing: The ``pricing`` object of a listing row.
        field: The rate's key, e.g. ``"prompt"``.

    Returns:
        The rate in USD per token, or ``None`` when absent, malformed or
        negative (OpenRouter marks router slugs whose price varies with ``-1``).
    """
    try:
        value = float(pricing[field])
    except (KeyError, TypeError, ValueError):
        return None
    return value if value >= 0 else None


def prices_from_pricing(pricing: Any) -> TokenPrices | None:
    """Build :class:`TokenPrices` from a listing row's ``pricing`` object.

    Args:
        pricing: The raw ``pricing`` value of an OpenRouter model row.

    Returns:
        The parsed prices, or ``None`` when the prompt or completion rate is
        missing — a model without both cannot be priced from the listing.
    """
    if not isinstance(pricing, Mapping):
        return None
    prompt = _rate(pricing, "prompt")
    completion = _rate(pricing, "completion")
    if prompt is None or completion is None:
        return None
    return TokenPrices(
        prompt=prompt,
        completion=completion,
        cache_read=_rate(pricing, "input_cache_read"),
        cache_write=_rate(pricing, "input_cache_write"),
        reasoning=_rate(pricing, "internal_reasoning"),
    )


def remember(rows: Iterable[Any]) -> None:
    """Replace the cached prices with a freshly downloaded model listing.

    Args:
        rows: The ``data`` rows of ``GET /api/v1/models``.
    """
    global _fetched_at
    parsed: dict[str, TokenPrices] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("id"), str):
            continue
        prices = prices_from_pricing(row.get("pricing"))
        if prices is not None:
            parsed[row["id"]] = prices
    if not parsed:
        return
    with _lock:
        _prices.clear()
        _prices.update(parsed)
        _fetched_at = time.monotonic()


def _refresh_if_stale() -> None:
    """Download the listing when the cache is older than the TTL.

    Failures are logged and throttled so a down listing costs one timeout a
    minute, not one per charge; the stale cache keeps serving meanwhile.
    """
    global _failed_at
    now = time.monotonic()
    with _lock:
        fresh = _prices and now - _fetched_at < _TTL_SECONDS
        throttled = now - _failed_at < _RETRY_SECONDS
    if not fetch_enabled or fresh or throttled:
        return
    try:
        response = httpx.get(MODELS_URL, timeout=_TIMEOUT_SECONDS)
        response.raise_for_status()
        rows = response.json().get("data", [])
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        with _lock:
            _failed_at = now
        logger.warning("OpenRouter price listing unavailable: %s", exc)
        return
    remember(rows)


def _slug_candidates(model_id: str) -> list[str]:
    """List the OpenRouter slugs a LiteLLM model id may correspond to.

    Args:
        model_id: A LiteLLM id such as ``openrouter/anthropic/claude-x``.

    Returns:
        Candidate listing ids, most specific first.
    """
    name = model_id.removeprefix("litellm_proxy/")
    candidates = [name]
    if name.startswith("openrouter/"):
        candidates.append(name.removeprefix("openrouter/"))
    return candidates


def live_prices(model_id: str) -> TokenPrices | None:
    """Return a model's current OpenRouter prices, refreshing the cache if stale.

    Args:
        model_id: The model id as billed (LiteLLM or bare OpenRouter slug).

    Returns:
        The model's prices, else its stored list prices, or ``None`` when
        neither prices it.
    """
    _refresh_if_stale()
    return cached_prices(model_id)


def cached_prices(model_id: str) -> TokenPrices | None:
    """Read cached or listed prices without refreshing the provider catalog.

    Args:
        model_id: The model id as billed.

    Returns:
        The known token prices, or None.
    """
    candidates = _slug_candidates(model_id)
    with _lock:
        for slug in candidates:
            prices = _prices.get(slug)
            if prices is not None:
                return prices
    for slug in candidates:
        if slug in _LISTED:
            return prices_from_pricing(_LISTED[slug])
    return None


def reset() -> None:
    """Drop the cache and failure throttle, for tests."""
    global _fetched_at, _failed_at
    with _lock:
        _prices.clear()
        _fetched_at = 0.0
        _failed_at = 0.0
