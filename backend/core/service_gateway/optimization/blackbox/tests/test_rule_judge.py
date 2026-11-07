"""Tests for the path-rule block record and the intent-only rule watch hook."""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

import pytest

from core import run_log

from ..rule_judge import (
    DEFAULT_WARN_CONFIDENCE,
    EVENT_BLOCK,
    EVENT_NOTE,
    EVENT_VERDICT,
    ILLEGAL_ACTION_MESSAGE,
    NO_TOOL_CALL_STEP,
    WATCH_EVERY_ACTIONS,
    WITHHELD_STEP,
    RuleJudge,
    describe_model_call,
    run_log_emitter,
    watch_prompt,
)

_RULES = ("Do not hardcode answers for dataset cases.", "Do not call an outside geocoding service.")
_INTENT = "Make the bot find the place a message names more accurately."


class _Hook:
    """Answer watch prompts with fixed replies and remember every prompt."""

    def __init__(self, *replies: str | None) -> None:
        """Queue the replies, the last one repeating.

        Args:
            replies: Model replies in order.
        """
        self.replies = list(replies)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str | None:
        """Return the next reply.

        Args:
            prompt: The watch prompt.

        Returns:
            The queued reply.
        """
        self.prompts.append(prompt)
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


def _answer(label: str, confidence: float = 0.9, reason: str = "fine") -> str:
    """Write a watch hook reply.

    Args:
        label: The verdict label.
        confidence: The verdict confidence.
        reason: The hook's reason.

    Returns:
        The reply text.
    """
    return json.dumps({"label": label, "confidence": confidence, "reason": reason})


def _judge(
    hook: _Hook | None = None, *, threshold: float = DEFAULT_WARN_CONFIDENCE
) -> tuple[RuleJudge, list[dict[str, Any]]]:
    """Build a watching judge whose events are collected and whose watch runs inline.

    Args:
        hook: The hook model stub.
        threshold: The warning threshold.

    Returns:
        The judge and its event list.
    """
    events: list[dict[str, Any]] = []
    judge = RuleJudge(
        _RULES,
        intent=_INTENT,
        ask=hook or _Hook(_answer("ok")),
        emit=events.append,
        threshold=threshold,
        run_watch=lambda watch: watch(),
    )
    return judge, events


def _act(judge: RuleJudge, count: int, prefix: str = "edit") -> None:
    """Record a number of agent steps.

    Args:
        judge: The judge.
        count: How many steps.
        prefix: The step text before its index.
    """
    for index in range(count):
        judge.record_action(f"{prefix} {index}")


def test_a_blocked_version_gets_the_fixed_message_and_an_event_with_the_reason() -> None:
    """The agent reads only the fixed text; the run's events keep the path and the rule."""
    judge, events = _judge()

    message = judge.blocked(["'data/labels.csv' is protected by the repository's optimize.toml ([paths] protected)."])

    assert message == (
        "Illegal action. Read optimize.toml and the rule files it names. You cannot do this action."
        " Do not repeat it, because it wastes time."
    )
    assert message == ILLEGAL_ACTION_MESSAGE
    assert events == [
        {
            "kind": EVENT_BLOCK,
            "rule": "optimize.toml [paths]",
            "reason": "'data/labels.csv' is protected by the repository's optimize.toml ([paths] protected).",
        }
    ]


def test_the_watch_runs_every_fifteen_steps_over_the_last_fifteen() -> None:
    """The watch reads only the last 15 steps, on every 15th step."""
    hook = _Hook(_answer("ok"))
    judge, _ = _judge(hook)

    _act(judge, WATCH_EVERY_ACTIONS - 1, "old")
    assert hook.prompts == []
    judge.record_action("old 14")
    assert len(hook.prompts) == 1
    _act(judge, WATCH_EVERY_ACTIONS, "new")

    assert len(hook.prompts) == 2
    second_steps = hook.prompts[1].split("<steps>", 1)[1]
    assert "new 0" in second_steps
    assert "new 14" in second_steps
    assert "old" not in second_steps


def test_a_sure_flag_warns_the_agent_once_and_logs_the_verdict() -> None:
    """A non-ok label at the threshold queues one note and logs the verdict and the note."""
    hook = _Hook(_answer("breaks_rule", 0.7, "The agent reads the docs of a hosted geocoding API."))
    judge, events = _judge(hook)

    _act(judge, WATCH_EVERY_ACTIONS)

    assert judge.take_notes() == [
        "Check the rule file that optimize.toml names. You are probably about to break one of its rules:"
        " The agent reads the docs of a hosted geocoding API."
    ]
    assert judge.take_notes() == []
    assert events == [
        {
            "kind": EVENT_VERDICT,
            "label": "breaks_rule",
            "confidence": 0.7,
            "reason": "The agent reads the docs of a hosted geocoding API.",
            "warned": True,
        },
        {"kind": EVENT_NOTE, "label": "breaks_rule", "reason": "The agent reads the docs of a hosted geocoding API."},
    ]


def test_an_off_intent_flag_names_the_intent() -> None:
    """Drift from the intent gets its own warning text."""
    judge, _ = _judge(_Hook(_answer("off_intent", 0.95, "It tunes only for the test sentences.")))

    _act(judge, WATCH_EVERY_ACTIONS)

    (note,) = judge.take_notes()
    assert note.startswith("Check the run's intent in the instructions file that optimize.toml names.")
    assert note.endswith("It tunes only for the test sentences.")


@pytest.mark.parametrize(
    ("reply", "threshold"),
    [
        (_answer("breaks_rule", 0.69, "unsure"), DEFAULT_WARN_CONFIDENCE),
        (_answer("breaks_rule", 0.85, "fairly sure"), 0.9),
        (_answer("ok", 1.0, "ordinary work"), DEFAULT_WARN_CONFIDENCE),
    ],
)
def test_an_unsure_or_ok_verdict_is_logged_but_never_warns(reply: str, threshold: float) -> None:
    """Below the run's threshold, or with an ok label, the agent gets no note; the verdict is still logged."""
    judge, events = _judge(_Hook(reply), threshold=threshold)

    _act(judge, WATCH_EVERY_ACTIONS)

    assert judge.take_notes() == []
    assert [event["kind"] for event in events] == [EVENT_VERDICT]
    assert events[0]["warned"] is False


@pytest.mark.parametrize("reply", [None, "not json", '{"label": "stop_run", "confidence": 1, "reason": "x"}'])
def test_an_unreadable_answer_is_dropped_and_never_stops_the_run(reply: str | None) -> None:
    """A bad hook answer leaves no note and no verdict, and the next watch still runs."""
    hook = _Hook(reply)
    judge, events = _judge(hook)

    _act(judge, WATCH_EVERY_ACTIONS * 2)

    assert len(hook.prompts) == 2
    assert judge.take_notes() == []
    assert events == []


def test_the_prompt_holds_the_intent_the_rules_and_the_steps_only() -> None:
    """The prompt carries the intent, the numbered rules, the label format and the steps; no metric."""
    prompt = watch_prompt(_INTENT, _RULES, ["STEP-ONE"])

    assert _INTENT in prompt
    assert "Rule 2: Do not call an outside geocoding service." in prompt
    assert "STEP-ONE" in prompt.split("<steps>", 1)[1]
    for label in ("ok", "off_intent", "breaks_rule"):
        assert label in prompt
    assert "metric" not in prompt.lower()
    assert "score" not in prompt.lower()


def test_the_prompt_works_with_only_an_intent_or_only_rules() -> None:
    """A repository may give just one of the two."""
    assert "(no rules)" in watch_prompt(_INTENT, (), ["step"])
    assert "(no intent given)" in watch_prompt(None, _RULES, ["step"])


def test_a_judge_without_a_hook_model_never_watches() -> None:
    """With the hook off, steps are not kept and no model is asked; blocks are still recorded."""
    events: list[dict[str, Any]] = []
    judge = RuleJudge(_RULES, intent=_INTENT, ask=None, emit=events.append, run_watch=lambda watch: watch())

    _act(judge, WATCH_EVERY_ACTIONS * 2)
    judge.blocked(["outside"])

    assert not judge.watching
    assert judge.take_notes() == []
    assert [event["kind"] for event in events] == [EVENT_BLOCK]


def test_the_watch_runs_off_the_agent_path_and_a_failure_is_dropped() -> None:
    """By default the watch runs on its own thread, and a failed call leaves no note."""
    called = threading.Event()

    def broken(_prompt: str) -> str | None:
        """Fail like a refused model call.

        Args:
            _prompt: The watch prompt.

        Raises:
            RuntimeError: Always.
        """
        called.set()
        raise RuntimeError("budget reached")

    judge = RuleJudge(_RULES, intent=_INTENT, ask=broken, emit=lambda _event: None)
    _act(judge, WATCH_EVERY_ACTIONS)

    assert called.wait(5)
    assert judge.take_notes() == []


_SCORE_REPLY = json.dumps({"score": 0.4217, "info": {"feedback": "FEEDBACK-SECRET: case 3 is wrong"}})


def _chat_call(name: str, arguments: dict[str, Any], result: str) -> dict[str, Any]:
    """Build a chat-format model call that ends with one tool call and its result.

    Args:
        name: The tool name.
        arguments: The tool input.
        result: The tool result text.

    Returns:
        The request body.
    """
    return {
        "messages": [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "PROMPT"},
            {
                "role": "assistant",
                "content": "THINKING",
                "tool_calls": [{"id": "t1", "function": {"name": name, "arguments": json.dumps(arguments)}}],
            },
            {"role": "tool", "tool_call_id": "t1", "content": result},
        ]
    }


def _messages_call(name: str, tool_input: dict[str, Any], result: str) -> dict[str, Any]:
    """Build a messages-format model call that ends with one tool call and its result.

    Args:
        name: The tool name.
        tool_input: The tool input.
        result: The tool result text.

    Returns:
        The request body.
    """
    return {
        "system": "SYSTEM",
        "messages": [
            {"role": "user", "content": "PROMPT"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "THINKING"},
                    {"type": "tool_use", "id": "t1", "name": name, "input": tool_input},
                ],
            },
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": result}]},
        ],
    }


def _responses_call(name: str, arguments: dict[str, Any], result: str) -> dict[str, Any]:
    """Build a responses-format model call that ends with one tool call and its result.

    Args:
        name: The tool name.
        arguments: The tool input.
        result: The tool result text.

    Returns:
        The request body.
    """
    return {
        "instructions": "SYSTEM",
        "input": [
            {"role": "user", "content": "PROMPT"},
            {"type": "reasoning", "summary": [{"text": "THINKING"}]},
            {"type": "function_call", "call_id": "t1", "name": name, "arguments": json.dumps(arguments)},
            {"type": "function_call_output", "call_id": "t1", "output": result},
        ],
    }


_FORMATS = pytest.mark.parametrize("build", [_chat_call, _messages_call, _responses_call])


@_FORMATS
def test_a_step_is_the_latest_tool_call_and_its_input_only(build: Any) -> None:
    """A step keeps the tool name and input; the result, the agent's text and the prompts never reach the hook."""
    step = describe_model_call(build("shell", {"command": "pytest tests -q"}, "RESULT-TEXT"))

    assert "shell" in step
    assert "pytest tests -q" in step
    for absent in ("RESULT-TEXT", "THINKING", "PROMPT", "SYSTEM"):
        assert absent not in step


def test_an_edit_call_keeps_its_path_and_content() -> None:
    """The hook reads what the agent writes, so it can spot a hardcoded answer."""
    step = describe_model_call(
        _messages_call("Edit", {"file_path": "repo/bot.py", "new_string": "ANSWERS = {'case 3': 'Paris'}"}, "ok")
    )

    assert "repo/bot.py" in step
    assert "ANSWERS = {'case 3': 'Paris'}" in step


def test_parallel_calls_all_reach_the_hook_but_older_calls_do_not() -> None:
    """The latest group of calls is the step; calls before the last results were seen by earlier steps."""
    body = {
        "input": [
            {"type": "function_call", "name": "shell", "arguments": '{"command": "OLD-CALL"}'},
            {"type": "function_call_output", "output": "old"},
            {"type": "function_call", "name": "shell", "arguments": '{"command": "FIRST-CALL"}'},
            {"type": "custom_tool_call", "name": "apply_patch", "input": "SECOND-CALL"},
            {"type": "function_call_output", "output": "one"},
            {"type": "custom_tool_call_output", "output": "two"},
        ]
    }

    step = describe_model_call(body)

    assert "FIRST-CALL" in step
    assert "SECOND-CALL" in step
    assert "OLD-CALL" not in step


@pytest.mark.parametrize(
    "body",
    [
        {"messages": [{"role": "system", "content": "SYSTEM"}, {"role": "user", "content": "run tests"}]},
        {"messages": [{"role": "assistant", "content": "I will think first."}]},
        {"input": "a plain prompt"},
        {},
    ],
)
def test_a_call_without_a_tool_call_is_a_fixed_step(body: dict[str, Any]) -> None:
    """Text alone carries no tool input, so the hook gets a fixed line and no prompt text."""
    assert describe_model_call(body) == NO_TOOL_CALL_STEP


def test_a_long_tool_input_is_cut() -> None:
    """Only the start of a long input is kept, with the tool name, so a watch prompt stays small."""
    step = describe_model_call(_chat_call("write", {"path": "repo/a.py", "content": "A" * 50_000}, "ok"))

    assert step.startswith('[{"tool": "write"')
    assert len(step) <= 2_000


def test_rule_events_reach_the_run_log(caplog: pytest.LogCaptureFixture) -> None:
    """Each event is a typed host record owned by the run, so the run log and its readers see it."""
    stream = logging.getLogger("core.service_gateway.optimization.blackbox.sandbox_log.stream")
    stream.addHandler(caplog.handler)
    try:
        run_log_emitter("run-1")({"kind": EVENT_VERDICT, "label": "breaks_rule", "reason": "A lookup table."})
    finally:
        stream.removeHandler(caplog.handler)

    (record,) = caplog.records
    assert record.sandbox_owner == "run-1"
    assert run_log.entry_fields(record)["event"] == EVENT_VERDICT
    assert run_log.entry_fields(record)["source"] == "host"
    assert "label=breaks_rule" in record.getMessage()
    assert record.levelno == logging.INFO
    assert "A lookup table." in record.getMessage()


_STATE_MD = (
    "# Round 2\n\n## Leaderboard\n\n| id | score |\n| --- | --- |\n| c001 | 0.4217 |\n\n"
    "## Failure dossier\n\n- `ex7` scored 0.100: FEEDBACK-SECRET expected 'Paris' but got 'paris'\n"
)


@_FORMATS
@pytest.mark.parametrize(
    ("name", "tool_input", "result"),
    [
        ("Read", {"file_path": "/work/STATE.md"}, _STATE_MD),
        ("shell", {"command": "cat archive/index.tsv"}, "id\tscore\tround\nc001\t0.4217\t1\n"),
        ("shell", {"command": "./eval.sh case-3"}, _SCORE_REPLY),
        ("shell", {"command": "sed -i 's/a/b/' eval.sh"}, ""),
    ],
)
def test_a_call_that_touches_an_engine_file_is_withheld(
    build: Any, name: str, tool_input: dict[str, Any], result: str
) -> None:
    """STATE.md, archive/ and eval.sh hold or make the scores and the scorer's feedback, so the call is withheld."""
    step = describe_model_call(build(name, tool_input, result))
    prompt = watch_prompt(_INTENT, _RULES, [step])

    assert step == WITHHELD_STEP
    assert "0.4217" not in prompt
    assert "FEEDBACK-SECRET" not in prompt


@_FORMATS
@pytest.mark.parametrize(
    "command",
    ["jq -r .info.feedback r.json", "python -c \"import json; print(json.load(open('r.json'))['score'])\""],
)
def test_derived_scorer_output_never_reaches_the_hook(build: Any, command: str) -> None:
    """A command that prints the score or feedback reaches the hook, but its output never does."""
    step = describe_model_call(build("shell", {"command": command}, _SCORE_REPLY))
    prompt = watch_prompt(_INTENT, _RULES, [step])

    assert "r.json" in step
    assert "0.4217" not in prompt
    assert "FEEDBACK-SECRET" not in prompt
