"""Tests for streaming scorer side info as mini-batch feedback events."""

from __future__ import annotations

from gepa.image import Image

from core.constants import PROGRESS_CANDIDATE, PROGRESS_CASE_SCORED, PROGRESS_MINIBATCH
from core.service_gateway.optimization import trajectory

from .. import feedback
from ..feedback import (
    candidate_parts,
    emit_candidate,
    emit_case_scored,
    emit_scorer_feedback,
    scorer_feedback_images,
    scorer_feedback_text,
    without_images,
)

PNG = "data:image/png;base64,iVBORw0KGgo="


def test_feedback_string_leads_and_other_keys_follow() -> None:
    """The scorer's prose comes first; every other note becomes a ``key: value`` line."""
    text = scorer_feedback_text({"score_parts": {"a": 1}, "feedback": "tight", "frames": ["note"]})

    assert text == 'tight\nscore_parts: {"a": 1}\nframes: ["note"]'


def test_named_scores_render_one_line_each_with_their_feedback() -> None:
    """Every named score reaches the reflection text as ``name: score — feedback`` after the overall feedback."""
    side_info = {
        "feedback": "overall",
        "scores": {"accuracy": {"score": 0.5, "feedback": "half wrong"}, "tone": {"score": 1, "feedback": "warm"}},
    }

    assert scorer_feedback_text(side_info) == "overall\naccuracy: 0.5 — half wrong\ntone: 1 — warm"
    shaped = feedback.gepa_side_info(side_info)
    assert shaped["scores"] == {"accuracy": 0.5, "tone": 1.0}
    assert shaped[feedback.SCORE_FEEDBACK_KEY] == {"accuracy": "half wrong", "tone": "warm"}


def test_images_and_empty_values_are_dropped() -> None:
    """Inline renders, ``None`` and empty containers never reach the feedback text."""
    side_info = {"render": PNG, "frames": [PNG, PNG], "feedback": "ok", "missing": None, "extra": {}}

    assert without_images(side_info) == {"frames": [], "feedback": "ok", "missing": None, "extra": {}}
    assert scorer_feedback_text(side_info) == "ok"


def test_revived_image_objects_are_dropped_too() -> None:
    """The ``Image`` the sandbox scorer wraps a render in never leaks back in as a data URL."""
    side_info = {"render_1": Image(url=PNG), "frames": [Image(url=PNG)], "feedback": "ok"}

    assert scorer_feedback_text(side_info) == "ok"


def test_crash_error_becomes_feedback() -> None:
    """A scorer crash reported by the eval server shows up as an ``error`` line."""
    assert scorer_feedback_text({"error": "ValueError: boom"}) == "error: ValueError: boom"


def test_silent_scorer_yields_no_text() -> None:
    """Nothing but images (or nothing at all) means no feedback event."""
    assert scorer_feedback_text({}) == ""
    assert scorer_feedback_text({"render": PNG}) == ""


def test_feedback_text_is_capped() -> None:
    """The text respects the same cap as DSPy mini-batch feedback."""
    text = scorer_feedback_text({"feedback": "x" * (trajectory.MINIBATCH_FEEDBACK_CHAR_CAP + 50)})

    assert len(text) == trajectory.MINIBATCH_FEEDBACK_CHAR_CAP


def test_emit_skips_when_nothing_to_say() -> None:
    """No callback, or no feedback text, emits nothing."""
    sink: list[tuple[str, dict]] = []

    emit_scorer_feedback(None, example_id="1", score=1.0, side_info={"feedback": "hi"})
    emit_scorer_feedback(lambda e, m: sink.append((e, m)), example_id="1", score=1.0, side_info={})

    assert sink == []


def test_emit_mirrors_the_dspy_event_shape() -> None:
    """The event carries the DSPy mini-batch fields and the active proposal iteration."""
    sink: list[tuple[str, dict]] = []
    token = trajectory._current_proposal_iteration.set(3)
    try:
        emit_scorer_feedback(
            lambda e, m: sink.append((e, m)), example_id="2", score=0.25, side_info={"feedback": "closer"}
        )
    finally:
        trajectory._current_proposal_iteration.reset(token)

    assert sink == [
        (
            PROGRESS_MINIBATCH,
            {
                "example_id": "2",
                "score": 0.25,
                "feedback": "closer",
                "iteration": 3,
                "images": [],
                "images_dropped": 0,
            },
        )
    ]


def test_emit_carries_renders_whole_next_to_the_text() -> None:
    """Raw and revived renders, top-level or listed, reach the event uncut and image-only side info still emits."""
    sink: list[tuple[str, dict]] = []

    emit_scorer_feedback(
        lambda e, m: sink.append((e, m)),
        example_id="2",
        score=0.5,
        side_info={"render_1": PNG, "frames": [Image(url=PNG)]},
        iteration=1,
    )

    (event,) = sink
    assert event[0] == PROGRESS_MINIBATCH
    assert event[1]["feedback"] == ""
    assert event[1]["images"] == [{"key": "render_1", "src": PNG}, {"key": "frames[0]", "src": PNG}]
    assert event[1]["images_dropped"] == 0


def test_emit_tags_the_version_only_when_named() -> None:
    """Engines without proposal iterations tie a note to its version; others leave the field out."""
    sink: list[tuple[str, dict]] = []
    emit = lambda e, m: sink.append((e, m))  # noqa: E731

    emit_scorer_feedback(emit, example_id="?", score=0.9, side_info={"feedback": "ok"}, candidate_id="3")
    emit_scorer_feedback(emit, example_id="?", score=0.9, side_info={"feedback": "ok"})

    assert sink[0][1]["candidate_id"] == "3"
    assert "candidate_id" not in sink[1][1]


def test_renders_past_the_byte_budget_are_counted_not_cut(monkeypatch) -> None:
    """The first renders that fit are kept whole; the rest are counted so the UI can say so."""
    monkeypatch.setattr(feedback, "MINIBATCH_IMAGES_BYTE_CAP", len(PNG) + 1)

    images, dropped = scorer_feedback_images({"a": PNG, "b": PNG, "c": [PNG], "d": "prose"})

    assert images == [{"key": "a", "src": PNG}]
    assert dropped == 2


def test_emit_prefers_an_explicit_iteration_over_the_context() -> None:
    """Engines scoring from worker threads pass the iteration themselves; it wins over the context."""
    sink: list[tuple[str, dict]] = []
    token = trajectory._current_proposal_iteration.set(3)
    try:
        emit_scorer_feedback(
            lambda e, m: sink.append((e, m)), example_id="0", score=1.0, side_info={"feedback": "hi"}, iteration=7
        )
    finally:
        trajectory._current_proposal_iteration.reset(token)

    assert sink[0][1]["iteration"] == 7


def _explode(event: str, metrics: dict) -> None:
    """Simulate a progress sink that raises.

    Args:
        event: Ignored.
        metrics: Ignored.

    Raises:
        RuntimeError: Always.
    """
    raise RuntimeError("sink down")


def test_emit_survives_a_failing_callback() -> None:
    """A broken progress sink must not break the scorer call."""
    emit_scorer_feedback(_explode, example_id="1", score=1.0, side_info={"feedback": "hi"})


def test_emit_case_scored_carries_the_version_and_its_case_count() -> None:
    """A scored case names its version, the case, the score and how many cases the version has."""
    sink: list[tuple[str, dict]] = []

    emit_case_scored(lambda e, m: sink.append((e, m)), trial=2, example_id="1", score=0.5, total=3)

    assert sink == [(PROGRESS_CASE_SCORED, {"trial": 2, "example_id": "1", "score": 0.5, "total": 3})]


def test_emit_case_scored_without_a_sink_or_with_a_broken_one_is_harmless() -> None:
    """No callback is a no-op and a raising callback never reaches the engine."""

    def broken(event: str, metrics: dict) -> None:
        """Fail like a sink that lost its job."""
        raise RuntimeError("gone")

    emit_case_scored(None, trial=0, example_id="0", score=1.0, total=1)
    emit_case_scored(broken, trial=0, example_id="0", score=1.0, total=1)


def test_candidate_parts_wraps_text_under_the_shared_key() -> None:
    """Text versions travel under ``current_candidate``; named parts travel as they are."""
    assert candidate_parts("aaa") == {"current_candidate": "aaa"}
    assert candidate_parts({"AGENTS.md": "a", "README.md": "b"}) == {"AGENTS.md": "a", "README.md": "b"}


def test_emit_candidate_mirrors_the_gepa_event_shape() -> None:
    """A scored version goes out exactly as GEPA's accepted candidates do."""
    sink: list[tuple[str, dict]] = []

    emit_candidate(
        lambda e, m: sink.append((e, m)),
        candidate_id="1",
        parent_id="0",
        generation=1,
        score=0.5,
        per_example=[("0", 0.25), ("1", 0.75)],
        candidate="aaa",
        discovered_at_evals=4,
        iteration=1,
    )

    assert sink == [
        (
            PROGRESS_CANDIDATE,
            {
                "candidate_id": "1",
                "parent_id": "0",
                "parents_extra": [],
                "generation": 1,
                "score": 0.5,
                "per_example": [{"id": "0", "score": 0.25}, {"id": "1", "score": 0.75}],
                "prompt": {"current_candidate": "aaa"},
                "discovered_at_evals": 4,
                "iteration": 1,
            },
        )
    ]


def test_emit_candidate_without_a_sink_or_with_a_broken_one_is_harmless() -> None:
    """No sink means no event; a raising sink must not break the engine loop."""
    for sink in (None, _explode):
        emit_candidate(
            sink,
            candidate_id="0",
            parent_id=None,
            generation=0,
            score=1.0,
            per_example=[("0", 1.0)],
            candidate={"AGENTS.md": "a"},
            discovered_at_evals=1,
            iteration=0,
        )
