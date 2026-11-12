"""Typed answers from the watch hook and the exploit judge (D49).

Both models answer with one JSON object: a ``label``, a ``confidence`` from 0
to 1 and a short ``reason``. A typed label with a confidence lets a cheap
classifier, such as TypeSafe Jev, do the work of a large model.

Public names:

- ``HookLabel`` / ``HOOK_LABELS``: ``ok``, ``off_intent``, ``breaks_rule``.
- ``JudgeLabel`` / ``JUDGE_LABELS``: ``exploit``, ``valid``.
- ``HookClassification`` / ``JudgeClassification``: the typed results, each
  with ``label``, ``confidence`` and ``reason``.
- ``parse_hook_reply`` / ``parse_judge_reply``: turn a model reply into a
  typed result, or raise ``ClassificationError``.
- ``json_schema``: the JSON Schema for a structured-output request.
- ``answer_format``: the prompt instruction that asks for that object.

A bad reply always raises. The parsers never fall back to a default label,
because a silent ``ok`` or ``valid`` would hide a broken judge. Warning
thresholds belong to the caller, not to this module.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, get_args

HookLabel = Literal["ok", "off_intent", "breaks_rule"]
JudgeLabel = Literal["exploit", "valid"]
HOOK_LABELS: tuple[str, ...] = get_args(HookLabel)
JUDGE_LABELS: tuple[str, ...] = get_args(JudgeLabel)

_FIELDS = ("label", "confidence", "reason")
# The reason lands in run events and agent feedback; a runaway reply must not flood them.
_MAX_REASON_CHARS = 1000
_DECODER = json.JSONDecoder()
_ANSWER_START = re.compile(r'\{\s*"label"\s*:')


class ClassificationError(ValueError):
    """A model reply that is not a valid classification."""


@dataclass(frozen=True)
class HookClassification:
    """The watch hook's answer about the agent's recent work."""

    label: HookLabel
    confidence: float
    reason: str


@dataclass(frozen=True)
class JudgeClassification:
    """The exploit judge's answer about one version."""

    label: JudgeLabel
    confidence: float
    reason: str


def json_schema(labels: Sequence[str]) -> dict[str, Any]:
    """Return the JSON Schema of one answer, for a structured-output request.

    Args:
        labels: The allowed labels, ``HOOK_LABELS`` or ``JUDGE_LABELS``.

    Returns:
        A strict object schema with the three required fields.
    """
    return {
        "type": "object",
        "properties": {
            "label": {"type": "string", "enum": list(labels)},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reason": {"type": "string"},
        },
        "required": list(_FIELDS),
        "additionalProperties": False,
    }


def answer_format(labels: Sequence[str]) -> str:
    """Return the prompt instruction that asks for one answer object.

    Args:
        labels: The allowed labels, ``HOOK_LABELS`` or ``JUDGE_LABELS``.

    Returns:
        The instruction text.
    """
    names = ", ".join(f'"{label}"' for label in labels)
    return (
        'Answer with one JSON object and nothing else: {"label": ..., "confidence": ..., "reason": ...}.'
        f" label is one of {names}. confidence is a number from 0 to 1."
        " reason is one short sentence."
    )


def _answer_objects(text: str) -> list[dict[str, Any]]:
    """Find every JSON object with a ``label`` key in a reply.

    Models wrap the answer in prose or code fences, so each ``{`` is tried
    as the start of an object. Judges quote code, so a quoted answer-shaped
    object can sit beside the real one; a ``{"label":`` that does not decode,
    such as a real answer cut off after a quoted one, makes the reply bad
    instead of letting the quoted object win.

    Args:
        text: The model reply.

    Returns:
        The answer objects, in reply order.

    Raises:
        ClassificationError: When an answer object starts but does not decode.
    """
    answers: list[dict[str, Any]] = []
    start = text.find("{")
    while start != -1:
        try:
            value, end = _DECODER.raw_decode(text, start)
        except json.JSONDecodeError:
            if _ANSWER_START.match(text, start):
                raise ClassificationError("The reply has an answer object that is cut off or not valid JSON.") from None
            start = text.find("{", start + 1)
            continue
        if isinstance(value, dict) and "label" in value:
            answers.append(value)
            start = text.find("{", end)
        else:
            start = text.find("{", start + 1)
    return answers


def _parse(text: str | None, labels: Sequence[str]) -> tuple[str, float, str]:
    """Check one reply against a label set.

    Args:
        text: The model reply.
        labels: The allowed labels.

    Returns:
        The label, the confidence and the reason.

    Raises:
        ClassificationError: When the reply has no answer object, answers
            that disagree, or a field that is missing or out of range.
    """
    answers = _answer_objects(text or "")
    if not answers:
        raise ClassificationError("The reply has no JSON object with a label.")
    if any(answer != answers[0] for answer in answers[1:]):
        raise ClassificationError("The reply has answer objects that disagree.")
    answer = answers[0]

    label = answer.get("label")
    label = label.strip().lower() if isinstance(label, str) else label
    if label not in labels:
        raise ClassificationError(f"The label {answer.get('label')!r} is not one of {', '.join(labels)}.")

    confidence = answer.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or math.isnan(confidence)
        or not 0 <= confidence <= 1
    ):
        raise ClassificationError(f"The confidence {confidence!r} is not a number from 0 to 1.")

    reason = answer.get("reason")
    reason = reason.strip() if isinstance(reason, str) else ""
    if not reason:
        raise ClassificationError("The reply gives no reason.")
    return label, float(confidence), reason[:_MAX_REASON_CHARS]


def parse_hook_reply(text: str | None) -> HookClassification:
    """Turn the watch hook's reply into a typed result.

    Args:
        text: The hook model's reply.

    Returns:
        The hook classification.

    Raises:
        ClassificationError: When the reply is not a valid hook answer.
    """
    label, confidence, reason = _parse(text, HOOK_LABELS)
    return HookClassification(label=label, confidence=confidence, reason=reason)  # type: ignore[arg-type]


def parse_judge_reply(text: str | None) -> JudgeClassification:
    """Turn the exploit judge's reply into a typed result.

    Args:
        text: The judge model's reply.

    Returns:
        The judge classification.

    Raises:
        ClassificationError: When the reply is not a valid judge answer.
    """
    label, confidence, reason = _parse(text, JUDGE_LABELS)
    return JudgeClassification(label=label, confidence=confidence, reason=reason)  # type: ignore[arg-type]
