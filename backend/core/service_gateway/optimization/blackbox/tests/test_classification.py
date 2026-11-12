"""Tests for the typed hook and judge classification schema."""

from __future__ import annotations

import json

import pytest

from core.service_gateway.optimization.blackbox.classification import (
    HOOK_LABELS,
    JUDGE_LABELS,
    ClassificationError,
    HookClassification,
    JudgeClassification,
    answer_format,
    json_schema,
    parse_hook_reply,
    parse_judge_reply,
)


def _reply(label: object, confidence: object = 0.9, reason: object = "Reads the scorer's labels.") -> str:
    """Build one model reply as JSON text.

    Args:
        label: The label field.
        confidence: The confidence field.
        reason: The reason field.

    Returns:
        The reply text.
    """
    return json.dumps({"label": label, "confidence": confidence, "reason": reason})


def test_label_sets_are_the_decided_ones() -> None:
    """The hook and the judge each have their own fixed label set."""
    assert HOOK_LABELS == ("ok", "off_intent", "breaks_rule")
    assert JUDGE_LABELS == ("exploit", "valid")


@pytest.mark.parametrize("label", ["ok", "off_intent", "breaks_rule"])
def test_hook_reply_parses_every_hook_label(label: str) -> None:
    """Each hook label turns into a typed hook result."""
    result = parse_hook_reply(_reply(label, 0.75, "  Edits the test file.  "))
    assert result == HookClassification(label=label, confidence=0.75, reason="Edits the test file.")


@pytest.mark.parametrize("label", ["exploit", "valid"])
def test_judge_reply_parses_every_judge_label(label: str) -> None:
    """Each judge label turns into a typed judge result."""
    result = parse_judge_reply(_reply(label, 1))
    assert result == JudgeClassification(label=label, confidence=1.0, reason="Reads the scorer's labels.")
    assert isinstance(result.confidence, float)


def test_reply_inside_prose_and_code_fence_parses() -> None:
    """Models wrap the answer in prose or a fence; the answer object is still found."""
    text = f"I checked the diff {{not json}}.\n```json\n{_reply('exploit', 0.8)}\n```\nDone."
    assert parse_judge_reply(text).label == "exploit"


def test_label_case_and_spaces_are_normalized() -> None:
    """A label in other case or with spaces still matches."""
    assert parse_judge_reply(_reply(" Valid ")).label == "valid"


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        (None, "no JSON object"),
        ("", "no JSON object"),
        ("The work looks fine.", "no JSON object"),
        (_reply("ok"), "label"),
        (_reply("maybe"), "label"),
        (_reply(None), "label"),
        (_reply("exploit", 1.2), "confidence"),
        (_reply("exploit", -0.1), "confidence"),
        (_reply("exploit", "0.9"), "confidence"),
        (_reply("exploit", True), "confidence"),
        (_reply("exploit", None), "confidence"),
        (_reply("exploit", reason=""), "reason"),
        (_reply("exploit", reason="   "), "reason"),
        (_reply("exploit", reason=3), "reason"),
        (json.dumps({"label": "exploit", "confidence": 0.9}), "reason"),
        (_reply("exploit") + _reply("valid"), "disagree"),
        ('{"label": "exploit", "confidence": 0.9, "reason": "cut', "cut off"),
        (_reply("valid") + '\n{"label": "exploit", "confidence": 0.9, "reason": "cut', "cut off"),
    ],
    ids=[
        "none",
        "empty",
        "prose_only",
        "hook_label_for_judge",
        "unknown_label",
        "null_label",
        "confidence_above_one",
        "confidence_below_zero",
        "confidence_string",
        "confidence_bool",
        "confidence_missing",
        "reason_empty",
        "reason_blank",
        "reason_not_text",
        "reason_missing",
        "two_answers_disagree",
        "cut_off",
        "quoted_answer_then_cut_off",
    ],
)
def test_bad_judge_reply_raises_a_clear_error(text: str | None, problem: str) -> None:
    """A bad reply raises an error that names the problem; it never falls back to a label."""
    with pytest.raises(ClassificationError, match=problem):
        parse_judge_reply(text)


def test_nan_confidence_is_rejected() -> None:
    """``NaN`` passes a range check by accident, so it is refused on its own."""
    with pytest.raises(ClassificationError, match="confidence"):
        parse_hook_reply('{"label": "ok", "confidence": NaN, "reason": "fine"}')


def test_judge_label_is_refused_by_the_hook_parser() -> None:
    """The two label sets do not mix."""
    with pytest.raises(ClassificationError, match="label"):
        parse_hook_reply(_reply("exploit"))


def test_identical_repeated_answers_parse() -> None:
    """A model that repeats the same answer is not a disagreement."""
    assert parse_hook_reply(_reply("ok") + "\n" + _reply("ok")).label == "ok"


def test_long_reason_is_cut() -> None:
    """A runaway reason is cut so it cannot flood the run events."""
    result = parse_hook_reply(_reply("ok", reason="x" * 5000))
    assert 0 < len(result.reason) <= 1000


def test_json_schema_lists_the_labels_and_the_fields() -> None:
    """The schema a typed model fills names the labels and requires every field."""
    schema = json_schema(JUDGE_LABELS)
    assert schema["properties"]["label"]["enum"] == ["exploit", "valid"]
    assert schema["properties"]["confidence"] == {"type": "number", "minimum": 0, "maximum": 1}
    assert schema["required"] == ["label", "confidence", "reason"]
    assert schema["additionalProperties"] is False


def test_answer_format_names_every_label() -> None:
    """The prompt instruction names each allowed label."""
    text = answer_format(HOOK_LABELS)
    for label in HOOK_LABELS:
        assert f'"{label}"' in text
    assert "confidence" in text
    assert "reason" in text
