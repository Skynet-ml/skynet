"""A ``dspy.ReActV2`` for the chat agents that does not end on a step with no tool call.

Stock ReActV2 ends the loop the first time a step comes back without a tool
call. A reasoning model that spends its whole output budget thinking (drafting
the full artifact in its reasoning, or falling into a repetition loop) returns
exactly that, and the forced ``submit`` that follows can only reply, never
apply the edit it was about to make. The user then sees an empty turn while the
editor is unchanged.

This module retries such a step once with low reasoning effort and, under
native tool calling, ``tool_choice="required"``, so the model acts on what it already worked out instead of
thinking again from the top.
"""

from __future__ import annotations

import logging

import dspy
from dspy.utils.exceptions import AdapterParseError

from ..react_compat import native_tool_calling_active, without_text_reasoning
from .steering import deliver_steering

logger = logging.getLogger(__name__)


def _has_tool_calls(prediction: dspy.Prediction) -> bool:
    """Tell whether a loop step asked for at least one tool.

    Args:
        prediction: One inner-loop prediction.

    Returns:
        ``True`` when the prediction carries a tool call.
    """
    envelope = getattr(prediction, "tool_calls", None)
    if envelope is None:
        return False
    calls = envelope.get("tool_calls") if isinstance(envelope, dict) else getattr(envelope, "tool_calls", None)
    return bool(calls)


def _low_effort(predictor: dspy.Predict, config: dict) -> dict:
    """Build the LM kwargs that drop a retry to low reasoning effort.

    Gateway LMs carry the effort twice, as ``reasoning_effort`` and as
    ``extra_body.reasoning.effort``, and OpenRouter rejects a request whose two
    values disagree, so both are lowered together. A per-call ``extra_body``
    replaces the LM's whole one, so the LM's body is copied first.

    Args:
        predictor: The step predictor, whose own LM wins over the context LM.
        config: The step's per-call LM kwargs.

    Returns:
        ``reasoning_effort`` set to ``"low"``, plus a matching ``extra_body``
        when the LM or the call sends a native ``reasoning`` object.
    """
    lm = predictor.lm or dspy.settings.lm
    lm_kwargs = getattr(lm, "kwargs", None) or {}
    body = config.get("extra_body", lm_kwargs.get("extra_body"))
    out: dict = {"reasoning_effort": "low"}
    if isinstance(body, dict) and isinstance(body.get("reasoning"), dict) and "effort" in body["reasoning"]:
        out["extra_body"] = {**body, "reasoning": {**body["reasoning"], "effort": "low"}}
    return out


class _ActingPredict(dspy.Predict):
    """The loop's step predictor, retried once when a step names no tool."""

    def forward(self, **kwargs):
        """Predict one loop step, retrying a step that ended without a tool call.

        The forced ``submit`` call already pins ``tool_choice``; it is passed
        through so its own fallback behaviour stays DSPy's, minus the text
        reasoning field that would replay earlier thinking into the prompt.

        Args:
            **kwargs: The per-call inputs ReActV2 passes, optionally with a
                ``config`` dict of LM kwargs.

        Returns:
            The step's prediction, from the retry when the first one had no
            tool call or failed to parse.

        Raises:
            AdapterParseError: When the retry also fails to parse.
            ValueError: When the retry raises a value error from the adapter.
        """
        deliver_steering(self, kwargs)
        config = dict(kwargs.get("config") or {})
        if "tool_choice" in config:
            return super().forward(**without_text_reasoning(self.signature, kwargs))
        try:
            prediction = super().forward(**kwargs)
            if _has_tool_calls(prediction):
                return prediction
            logger.warning("Agent step ended without a tool call; retrying it with a required tool call.")
        except (AdapterParseError, ValueError) as err:
            logger.warning("Agent step failed to parse; retrying it with a required tool call: %s", err)
        retry_config = {**config, **_low_effort(self, config)}
        # The text tool protocol sends no tools, and providers reject a
        # tool_choice without them.
        if native_tool_calling_active():
            retry_config["tool_choice"] = "required"
        return super().forward(**{**kwargs, "config": retry_config})


class ActingReActV2(dspy.ReActV2):
    """A ReActV2 whose steps are retried once instead of ending the loop with no tool call."""

    def __init__(self, signature, tools, max_iters: int = 20):
        """Build the stock loop, then swap in the retrying step predictor.

        Args:
            signature: Task signature, as for ``dspy.ReActV2``.
            tools: Tool roster, as for ``dspy.ReActV2``.
            max_iters: Loop budget, as for ``dspy.ReActV2``.
        """
        super().__init__(signature, tools, max_iters=max_iters)
        self.react = _ActingPredict(self.react.signature)
