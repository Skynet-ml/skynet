"""Pick the model that writes the composer's next-message suggestion.

A suggestion is one short line that shows after each agent reply, so it must
be quick and cheap, yet good enough that the user takes it. It never runs on
the composer, interview or agent model. Every route uses the same weighted
score of speed, cost and Artificial Analysis intelligence (see
:func:`score_models`), over the models that route can run:

- Hosted: the catalog models the platform holds a key for.
- Mac app on an OpenRouter key: every OpenRouter catalog model, on that key.
- Mac app on a CLI subscription: the catalog models that CLI runs.

Picks read only data the server already holds (the cached catalog and the
interview refresh's measured speeds), so a pick never waits on the network.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass

from ..local.cli_catalog import CLI_MODELS, CliProvider
from .interview_model import measured_speeds
from .model_catalog import CatalogModel, catalog_snapshot

logger = logging.getLogger(__name__)

# The user reads a suggestion only if it shows before they start typing, and
# it runs after every reply, so speed and cost count as much as quality. The
# intelligence weight keeps a fast, cheap model that writes poor suggestions
# from winning.
WEIGHT_SPEED = 0.35
WEIGHT_COST = 0.30
WEIGHT_INTELLIGENCE = 0.35

# One call sends the recent conversation (about 1,500 tokens) and asks for one
# line (about 60 tokens); speed and cost are scored for that call shape.
SUGGEST_PROMPT_TOKENS = 1500
SUGGEST_REPLY_TOKENS = 60

_OPENROUTER_PREFIX = "openrouter/"
_PICK_TTL_SECONDS = 10 * 60

HOSTED_ROUTE = "hosted"
BYOK_ROUTE = "byok"

# The Mac app has no platform provider key, so its catalog is empty: no
# benchmark, no price. A CLI or a saved key still runs these small, fast
# models, so those routes fall back to them instead of showing no suggestion.
# The hosted route has no fallback, because the platform serves only what its
# own catalog lists.
_FIXED_FALLBACKS = {
    "claude": _OPENROUTER_PREFIX + "anthropic/claude-haiku-5.5",
    "codex": _OPENROUTER_PREFIX + "openai/gpt-6-luna",
    BYOK_ROUTE: _OPENROUTER_PREFIX + "anthropic/claude-haiku-5.5",
}

_lock = threading.Lock()
_picks: dict[str, tuple[str, float]] = {}


@dataclass(frozen=True)
class SuggestScore:
    """One candidate's normalized scores, each from 0 (worst) to 1 (best)."""

    value: str
    total: float
    speed: float | None
    cost: float
    intelligence: float
    reply_ms: float | None
    call_cost_usd: float
    intelligence_index: float


def _call_cost(model: CatalogModel) -> float:
    """Return the price of one suggestion call on ``model``.

    Args:
        model: A priced catalog entry.

    Returns:
        USD for the typical prompt and reply sizes.
    """
    return (model.input_cost_per_token or 0.0) * SUGGEST_PROMPT_TOKENS + (
        model.output_cost_per_token or 0.0
    ) * SUGGEST_REPLY_TOKENS


def _reply_ms(speed: tuple[float, float]) -> float:
    """Return the milliseconds a suggestion takes at one measured speed.

    Args:
        speed: ``(tokens per second, milliseconds to the first token)``.

    Returns:
        Time to the first token plus the reply at that throughput.
    """
    tokens_per_second, first_token_ms = speed
    return first_token_ms + SUGGEST_REPLY_TOKENS / tokens_per_second * 1000


def _scaler(values: Sequence[float], *, higher_is_better: bool) -> Callable[[float], float]:
    """Return a min-max scaler onto 0..1 for ``values``.

    Args:
        values: The candidates' raw values.
        higher_is_better: Whether the largest value scores 1.

    Returns:
        A function from a raw value to its score; 1 for every value when all
        values are equal.
    """
    low, high = min(values), max(values)

    def scale(value: float) -> float:
        """Map ``value`` onto 0..1.

        Args:
            value: One raw value from ``values``.

        Returns:
            The score.
        """
        if high == low:
            return 1.0
        ratio = (value - low) / (high - low)
        return ratio if higher_is_better else 1.0 - ratio

    return scale


def _eligible(model: CatalogModel, *, platform_key: bool) -> bool:
    """Return whether a suggestion can be scored on ``model``.

    Args:
        model: A catalog entry.
        platform_key: Whether the platform's own key must serve it (hosted).

    Returns:
        ``True`` for a priced, benchmarked OpenRouter model that can answer
        without thinking, and that the platform serves when it must.
    """
    return (
        (model.available or not platform_key)
        and model.value.startswith(_OPENROUTER_PREFIX)
        and model.input_cost_per_token is not None
        and model.output_cost_per_token is not None
        and model.intelligence_index is not None
        # A model that must think spends the 60-token reply on reasoning.
        and not model.reasoning_mandatory
    )


def score_models(
    models: Sequence[CatalogModel],
    speeds: dict[str, tuple[float, float]],
    *,
    allowed: Collection[str] | None = None,
    platform_key: bool = True,
) -> list[SuggestScore]:
    """Score each eligible model for suggestions, best first.

    Reply time and call cost span orders of magnitude, so they are compared on
    a log scale; the intelligence index is compared as it is. With no measured
    speed for any candidate, the speed weight drops out and the other two
    weights are rescaled.

    Args:
        models: Catalog entries.
        speeds: Measured ``(tokens per second, ms to first token)`` by id.
        allowed: Catalog ids the route can run, or ``None`` for any.
        platform_key: Whether the platform's own key must serve the model.

    Returns:
        The scores, highest total first, ties broken by model id; empty when
        no model qualifies.
    """
    candidates = [
        m for m in models if _eligible(m, platform_key=platform_key) and (allowed is None or m.value in allowed)
    ]
    if any(m.value in speeds for m in candidates):
        # A model with no measured speed cannot be compared on speed, and a
        # guessed speed would let it win on a number nobody measured.
        candidates = [m for m in candidates if m.value in speeds]
    if not candidates:
        return []
    timed = all(m.value in speeds for m in candidates)
    costs = {m.value: _call_cost(m) for m in candidates}
    cost_score = _scaler([math.log(max(c, 1e-12)) for c in costs.values()], higher_is_better=False)
    intel_score = _scaler([m.intelligence_index or 0.0 for m in candidates], higher_is_better=True)
    times = {m.value: _reply_ms(speeds[m.value]) for m in candidates} if timed else {}
    speed_score = _scaler([math.log(t) for t in times.values()], higher_is_better=False) if timed else None
    weight_total = WEIGHT_COST + WEIGHT_INTELLIGENCE + (WEIGHT_SPEED if timed else 0.0)

    scores: list[SuggestScore] = []
    for m in candidates:
        cost = cost_score(math.log(max(costs[m.value], 1e-12)))
        intelligence = intel_score(m.intelligence_index or 0.0)
        speed = speed_score(math.log(times[m.value])) if speed_score is not None else None
        total = (WEIGHT_COST * cost + WEIGHT_INTELLIGENCE * intelligence + WEIGHT_SPEED * (speed or 0.0)) / weight_total
        scores.append(
            SuggestScore(
                value=m.value,
                total=total,
                speed=speed,
                cost=cost,
                intelligence=intelligence,
                reply_ms=times.get(m.value),
                call_cost_usd=costs[m.value],
                intelligence_index=m.intelligence_index or 0.0,
            )
        )
    scores.sort(key=lambda s: (-s.total, s.value))
    return scores


def _route_scope(route: str) -> tuple[Collection[str] | None, bool]:
    """Return the models a route can run and whether the platform key serves them.

    Args:
        route: :data:`HOSTED_ROUTE`, :data:`BYOK_ROUTE` or a CLI name.

    Returns:
        ``(allowed catalog ids or None for any, platform_key)``.
    """
    if route == HOSTED_ROUTE:
        return None, True
    if route == BYOK_ROUTE:
        return None, False
    return {_OPENROUTER_PREFIX + slug for slug in CLI_MODELS[route]}, False  # type: ignore[index]


def _cli_fallback(models: Sequence[CatalogModel], allowed: Collection[str]) -> str | None:
    """Return a CLI's cheapest priced model, for a catalog with no benchmark for that CLI.

    Args:
        models: Catalog entries.
        allowed: The CLI's catalog ids.

    Returns:
        The cheapest id by input plus output price, ties by id, or ``None``.
    """
    priced = [
        m
        for m in models
        if m.value in allowed and m.input_cost_per_token is not None and m.output_cost_per_token is not None
    ]
    if not priced:
        return None
    return min(priced, key=lambda m: (_call_cost(m), m.value)).value


def suggest_model_for(route: str) -> str | None:
    """Return a route's suggestion model, from one cache refreshed every 10 minutes.

    Args:
        route: :data:`HOSTED_ROUTE`, :data:`BYOK_ROUTE` or a CLI name
            (``claude`` or ``codex``).

    Returns:
        The best-scored catalog id (``openrouter/...``). With no
        benchmarked model, a CLI gets its cheapest priced model, because its
        calls cost the user nothing extra; then a CLI or the key route gets
        its fixed small model, also with no catalog. The hosted route gets
        ``None`` and no suggestion.
    """
    with _lock:
        cached = _picks.get(route)
    if cached is not None and time.monotonic() - cached[1] < _PICK_TTL_SECONDS:
        return cached[0]
    catalog = catalog_snapshot()
    models = catalog.models if catalog is not None else []
    allowed, platform_key = _route_scope(route)
    scores = score_models(models, measured_speeds(), allowed=allowed, platform_key=platform_key)
    if scores:
        best = scores[0]
        pick: str | None = best.value
        logger.info(
            "Suggestion model for %s: %s (score %.3f, speed %s, cost %.3f, intelligence %.3f)",
            route,
            best.value,
            best.total,
            "n/a" if best.speed is None else f"{best.speed:.3f}",
            best.cost,
            best.intelligence,
        )
    else:
        pick = _cli_fallback(models, allowed) if allowed is not None else None
        pick = pick or _FIXED_FALLBACKS.get(route)
        logger.log(
            logging.INFO if pick else logging.WARNING,
            "No suggestion model for %s has Artificial Analysis data; using %s",
            route,
            pick or "no suggestion",
        )
    if pick is not None and catalog is not None:
        with _lock:
            _picks[route] = (pick, time.monotonic())
    return pick


def hosted_suggest_model() -> str | None:
    """Return the hosted suggestion model.

    Returns:
        See :func:`suggest_model_for`.
    """
    return suggest_model_for(HOSTED_ROUTE)


def byok_suggest_model() -> str | None:
    """Return the Mac app's suggestion model on the user's OpenRouter key.

    Returns:
        See :func:`suggest_model_for`.
    """
    return suggest_model_for(BYOK_ROUTE)


def cli_suggest_model(cli: CliProvider) -> str | None:
    """Return a CLI's suggestion model as a CLI slug.

    Args:
        cli: The CLI.

    Returns:
        A ``CLI_MODELS`` slug such as ``anthropic/claude-haiku-5.5``, or ``None``.
    """
    pick = suggest_model_for(cli)
    return pick.removeprefix(_OPENROUTER_PREFIX) if pick else None
