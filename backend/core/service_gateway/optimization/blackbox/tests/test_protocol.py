"""Tests for the eval server: budgets, lanes, best tracking and crash handling."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from .. import protocol
from ..protocol import (
    METRIC_ERROR,
    NO_FEEDBACK_WARNING,
    RUN_FAILED_STATUS,
    STATUS_KEY,
    TOO_DEEP_ARGUMENTS,
    UNSCORED,
    BudgetExhaustedError,
    EvalServer,
    ScorerAbortError,
    Task,
    candidate_key,
    engine_score,
    fill_unscored,
    openai_tools,
    safe_tool_call,
    score_batch,
    tool_calls_from_openai,
    warn_once_without_feedback,
)
from ..runner import normalize_score
from ..scorer import normalize_score as scorer_normalize_score
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


def test_evaluate_returns_none_for_a_metric_error() -> None:
    """evaluate() returns no score for a crashed case; no floor number takes its place."""
    server = EvalServer(_crashing, max_evals=5)

    assert server._score("boom", None) == (None, {"error": "ValueError: cannot score this", "status": METRIC_ERROR})
    assert server.evaluate("boom") == (None, {"error": "ValueError: cannot score this", "status": METRIC_ERROR})


def test_engine_score_gives_upstream_its_own_no_score_value() -> None:
    """Only the upstream boundary maps a missing score to negative infinity; a real score is unchanged."""
    assert engine_score(None) == UNSCORED == float("-inf")
    assert engine_score(-3.5) == -3.5
    assert engine_score(0.0) == 0.0


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
    """A server that scores through an upstream-shaped evaluate() reads the status, not the number."""
    outer = EvalServer(_crashing, max_evals=5)

    def upstream_shaped(candidate: Any, example: Any = None) -> tuple[float, dict[str, Any]]:
        """Score like the upstream server an Auto lane scores through.

        Args:
            candidate: The version to score.
            example: The case.

        Returns:
            The outer score, with negative infinity for a metric error.
        """
        score, side_info = outer.evaluate(candidate, example)
        return engine_score(score), side_info

    inner = EvalServer(upstream_shaped, max_evals=5)

    score, side_info = inner.evaluate("boom")
    inner.evaluate("abc")

    assert score is None
    assert side_info["status"] == METRIC_ERROR
    assert inner.mean_score("boom") is None
    assert inner.best_candidate == "abc"


def test_a_metric_error_logs_the_error_not_a_score(caplog: pytest.LogCaptureFixture) -> None:
    """The run log names the metric error and its text, and never prints a number for it."""
    server = EvalServer(_crashing, max_evals=5)

    with caplog.at_level(logging.DEBUG, logger="core.service_gateway.optimization.blackbox.protocol"):
        server.evaluate("boom")

    messages = [record.getMessage() for record in caplog.records]
    assert "scorer raised on a candidate (metric_error): ValueError: cannot score this" in messages
    assert "scorer eval 1/5 metric_error: ValueError: cannot score this" in messages
    assert not any("score=" in message or "inf" in message for message in messages)


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


def _wrong_form_after_good(candidate: Any, case: Any = None) -> tuple[float, dict[str, Any]]:
    """Score "good" with a valid answer; read every other version's bare number as a wrong answer form.

    Args:
        candidate: The version.
        case: Ignored.

    Returns:
        The checked answer.

    Raises:
        ServiceError: For every version other than "good".
    """
    return scorer_normalize_score({"score": 1.0} if candidate == "good" else 0.5)


def test_a_wrong_answer_form_before_any_score_stops_the_run() -> None:
    """The first answer has a wrong form, so the run stops instead of spending its budget on errors (SKY-769)."""
    server = EvalServer(_wrong_form_after_good, max_evals=5)

    with pytest.raises(ScorerAbortError, match=r"bare number\. Return one object"):
        server.evaluate("bad")

    assert server.used == 1


def test_a_wrong_answer_form_after_a_score_is_one_versions_metric_error() -> None:
    """After a version has a score, a wrong answer form gives that version a metric error, and the run goes on."""
    server = EvalServer(_wrong_form_after_good, max_evals=5)

    good = server.lane(2).evaluate("good")
    score, side_info = server.evaluate("bad")

    assert good[0] == 1.0
    assert score is None
    assert side_info[STATUS_KEY] == METRIC_ERROR
    assert "bare number" in side_info["error"]
    assert server.used == 2


def _scores_without_feedback(candidate: Any, case: Any = None) -> tuple[float, dict[str, Any]]:
    """Score like a metric whose answer has no feedback, only a named score without feedback.

    Args:
        candidate: The version; its length is the score.
        case: Ignored.

    Returns:
        The checked answer.
    """
    return normalize_score({"score": len(candidate) / 10, "scores": {"format": {"score": 1.0}}})


def test_a_metric_without_feedback_runs_and_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    """A metric with no feedback scores every version, and the run log says so one time (SKY-769)."""
    server = EvalServer(warn_once_without_feedback(_scores_without_feedback), max_evals=5)

    with caplog.at_level(logging.WARNING, logger="core.service_gateway.optimization.blackbox.protocol"):
        results = [server.evaluate("ab"), server.evaluate("abc"), server.lane(2).evaluate("abcd")]

    assert [score for score, _ in results] == [pytest.approx(0.2), pytest.approx(0.3), pytest.approx(0.4)]
    assert results[0][1]["feedback"] == ""
    assert [record.getMessage() for record in caplog.records] == [NO_FEEDBACK_WARNING]


@pytest.mark.parametrize(
    "answer",
    [
        {"score": 0.5, "feedback": "Close.", "scores": {"format": {"score": 1.0}}},
        {"score": None, "feedback": "", "status": RUN_FAILED_STATUS},
    ],
)
def test_no_warning_for_feedback_or_a_failed_run(caplog: pytest.LogCaptureFixture, answer: dict[str, Any]) -> None:
    """Feedback on the main score, even with a named score that has none, or a run with no score gives no line.

    Args:
        caplog: Pytest fixture.
        answer: What the scorer returns.
    """
    scorer = warn_once_without_feedback(lambda candidate, case=None: (answer["score"], dict(answer)))

    with caplog.at_level(logging.WARNING, logger="core.service_gateway.optimization.blackbox.protocol"):
        scorer("x")

    assert NO_FEEDBACK_WARNING not in [record.getMessage() for record in caplog.records]


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


def test_fill_unscored_gives_a_null_case_its_versions_scored_mean() -> None:
    """A null case takes the same version's scored mean; a version with no scored case stays UNSCORED."""
    candidates = ["a", "a", "a", "b", "b"]

    assert fill_unscored(candidates, [0.2, None, 0.6, None, None]) == pytest.approx([0.2, 0.4, 0.6, UNSCORED, UNSCORED])


def test_fill_unscored_gives_a_failed_run_version_no_score() -> None:
    """A version with a failed [run] case is UNSCORED on every case and ranks below a lower scored version (SKY-751)."""
    candidates = ["crash", "crash", "crash", "low", "low", "low"]
    failed = {"status": RUN_FAILED_STATUS}
    infos = [{}, {}, failed, {}, {}, {}]

    filled = fill_unscored(candidates, [0.9, 0.9, None, 0.3, 0.3, 0.3], infos)

    assert filled[:3] == [UNSCORED] * 3
    assert filled[3:] == pytest.approx([0.3] * 3)
    assert sum(filled[3:]) > sum(filled[:3])


def test_fill_unscored_keeps_a_scorers_own_run_failed_status_with_a_score() -> None:
    """A scorer's own ``run_failed`` status next to a real score is not a failed [run] entry (SKY-221, SKY-751)."""
    assert fill_unscored(["a", "a"], [0.4, 0.6], [{"status": RUN_FAILED_STATUS}, {}]) == pytest.approx([0.4, 0.6])


def test_a_scorers_own_run_failed_status_with_a_finite_score_keeps_its_score() -> None:
    """Only a failed [run] entry, which sends no score, drops a score; a real score with that status stays (SKY-751)."""
    server = EvalServer(lambda c, e: (0.4, {"feedback": "ok", "status": RUN_FAILED_STATUS}), max_evals=5)

    score, info = server.evaluate("x")

    assert score == pytest.approx(0.4)
    assert info == {"feedback": "ok", "status": RUN_FAILED_STATUS}
    assert server.mean_score("x") == pytest.approx(0.4)
    assert server.run_failed("x") is False
    assert server.metric_error("x") is None


def test_nested_server_marks_an_unscored_failed_run_as_no_score() -> None:
    """An Auto lane gets a failed [run] case as UNSCORED; it stays a run failure with no score (SKY-751)."""
    server = EvalServer(lambda c, e: (UNSCORED, {"status": RUN_FAILED_STATUS}), max_evals=5)

    score, info = server.evaluate("x")

    assert score is None
    assert info["status"] == RUN_FAILED_STATUS
    assert server.metric_error("x") is not None
    assert server.best_candidate is None


def test_a_failed_run_after_a_clean_case_marks_the_version_as_run_failed() -> None:
    """A version scored on a clean case, then a failed [run] case, keeps the run-failed mark (SKY-751)."""

    def scorer(candidate: str, example: int) -> tuple[float | None, dict[str, Any]]:
        """Score the first case; fail the [run] entry on the second.

        Args:
            candidate: The version.
            example: The case number.

        Returns:
            The score and side info.
        """
        if example == 2:
            return None, {"feedback": "Run failed: boom", "status": RUN_FAILED_STATUS}
        return 0.7, {"feedback": "ok"}

    server = EvalServer(scorer, max_evals=5)
    server.evaluate("c", 1)
    server.evaluate("c", 2)

    assert server.run_failed("c") is True
    assert server.mean_score("c") is None
    [record] = server.history
    assert (record.run_failed, record.metric_error) == (True, "Run failed: boom")


def test_a_failed_run_after_a_metric_error_marks_the_version_as_run_failed() -> None:
    """A metric error on one case, then a failed [run] case, still marks the version as run-failed (SKY-751)."""

    def scorer(candidate: str, example: int) -> tuple[float | None, dict[str, Any]]:
        """Crash the metric on the first case; fail the [run] entry on the second.

        Args:
            candidate: The version.
            example: The case number.

        Returns:
            The score and side info.
        """
        if example == 1:
            return None, {"error": "metric boom"}
        return None, {"feedback": "Run failed: boom", "status": RUN_FAILED_STATUS}

    server = EvalServer(scorer, max_evals=5)
    server.evaluate("c", 1)
    server.evaluate("c", 2)

    assert server.run_failed("c") is True
    [record] = server.history
    assert (record.run_failed, record.metric_error) == (True, "Run failed: boom")


def test_score_batch_keeps_order_and_metric_error_status() -> None:
    """A batch scores each pair once, keeps the order, and still marks the filled case as a metric error."""
    server = EvalServer(lambda c, e: _crashing("boom" if e == 2 else c, e), max_evals=10)

    def score_one(candidate: Any, example: Any) -> tuple[float | None, dict[str, Any]]:
        """Score one pair through the server.

        Args:
            candidate: The version.
            example: The case.

        Returns:
            The server's score, ``None`` after a metric error, and side info.
        """
        return server.evaluate(candidate, example)

    for workers in (1, 4):
        results = score_batch(score_one, [("ab", 1), ("ab", 2), ("abcd", 3)], workers)

        assert [score for score, _ in results] == [-2.0, -2.0, -4.0]
        assert results[1][1]["status"] == METRIC_ERROR
    assert server.used == 6


# An assistant message as LiteLLM's ``response.choices[0].message.model_dump()`` gives it.
_OPENAI_MESSAGE: dict[str, Any] = {
    "role": "assistant",
    "content": None,
    "tool_calls": [
        {
            "id": "call_abc",
            "type": "function",
            "function": {"name": "bash", "arguments": '{"command": "ls -la"}'},
        },
        {"id": "call_def", "type": "function", "function": {"name": "bash", "arguments": ""}},
    ],
}


def test_openai_tools_converts_flat_definitions() -> None:
    """A flat definition becomes an OpenAI function tool whose ``parameters`` is the input schema."""
    schema = {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}

    tools = openai_tools([{"name": "bash", "description": "Run a command.", "input_schema": schema}])

    assert tools == [
        {"type": "function", "function": {"name": "bash", "description": "Run a command.", "parameters": schema}}
    ]


def test_tool_calls_from_openai_parses_arguments_and_keeps_ids() -> None:
    """Each call keeps its id and name, and its JSON arguments become a dict; empty arguments are an empty dict."""
    assert tool_calls_from_openai(_OPENAI_MESSAGE) == [
        {"id": "call_abc", "name": "bash", "arguments": {"command": "ls -la"}},
        {"id": "call_def", "name": "bash", "arguments": {}},
    ]


@pytest.mark.parametrize("message", [{"role": "assistant", "content": "done"}, {"tool_calls": None}])
def test_tool_calls_from_openai_gives_no_calls_for_a_plain_reply(message: dict[str, Any]) -> None:
    """A reply that asks for no tool gives no calls."""
    assert tool_calls_from_openai(message) == []


@pytest.mark.parametrize("arguments", ['{"command": ', "[1, 2]"])
def test_tool_calls_from_openai_keeps_raw_text_that_is_no_json_object(arguments: str) -> None:
    """Bad JSON, or JSON that is no object, stays raw text, so the engine can answer it with an error result."""
    message = {
        "tool_calls": [{"id": "call_x", "type": "function", "function": {"name": "bash", "arguments": arguments}}]
    }

    assert tool_calls_from_openai(message) == [{"id": "call_x", "name": "bash", "arguments": arguments}]


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"command": "ls"}, {"command": "ls"}),
        (["ls"], '["ls"]'),
        (7, "7"),
        (0, "0"),
        (False, "false"),
        ([], "[]"),
        ({}, {}),
        (None, {}),
        ("", {}),
    ],
)
def test_tool_calls_from_openai_keeps_parsed_or_odd_arguments(arguments: Any, expected: Any) -> None:
    """A dict stays as it is, missing arguments are ``{}``, and any other value becomes its JSON text.

    A value that is no text does not raise, so no reply is lost, and a falsy one keeps its value.
    """
    message = {
        "tool_calls": [{"id": "call_x", "type": "function", "function": {"name": "bash", "arguments": arguments}}]
    }

    assert tool_calls_from_openai(message) == [{"id": "call_x", "name": "bash", "arguments": expected}]


def _deep_list(levels: int) -> list[Any]:
    """Build a list nested to a depth, with no recursion.

    Args:
        levels: The number of nested lists.

    Returns:
        ``[[...[]...]]``, ``levels`` lists deep.
    """
    value: list[Any] = []
    for _ in range(levels - 1):
        value = [value]
    return value


def _deep_dict(levels: int) -> dict[str, Any]:
    """Build an object nested to a depth, with no recursion.

    Args:
        levels: The number of nested objects, the outer one included.

    Returns:
        ``{"a": {"a": ... {}}}``, ``levels`` objects deep.
    """
    value: dict[str, Any] = {}
    for _ in range(levels - 1):
        value = {"a": value}
    return value


# 900 levels parses with no RecursionError at a shallow stack, but a later read at a deeper stack can raise. 5000
# levels raises at any stack.
@pytest.mark.parametrize(
    "arguments",
    [
        "[" * 900 + "]" * 900,
        '{"a": ' * 900 + "1" + "}" * 900,
        "[" * 900,
        _deep_list(900),
        _deep_dict(900),
        "[" * 5000 + "]" * 5000,
        _deep_list(5000),
    ],
    ids=["array-text", "object-text", "bad-text", "list-value", "dict-value", "text-past-limit", "value-past-limit"],
)
def test_tool_calls_from_openai_gives_too_deep_arguments_for_deep_arguments(arguments: Any) -> None:
    """Arguments past the fixed depth bound give ``TOO_DEEP_ARGUMENTS``, whatever the stack and the JSON shape.

    Deep text must not reach the transcript, because a later call would read it again and raise.

    Args:
        arguments: The deep arguments, as text to read or as a value to write as JSON text.
    """
    message = {
        "tool_calls": [
            {"id": "call_x", "type": "function", "function": {"name": "bash", "arguments": arguments}},
            {"id": "call_y", "type": "function", "function": {"name": "bash", "arguments": '{"command": "ls"}'}},
        ]
    }

    assert tool_calls_from_openai(message) == [
        {"id": "call_x", "name": "bash", "arguments": TOO_DEEP_ARGUMENTS},
        {"id": "call_y", "name": "bash", "arguments": {"command": "ls"}},
    ]


def test_tool_calls_from_openai_gives_too_deep_arguments_when_parsing_overflows_the_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Text under the depth bound that still overflows the caller's stack fails only its own call.

    Args:
        monkeypatch: Replaces the JSON reader with one that overflows, as a deep caller stack does.
    """

    def overflow(value: Any, **_: Any) -> Any:
        """Raise as ``json.loads`` and ``json.dumps`` do when the caller's stack is already deep.

        Args:
            value: The arguments text or value.

        Raises:
            RecursionError: Always.
        """
        raise RecursionError(str(value)[:10])

    monkeypatch.setattr(protocol, "json", SimpleNamespace(loads=overflow, dumps=overflow))
    message = {
        "tool_calls": [
            {"id": "call_x", "type": "function", "function": {"name": "bash", "arguments": '{"command": "ls"}'}},
            {"id": "call_y", "type": "function", "function": {"name": "bash", "arguments": [["ls"]]}},
            {"id": "call_z", "type": "function", "function": {"name": "bash", "arguments": {"command": "pwd"}}},
        ]
    }

    assert tool_calls_from_openai(message) == [
        {"id": "call_x", "name": "bash", "arguments": TOO_DEEP_ARGUMENTS},
        {"id": "call_y", "name": "bash", "arguments": TOO_DEEP_ARGUMENTS},
        {"id": "call_z", "name": "bash", "arguments": {"command": "pwd"}},
    ]


def test_tool_calls_from_openai_keeps_arguments_at_the_depth_bound() -> None:
    """Arguments that nest exactly 97 levels stay; one level more gives ``TOO_DEEP_ARGUMENTS``."""
    # A literal, not the constant, so a change to the bound fails this test.
    bound = 97
    at_bound = "[" * bound + "]" * bound
    message = {
        "tool_calls": [
            {"id": "call_a", "type": "function", "function": {"name": "bash", "arguments": at_bound}},
            {"id": "call_b", "type": "function", "function": {"name": "bash", "arguments": f"[{at_bound}]"}},
            {"id": "call_c", "type": "function", "function": {"name": "bash", "arguments": _deep_list(bound)}},
            {"id": "call_d", "type": "function", "function": {"name": "bash", "arguments": _deep_list(bound + 1)}},
            {"id": "call_e", "type": "function", "function": {"name": "bash", "arguments": _deep_dict(bound)}},
            {"id": "call_f", "type": "function", "function": {"name": "bash", "arguments": _deep_dict(bound + 1)}},
        ]
    }

    assert [call["arguments"] for call in tool_calls_from_openai(message)] == [
        at_bound,
        TOO_DEEP_ARGUMENTS,
        json.dumps(_deep_list(bound)),
        TOO_DEEP_ARGUMENTS,
        _deep_dict(bound),
        TOO_DEEP_ARGUMENTS,
    ]


@pytest.mark.parametrize(
    ("text", "deeper"),
    [
        ('{"a": "' + "[" * 200 + '"}', False),
        ('{"a": "\\\\", "b": [[1]]}', True),
        ('{"a": "\\"[[["}', False),
        ("[[1]]", False),
        ("[[[1]]]", True),
        ("[[1], [2]]", False),
        ("]]][[[", True),
        ('{"a":1}}}}[[[', True),
    ],
    ids=[
        "brackets-in-string",
        "escaped-backslash",
        "escaped-quote",
        "at-bound",
        "past-bound",
        "siblings",
        "stray-closers-first",
        "stray-closers-after-a-value",
    ],
)
def test_text_nests_deeper_counts_only_brackets_outside_strings(text: str, deeper: bool) -> None:
    """Brackets inside JSON strings, quotes that a backslash escapes, and stray closers do not lower the bound.

    Args:
        text: The JSON text.
        deeper: Whether it opens more than two levels at one time.
    """
    assert protocol.text_nests_deeper(text, 2) is deeper


# Compact JSON: json.dumps adds spaces, so the test fails if the code dumps the parsed object again.
@pytest.mark.parametrize("arguments", ['{"text":"\\ud800"}', '{"k\\udfff":1}', '{"a":[{"b":"\\udc00"}]}', "\ud800"])
def test_tool_calls_from_openai_keeps_raw_text_when_the_parsed_arguments_hold_a_lone_surrogate(arguments: str) -> None:
    """A lone surrogate in a key or a value, at any depth, keeps the raw text, escaped, so the step log stores it."""
    message = {
        "tool_calls": [{"id": "call_x", "type": "function", "function": {"name": "bash", "arguments": arguments}}]
    }

    [call] = tool_calls_from_openai(message)

    assert call["arguments"] == arguments.encode("utf-8", "backslashreplace").decode("utf-8")
    json.dumps(call, ensure_ascii=False).encode("utf-8")  # The step log encodes so; a surrogate raises.


def test_safe_tool_call_escapes_surrogates_and_keeps_a_safe_call() -> None:
    """Parsed arguments with a surrogate become JSON text; the id and the name get escapes; a safe call stays."""
    safe = {"id": "call_x", "name": "bash", "arguments": {"command": "ls \u00e9"}}

    assert safe_tool_call(safe) == safe
    assert safe_tool_call({"id": "c\ud800", "name": "b\udfff", "arguments": {"k": ["\ud83d"]}}) == {
        "id": "c\\ud800",
        "name": "b\\udfff",
        "arguments": '{"k": ["\\ud83d"]}',
    }
    hebrew = safe_tool_call({"id": "c", "name": "b", "arguments": {"a": "\ud800 שלום"}})
    assert hebrew["arguments"] == '{"a": "\\ud800 שלום"}'


def test_flat_tool_shapes_round_trip_through_the_openai_form() -> None:
    """A flat definition sent in OpenAI form, and the OpenAI call it gets, convert back to the flat shapes."""
    definition = {"name": "bash", "description": "Run a command.", "input_schema": {"type": "object"}}
    sent = openai_tools([definition])[0]["function"]
    call = {"id": "call_1", "name": sent["name"], "arguments": {"command": "pytest -q"}}
    message = {
        "tool_calls": [
            {
                "id": call["id"],
                "type": "function",
                "function": {"name": call["name"], "arguments": json.dumps(call["arguments"])},
            }
        ]
    }

    assert {"name": sent["name"], "description": sent["description"], "input_schema": sent["parameters"]} == definition
    assert tool_calls_from_openai(message) == [call]


_DIRECTION_SCORES = {"low": 0.2, "mid": 0.5, "high": 0.8}


def _direction_scorer(candidate: Any, _case: Any) -> tuple[float, dict[str, Any]]:
    """Score each named version with a fixed raw score."""
    return _DIRECTION_SCORES[candidate], {}


def test_a_minimize_server_gives_the_engines_negated_scores_and_the_lowest_version_as_best() -> None:
    """Every engine maximizes, so a minimize run negates each score; raw_score turns it back (SKY-750)."""
    seen: list[float | None] = []
    server = EvalServer(
        _direction_scorer, max_evals=10, on_eval=lambda _server, score: seen.append(score), direction="minimize"
    )

    scores = [server.evaluate(name)[0] for name in ("mid", "low", "high")]

    assert server.minimizes
    assert scores == [-0.5, -0.2, -0.8]
    assert seen == [0.5, 0.2, 0.8]
    assert server.best_candidate == "low"
    assert server.raw_score(server.best_score) == 0.2
    assert server.raw_score(server.raw_score(0.3)) == 0.3
    assert server.raw_score(None) is None


def test_a_minimize_lane_follows_its_root_and_record_external_takes_a_raw_score() -> None:
    """A lane shares the root's direction, and an outside score is the metric's own score (SKY-750)."""
    seen: list[float | None] = []
    root = EvalServer(
        _direction_scorer, max_evals=10, on_eval=lambda _server, score: seen.append(score), direction="minimize"
    )
    lane = root.lane(5)

    lane.charge("ext", 0.1, {})

    assert lane.minimizes
    assert root.mean_score("ext") == -0.1
    assert lane.raw_score(lane.mean_score("ext")) == 0.1
    assert seen == [0.1]


def test_a_maximize_server_keeps_raw_scores() -> None:
    """The default direction leaves every score as the metric gave it."""
    server = EvalServer(_direction_scorer, max_evals=10)

    assert [server.evaluate(name)[0] for name in ("low", "high")] == [0.2, 0.8]
    assert not server.minimizes
    assert server.best_candidate == "high"
    assert server.raw_score(0.8) == 0.8
