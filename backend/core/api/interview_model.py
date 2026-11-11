"""Pick the model agent interviews run on when the user chooses none.

Interviews send short questions and option lists, so the user mostly waits on
time to the first token and a short reply. Every input comes from OpenRouter,
with no list of labs or models written in:

1. Candidates are the catalog's featured models (each leading lab's newest,
   strongest releases, chosen from release dates and benchmarks) that are
   served through OpenRouter, priced and benchmarked.
2. "Small" keeps the cheaper half by input plus output price.
3. Models beaten on benchmark score, price and reply time at once drop out.
4. The fastest remaining model wins. Reply time is the fastest healthy
   endpoint's median time to first token plus a short reply at its median
   throughput, from OpenRouter's last-30-minute endpoint stats.

The pick refreshes in the background every 30 minutes. When the stats can't be
fetched it keeps the last pick, and before the first one it falls back to the
cheapest small candidate.

The same refresh measures every OpenRouter model in the catalog, so the model
picker's Fastest tab sorts on the throughput of each model's fastest healthy
endpoint, and its speed tooltip shows that endpoint's time to the first token
and to a short reply.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from threading import Lock, Thread

from ..config import settings
from .model_catalog import (
    CatalogModel,
    ModelCatalogResponse,
    agent_model_id,
    catalog_snapshot,
    featured_score,
    supports_effort_control,
)

logger = logging.getLogger(__name__)

_OPENROUTER_PREFIX = "openrouter/"
_ENDPOINTS_URL = "https://openrouter.ai/api/v1/models/{model}/endpoints"
_STATS_TTL_SECONDS = 30 * 60
# Tokens in a typical interview reply: one question and a few short options.
_REPLY_TOKENS = 150
# An endpoint that failed more than 1% of requests over the last day would
# stall interviews more than its speed saves.
_MIN_UPTIME_PERCENT = 99.0
_FETCH_TIMEOUT_SECONDS = 6
_FETCH_WORKERS = 16

_lock = Lock()
_pick: str | None = None
_picked_at: float = 0.0
_speeds: dict[str, tuple[float, float]] = {}
_measured_at: float = 0.0
_refresh_in_flight = False


def _price(model: CatalogModel) -> float:
    """Return a model's input plus output price per token.

    Args:
        model: A priced catalog entry.

    Returns:
        The summed per-token price in USD.
    """
    return (model.input_cost_per_token or 0.0) + (model.output_cost_per_token or 0.0)


def small_candidates(catalog: ModelCatalogResponse) -> list[CatalogModel]:
    """Return the featured OpenRouter models in the cheaper half by price.

    Args:
        catalog: A built catalog.

    Returns:
        The candidates, cheapest first; empty when nothing qualifies.
    """
    featured = [
        m
        for m in catalog.models
        if m.featured
        and supports_effort_control(m)
        and m.available
        and m.value.startswith(_OPENROUTER_PREFIX)
        and m.input_cost_per_token is not None
        and m.output_cost_per_token is not None
        and featured_score(m.value) is not None
    ]
    if not featured:
        return []
    median = statistics.median(_price(m) for m in featured)
    return sorted((m for m in featured if _price(m) <= median), key=_price)


def _short_reply_ms(speed: tuple[float, float]) -> float:
    """Return the time a short reply takes at one endpoint's speed.

    Args:
        speed: ``(tokens per second, milliseconds to the first token)``.

    Returns:
        Milliseconds to the first token plus :data:`_REPLY_TOKENS`.
    """
    tokens_per_second, first_token_ms = speed
    return first_token_ms + _REPLY_TOKENS / tokens_per_second * 1000


def endpoint_speed(endpoints: list[dict]) -> tuple[float, float] | None:
    """Return the speed of the healthy endpoint that answers a short reply first.

    Args:
        endpoints: OpenRouter's ``data.endpoints`` list for one model.

    Returns:
        ``(median tokens per second, median milliseconds to the first token)``
        over the last 30 minutes, or ``None`` when no endpoint has stats and
        enough uptime.
    """
    best: tuple[float, float] | None = None
    for endpoint in endpoints:
        latency = endpoint.get("latency_last_30m") or {}
        throughput = endpoint.get("throughput_last_30m") or {}
        uptime = endpoint.get("uptime_last_1d")
        first_token, tokens_per_second = latency.get("p50"), throughput.get("p50")
        if not first_token or not tokens_per_second or uptime is None or uptime < _MIN_UPTIME_PERCENT:
            continue
        speed = (float(tokens_per_second), float(first_token))
        if best is None or _short_reply_ms(speed) < _short_reply_ms(best):
            best = speed
    return best


def reply_ms(endpoints: list[dict]) -> float | None:
    """Return the fastest healthy endpoint's time to a short reply.

    Args:
        endpoints: OpenRouter's ``data.endpoints`` list for one model.

    Returns:
        Milliseconds to the first token plus :data:`_REPLY_TOKENS` at median
        throughput, or ``None`` when no endpoint has stats and enough uptime.
    """
    speed = endpoint_speed(endpoints)
    return _short_reply_ms(speed) if speed else None


def choose(candidates: list[CatalogModel], times: dict[str, float]) -> str | None:
    """Return the fastest candidate no other candidate beats on every axis.

    Args:
        candidates: Output of :func:`small_candidates`.
        times: Measured reply milliseconds by catalog id; unmeasured
            candidates are left out.

    Returns:
        The winning catalog id, or ``None`` when no candidate was measured.
    """
    measured = [(m, featured_score(m.value) or 0.0, _price(m), times[m.value]) for m in candidates if m.value in times]

    def beaten(entry: tuple[CatalogModel, float, float, float]) -> bool:
        _, score, price, ms = entry
        return any(
            o_score >= score and o_price <= price and o_ms <= ms and (o_score, o_price, o_ms) != (score, price, ms)
            for o, o_score, o_price, o_ms in measured
            if o is not entry[0]
        )

    frontier = [entry for entry in measured if not beaten(entry)]
    if not frontier:
        return None
    return min(frontier, key=lambda entry: entry[3])[0].value


def _fetch_endpoints(model_value: str, api_key: str) -> list[dict] | None:
    """Fetch OpenRouter's per-endpoint stats for one model.

    Args:
        model_value: A catalog id under the ``openrouter/`` prefix.
        api_key: The platform's OpenRouter key; without one the stats are empty.

    Returns:
        The endpoint list, or ``None`` when the request fails.
    """
    slug = urllib.parse.quote(model_value.removeprefix(_OPENROUTER_PREFIX), safe="/")
    request = urllib.request.Request(
        _ENDPOINTS_URL.format(model=slug),
        headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_SECONDS) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        logger.warning("OpenRouter endpoint stats for %s failed: %s", model_value, exc)
        return None
    endpoints = (body.get("data") or {}).get("endpoints") if isinstance(body, dict) else None
    return endpoints if isinstance(endpoints, list) else None


def _measure(models: list[CatalogModel]) -> dict[str, tuple[float, float]]:
    """Measure each model's speed from OpenRouter's endpoint stats.

    Args:
        models: OpenRouter catalog entries.

    Returns:
        :func:`endpoint_speed` by catalog id, for the models with stats.
    """
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key or not models:
        return {}
    with ThreadPoolExecutor(max_workers=min(_FETCH_WORKERS, len(models))) as pool:
        listings = list(pool.map(lambda m: _fetch_endpoints(m.value, api_key), models))
    speeds: dict[str, tuple[float, float]] = {}
    for model, endpoints in zip(models, listings, strict=True):
        speed = endpoint_speed(endpoints or [])
        if speed is not None:
            speeds[model.value] = speed
    return speeds


def _refresh() -> None:
    """Recompute the pick and store it; never raises."""
    global _pick, _picked_at, _measured_at, _refresh_in_flight
    try:
        catalog = catalog_snapshot()
        if catalog is not None:
            speeds = _measure([m for m in catalog.models if m.value.startswith(_OPENROUTER_PREFIX)])
            candidates = small_candidates(catalog)
            winner = choose(candidates, {value: _short_reply_ms(speed) for value, speed in speeds.items()})
            with _lock:
                _measured_at = time.monotonic()
                # Merge so a model whose fetch failed this round keeps its last speed.
                _speeds.update(speeds)
                if winner:
                    _pick = winner
                    _picked_at = time.monotonic()
                    logger.info("Interview model picked: %s", winner)
    except Exception:
        logger.exception("Interview model refresh failed; keeping the previous pick")
    finally:
        with _lock:
            _refresh_in_flight = False


def _kick_refresh() -> None:
    """Start a background refresh unless one is running."""
    global _refresh_in_flight
    with _lock:
        if _refresh_in_flight:
            return
        _refresh_in_flight = True
    Thread(target=_refresh, name="interview-model-refresh", daemon=True).start()


def interview_model_id() -> str:
    """Return the model an agent interview runs when the user picks none.

    Never blocks: a stale or missing pick starts a background refresh and the
    caller gets the last pick meanwhile.

    Every composer must show and run one default, so the catalog's
    best-value default wins over the fast pick whenever the catalog flags one.

    Returns:
        ``INTERVIEW_AGENT_MODEL`` when set, else the catalog's best-value
        default, else the measured pick, else the cheapest small featured
        model, else the agent default.
    """
    if settings.interview_agent_model.strip():
        return settings.interview_agent_model.strip()
    snapshot = catalog_snapshot()
    default = next((m.value for m in snapshot.models if m.is_default), None) if snapshot is not None else None
    if default:
        return default
    with _lock:
        pick, stale = _pick, time.monotonic() - _picked_at >= _STATS_TTL_SECONDS
    if (
        pick
        and snapshot is not None
        and not any(model.value == pick and supports_effort_control(model) for model in snapshot.models)
    ):
        pick = None
    if pick is None or stale:
        _kick_refresh()
    if pick:
        return pick
    catalog = catalog_snapshot()
    cheapest = small_candidates(catalog) if catalog is not None else []
    return cheapest[0].value if cheapest else agent_model_id(settings.code_agent_model)


def measured_speeds() -> dict[str, tuple[float, float]]:
    """Return the last measured speed of each catalog model, without waiting.

    Returns:
        ``(tokens per second, milliseconds to the first token)`` by catalog id;
        empty before the first measurement. A stale set starts a background
        refresh and is still returned.
    """
    with _lock:
        speeds, stale = dict(_speeds), time.monotonic() - _measured_at >= _STATS_TTL_SECONDS
    if stale:
        _kick_refresh()
    return speeds


def with_interview_default(catalog: ModelCatalogResponse) -> ModelCatalogResponse:
    """Flag the model interviews run by default and attach each model's measured speed.

    Args:
        catalog: A cached catalog, left unmodified.

    Returns:
        A copy whose ``is_interview_default`` marks the current pick and whose
        ``throughput_tps``, ``ttft_ms`` and ``latency_ms`` carry the last measured
        speed of the model's fastest healthy endpoint.
    """
    pick = interview_model_id()
    with _lock:
        speeds, stale = dict(_speeds), time.monotonic() - _measured_at >= _STATS_TTL_SECONDS
    # A pinned INTERVIEW_AGENT_MODEL skips the pick's own refresh, yet the
    # picker's speeds still need one.
    if stale:
        _kick_refresh()
    models = []
    for m in catalog.models:
        speed = speeds.get(m.value)
        update = {"is_interview_default": m.value == pick, "throughput_tps": None, "ttft_ms": None, "latency_ms": None}
        if speed is not None:
            update.update(throughput_tps=speed[0], ttft_ms=speed[1], latency_ms=_short_reply_ms(speed))
        models.append(m.model_copy(update=update))
    return catalog.model_copy(update={"models": models})
