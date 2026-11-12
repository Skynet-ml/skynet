"""Tests for the shared interview-turn driver.

The streamify loop is replaced by a scripted generator, so no LLM or network
is touched: the tests pin the event sequence the intake interview
relays and the retry / salvage behavior.
"""

from __future__ import annotations

import asyncio
from typing import Any

import dspy
import pytest

from core.service_gateway.agents import code_interview
from core.service_gateway.agents.code_interview import (
    _parse_json,
    relay_interview_turn,
)


def _chunk(field: str, text: str, last: bool) -> dspy.streaming.StreamResponse:
    """Build one streamed chunk of an output field."""
    return dspy.streaming.StreamResponse(
        predict_name="predict", signature_field_name=field, chunk=text, is_last_chunk=last
    )


def _script(*attempts: list[Any] | Exception):
    """Return a streamify stand-in that replays one scripted attempt per call.

    Args:
        *attempts: Per attempt, the chunks to yield, or an exception to raise
            before yielding anything.

    Returns:
        A callable shaped like ``dspy.streamify``.
    """
    queue = list(attempts)

    def fake_streamify(*_args: Any, **_kwargs: Any):
        """Hand out the next scripted attempt as an async program."""
        attempt = queue.pop(0)

        async def program(**_inputs: Any):
            """Yield the scripted chunks, or raise the scripted error."""
            if isinstance(attempt, Exception):
                raise attempt
            for item in attempt:
                yield item

        return program

    return fake_streamify


async def _collect(**kwargs: Any) -> list[dict]:
    """Drain :func:`relay_interview_turn` into a list."""
    return [event async for event in relay_interview_turn(**kwargs)]


def _relay_kwargs(parse=None) -> dict[str, Any]:
    """Build the relay's keyword arguments with a stub LM and parser."""
    return {
        "predict": object(),
        "lm": dspy.LM("openai/test-model"),
        "inputs": {"x": "1"},
        "asked": 2,
        "model": "openai/test-model",
        "parse": parse or (lambda pred, asked: {"message": pred.message, "asked": asked}),
    }


def test_parse_json_falls_back_on_garbage() -> None:
    """Empty or unparseable text yields the fallback, valid JSON parses."""
    assert _parse_json("", []) == []
    assert _parse_json("{broken", {}) == {}
    assert _parse_json('["a"]', []) == ["a"]


def test_relay_streams_message_hint_and_done(monkeypatch: pytest.MonkeyPatch) -> None:
    """Message chunks, the ``done`` hint and the parsed terminal event arrive in order."""
    monkeypatch.setattr(
        code_interview.dspy,
        "streamify",
        _script(
            [
                _chunk("message", "Hel", False),
                _chunk("message", "lo", True),
                _chunk("done", "tr", False),
                _chunk("done", "ue", True),
                dspy.Prediction(message="Hello", done="true"),
            ]
        ),
    )
    events = asyncio.run(_collect(**_relay_kwargs()))
    names = [e["event"] for e in events]
    assert names == ["message_patch", "message_patch", "message_end", "turn_hint", "interview_done"]
    assert events[3]["data"] == {"final": True}
    done = events[-1]["data"]
    assert done["message"] == "Hello"
    assert done["asked"] == 2
    assert done["model"] == "openai/test-model"


def test_relay_retries_once_with_a_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed first attempt is retried after a ``message_reset``."""
    monkeypatch.setattr(
        code_interview.dspy,
        "streamify",
        _script(RuntimeError("boom"), [dspy.Prediction(message="ok", done="false")]),
    )
    events = asyncio.run(_collect(**_relay_kwargs()))
    assert [e["event"] for e in events] == ["message_reset", "interview_done"]
    assert events[-1]["data"]["message"] == "ok"


def test_relay_raises_after_the_last_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every attempt failing propagates the error for the router to translate."""
    monkeypatch.setattr(
        code_interview.dspy,
        "streamify",
        _script(RuntimeError("one"), RuntimeError("two")),
    )
    with pytest.raises(RuntimeError, match="two"):
        asyncio.run(_collect(**_relay_kwargs()))
