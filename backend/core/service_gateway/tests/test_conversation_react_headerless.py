"""Tests for header-less tool calls on the text protocol of :mod:`...agents.conversation_react`.

A subscription CLI route has no native tools, so the loop runs DSPy's text
protocol. With native reasoning on, ``tool_calls`` is the only output field, and
Claude sometimes writes its ``{"tool_calls": [...]}`` value without the
``[[ ## tool_calls ## ]]`` header. The LM texts below are copied from the
app's ``api.log``.
"""

from __future__ import annotations

from typing import Any

import dspy
import litellm
import pytest
from dspy.utils.exceptions import AdapterParseError

from ..agents.conversation_react import AppendOnlyChatAdapter, ConversationReAct

_PLAN_AND_LIST = (
    '{"tool_calls": [{"name": "share_plan", "args": {"plan": "I\'ll list your existing optimizations first so '
    'you can confirm exactly what will be permanently deleted."}}, {"name": "list_jobs_optimizations_get", '
    '"args": {}}]}\n\n[[ ## completed ## ]]'
)

_SUBMIT = (
    '{"tool_calls": [{"name": "submit", "args": {"assistant_message": "I can\'t open the Skynet documentation '
    "for you.\\n\\nHere's what I can do instead:\\n- **Open the About page** in settings.\"}}]}"
    "\n\n[[ ## completed ## ]]"
)


def _inner_signature() -> type[dspy.Signature]:
    """Build the loop's inner signature as the text protocol renders it with native reasoning on.

    Returns:
        The ReActV2 inner signature without its ``next_thought`` field.
    """
    signature = dspy.Signature("user_message: str -> assistant_message: str", "Be helpful.")
    program = ConversationReAct(signature, tools=[_lookup], max_iters=3)
    return program.react.signature.delete("next_thought")


def _lookup(name: str) -> str:
    """Return a fixed value for any name.

    Args:
        name: The thing to look up.

    Returns:
        A constant string.
    """
    return f"value of {name}"


def test_parses_parallel_calls_without_header() -> None:
    """A header-less JSON reply with two tool calls parses into both calls."""
    fields = AppendOnlyChatAdapter().parse(_inner_signature(), _PLAN_AND_LIST)

    calls = fields["tool_calls"].tool_calls
    assert [call.name for call in calls] == ["share_plan", "list_jobs_optimizations_get"]
    assert calls[1].args == {}


def test_parses_submit_without_header() -> None:
    """A header-less submit keeps the reply text intact."""
    fields = AppendOnlyChatAdapter().parse(_inner_signature(), _SUBMIT)

    (call,) = fields["tool_calls"].tool_calls
    assert call.name == "submit"
    assert call.args["assistant_message"].startswith("I can't open the Skynet documentation")
    assert "**Open the About page**" in call.args["assistant_message"]


def test_headed_reply_still_parses() -> None:
    """The normal text protocol is unchanged."""
    fields = AppendOnlyChatAdapter().parse(_inner_signature(), f"[[ ## tool_calls ## ]]\n{_SUBMIT}")

    assert fields["tool_calls"].tool_calls[0].name == "submit"


@pytest.mark.parametrize(
    "completion",
    [
        "Sure, here is my answer.\n\n[[ ## completed ## ]]",
        '{"answer": "hi"}\n\n[[ ## completed ## ]]',
    ],
)
def test_other_failures_still_raise(completion: str) -> None:
    """Prose or a JSON object that is not tool calls still fails, so the predictor still resamples.

    Args:
        completion: An LM reply that is not a valid header-less tool-call object.
    """
    with pytest.raises(AdapterParseError) as caught:
        AppendOnlyChatAdapter().parse(_inner_signature(), completion)

    assert caught.value.lm_response == completion


class _TextOnlyLM(dspy.BaseLM):
    """An LM without native tools, like a subscription CLI route, that answers with fixed texts."""

    def __init__(self, replies: list[str]) -> None:
        """Store the replies.

        Args:
            replies: One raw assistant text per expected LM call.
        """
        super().__init__(model="scripted")
        self._replies = list(replies)
        self.requests: list[dict[str, Any]] = []

    @property
    def supports_reasoning(self) -> bool:
        """Report native reasoning support, which removes ``next_thought`` from the text protocol."""
        return True

    def forward(self, prompt=None, messages=None, **kwargs):
        """Record the request and return the next text.

        Args:
            prompt: Unused legacy prompt.
            messages: The rendered chat messages.
            **kwargs: Provider kwargs.

        Returns:
            An OpenAI-shaped text response.
        """
        self.requests.append({"messages": messages, **kwargs})
        message = {"role": "assistant", "content": self._replies.pop(0)}
        return litellm.ModelResponse(
            model="scripted",
            choices=[{"index": 0, "finish_reason": "stop", "message": message}],
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )


def test_loop_answers_through_header_less_turns() -> None:
    """The loop runs the tool and submits without a resample or a forced submit."""
    lm = _TextOnlyLM(
        [
            '{"tool_calls": [{"name": "_lookup", "args": {"name": "a"}}]}\n\n[[ ## completed ## ]]',
            _SUBMIT,
        ]
    )
    signature = dspy.Signature("user_message: str -> assistant_message: str", "Be helpful.")
    program = ConversationReAct(signature, tools=[_lookup], max_iters=3)

    with dspy.context(lm=lm):
        prediction = program(user_message="Open the docs.")

    assert prediction.termination_reason == "submit"
    assert prediction.assistant_message.startswith("I can't open the Skynet documentation")
    assert len(lm.requests) == 2
    assert "tools" not in lm.requests[0]
