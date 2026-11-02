"""Suggest the next message a user would likely send an agent.

After an agent turn ends, the empty composer shows one greyed suggestion the
user can accept with Tab. A small fast model writes it from a compact
transcript: the user's own messages as style samples, the last few turns
verbatim, and the user's latest actions, with the newest material placed
last and marked as what matters most. The suggestion is in the user's voice
and style, never the assistant's.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

MAX_SUGGESTION_CHARS = 100
# A reply this much longer than the cap is a paragraph, not a next message;
# cutting it would leave a fragment, so it is dropped instead.
_DROP_ABOVE_CHARS = 160
RECENT_TURNS = 3
OLDER_USER_TURNS = 6
RECENT_TURN_CHARS = 1200
# The last assistant reply decides what comes next more than anything else,
# so it gets the largest share of the budget.
LAST_REPLY_CHARS = 2000
MAX_ACTIONS = 8
ACTION_CHARS = 120
# About 1,500 tokens: enough context for one short line, small enough that
# the call stays fast and cheap.
PROMPT_BUDGET_CHARS = 6000

_SYSTEM_PROMPT = (
    "You write the next message the USER will type to an AI assistant in Skynet, an app that optimizes "
    "prompts, programs, agents and workflows. You are the user, not the assistant.\n"
    "Content: reply to or follow up on the assistant's latest reply and the latest actions; use older turns "
    "only as background. Say what the user wants, has or will do next, as a request or an answer (for "
    "example: has a file and asks to upload it). Never claim that something already "
    'happened ("Here\'s the CSV", "I uploaded it") unless the actions show it did. If the reply asks a '
    "question, answer it only when the answer is clear from the conversation; otherwise ask for the "
    "natural next step. Never repeat a message the user already sent.\n"
    "Style: the user's own messages below are samples of how the user writes. Write as if the same person "
    "typed it. Copy their language, capitalization (lowercase only if they write lowercase; capitals if they "
    "use capitals), punctuation, length, formality, tone and slang. Always spell every word "
    "correctly: never add spelling mistakes, even if the samples have some. If there are no samples, write "
    "short and casual, in {language}. No assistant voice, no greetings, and no filler openers such as "
    "'Sure', 'Certainly' or 'Great'.\n"
    "Output: one line, at most 12 words and {max_chars} characters. Only the message: no quotes, labels, "
    "Markdown or explanation. If no next message is likely, write nothing."
)
# Assistant-style openers a small model adds despite the prompt.
_FILLER_OPENER = re.compile(r"^(sure|certainly|of course|absolutely|great)[,!.]\s+", re.IGNORECASE)
_LIST_MARKER = re.compile(r"^(?:[-*>]|\d+[.)])\s+")
# Enough of each user message to show how the user writes, without letting
# one long paste fill the budget.
STYLE_SAMPLE_CHARS = 200


def _clip(text: str, limit: int, *, keep_tail: bool = False) -> str:
    """Flatten ``text`` to one line and cut it to ``limit`` characters.

    Args:
        text: Raw turn or action text.
        limit: Maximum characters kept.
        keep_tail: Keep the end instead of the start, for a reply whose
            closing lines carry its question or next step.

    Returns:
        The flattened text, with an ellipsis where it was cut.
    """
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return "…" + flat[-(limit - 1) :] if keep_tail else flat[: limit - 1] + "…"


def writes_lowercase(turns: Sequence[tuple[str, str]]) -> bool:
    """Return whether the user types Latin letters only in lowercase.

    Small models drift back to sentence case, so this style is checked in code.

    Args:
        turns: ``(role, content)`` pairs, oldest first.

    Returns:
        ``True`` when the user's messages hold lowercase letters and no capitals.
        Scripts with no case, such as Hebrew, give ``False``.
    """
    text = "".join(content for role, content in turns if role == "user")
    return any(c.islower() for c in text) and not any(c.isupper() for c in text)


def build_messages(
    turns: Sequence[tuple[str, str]],
    actions: Sequence[str],
    language: str,
) -> list[dict[str, str]]:
    """Build the chat messages that ask for the next user message.

    Args:
        turns: ``(role, content)`` pairs, oldest first; role is ``user`` or
            ``assistant``.
        actions: Short descriptions of what the user and agent just did
            (tool results, approvals, uploads), oldest first.
        language: English name of the language the suggestion is written in.

    Returns:
        A system message and one user message holding the compact transcript.
    """
    recent = list(turns[-RECENT_TURNS:])
    samples = [content for role, content in turns if role == "user" and content.strip()][-OLDER_USER_TURNS:]
    sections: list[str] = []
    if samples:
        lines = "\n".join(f"> {_clip(text, STYLE_SAMPLE_CHARS)}" for text in samples)
        sections.append(
            f"The user's own messages, oldest first (background, and samples of how the user writes):\n{lines}"
        )
    recent_lines = []
    for index, (role, content) in enumerate(recent):
        last = index == len(recent) - 1
        limit = LAST_REPLY_CHARS if last and role == "assistant" else RECENT_TURN_CHARS
        text = _clip(content, limit, keep_tail=role == "assistant")
        recent_lines.append(f"{'USER' if role == 'user' else 'ASSISTANT'}: {text}")
    if recent_lines:
        sections.append("Latest turns, newest last (most important):\n" + "\n".join(recent_lines))
    clipped_actions = [_clip(a, ACTION_CHARS) for a in actions if a.strip()][-MAX_ACTIONS:]
    if clipped_actions:
        lines = "\n".join(f"- {a}" for a in clipped_actions)
        sections.append(f"What just happened, newest last (most important):\n{lines}")
    body = "\n\n".join(sections)
    if len(body) > PROMPT_BUDGET_CHARS:
        # The newest sections sit last, so the cut drops the oldest context.
        body = "…" + body[-(PROMPT_BUDGET_CHARS - 1) :]
    ask = "The user's next message, written the way the user writes"
    ask += " (all lowercase):" if writes_lowercase(turns) else ":"
    return [
        {
            "role": "system",
            "content": _SYSTEM_PROMPT.format(language=language, max_chars=MAX_SUGGESTION_CHARS),
        },
        {"role": "user", "content": body + "\n\n" + ask},
    ]


def clean_suggestion(raw: str, sent: Sequence[str], *, lowercase: bool = False) -> str:
    """Turn the model's raw answer into one short line, or an empty string.

    Args:
        raw: The model's text.
        sent: Messages the user already sent, which a suggestion must not repeat.
        lowercase: Lowercase the line, for a user who writes in lowercase.

    Returns:
        The suggestion, at most ``MAX_SUGGESTION_CHARS`` characters, or ``""``
        when the answer is empty, a paragraph, or a repeat.
    """
    # Through a CLI the system prompt arrives as plain text, and a small model
    # sometimes puts a Markdown heading above the message.
    lines = [line for line in raw.strip().splitlines() if line.strip() and not line.lstrip().startswith("#")]
    # It may also open with a preamble ("Here's the user's next message:").
    if len(lines) > 1 and lines[0].rstrip().endswith(":"):
        lines = lines[1:]
    if not lines:
        return ""
    text = " ".join(lines[0].split())
    for prefix in ("User:", "USER:", "Next message:"):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
    # Markdown list markers and emphasis would show as literal symbols.
    text = _LIST_MARKER.sub("", text)
    for mark in ("**", "__", "*", "_"):
        if len(text) > 2 * len(mark) and text.startswith(mark) and text.endswith(mark):
            text = text[len(mark) : -len(mark)].strip()
    text = text.strip("\"'“”‘’`").strip()
    unfilled = _FILLER_OPENER.sub("", text)
    if unfilled and unfilled != text:
        text = unfilled[0].upper() + unfilled[1:] if text[0].isupper() else unfilled
    if not text or len(text) > _DROP_ABOVE_CHARS:
        return ""
    if len(text) > MAX_SUGGESTION_CHARS:
        cut = text[:MAX_SUGGESTION_CHARS].rsplit(" ", 1)[0].rstrip(",;:")
        text = cut or text[:MAX_SUGGESTION_CHARS]
    if lowercase:
        text = text.lower()
    seen = {" ".join(s.split()).casefold() for s in sent}
    return "" if text.casefold() in seen else text


def suggest_next_prompt(
    lm: Any,
    turns: Sequence[tuple[str, str]],
    actions: Sequence[str],
    language: str,
) -> str:
    """Ask ``lm`` for the user's likely next message.

    Args:
        lm: The small side-task model.
        turns: ``(role, content)`` pairs, oldest first.
        actions: What just happened, oldest first.
        language: English name of the suggestion's language.

    Returns:
        The cleaned suggestion, or ``""`` when the model gives nothing usable.
    """
    outputs = lm(messages=build_messages(turns, actions, language))
    first = outputs[0] if outputs else ""
    if isinstance(first, dict):
        first = first.get("text") or ""
    sent = [content for role, content in turns if role == "user"]
    return clean_suggestion(str(first or ""), sent, lowercase=writes_lowercase(turns))
