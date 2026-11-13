"""Condense an agent chat's older turns into one structured summary.

The client sends the whole chat every turn, with every tool result in full.
Two steps keep the prompt in the model's context window. First, once the
estimated prompt passes :func:`clear_at`, the older tool results are cleared:
the newest :data:`KEEP_TOOL_RESULTS` stay in full and the older ones become a
short placeholder (:func:`cleared_tool_result`). Second, only when the prompt
still reaches :data:`COMPACT_AT` of the window are the older turns summarized
and replaced by that summary, so a long chat keeps working. The summary rules and prompt follow the
Pi coding agent's compaction (docs/compaction.md of
``@earendil-works/pi-coding-agent``): keep about 20000 tokens of recent turns,
cut only at a user turn, and fold the previous summary into the next one. A
small window scales the kept budget down.

The token counts are estimates: four ASCII characters for each token, as in
Pi, and one token for each other character. Hebrew, Arabic and CJK text takes
far more tokens for each character than English, so a chars/4 count would let
such a chat overflow the window before the compaction starts.
"""

from __future__ import annotations

import json
import math
from typing import Any

from ...billing.model_dispatch import mark_lm_prompt_cache

# The reply room of the summary call. It does not decide when a chat compacts.
RESERVE_TOKENS = 16384
# Compaction starts when the prompt, after clearing, reaches this part of the window.
COMPACT_AT = 0.8
# Old tool results are cleared long before a compaction, as in Anthropic's
# context editing: full results make every request large, and a large window
# would carry them for hundreds of thousands of tokens. Clearing starts at
# CLEAR_AT of the window, or at CLEAR_AT_TOKENS when that is smaller.
CLEAR_AT = 0.4
CLEAR_AT_TOKENS = 60000
KEEP_TOOL_RESULTS = 3
# Results are cleared only in full batches of this size, oldest first. The
# provider caches the prompt prefix, and each new clear changes that prefix,
# so a clear frees enough room for several turns before the next one.
CLEAR_BATCH = 5
KEEP_RECENT_TOKENS = 20000
# Used when the model catalog has no context window for the model.
DEFAULT_CONTEXT_WINDOW = 128000
# The system prompt, the screen instructions and the tool schemas are not in
# the chat history, but they share the window with it.
PROMPT_OVERHEAD_TOKENS = 12000
TOOL_RESULT_MAX_CHARS = 2000
_CHARS_PER_TOKEN = 4
# Each non-ASCII character counts as this many ASCII characters: one token.
_NON_ASCII_WEIGHT = _CHARS_PER_TOKEN
# The summary must fit the reply room it was written in. The client sends it
# back every turn, and the request fields cap it at this length.
SUMMARY_MAX_CHARS = RESERVE_TOKENS * _CHARS_PER_TOKEN
# A structured summary in Pi's format is rarely longer than this. Folding fewer
# tokens than this into a summary does not make the prompt smaller.
MIN_SUMMARIZED_TOKENS = 4096

SUMMARIZATION_SYSTEM_PROMPT = (
    "You are a context summarization assistant. Your task is to read a conversation between a user and an "
    "AI assistant, then produce a structured summary following the exact format specified.\n\n"
    "Do NOT continue the conversation. Do NOT respond to any questions in the conversation. "
    "ONLY output the structured summary."
)

SUMMARIZATION_PROMPT = """The messages above are a conversation to summarize. Create a structured context checkpoint summary that another LLM will use to continue the work.

Use this EXACT format:

## Goal
[What is the user trying to accomplish? Can be multiple items if the session covers different tasks.]

## Constraints & Preferences
- [Any constraints, preferences, or requirements mentioned by user]
- [Or "(none)" if none were mentioned]

## Progress
### Done
- [x] [Completed tasks/changes]

### In Progress
- [ ] [Current work]

### Blocked
- [Issues preventing progress, if any]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [Ordered list of what should happen next]

## Critical Context
- [Any data, examples, or references needed to continue]
- [Or "(none)" if not applicable]

Keep each section concise. Preserve exact file paths, function names, and error messages."""

UPDATE_SUMMARIZATION_PROMPT = """The messages above are NEW conversation messages to incorporate into the existing summary provided in <previous-summary> tags.

Update the existing structured summary with new information. RULES:
- PRESERVE all existing information from the previous summary
- ADD new progress, decisions, and context from the new messages
- UPDATE the Progress section: move items from "In Progress" to "Done" when completed
- UPDATE "Next Steps" based on what was accomplished
- PRESERVE exact file paths, function names, and error messages
- If something is no longer relevant, you may remove it

Use this EXACT format:

## Goal
[Preserve existing goals, add new ones if the task expanded]

## Constraints & Preferences
- [Preserve existing, add new ones discovered]

## Progress
### Done
- [x] [Include previously done items AND newly completed items]

### In Progress
- [ ] [Current work - update based on progress]

### Blocked
- [Current blockers - remove if resolved]

## Key Decisions
- **[Decision]**: [Brief rationale] (preserve all previous, add new)

## Next Steps
1. [Update based on current state]

## Critical Context
- [Preserve important context, add new if needed]

Keep each section concise. Preserve exact file paths, function names, and error messages."""

SUMMARY_PREFIX = "The conversation history before this point was compacted into the following summary:\n\n<summary>\n"
SUMMARY_SUFFIX = "\n</summary>"
# The loop replays history as user/assistant pairs, so the summary turn gets a
# short reply that only confirms it was read.
SUMMARY_ACK = "Understood. I will continue from this summary."


class CompactionError(RuntimeError):
    """The summary call returned no usable summary."""


def _weight(char: str) -> int:
    """Return the estimate weight of one character, in quarter tokens.

    Args:
        char: One character.

    Returns:
        1 for an ASCII character, :data:`_NON_ASCII_WEIGHT` for any other.
    """
    return 1 if char.isascii() else _NON_ASCII_WEIGHT


def estimate_tokens(text: str) -> int:
    """Estimate the tokens of ``text``: four ASCII characters or one other character for each token.

    Args:
        text: Any prompt text.

    Returns:
        The estimate, rounded up.
    """
    non_ascii = 0 if text.isascii() else sum(1 for char in text if not char.isascii())
    return math.ceil((len(text) - non_ascii + non_ascii * _NON_ASCII_WEIGHT) / _CHARS_PER_TOKEN)


def _tail_within(text: str, tokens: int) -> str:
    """Return the longest end of ``text`` whose estimate fits in ``tokens``.

    Args:
        text: Any prompt text.
        tokens: The token budget.

    Returns:
        ``text`` itself when it fits, else its newest part that fits.
    """
    budget = max(0, tokens) * _CHARS_PER_TOKEN
    used = 0
    for index in range(len(text) - 1, -1, -1):
        used += _weight(text[index])
        if used > budget:
            return text[index + 1 :]
    return text


def entry_tokens(entry: dict[str, Any]) -> int:
    """Estimate the tokens one history entry adds to the prompt.

    Args:
        entry: A ``{role, content, tools?}`` dict, as ``history_for_agent`` makes it.

    Returns:
        The estimate for its text and its tool trace.
    """
    tools = entry.get("tools") or []
    trace = json.dumps(tools, ensure_ascii=False) if tools else ""
    return estimate_tokens(str(entry.get("content") or "")) + estimate_tokens(trace)


def context_tokens(history: list[dict[str, Any]], user_message: str, summary: str | None) -> int:
    """Estimate the prompt tokens of a turn.

    Args:
        history: The history entries sent to the agent, oldest first.
        user_message: The new user message, or ``""`` for no new turn.
        summary: The summary that stands in for older turns, if any.

    Returns:
        The estimate, including :data:`PROMPT_OVERHEAD_TOKENS`.
    """
    total = PROMPT_OVERHEAD_TOKENS + estimate_tokens(user_message)
    if summary:
        total += estimate_tokens(SUMMARY_PREFIX + summary + SUMMARY_SUFFIX + SUMMARY_ACK)
    return total + sum(entry_tokens(entry) for entry in history)


def reserve_tokens(window: int) -> int:
    """Return the reply room for a window: Pi's reserve, or a quarter of a small window.

    Args:
        window: The model's context window.

    Returns:
        ``RESERVE_TOKENS``, or ``window // 4`` when that is smaller.
    """
    return min(RESERVE_TOKENS, window // 4)


def keep_tokens(window: int) -> int:
    """Return how many recent tokens a compaction keeps: Pi's budget, or a quarter of a small window.

    Pi's fixed budgets fill a window below about 48k tokens with the reserve,
    the kept turns and the prompt overhead, so every turn would compact again.

    Args:
        window: The model's context window.

    Returns:
        ``KEEP_RECENT_TOKENS``, or ``window // 4`` when that is smaller.
    """
    return min(KEEP_RECENT_TOKENS, window // 4)


def should_compact(tokens: int, window: int) -> bool:
    """Tell whether a prompt of ``tokens`` is large enough to clear tool results, then to compact.

    Args:
        tokens: The estimated prompt tokens.
        window: The model's context window.

    Returns:
        True when ``tokens`` is at least :data:`COMPACT_AT` of ``window``.
    """
    return tokens >= window * COMPACT_AT


def clear_at(window: int) -> int:
    """Return the prompt size at which old tool results are cleared.

    Args:
        window: The model's context window.

    Returns:
        :data:`CLEAR_AT` of ``window``, or :data:`CLEAR_AT_TOKENS` when that is smaller.
    """
    return min(int(window * CLEAR_AT), CLEAR_AT_TOKENS)


def cleared_tool_result(tool: str) -> str:
    """Return the placeholder that replaces a cleared tool result.

    Args:
        tool: The name of the tool whose output was cleared.

    Returns:
        A short note that names the tool and says how to get the output again.
    """
    return f"[Output of {tool or 'the tool'} cleared to save context. Call the tool again if you need it.]"


def clear_tool_results(
    history: list[dict[str, Any]], user_message: str, summary: str | None, window: int
) -> list[dict[str, Any]]:
    """Clear the oldest tool results until the prompt is below :func:`clear_at`.

    The newest :data:`KEEP_TOOL_RESULTS` results stay in full. The older ones
    are cleared in full batches of :data:`CLEAR_BATCH`, oldest first. The
    history is cleared again from the full results on every turn, so the same
    chat always clears the same results and the cached prefix changes only
    when a new batch is cleared. Only a prompt that still reaches
    :data:`COMPACT_AT` after the full batches also clears the rest of the
    older results, because the compaction that follows changes the prefix
    anyway.

    Args:
        history: The history entries, oldest first, as ``history_for_agent`` makes them.
        user_message: The new user message.
        summary: The summary that stands in for older turns, if any.
        window: The model's context window.

    Returns:
        ``history`` itself when the prompt is below :func:`clear_at`, else a
        copy with the cleared results replaced by :func:`cleared_tool_result`.
    """
    trigger = clear_at(window)
    if context_tokens(history, user_message, summary) < trigger:
        return history
    slots = [
        (entry_index, call_index)
        for entry_index, entry in enumerate(history)
        for call_index, call in enumerate(entry.get("tools") or [])
        if call.get("result")
    ]
    clearable = slots[: max(0, len(slots) - KEEP_TOOL_RESULTS)]

    def cleared_through(end: int) -> list[dict[str, Any]]:
        """Copy the history with its ``end`` oldest clearable results cleared.

        Args:
            end: How many of the oldest clearable results to clear.

        Returns:
            The copy; ``history`` is not changed.
        """
        copy = [
            {**entry, "tools": [dict(call) for call in entry["tools"]]} if entry.get("tools") else entry
            for entry in history
        ]
        for entry_index, call_index in clearable[:end]:
            call = copy[entry_index]["tools"][call_index]
            call["result"] = cleared_tool_result(str(call.get("tool") or ""))
        return copy

    cleared = history
    for end in range(CLEAR_BATCH, len(clearable) + 1, CLEAR_BATCH):
        cleared = cleared_through(end)
        if context_tokens(cleared, user_message, summary) < trigger:
            return cleared
    if clearable and should_compact(context_tokens(cleared, user_message, summary), window):
        cleared = cleared_through(len(clearable))
    return cleared


def shrinks_prompt(history: list[dict[str, Any]], cut: int) -> bool:
    """Tell whether summarizing ``history[:cut]`` makes the prompt smaller.

    Args:
        history: The history entries, oldest first.
        cut: The index of the first kept entry.

    Returns:
        True when the summarized entries hold more than ``MIN_SUMMARIZED_TOKENS``.
    """
    return sum(entry_tokens(entry) for entry in history[:cut]) > MIN_SUMMARIZED_TOKENS


def find_cut(history: list[dict[str, Any]], keep_tokens: int = KEEP_RECENT_TOKENS) -> int:
    """Find the first history entry to keep after a compaction.

    The cut is always at a user entry. An assistant entry holds its tool calls
    and their results together, so no cut splits a call from its result.

    Args:
        history: The history entries, oldest first.
        keep_tokens: About how many tokens of recent entries to keep.

    Returns:
        The index of the first kept entry: ``0`` when everything fits in
        ``keep_tokens`` (nothing to summarize), ``len(history)`` when no user
        entry is recent enough to keep.
    """
    kept = 0
    for index in range(len(history) - 1, -1, -1):
        kept += entry_tokens(history[index])
        if kept >= keep_tokens:
            return next(
                (cut for cut in range(index, len(history)) if history[cut].get("role") == "user"),
                len(history),
            )
    return 0


def manual_cut(history: list[dict[str, Any]], keep_tokens: int = KEEP_RECENT_TOKENS) -> int:
    """Find the cut for a compaction the user asked for.

    A short chat has nothing older than ``keep_tokens``, and one large recent
    reply can leave no user entry inside it; in both cases the cut is at the
    latest user entry, so everything but the last exchange is summarized.

    Args:
        history: The history entries, oldest first.
        keep_tokens: About how many tokens of recent entries to keep.

    Returns:
        The index of the first kept entry, or ``0`` when there is nothing to summarize.
    """
    cut = find_cut(history, keep_tokens)
    if 0 < cut < len(history):
        return cut
    return next((i for i in range(len(history) - 1, 0, -1) if history[i].get("role") == "user"), 0)


def serialize_conversation(turns: list[dict[str, Any]]) -> str:
    """Write chat turns as plain text for the summary call.

    Args:
        turns: ``{role, content, tool_calls?}`` dicts, oldest first; each tool
            call is a ``{tool, status, result}`` dict.

    Returns:
        ``[User]:``, ``[Assistant tool calls]:``, ``[Tool result]:`` and
        ``[Assistant]:`` blocks, with each tool result cut to
        :data:`TOOL_RESULT_MAX_CHARS` characters.
    """
    parts: list[str] = []
    for turn in turns:
        content = str(turn.get("content") or "")
        if turn.get("role") == "user":
            if content:
                parts.append(f"[User]: {content}")
            continue
        calls = turn.get("tool_calls") or []
        if calls:
            parts.append("[Assistant tool calls]: " + "; ".join(f"{call.get('tool')}()" for call in calls))
            for call in calls:
                result = str(call.get("result") or "")
                if len(result) > TOOL_RESULT_MAX_CHARS:
                    result = result[:TOOL_RESULT_MAX_CHARS] + "\n[... truncated]"
                status = " (error)" if call.get("status") == "error" else ""
                parts.append(f"[Tool result{status}]: {result}")
        if content:
            parts.append(f"[Assistant]: {content}")
    return "\n\n".join(parts)


def summary_messages(conversation: str, previous_summary: str | None, instructions: str | None) -> list[dict[str, str]]:
    """Build the chat messages of the summary call.

    Args:
        conversation: The serialized turns to summarize.
        previous_summary: The summary of the turns before them, if any; the
            call then updates it instead of starting a new one.
        instructions: What the user asked the summary to focus on, if anything.

    Returns:
        A system message and one user message.
    """
    prompt = f"<conversation>\n{conversation}\n</conversation>\n\n"
    if previous_summary:
        prompt += f"<previous-summary>\n{previous_summary}\n</previous-summary>\n\n"
        prompt += UPDATE_SUMMARIZATION_PROMPT
    else:
        prompt += SUMMARIZATION_PROMPT
    if instructions and instructions.strip():
        prompt += f"\n\nAdditional focus: {instructions.strip()}"
    return [
        {"role": "system", "content": SUMMARIZATION_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]


def summarize(
    lm: Any,
    turns: list[dict[str, Any]],
    *,
    previous_summary: str | None,
    instructions: str | None,
    window: int,
) -> str:
    """Ask ``lm`` for the structured summary of ``turns``.

    Args:
        lm: The turn's metered model; the caller has put it in the turn's usage sink.
        turns: The chat turns to summarize, oldest first.
        previous_summary: The summary of the turns before them, if any.
        instructions: What the user asked the summary to focus on, if anything.
        window: The model's context window.

    Returns:
        The summary text.

    Raises:
        CompactionError: When the model returns no text.
    """
    conversation = serialize_conversation(turns)
    # The call itself must fit the window. When it does not, the oldest text
    # goes first: the previous summary already covers what came before it.
    fixed = estimate_tokens("".join(m["content"] for m in summary_messages("", previous_summary, instructions)))
    conversation = _tail_within(conversation, window - reserve_tokens(window) - fixed)
    messages = summary_messages(conversation, previous_summary, instructions)
    outputs = lm(messages=mark_lm_prompt_cache(str(getattr(lm, "model", "")), messages))
    first = outputs[0] if outputs else ""
    if isinstance(first, dict):
        first = first.get("text") or ""
    summary = str(first or "").strip()[:SUMMARY_MAX_CHARS]
    if not summary:
        raise CompactionError("The summary call returned no text.")
    return summary


def history_with_summary(summary: str | None, kept: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Put the summary in front of the kept history entries.

    Args:
        summary: The summary of the turns before ``kept``, or ``None``.
        kept: The history entries after the cut, oldest first.

    Returns:
        The history the agent gets: a summary turn pair, then ``kept``.
    """
    if not summary:
        return kept
    return [
        {"role": "user", "content": SUMMARY_PREFIX + summary + SUMMARY_SUFFIX},
        {"role": "assistant", "content": SUMMARY_ACK},
        *kept,
    ]


def fit_history(
    history: list[dict[str, Any]], user_message: str, summary: str | None, window: int
) -> list[dict[str, Any]]:
    """Drop the oldest turns until the prompt is below the threshold, for a failed summary call.

    Args:
        history: The history entries, oldest first.
        user_message: The new user message.
        summary: The summary that stands in for older turns, if any.
        window: The model's context window.

    Returns:
        The newest entries that fit, starting at a user entry.
    """
    start = 0
    while start < len(history) and should_compact(context_tokens(history[start:], user_message, summary), window):
        start = next((i for i in range(start + 1, len(history)) if history[i].get("role") == "user"), len(history))
    return history[start:]
