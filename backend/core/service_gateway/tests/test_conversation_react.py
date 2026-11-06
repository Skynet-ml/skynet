"""Tests for the cache-stable conversation loop in :mod:`...agents.conversation_react`.

Pins the property the prompt cache depends on: every loop call, and every later
user turn, renders a message list that starts with exactly the messages the
call before it sent. Also pins the stored-history round trip and the replay of
plain transcripts as native history.
"""

from __future__ import annotations

import json
from typing import Any

import dspy
import litellm
import pytest

from ..agents.conversation_react import (
    AppendOnlyChatAdapter,
    ConversationReAct,
    dump_history,
    history_from_turns,
    load_history,
)


class _ScriptedLM(dspy.BaseLM):
    """An LM that records every request and answers with scripted tool calls."""

    def __init__(self, script: list[tuple[str, dict[str, Any]]], *, reasoning: bool = True) -> None:
        """Store the script.

        Args:
            script: One ``(tool name, arguments)`` pair per expected LM call.
            reasoning: Whether the LM claims native reasoning support.
        """
        super().__init__(model="scripted")
        self._script = list(script)
        self._reasoning = reasoning
        self.requests: list[dict[str, Any]] = []

    @property
    def supports_function_calling(self) -> bool:
        """Report native tool-call support."""
        return True

    @property
    def supports_reasoning(self) -> bool:
        """Report native reasoning support as configured."""
        return self._reasoning

    def forward(self, prompt=None, messages=None, **kwargs):
        """Record the request and return the next scripted tool call.

        Args:
            prompt: Unused legacy prompt.
            messages: The rendered chat messages.
            **kwargs: Provider kwargs, including ``tools``.

        Returns:
            An OpenAI-shaped response carrying one tool call.
        """
        self.requests.append({"messages": messages, **kwargs})
        name, args = self._script.pop(0)
        call = {
            "id": f"call_{len(self.requests)}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }
        content = None if self._reasoning else "[[ ## next_thought ## ]]\nthinking\n\n[[ ## completed ## ]]"
        message = {"role": "assistant", "content": content, "tool_calls": [call]}
        return litellm.ModelResponse(
            model="scripted",
            choices=[{"index": 0, "finish_reason": "tool_calls", "message": message}],
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )


def _lookup(name: str) -> str:
    """Return a fixed value for any name.

    Args:
        name: The thing to look up.

    Returns:
        A constant string.
    """
    return f"value of {name}"


def _program() -> ConversationReAct:
    """Build a one-tool conversation loop.

    Returns:
        The program under test.
    """
    signature = dspy.Signature("user_message: str -> reply: str", "Be helpful.")
    return ConversationReAct(signature, tools=[_lookup], max_iters=5)


def _stable(messages: list[dict[str, Any]], *, reasoning: bool) -> list[dict[str, Any]]:
    """Return the part of a request that the next request must start with.

    Args:
        messages: One request's messages.
        reasoning: Whether the LM handled reasoning natively. When it did not,
            the final message is the format reminder, which is never replayed.

    Returns:
        The replayed prefix.
    """
    return messages if reasoning else messages[:-1]


@pytest.mark.parametrize("reasoning", [True, False])
def test_each_call_extends_the_previous_prompt(reasoning: bool) -> None:
    """Within a turn and across turns, every request starts with the one before it."""
    script = [
        ("_lookup", {"name": "a"}),
        ("_lookup", {"name": "b"}),
        ("submit", {"reply": "first answer"}),
        ("_lookup", {"name": "c"}),
        ("submit", {"reply": "second answer"}),
    ]
    lm = _ScriptedLM(script, reasoning=reasoning)
    program = _program()

    with dspy.context(lm=lm):
        first = program(user_message="first question")
        stored = load_history(json.loads(json.dumps(dump_history(first.history))))
        second = program(user_message="second question", history=stored)

    assert first.reply == "first answer"
    assert second.reply == "second answer"
    assert len(lm.requests) == 5
    for earlier, later in zip(lm.requests, lm.requests[1:], strict=False):
        prefix = _stable(earlier["messages"], reasoning=reasoning)
        assert later["messages"][: len(prefix)] == prefix
        assert len(later["messages"]) > len(prefix)
        assert later["tools"] == earlier["tools"]


def test_tools_travel_natively_not_as_prompt_text() -> None:
    """The tool roster is sent on the ``tools`` channel and never rendered into a message."""
    lm = _ScriptedLM([("submit", {"reply": "done"})])

    with dspy.context(lm=lm):
        _program()(user_message="hello")

    request = lm.requests[0]
    assert {tool["function"]["name"] for tool in request["tools"]} == {"_lookup", "submit"}
    assert [message["role"] for message in request["messages"]] == ["system", "user"]
    assert request["messages"][1]["content"] == "[[ ## user_message ## ]]\nhello"
    assert "[[ ## completed ## ]]" not in request["messages"][0]["content"]
    assert "Be helpful." in request["messages"][0]["content"]


def test_transcript_replays_as_native_history() -> None:
    """Earlier plain-text turns render as user messages and ``submit`` calls."""
    history = history_from_turns(
        [("user", "hi"), ("assistant", "hello, how can I help?"), ("user", "one more thing")],
        input_field="user_message",
        output_field="reply",
    )
    lm = _ScriptedLM([("submit", {"reply": "sure"})])

    with dspy.context(lm=lm):
        _program()(user_message="do it", history=history)

    messages = lm.requests[0]["messages"]
    assert [message["role"] for message in messages] == ["system", "user", "assistant", "tool", "user", "user"]
    assert messages[1]["content"].endswith("hi")
    submitted = messages[2]["tool_calls"][0]["function"]
    assert submitted["name"] == "submit"
    assert json.loads(submitted["arguments"]) == {"reply": "hello, how can I help?"}
    assert messages[3]["tool_call_id"] == messages[2]["tool_calls"][0]["id"]
    assert messages[5]["content"].endswith("do it")


def test_stored_history_renders_like_the_live_one() -> None:
    """A history that went through JSON storage produces the same next prompt."""
    lm = _ScriptedLM([("_lookup", {"name": "a"}), ("submit", {"reply": "answer"})])
    program = _program()
    with dspy.context(lm=lm):
        prediction = program(user_message="question")

    adapter = AppendOnlyChatAdapter()
    signature = program.react.signature.delete("tools").delete("tool_calls").delete("next_thought")
    stored = load_history(json.loads(json.dumps(dump_history(prediction.history))))
    live = adapter.format(signature, [], {"history": prediction.history, "user_message": "next"})
    reloaded = adapter.format(signature, [], {"history": stored, "user_message": "next"})

    assert reloaded == live


class _ThinkingLM(_ScriptedLM):
    """A scripted LM whose first step thinks natively and whose second names no tool."""

    def forward(self, prompt=None, messages=None, **kwargs):
        """Think on the first step, skip the tool on the second, then follow the script.

        Args:
            prompt: Unused legacy prompt.
            messages: The rendered chat messages.
            **kwargs: Provider kwargs.

        Returns:
            An OpenAI-shaped response.
        """
        response = super().forward(prompt=prompt, messages=messages, **kwargs)
        message = response.choices[0].message
        if len(self.requests) == 1:
            message.reasoning_content = "PRIVATE THINKING"
        elif len(self.requests) == 2:
            message.tool_calls = None
            message.content = "no tool"
        return response


def test_forced_submit_never_replays_native_thinking() -> None:
    """The forced submit neither replays earlier thinking nor asks for reasoning as text.

    Anthropic's safeguards block that prompt as reasoning extraction.
    """
    lm = _ThinkingLM([("_lookup", {"name": "a"}), ("_lookup", {"name": "b"}), ("submit", {"reply": "done"})])

    with dspy.context(lm=lm):
        prediction = _program()(user_message="question")

    forced = lm.requests[-1]
    assert forced["tool_choice"] == {"type": "function", "function": {"name": "submit"}}
    rendered = json.dumps(forced["messages"])
    assert "PRIVATE THINKING" not in rendered
    assert "next_thought" not in rendered
    assert prediction.reply == "done"
    assert prediction.history.messages[0]["next_thought"].content == "PRIVATE THINKING"


def test_submit_guard_refusal_keeps_the_loop_going() -> None:
    """A refused submit reads as a tool result; the next submit ends the turn."""
    lm = _ScriptedLM([("submit", {"reply": "done"}), ("submit", {"reply": "done?"})])
    seen: list[str] = []

    def guard(outputs: dict[str, Any]) -> str | None:
        """Refuse a reply with no question mark."""
        seen.append(outputs["reply"])
        return None if "?" in outputs["reply"] else "Ask a question."

    signature = dspy.Signature("user_message: str -> reply: str", "Be helpful.")
    program = ConversationReAct(signature, tools=[_lookup], max_iters=5, submit_guard=guard)
    with dspy.context(lm=lm):
        result = program(user_message="hi")
    assert result.reply == "done?"
    assert seen == ["done", "done?"]
    assert "Ask a question." in json.dumps(dump_history(result.history))


def test_submit_guard_never_refuses_the_forced_submit() -> None:
    """At the loop's budget the forced submit ends the turn even if the guard objects."""
    lm = _ScriptedLM([("submit", {"reply": "done"}), ("submit", {"reply": "still done"})])
    signature = dspy.Signature("user_message: str -> reply: str", "Be helpful.")
    program = ConversationReAct(signature, tools=[_lookup], max_iters=1, submit_guard=lambda _: "No.")
    with dspy.context(lm=lm):
        result = program(user_message="hi")
    assert result.reply == "still done"
    assert result.termination_reason == "forced_submit"
