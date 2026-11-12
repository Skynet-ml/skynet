"""Tests for the run-outcome telemetry helper."""

from __future__ import annotations

import pytest

from ...billing.budgets import BudgetInsufficientError
from ...exceptions import InfrastructureInterruptionError
from ..run_outcome import (
    FAILURE_STAGE_BILLING,
    FAILURE_STAGE_OPTIMIZE,
    FAILURE_STAGE_SANDBOX,
    FAILURE_STAGE_UNKNOWN,
    completed_properties,
    failed_properties,
    is_sample_dataset,
    payload_rows,
    score_facts,
)

_SAMPLES = {
    "sentiment": {
        "input_columns": ["text"],
        "output_columns": ["label"],
        "rows": [
            {"text": "great product", "label": "positive"},
            {"text": "broke in a day", "label": "negative"},
            {"text": "it is fine", "label": "neutral"},
        ],
    },
    "qa": {
        "input_columns": ["question"],
        "output_columns": ["answer"],
        "rows": [{"question": "Capital of France?", "answer": "Paris"}],
    },
}


def test_blackbox_result_uses_report_held_out_flag() -> None:
    """A unified black-box result takes held_out from its split report."""
    split = {"train": 20, "val": 5, "test": 22, "selection_split": "val", "report_split": "test"}
    facts = score_facts({"baseline_score": 0.4, "best_score": 0.7, "split_counts": {**split, "held_out": True}})
    assert facts["baseline"] == 0.4
    assert facts["optimized"] == 0.7
    assert facts["lift"] == pytest.approx(0.3)
    assert facts["held_out"] is True
    assert facts["held_out_rows"] == 22

    unheld = score_facts({"baseline_score": 0.4, "best_score": 0.7, "split_counts": {**split, "held_out": False}})
    assert unheld["held_out"] is False


def test_legacy_blackbox_result_has_no_held_out_evidence() -> None:
    """A black-box result without a split carries scores but no held-out rows."""
    facts = score_facts({"baseline_score": 0.1, "best_score": 0.3})
    assert facts["lift"] == pytest.approx(0.2)
    assert facts["held_out"] is False
    assert facts["held_out_rows"] is None


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"best_score": 0.9},
        {"baseline_score": None, "best_score": None},
        None,
    ],
)
def test_missing_scores_yield_null_lift(result) -> None:
    """A result without both scores reports lift as null, never 0."""
    assert score_facts(result)["lift"] is None


def test_boolean_scores_are_not_numbers() -> None:
    """A stray boolean score is ignored rather than read as 0/1."""
    assert score_facts({"baseline_score": True, "best_score": 0.5})["lift"] is None


def test_sample_rows_match_in_every_payload_shape() -> None:
    """A bundled sample is recognised whether staged as cases or unified cases."""
    rows = _SAMPLES["sentiment"]["rows"]
    blackbox_payload = {"cases": [dict(row) for row in rows[:2]]}
    unified_payload = {
        "data": {
            "cases": [{"input": {"text": row["text"]}, "expected": {"label": row["label"]}} for row in rows],
        }
    }
    for payload in (blackbox_payload, unified_payload):
        assert is_sample_dataset(payload_rows(payload), _SAMPLES) is True


def test_user_upload_is_not_a_sample() -> None:
    """A user dataset, or one that only partly overlaps a sample, is not a sample."""
    upload = {"cases": [{"text": "my own row", "label": "positive"}]}
    assert is_sample_dataset(payload_rows(upload), _SAMPLES) is False
    mixed = {"cases": [*_SAMPLES["sentiment"]["rows"], {"text": "extra row", "label": "neutral"}]}
    assert is_sample_dataset(payload_rows(mixed), _SAMPLES) is False
    assert is_sample_dataset(payload_rows({}), _SAMPLES) is False


def test_completed_properties_merge_scores_and_sample_flag() -> None:
    """``completed_properties`` carries every run_completed fact."""
    props = completed_properties(
        {"baseline_score": 0.5, "best_score": 0.6},
        {"cases": _SAMPLES["qa"]["rows"]},
        _SAMPLES,
    )
    assert props["from_sample"] is True
    assert set(props) == {"baseline", "optimized", "lift", "held_out", "held_out_rows", "from_sample"}


def test_sandbox_and_optimizer_errors_get_different_stages() -> None:
    """A sandbox failure and an optimizer failure land in different stages, with class names only."""
    sandbox = failed_properties(InfrastructureInterruptionError("vercel host pressure secret-token"), "optimize")
    optimizer = failed_properties(RuntimeError("model said something private"), "optimize")
    assert sandbox == {"failure_stage": FAILURE_STAGE_SANDBOX, "error_class": "InfrastructureInterruptionError"}
    assert optimizer == {"failure_stage": FAILURE_STAGE_OPTIMIZE, "error_class": "RuntimeError"}
    for props in (sandbox, optimizer):
        assert not any("secret" in str(value) or "private" in str(value) for value in props.values())


def test_sandbox_setup_phase_is_reported() -> None:
    """An error raised while the sandbox is being prepared is a sandbox failure."""
    assert failed_properties(ValueError("bad image"), "sandbox")["failure_stage"] == FAILURE_STAGE_SANDBOX


def test_billing_errors_classify_as_billing() -> None:
    """Budget errors are billing failures whatever phase they hit."""
    props = failed_properties(BudgetInsufficientError("no funds"), "optimize")
    assert props == {"failure_stage": FAILURE_STAGE_BILLING, "error_class": "BudgetInsufficientError"}


def test_remote_error_class_wins_over_wrapper() -> None:
    """A child-process failure reports the child's class, not the re-raised wrapper."""
    exc = RuntimeError("wrapped")
    exc.remote_error_class = "ScorerError"  # type: ignore[attr-defined]
    props = failed_properties(exc, "optimize")
    assert props == {"failure_stage": "evaluate", "error_class": "ScorerError"}


def test_unknown_phase_and_missing_exception() -> None:
    """An unrecognised phase is unknown; a failure without an exception has no class."""
    assert failed_properties(RuntimeError("x"), None)["failure_stage"] == FAILURE_STAGE_UNKNOWN
    assert failed_properties(None, "optimize") == {"failure_stage": FAILURE_STAGE_OPTIMIZE, "error_class": None}
