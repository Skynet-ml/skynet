"""Write a short title for a new agent chat from its first user message.

The chat first gets a title cut from the message. A small side-task model
then writes a better one in parallel with the agent turn. When it is ready,
the stream sends a ``conversation_title`` event, so the panel and the history
list show it without a reload. It is its own event, not a second
``conversation_meta``: the panel's meta handler also settles the turn, and a
title that lands after ``done`` must not touch the next turn.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

logger = logging.getLogger(__name__)

MAX_TITLE_WORDS = 10
MAX_TITLE_CHARS = 80
# Chinese and Japanese write no spaces, so the word limit never applies there;
# about 24 of their characters say as much as ten English words.
MAX_UNSPACED_TITLE_CHARS = 24
# A title only needs the gist; a long paste would make the call slow and costly.
MESSAGE_CHARS = 1500
# The panel settles on ``done``, so a short wait after the turn's last event
# only keeps the connection open for the title; it never holds the composer.
# Keep in sync with TITLE_RELOAD_GAPS_MS in frontend/src/features/agent-panel/lib/title-reload.ts.
TAIL_WAIT_SECONDS = 10.0

_SYSTEM_PROMPT = (
    f"Write a short title, at most {MAX_TITLE_WORDS} words, for a chat that starts with the user's message. "
    "Use the message's language. Output only the title: one line, no quotes, no final period."
)
# A Markdown heading marker, not a "#" that is part of the title ("#1 crash").
_HEADING = re.compile(r"^#{1,6}\s+")
# A lead-in on the title's own line ("Here is a title: Fix the upload"). Only
# known lead-ins match: "Page title: missing on mobile" is a real title. Bold
# can close before or after the colon ("**Title:** x", "**Title**: x"), or at
# the line's end ("**Title: x**"); ``clean_title`` then drops that end mark.
_PREAMBLE = re.compile(
    r"^(?P<mark>\*\*|__)?(?:here(?: is|'s|’s) (?:a|the|your) )?(?:(?:short|suggested|possible|new|chat) )*title"
    r"(?: for (?:this|the|your) chat)?(?P<shut>(?P=mark))?\s*:\s*(?P<after>(?P=mark))?\s*",
    re.IGNORECASE,
)
# Han and kana. Korean (Hangul) puts spaces between words, so the word limit covers it.
_UNSPACED_SCRIPT = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
# Quotes come off only as a pair around the whole title: "'90s music" and
# '"Fix" the upload' keep their marks.
_QUOTE_PAIRS = {'"': '"', "'": "'", "“": "”", "‘": "’", "«": "»", "「": "」"}
# Emphasis or code that wraps the whole title; an edge mark alone is content.
_WRAPPER = re.compile(r"(\*\*|__|[*_`])((?:(?!\1).)+)\1")
# A code fence, or a line with no letter or digit (a rule, a bare "#"), is
# never the title.
_NOISE = re.compile(r"^(?:```|~~~)|^[^\w]*$")


def build_messages(message: str) -> list[dict[str, str]]:
    """Build the chat messages that ask for a title.

    Args:
        message: The user's typed words from the first message.

    Returns:
        A system message and one user message.
    """
    flat = " ".join(message.split())[:MESSAGE_CHARS]
    return [{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": flat}]


def clean_title(raw: str) -> str:
    """Turn the model's raw answer into one short title line, or ``""``.

    Args:
        raw: The model's text.

    Returns:
        The title, at most ``MAX_TITLE_WORDS`` words and ``MAX_TITLE_CHARS``
        characters (``MAX_UNSPACED_TITLE_CHARS`` for Chinese, Japanese or a
        title with no spaces), or ``""`` when the answer is empty.
    """
    # Through a CLI the system prompt arrives as plain text, and a small model
    # sometimes puts a Markdown heading marker or a preamble line first.
    lines = [_HEADING.sub("", line.strip()).strip() for line in raw.strip().splitlines()]
    lines = [line for line in lines if line and not _NOISE.match(line)]
    if len(lines) > 1 and lines[0].endswith(":"):
        lines = lines[1:]
    if not lines:
        return ""
    text = " ".join(lines[0].split())
    preamble = _PREAMBLE.match(text)
    if preamble:
        text = text[preamble.end() :]
        mark = preamble.group("mark")
        if mark and not (preamble.group("shut") or preamble.group("after")):
            text = text.removesuffix(mark)
        text = text.strip()
    # A final period outside the marks ('"Fix it".', "**Fix it**.") would hide them.
    text = text.rstrip(".。").strip()
    wrapped = _WRAPPER.fullmatch(text)
    if wrapped:
        text = wrapped.group(2).strip()
    close = _QUOTE_PAIRS.get(text[:1])
    # '"Fix" vs "Break"' starts and ends with a quote, but they are two pairs.
    inner = text[1:-1]
    if close is not None and len(text) >= 2 and text[-1] == close and text[0] not in inner and close not in inner:
        text = text[1:-1]
    # Cut the words first: a period inside the message can become the last character.
    words = text.split()[:MAX_TITLE_WORDS]
    text = " ".join(words).rstrip(".。")
    if _UNSPACED_SCRIPT.search(text):
        head = text[:MAX_UNSPACED_TITLE_CHARS]
        # A Latin word beside the Han or kana must not lose its end. A cut
        # just before a space ends a whole word, so that word stays.
        cut_inside_word = len(text) > MAX_UNSPACED_TITLE_CHARS and text[MAX_UNSPACED_TITLE_CHARS] != " "
        if cut_inside_word and " " in head:
            head = head.rsplit(" ", 1)[0]
        return head.strip()
    if " " not in text:
        return text[:MAX_UNSPACED_TITLE_CHARS]
    if len(text) > MAX_TITLE_CHARS:
        head = text[:MAX_TITLE_CHARS]
        # As above: a cut just before a space keeps the whole last word.
        if text[MAX_TITLE_CHARS] != " ":
            head = head.rsplit(" ", 1)[0] or head
        text = head
    return text.strip()


def write_chat_title(lm: Any, message: str) -> str:
    """Ask ``lm`` for a chat title.

    Args:
        lm: The small side-task model.
        message: The user's typed words from the first message.

    Returns:
        The cleaned title, or ``""`` when the model gives nothing usable.
    """
    outputs = lm(messages=build_messages(message))
    first = outputs[0] if outputs else ""
    if isinstance(first, dict):
        first = first.get("text") or ""
    return clean_title(str(first or ""))


async def with_title_event(
    source: AsyncIterator[dict[str, Any]],
    title_task: asyncio.Future[str],
    conversation_id: str,
    cut_title: str,
) -> AsyncIterator[dict[str, Any]]:
    """Pass ``source`` through and add a ``conversation_title`` event when the title is ready.

    The event goes out as soon as the title task ends, also between two
    source events. After the source ends, the stream waits at most
    ``TAIL_WAIT_SECONDS`` for the title. A failed or empty title sends the
    event with ``title: ""``, so the panel stops waiting and reads nothing again.

    Args:
        source: The turn's event stream.
        title_task: Resolves to the saved new title, or ``""``.
        conversation_id: The new conversation's id.
        cut_title: The title the chat started with. The panel applies the new
            title only while its row still shows this one, so a rename wins.

    Yields:
        The source events, plus at most one ``conversation_title`` event, and
        a ``turn_billed`` event after the source ends while the title is late.
    """
    iterator = aiter(source)
    pending: asyncio.Future[str] | None = title_task
    next_event: asyncio.Task | None = None
    started = False

    def _title_event(task: asyncio.Future[str]) -> dict[str, Any]:
        """Read the finished title task as a title event.

        Args:
            task: The finished title task.

        Returns:
            The event. Its title is ``""`` when the task failed or found no title.
        """
        try:
            title = task.result()
        except Exception:
            logger.warning("chat title task failed", exc_info=True)
            title = ""
        return {
            "event": "conversation_title",
            "data": {"conversation_id": conversation_id, "title": title, "cut_title": cut_title},
        }

    try:
        while True:
            if next_event is None:
                # Each step of ``source`` runs in a new task, as in
                # ``sse_from_events``. So the wrapped stream must never hold a
                # contextvar token, an anyio task group or an
                # ``asyncio.timeout`` across a yield: its exit would run in
                # another task and fail (see ``code_interview.py``).
                next_event = asyncio.ensure_future(anext(iterator))
            # The title waits for the first source event, the turn's own
            # ``conversation_meta``, so the panel knows the chat first.
            waits = {next_event} if pending is None or not started else {next_event, pending}
            done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            if next_event in done:
                try:
                    item = next_event.result()
                except StopAsyncIteration:
                    next_event = None
                    break
                next_event = None
                started = True
                yield item
            if started and pending is not None and pending.done():
                event = _title_event(pending)
                pending = None
                yield event
        if pending is not None:
            # The source ended, so its metering ``finally`` ran: the turn is
            # debited. The panel refreshes the balance now, not after the tail.
            yield {"event": "turn_billed", "data": {}}
            await asyncio.wait({pending}, timeout=TAIL_WAIT_SECONDS)
            if pending.done():
                yield _title_event(pending)
    finally:
        if next_event is not None:
            next_event.cancel()
