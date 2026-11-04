"""Shared driver for the streamed, single-turn authoring interviews. [INTERNAL]

Mirrors the tagger's dataset interview (``core.service_gateway.tagging``):
each interview is a stateless, single-turn dspy Signature streamed over the
generalist agent's event shapes. The transcript is client-owned and re-sent
on every turn; nothing here touches the database. The account intake
interview (:mod:`.intake_interview`) runs its turns through
:func:`relay_interview_turn` with its own ``parse``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

import dspy

from ..language_models import served_model_from
from .answer_options import normalize_options
from .code import ReasoningStreamListener
from .constants import REASONING_FIELD
from .parse_salvage import salvage_prediction

# The tagger imports normalize_options from here; keep the re-export stable.
__all__ = [
    "INTERVIEW_TURN_ATTEMPTS",
    "normalize_options",
    "relay_interview_turn",
]

logger = logging.getLogger(__name__)

INTERVIEW_TURN_ATTEMPTS = 2
"""LLM attempts per turn; a parse-salvaged reply never spends the retry."""


def _parse_json(text: str, fallback: Any) -> Any:
    """Parse a JSON-in-a-string model output, returning ``fallback`` on failure.

    Args:
        text: The raw output field value.
        fallback: Value returned when the text is empty or unparseable.
    """
    try:
        return json.loads(text) if text and text.strip() else fallback
    except (json.JSONDecodeError, TypeError):
        return fallback


async def _drive_interview_turn(
    *,
    predict: dspy.Predict,
    lm: dspy.LM,
    inputs: dict[str, Any],
    asked: int,
    model: str | None,
    queue: asyncio.Queue[dict | None],
    parse: Callable[[Any, int], dict[str, Any]],
) -> None:
    """Drive the interview predictor to completion, fanning events onto ``queue``.

    Runs the whole streamify loop inside this one task so that ``dspy.context``'s
    contextvar token and streamify's internal anyio task group are entered and
    exited in the same task. The previous shape yielded SSE events directly from
    inside the loop, which tied the streamify generator's finalization to the
    Starlette response task; when consumption crossed task boundaries the
    teardown raised ``RuntimeError: exit cancel scope in a different task`` and
    ``ValueError: Token was created in a different Context``, failing every turn.
    Pushing to an unbounded queue keeps the loop within this task, exactly as the
    code agent's seed/edit streams do.

    A terminal ``interview_done`` is enqueued on success; a total failure
    propagates out (the caller re-raises it for the router to translate into an
    ``error`` event). A ``None`` sentinel always closes the queue.

    Args:
        predict: The interview predictor (reused across attempts).
        lm: Language model conducting the interview.
        inputs: Keyword inputs forwarded to the streamify program.
        asked: Count of assistant turns so far, for the parsed turn's numbering.
        model: LiteLLM id conducting the interview; stamped on the parsed turn
            when set. ``None`` runs the default.
        queue: SSE event queue; receives event dicts and a trailing ``None``.
        parse: Turns the final prediction (and ``asked``) into the
            ``interview_done`` payload. The predictor's signature must stream
            a ``message`` and a ``done`` output field.
    """
    prediction: Any = None
    try:
        for attempt in range(INTERVIEW_TURN_ATTEMPTS):
            # Stream listeners are single-use; rebuild the program per attempt.
            program = dspy.streamify(
                predict,
                stream_listeners=[
                    dspy.streaming.StreamListener(signature_field_name="message"),
                    dspy.streaming.StreamListener(signature_field_name="done"),
                    ReasoningStreamListener(predict=predict),
                ],
                async_streaming=True,
            )
            if attempt:
                await queue.put({"event": "message_reset", "data": {}})
            done_text = ""
            try:
                with dspy.context(lm=lm):
                    async for chunk in program(**inputs):
                        if isinstance(chunk, dspy.streaming.StreamResponse):
                            if chunk.signature_field_name == REASONING_FIELD:
                                await queue.put({"event": "reasoning_patch", "data": {"chunk": chunk.chunk}})
                            elif chunk.signature_field_name == "message":
                                await queue.put({"event": "message_patch", "data": {"chunk": chunk.chunk}})
                                if chunk.is_last_chunk:
                                    await queue.put({"event": "message_end", "data": {}})
                            elif chunk.signature_field_name == "done":
                                done_text += chunk.chunk
                                if chunk.is_last_chunk:
                                    await queue.put(
                                        {
                                            "event": "turn_hint",
                                            "data": {"final": "true" in done_text.lower()},
                                        }
                                    )
                        elif isinstance(chunk, dspy.Prediction):
                            prediction = chunk
                break
            except Exception as err:
                # Keep a prediction already captured this attempt — an exception
                # raised during teardown must not discard a completed reply.
                if prediction is None:
                    prediction = salvage_prediction(err)
                if prediction is not None:
                    break
                if attempt + 1 >= INTERVIEW_TURN_ATTEMPTS:
                    raise
                logger.warning("interview turn failed; retrying", exc_info=True)
        turn = parse(prediction, asked)
        if model:
            turn["model"] = model
        turn["served_model"] = served_model_from(lm)
        await queue.put({"event": "interview_done", "data": turn})
    finally:
        await queue.put(None)


async def relay_interview_turn(
    *,
    predict: dspy.Predict,
    lm: dspy.LM,
    inputs: dict[str, Any],
    asked: int,
    model: str | None,
    parse: Callable[[Any, int], dict[str, Any]],
) -> Any:
    """Run :func:`_drive_interview_turn` in its own task and yield its events.

    Args:
        predict: The interview predictor.
        lm: Language model conducting the interview.
        inputs: Keyword inputs for the predictor.
        asked: Assistant questions asked before this turn.
        model: LiteLLM id stamped on the parsed turn; ``None`` keeps the default.
        parse: Builds the ``interview_done`` payload from the final prediction.

    Yields:
        ``{"event", "data"}`` mappings, ending with ``interview_done``.
    """
    # Drive the streamify loop in its own task and relay its events off a queue:
    # yielding directly from inside the loop finalizes the dspy.context token and
    # streamify's anyio task group in the SSE consumer's task, corrupting both.
    queue: asyncio.Queue[dict | None] = asyncio.Queue()
    task = asyncio.create_task(
        _drive_interview_turn(predict=predict, lm=lm, inputs=inputs, asked=asked, model=model, queue=queue, parse=parse)
    )
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item
        await task
    except asyncio.CancelledError:
        task.cancel()
        raise
