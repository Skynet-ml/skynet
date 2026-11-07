"""Tests for the eval server: budgets, lanes, best tracking and crash handling."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from ..protocol import (
    METRIC_ERROR,
    SEARCH_FLOOR,
    BudgetExhaustedError,
    EvalServer,
    ScorerAbortError,
    Task,
    candidate_key,
)
from ..runner import normalize_score
from .mocks import vowel_scorer


def test_evaluate_counts_and_stops_at_budget() -> None:
    """The server serves exactly ``max_evals`` calls, then raises."""
    server = EvalServer(vowel_scorer, max_evals=2)

    server.evaluate("aaa")
    server.evaluate("bbb")

    assert server.used == 2
    assert server.remaining == 0
    with pytest.raises(BudgetExhaustedError, match="budget of 2"):
        server.evaluate("ccc")


def test_lane_counts_against_parent_and_is_clamped() -> None:
    """Lane evaluations spend the parent's budget; a lane can never exceed it."""
    parent = EvalServer(vowel_scorer, max_evals=5)
    lane = parent.lane(3)
    oversized = parent.lane(50)

    lane.evaluate("aaa")
    lane.evaluate("bbb")

    assert lane.used == 2
    assert parent.used == 2
    assert lane.remaining == 1
    assert oversized.max_evals == 5
    assert oversized.remaining == 3


def test_lane_exhaustion_does_not_touch_parent_remaining() -> None:
    """A spent lane raises while the parent still has budget for the next lane."""
    parent = EvalServer(vowel_scorer, max_evals=4)
    lane = parent.lane(1)
    lane.evaluate("aaa")

    with pytest.raises(BudgetExhaustedError):
        lane.evaluate("bbb")
    assert parent.remaining == 3
    assert parent.lane(parent.remaining).max_evals == 3


def test_best_candidate_uses_mean_across_cases() -> None:
    """The best version is the one with the highest mean, not the highest single score."""
    server = EvalServer(vowel_scorer, max_evals=10)

    server.evaluate("aaaa", {"i": 0})
    server.evaluate("aaaa", {"i": 1})
    server.evaluate("axxx", {"i": 0})

    assert server.best_candidate == "aaaa"
    assert server.best_score == 1.0
    assert server.mean_score("axxx") == 0.25
    assert server.mean_score("never") is None


def test_best_tracking_is_shared_with_lanes() -> None:
    """The root server sees candidates scored through a lane."""
    parent = EvalServer(vowel_scorer, max_evals=10)
    lane = parent.lane(5)

    lane.evaluate("aaa")
    lane.evaluate("xxx")

    assert parent.best_candidate == "aaa"
    assert lane.best_candidate == "aaa"


def _crashing(candidate: Any, case: Any = None) -> tuple[float, dict[str, Any]]:
    """Raise for one specific version, and score every other version by its length below zero.

    Args:
        candidate: The version.
        case: Ignored.

    Returns:
        ``-len(candidate)`` for anything but ``"boom"``, so every valid score is negative.

    Raises:
        ValueError: For ``"boom"``.
    """
    if candidate == "boom":
        raise ValueError("cannot score this")
    return -float(len(candidate)), {"feedback": "ok"}


def test_scorer_crash_is_a_metric_error_with_no_score() -> None:
    """A scorer that raises records the version with a null score and the error text, never 0."""
    server = EvalServer(_crashing, max_evals=5)

    _, side_info = server.evaluate("boom")

    assert side_info == {"error": "ValueError: cannot score this", "status": METRIC_ERROR}
    assert server.used == 1
    (record,) = server.history
    assert record.mean_score is None
    assert record.crashed
    assert record.metric_error == "ValueError: cannot score this"
    assert server.mean_score("boom") is None


def test_root_score_hides_the_search_floor() -> None:
    """The internal scorer path returns None for a crash; only evaluate() hands the engines the floor."""
    server = EvalServer(_crashing, max_evals=5)

    assert server._score("boom", None) == (None, {"error": "ValueError: cannot score this", "status": METRIC_ERROR})
    score, _ = server.evaluate("boom")
    assert score == SEARCH_FLOOR


def test_a_crashed_version_is_never_best_even_below_zero() -> None:
    """With every valid score below 0, the crashed version is still not the best."""
    server = EvalServer(_crashing, max_evals=5)

    server.evaluate("boom")
    server.evaluate("abcdef")
    server.evaluate("abcdefghij")

    assert server.best_candidate == "abcdef"
    assert server.best_score == -6.0


def test_only_crashed_versions_leave_no_best() -> None:
    """A run where every version crashed has no best version and no best score."""
    server = EvalServer(_crashing, max_evals=5)

    server.evaluate("boom")

    assert server.best_candidate is None
    assert server.best_score is None


def test_one_crashed_case_nulls_the_whole_version() -> None:
    """A metric error on any case makes the version's mean null, not an average with a zero."""

    def flaky(candidate: Any, case: Any = None) -> tuple[float, dict[str, Any]]:
        """Score case 1, crash on case 2.

        Args:
            candidate: Ignored.
            case: The case number.

        Returns:
            0.9 on case 1.

        Raises:
            RuntimeError: On case 2.
        """
        if case == 2:
            raise RuntimeError("metric blew up")
        return 0.9, {"feedback": "ok"}

    server = EvalServer(flaky, max_evals=5)

    server.evaluate("v", 1)
    server.evaluate("v", 2)
    server.evaluate("v", 1)

    (record,) = server.history
    assert record.count == 3
    assert record.mean_score is None
    assert record.metric_error == "RuntimeError: metric blew up"
    assert server.best_candidate is None


def test_a_nested_server_keeps_the_metric_error() -> None:
    """A server that scores through another server's evaluate() reads the status, not the floor."""
    outer = EvalServer(_crashing, max_evals=5)
    inner = EvalServer(outer.evaluate, max_evals=5)

    score, side_info = inner.evaluate("boom")
    inner.evaluate("abc")

    assert score == SEARCH_FLOOR
    assert side_info["status"] == METRIC_ERROR
    assert inner.mean_score("boom") is None
    assert inner.best_candidate == "abc"


def test_a_metric_error_logs_the_error_not_a_score(caplog: pytest.LogCaptureFixture) -> None:
    """The run log names the metric error and its text, and never prints the floor."""
    server = EvalServer(_crashing, max_evals=5)

    with caplog.at_level(logging.DEBUG, logger="core.service_gateway.optimization.blackbox.protocol"):
        server.evaluate("boom")

    messages = [record.getMessage() for record in caplog.records]
    assert "scorer raised on a candidate (metric_error): ValueError: cannot score this" in messages
    assert "scorer eval 1/5 metric_error: ValueError: cannot score this" in messages
    assert not any(str(SEARCH_FLOOR) in message or "e+09" in message for message in messages)


def test_on_eval_gets_none_for_a_metric_error() -> None:
    """The progress listener sees a null score for a crashed case, never the floor."""
    seen: list[float | None] = []
    server = EvalServer(_crashing, max_evals=5, on_eval=lambda srv, score: seen.append(score))

    server.evaluate("boom")
    server.evaluate("ab")

    assert seen == [None, -2.0]


def test_scorer_abort_is_not_floored() -> None:
    """A scorer that asks to stop the run passes through instead of becoming a zero."""

    def aborting(candidate: Any, case: Any = None) -> tuple[float, dict[str, Any]]:
        """Abort on every version.

        Args:
            candidate: Ignored.
            case: Ignored.

        Raises:
            ScorerAbortError: Always.
        """
        raise ScorerAbortError("harness is dead")

    server = EvalServer(aborting, max_evals=5)

    with pytest.raises(ScorerAbortError, match="harness is dead"):
        server.evaluate("anything")


def test_a_scorer_without_feedback_stops_the_run() -> None:
    """Missing feedback is a scorer error that ends the run with the contract message, not a silent zero."""

    def bare(candidate: Any, case: Any = None) -> tuple[float, dict[str, Any]]:
        """Return a bare number the way a scorer without feedback would.

        Args:
            candidate: Ignored.
            case: Ignored.

        Returns:
            Never; the contract check raises.
        """
        return normalize_score(0.5)

    server = EvalServer(bare, max_evals=5)

    with pytest.raises(ScorerAbortError, match="The scorer must return feedback"):
        server.evaluate("anything")


def test_on_eval_fires_only_at_the_root() -> None:
    """The listener sees every evaluation once, whichever lane made it."""
    seen: list[tuple[int, float]] = []
    parent = EvalServer(vowel_scorer, max_evals=10, on_eval=lambda srv, score: seen.append((srv.used, score)))

    parent.evaluate("aaa")
    parent.lane(3).evaluate("xxx")

    assert seen == [(1, 1.0), (2, 0.0)]


def test_dict_candidates_have_stable_keys() -> None:
    """Named-part candidates are keyed by their sorted JSON form."""
    assert candidate_key({"b": "2", "a": "1"}) == candidate_key({"a": "1", "b": "2"})
    assert candidate_key("text") == "text"


def test_task_mode_flags() -> None:
    """``str_mode`` covers text and seedless tasks; ``has_dataset`` needs cases."""
    assert Task(seed_candidate="hi").str_mode
    assert Task(seed_candidate=None).str_mode
    assert not Task(seed_candidate={"a": "b"}).str_mode
    assert not Task(seed_candidate="hi").has_dataset
    assert Task(seed_candidate="hi", cases=[{"x": 1}]).has_dataset


def test_history_lists_distinct_versions_in_first_seen_order() -> None:
    """Every distinct version is recorded once, with its mean score and latest side info."""
    server = EvalServer(vowel_scorer, max_evals=10)
    lane = server.lane(10)

    lane.evaluate("aaa", "case-1")
    lane.evaluate("xyz", "case-1")
    lane.evaluate("aaa", "case-2")

    assert [record.candidate for record in server.history] == ["aaa", "xyz"]
    first, second = server.history
    assert (first.count, first.first_eval, first.mean_score) == (2, 1, 1.0)
    assert first.side_info == {"vowels": 3}
    assert (second.count, second.first_eval, second.mean_score) == (1, 2, 0.0)
    assert [record.candidate for record in lane.history] == ["aaa", "xyz"]


def test_evaluate_logs_a_debug_heartbeat_per_scorer_call(caplog: pytest.LogCaptureFixture) -> None:
    """Each scorer call leaves a DEBUG line with its budget position and score for the verbose log view."""
    server = EvalServer(lambda candidate, example: (0.25 * len(candidate), {}), max_evals=5)
    lane = server.lane(3)

    with caplog.at_level(logging.DEBUG, logger="core.service_gateway.optimization.blackbox.protocol"):
        server.evaluate("aa")
        lane.evaluate("aaa")

    heartbeats = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert [r.getMessage() for r in heartbeats] == [
        "scorer eval 1/5 score=0.500",
        "scorer eval 2/5 score=0.750",
    ]
