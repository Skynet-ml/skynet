"""Tests for the per-job cost ceiling callback.

The callback re-prices accumulated per-model LM usage after each call and
hard-stops the run once its full per-model cost exceeds the cap. These
exercise the trip boundary, the no-ceiling inert case, multi-LM totalling, and
the latch — with budgets set relative to the pricing engine so the assertions
don't hinge on exact per-token arithmetic.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.billing.pricing import cents_for_usage, usages_from_breakdown
from core.service_gateway.language_models import usage_by_model_from_history
from core.service_gateway.optimization.cost_ceiling import (
    CostCeilingCallback,
    CostCeilingExceededError,
)


class _FakeLM:
    """Minimal stand-in exposing a growing ``history`` and a ``model`` like ``dspy.LM``."""

    def __init__(self, model: str = "test/ceiling") -> None:
        """Start with an empty call history on an unpriced model (default costs).

        Args:
            model: Model id the LM reports; the default is unpriced so per-token
                costs fall back to the engine defaults, keeping pricing stable.
        """
        self.history: list[dict[str, Any]] = []
        self.model = model

    def record(self, input_tokens: int) -> None:
        """Append one history entry reporting ``input_tokens`` of prompt usage.

        Args:
            input_tokens: Prompt-token usage to stamp on the new history row.
        """
        self.history.append({"usage": {"prompt_tokens": input_tokens, "completion_tokens": 0}})


def _cents(*lms: _FakeLM) -> int:
    """Return the per-model cost, in cents, of the LMs' accumulated usage."""
    breakdown = usage_by_model_from_history(*lms)
    return cents_for_usage(usages_from_breakdown(breakdown)) if breakdown else 0


def test_does_not_trip_while_usage_within_budget() -> None:
    """Usage that prices below the cents cap never raises."""
    lm = _FakeLM()
    cb = CostCeilingCallback(1000, lm)
    lm.record(50_000)
    cb.on_lm_end("c1", outputs={})
    lm.record(50_000)
    cb.on_lm_end("c2", outputs={})  # ~15 cents, well under the 1000-cent cap


def test_trips_once_cost_exceeds_budget() -> None:
    """The first call that prices the run past the cents cap raises."""
    first_chunk = _FakeLM()
    first_chunk.record(50_000)
    budget = _cents(first_chunk)  # cap == the cost of the first chunk alone
    assert budget > 0

    lm = _FakeLM()
    cb = CostCeilingCallback(budget, lm)
    lm.record(50_000)
    cb.on_lm_end("c1", outputs={})  # used == cap → no trip
    lm.record(50_000)
    with pytest.raises(CostCeilingExceededError):
        cb.on_lm_end("c2", outputs={})  # now over the cap


def test_latches_after_tripping() -> None:
    """Once tripped every later boundary continues unwinding the stopped run."""
    lm = _FakeLM()
    cb = CostCeilingCallback(5, lm)
    lm.record(100_000)  # ~15 cents > 5
    with pytest.raises(CostCeilingExceededError):
        cb.on_lm_end("c1", outputs={})
    with pytest.raises(CostCeilingExceededError):
        cb.on_lm_end("c2", outputs={})


def test_totals_cost_across_generation_and_reflection() -> None:
    """The ceiling prices usage across every bound LM, not just the generation LM."""
    gen = _FakeLM()
    refl = _FakeLM()
    gen.record(50_000)
    refl.record(50_000)
    combined = _cents(gen, refl)
    # Each LM alone prices below the cap; only their summed cost trips it.
    assert _cents(gen) < combined
    cb = CostCeilingCallback(combined - 1, gen, refl)
    with pytest.raises(CostCeilingExceededError):
        cb.on_lm_end("c1", outputs={})


def test_none_lm_is_tolerated() -> None:
    """A ``None`` LM (no reflection model) is skipped, not an error."""
    gen = _FakeLM()
    cb = CostCeilingCallback(5, gen, None)
    gen.record(100_000)  # ~15 cents > 5
    with pytest.raises(CostCeilingExceededError):
        cb.on_lm_end("c1", outputs={})


def test_non_positive_budget_is_inert() -> None:
    """A zero/negative cap disables the ceiling entirely."""
    lm = _FakeLM()
    cb = CostCeilingCallback(0, lm)
    lm.record(10_000_000)
    cb.on_lm_end("c1", outputs={})


def test_no_usage_does_not_trip() -> None:
    """Untracked usage (no history rows) reads as 'not tracked' and never trips."""
    lm = _FakeLM()
    cb = CostCeilingCallback(100, lm)
    cb.on_lm_end("c1", outputs={})
