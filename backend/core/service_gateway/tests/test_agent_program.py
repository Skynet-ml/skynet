"""Tests for the program definition in :mod:`...agents.agent_program`.

Pi runs the loop, so these tests pin only what the definition still owns: the
transcript replay as native history, its rendering with the append-only
adapter, and the ``submit`` guard. ``test_pi_react.py`` covers the turn.
"""

from __future__ import annotations

import json
from typing import Any

import dspy

from ..agents.agent_program import SUBMIT_TOOL, AgentProgram, AppendOnlyChatAdapter, history_from_turns


def _lookup(name: str) -> str:
    """Return a fixed value for any name.

    Args:
        name: The thing to look up.

    Returns:
        A constant string.
    """
    return f"value of {name}"


def _program(**kwargs: Any) -> AgentProgram:
    """Build a one-tool program definition.

    Args:
        **kwargs: Extra :class:`AgentProgram` arguments.

    Returns:
        The program under test.
    """
    signature = dspy.Signature("user_message: str -> reply: str", "Be helpful.")
    return AgentProgram(signature, tools=[_lookup], max_iters=5, **kwargs)


def test_definition_has_the_tools_and_the_step_signature() -> None:
    """The definition holds the user's tools plus ``submit`` and the loop's step predictor."""
    program = _program()

    assert set(program.tools) == {"_lookup", SUBMIT_TOOL}
    assert {"tools", "tool_calls"} <= set(program.react.signature.fields)
    assert program.max_iters == 5


def test_transcript_replays_as_native_history() -> None:
    """Earlier plain-text turns render as user messages and ``submit`` calls."""
    history = history_from_turns(
        [("user", "hi"), ("assistant", "hello, how can I help?"), ("user", "one more thing")],
        input_field="user_message",
        output_field="reply",
    )
    signature = _program().react.signature.delete("tools").delete("tool_calls").delete("next_thought")

    messages = AppendOnlyChatAdapter().format(signature, [], {"history": history, "user_message": "do it"})

    assert [message["role"] for message in messages] == ["system", "user", "assistant", "tool", "user", "user"]
    assert messages[1]["content"].endswith("hi")
    submitted = messages[2]["tool_calls"][0]["function"]
    assert submitted["name"] == SUBMIT_TOOL
    assert json.loads(submitted["arguments"]) == {"reply": "hello, how can I help?"}
    assert messages[3]["tool_call_id"] == messages[2]["tool_calls"][0]["id"]
    assert messages[5]["content"].endswith("do it")


def test_submit_guard_refusal_is_a_tool_result() -> None:
    """A refused submit returns the refusal text; an accepted one returns the outputs."""
    seen: list[str] = []

    def guard(outputs: dict[str, Any]) -> str | None:
        """Refuse a reply with no question mark.

        Args:
            outputs: The submit call's outputs.

        Returns:
            The refusal, or ``None``.
        """
        seen.append(outputs["reply"])
        return None if "?" in outputs["reply"] else "Ask a question."

    submit = _program(submit_guard=guard).tools[SUBMIT_TOOL]

    assert submit(reply="done") == "Ask a question."
    assert submit(reply="done?") == {"reply": "done?"}
    assert seen == ["done", "done?"]


def test_submit_guard_never_refuses_the_forced_submit() -> None:
    """While the turn forces ``submit``, the guard does not run."""
    program = _program(submit_guard=lambda _: "No.")
    program._forcing = True

    assert program.tools[SUBMIT_TOOL](reply="still done") == {"reply": "still done"}
