"""Outbound result payloads: terminal outcome, model usage and LM activity."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class TerminalOutcome(BaseModel):
    status: Literal["stopped"] = "stopped"
    stop_reason: Literal["budget_reached"] = "budget_reached"
    result_availability: Literal["evaluated", "none"]
    result: dict[str, Any] | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    message: str


# Per-(LM, stage) cell of the LM activity matrix. Pydantic class docstrings
# are part of the OpenAPI contract — see AGENTS.md "Pydantic class
# docstrings" — so this annotation lives in a comment, not in the class body.
class LMStageStats(BaseModel):
    calls: int = 0
    avg_response_time_ms: float | None = None


# Two-LM × N-stage matrix that BlackboxRunResponse.lm_activity returns.
# Inner dicts are keyed by stage name ("baseline" / "training" /
# "evaluation"); missing keys mean "no calls in that stage". The wire
# shape is stable — the frontend renders rows in a fixed order.
class LMActivity(BaseModel):
    generation: dict[str, LMStageStats] = Field(default_factory=dict)
    reflection: dict[str, LMStageStats] = Field(default_factory=dict)


# Per-model measured token usage (input/output split) stamped onto a run result
# so the billing worker charges per-model and the UI reconciles the pre-run
# estimate against real per-model spend. Pydantic class docstrings are part of
# the OpenAPI contract — see AGENTS.md — so this annotation lives in a comment.
class ModelTokenUsage(BaseModel):
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    # Provider-reported cost of the calls that returned one, and the tokens it
    # covers; the rest is priced per token, with cache and reasoning tokens at
    # their own rates.
    reported_cost_usd: float = 0.0
    reported_input_tokens: int = 0
    reported_output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0


def result_scores(result: dict[str, Any]) -> tuple[Any, Any]:
    """Read a stored result's baseline and optimized scores, whatever job wrote it.

    Runs store the scores of their final run under ``baseline_score`` /
    ``best_score``. The held-out ``*_test_metric`` keys serve only older
    stored results.

    Args:
        result: A stored ``result`` mapping.

    Returns:
        The raw ``(baseline, optimized)`` values, each ``None`` when absent.
    """
    baseline = result.get("baseline_test_metric")
    optimized = result.get("optimized_test_metric")
    return (
        result.get("baseline_score") if baseline is None else baseline,
        result.get("best_score") if optimized is None else optimized,
    )
