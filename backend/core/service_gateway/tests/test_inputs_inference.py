"""Tests for the submission wizard's step-2 inputs inference.

The predictor runs on a scripted ``DummyLM``, so the whole path (prompt,
parse, validation against the real columns, the deterministic split and
held-out count) is covered without a network call.
"""

from __future__ import annotations

from typing import Any

import pytest
from dspy.utils import DummyLM

from core.models import MIN_PAIRS_FOR_HELD_OUT, InputsInferenceRequest
from core.models.common import SplitFractions
from core.service_gateway.agents import inputs_inference
from core.service_gateway.agents.inputs_inference import (
    held_out_count,
    infer_inputs,
    suggested_split,
)


def _stub_lm(monkeypatch: pytest.MonkeyPatch, answer: dict[str, str] | None) -> None:
    """Make the predictor answer ``answer``, or fail when it is ``None``.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        answer: Output fields the stub LM returns.
    """

    def build(*_args: Any, **_kwargs: Any) -> DummyLM:
        """Return the scripted LM, or raise like a provider outage."""
        if answer is None:
            raise RuntimeError("provider down")
        return DummyLM([answer])

    monkeypatch.setattr(inputs_inference, "_build_agent_lm", build)


def _request(**overrides: Any) -> InputsInferenceRequest:
    """Build a pairs-shaped request with ``overrides`` applied."""
    base: dict[str, Any] = {
        "objective": "Classify support tickets.",
        "artifact_kind": "dspy_program",
        "columns": ["ticket", "label"],
        "sample_rows": [{"ticket": "refund please", "label": "billing"}],
        "row_count": 40,
        "locale": "en",
    }
    return InputsInferenceRequest(**{**base, **overrides})


def _answer(**overrides: str) -> dict[str, str]:
    """Build the stub LM's output fields."""
    base = {
        "mode": "pairs",
        "input_fields_json": '["ticket"]',
        "expected_field": "label",
        "single_input_json": "{}",
        "rationale": "Each row is a ticket with its label.",
    }
    return {**base, **overrides}


def test_pairs_mode_picks_columns_and_holds_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Many labeled rows become pairs with the default split and a held-out share."""
    _stub_lm(monkeypatch, _answer())
    result = infer_inputs(_request())
    assert result.mode == "pairs"
    assert result.input_fields == ["ticket"]
    assert result.expected_field == "label"
    assert result.single_input is None
    assert result.suggested_split == SplitFractions()
    assert result.held_out_count == 6
    assert result.warnings == []
    assert result.rationale == "Each row is a ticket with its label."


def test_single_mode_builds_the_one_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single-input task carries the one input and holds nothing out."""
    _stub_lm(
        monkeypatch,
        _answer(
            mode="single",
            input_fields_json='["document"]',
            expected_field="",
            single_input_json='{"document": "The report.", "junk": 1}',
        ),
    )
    result = infer_inputs(_request(columns=["document"], sample_rows=[{"document": "The report."}], row_count=1))
    assert result.mode == "single"
    assert result.input_fields == ["document"]
    assert result.expected_field is None
    assert result.single_input == {"document": "The report."}
    assert result.held_out_count == 0


def test_unknown_columns_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invented columns never survive; the expected field is never an input."""
    _stub_lm(monkeypatch, _answer(input_fields_json='["ghost", "label"]', expected_field="label"))
    result = infer_inputs(_request())
    assert result.input_fields == ["ticket"]
    assert result.expected_field == "label"


def test_invented_expected_field_is_cleared(monkeypatch: pytest.MonkeyPatch) -> None:
    """An expected field that is not a real column is dropped."""
    _stub_lm(monkeypatch, _answer(expected_field="answer"))
    assert infer_inputs(_request()).expected_field is None


def test_few_pairs_hold_nothing_out_and_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Below the minimum, the test share is zero and the user is warned."""
    _stub_lm(monkeypatch, _answer())
    result = infer_inputs(_request(row_count=MIN_PAIRS_FOR_HELD_OUT - 1))
    assert result.mode == "pairs"
    assert result.suggested_split.test == 0.0
    assert result.suggested_split.train == pytest.approx(0.85)
    assert result.held_out_count == 0
    assert len(result.warnings) == 1
    assert result.warnings[0].strip()


def test_pairs_with_one_row_falls_back_to_single(monkeypatch: pytest.MonkeyPatch) -> None:
    """One row cannot be pairs, whatever the model says."""
    _stub_lm(monkeypatch, _answer())
    result = infer_inputs(_request(row_count=1))
    assert result.mode == "single"
    assert result.single_input == {"ticket": "refund please"}


def test_model_failure_falls_back_to_a_column_guess(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed call still proposes something: pairs over every column."""
    _stub_lm(monkeypatch, None)
    result = infer_inputs(_request())
    assert result.mode == "pairs"
    assert result.input_fields == ["ticket", "label"]
    assert result.expected_field is None
    assert result.held_out_count == 6


@pytest.mark.parametrize(
    ("mode", "rows", "expected"),
    [
        ("single", 1, 0),
        ("pairs", MIN_PAIRS_FOR_HELD_OUT - 1, 0),
        ("pairs", MIN_PAIRS_FOR_HELD_OUT, 1),
        ("pairs", 10, 2),
        ("pairs", 100, 15),
    ],
)
def test_held_out_count_math(mode: str, rows: int, expected: int) -> None:
    """The held-out count is deterministic: rounded share, at least one, never all."""
    assert held_out_count(mode, rows, suggested_split(mode, rows)) == expected  # type: ignore[arg-type]


def test_held_out_count_never_takes_every_case() -> None:
    """Even an all-test split leaves one case to optimize on."""
    split = SplitFractions(train=0.0, val=0.0, test=1.0)
    assert held_out_count("pairs", 6, split) == 5
