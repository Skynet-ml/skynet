"""Tests for header-less tool calls on the text protocol of :mod:`...agents.agent_program`.

A subscription CLI route has no native tools, so the loop runs DSPy's text
protocol, and the Pi model relay parses it. With native reasoning on, ``tool_calls`` is the only output field, and
Claude sometimes writes its ``{"tool_calls": [...]}`` value without the
``[[ ## tool_calls ## ]]`` header. The LM texts below are copied from the
app's ``api.log``.
"""

from __future__ import annotations

import dspy
import pytest
from dspy.utils.exceptions import AdapterParseError

from ..agents.agent_program import AgentProgram, AppendOnlyChatAdapter

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
    program = AgentProgram(signature, tools=[_lookup], max_iters=3)
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
