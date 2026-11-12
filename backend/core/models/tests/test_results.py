"""Tests for the LM activity result models."""

from __future__ import annotations

import pytest

from core.models.results import LMActivity, LMStageStats


def test_lm_stage_stats_defaults() -> None:
    """``LMStageStats`` defaults to zero calls and no recorded average."""
    s = LMStageStats()
    assert s.calls == 0
    assert s.avg_response_time_ms is None


def test_lm_activity_round_trips_through_json() -> None:
    """``LMActivity`` survives a dump+load cycle preserving per-stage stats."""
    activity = LMActivity(
        generation={
            "baseline": LMStageStats(calls=3, avg_response_time_ms=120.5),
            "training": LMStageStats(calls=10, avg_response_time_ms=180.0),
        },
        reflection={
            "training": LMStageStats(calls=4, avg_response_time_ms=900.0),
        },
    )
    payload = activity.model_dump()
    restored = LMActivity.model_validate(payload)

    assert restored.generation["baseline"].calls == 3
    assert restored.generation["baseline"].avg_response_time_ms == pytest.approx(120.5)
    assert restored.generation["training"].calls == 10
    assert restored.reflection["training"].calls == 4
    assert "evaluation" not in restored.generation
    assert "baseline" not in restored.reflection
