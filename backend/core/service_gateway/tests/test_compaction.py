"""Tests for the agent chat compaction rules (Pi's threshold, cut and summary prompt)."""

from __future__ import annotations

from typing import Any

import pytest

from core.service_gateway.agents import compaction


def _turn(role: str, chars: int, tools: int = 0) -> dict[str, Any]:
    """Build one history entry of about ``chars`` characters.

    Args:
        role: ``"user"`` or ``"assistant"``.
        chars: The length of its text.
        tools: How many tool calls an assistant entry carries.

    Returns:
        A ``{role, content, tools?}`` entry.
    """
    entry: dict[str, Any] = {"role": role, "content": "x" * chars}
    if tools:
        entry["tools"] = [{"tool": f"t{i}", "status": "done", "result": "r" * 400} for i in range(tools)]
    return entry


class _FakeLM:
    """Record the messages of a summary call and answer with a fixed text."""

    def __init__(self, answer: Any = "## Goal\nShip it.") -> None:
        """Set the answer.

        Args:
            answer: What the call returns as its first output.
        """
        self.answer = answer
        self.calls: list[list[dict[str, str]]] = []

    def __call__(self, *, messages: list[dict[str, str]]) -> list[Any]:
        """Record ``messages`` and return the answer.

        Args:
            messages: The chat messages of the call.

        Returns:
            A one-item output list.
        """
        self.calls.append(messages)
        return [self.answer]


def test_threshold_triggers_at_80_percent_of_the_window() -> None:
    """Clearing and compaction start when the prompt reaches 80% of the window, for any window size."""
    for window in (20_000, 100_000, 1_000_000):
        assert not compaction.should_compact(int(window * 0.8) - 1, window)
        assert compaction.should_compact(int(window * 0.8), window)


def _results_chat(results: int, chars: int = 4000) -> list[dict[str, Any]]:
    """Build one exchange whose assistant entry holds ``results`` tool results.

    Args:
        results: How many tool calls the assistant entry carries.
        chars: The length of each result.

    Returns:
        A user entry and an assistant entry, oldest first.
    """
    tools = [{"tool": f"t{i}", "status": "done", "result": f"{i}".ljust(chars, "r")} for i in range(results)]
    return [{"role": "user", "content": "go"}, {"role": "assistant", "content": "ok", "tools": tools}]


def _window_clearing_at(tokens: int) -> int:
    """Return a window whose clearing trigger sits just at or below ``tokens``.

    Args:
        tokens: The prompt estimate that must reach the trigger.

    Returns:
        The window.
    """
    return int(tokens / compaction.CLEAR_AT)


def _results(history: list[dict[str, Any]]) -> list[str]:
    """List every tool result in a history, oldest first.

    Args:
        history: History entries.

    Returns:
        The result texts.
    """
    return [call["result"] for entry in history for call in entry.get("tools") or []]


def test_clearing_starts_at_40_percent_or_60k_tokens_and_compaction_stays_at_80() -> None:
    """Clearing starts long before compaction: at 40% of the window, capped at 60k tokens for a large window."""
    assert compaction.clear_at(100_000) == 40_000
    assert compaction.clear_at(1_000_000) == 60_000
    assert not compaction.should_compact(79_999, 100_000)
    assert compaction.should_compact(80_000, 100_000)


def test_clearing_leaves_a_history_below_the_trigger_as_it_is_across_turns() -> None:
    """Below the trigger no result is cleared, and the same list comes back each turn, so the cached prefix does not change."""
    history = _results_chat(12)
    window = _window_clearing_at(compaction.context_tokens(history, "hi", None)) + 100

    assert compaction.clear_tool_results(history, "hi", None, window) is history
    longer = [*history, {"role": "user", "content": "next"}]
    assert compaction.clear_tool_results(longer, "hi", None, window) is longer


def test_clearing_clears_the_oldest_batch_and_keeps_the_newest_three() -> None:
    """Above the trigger the oldest five results go as one batch, each placeholder names its tool, and the input is not changed."""
    history = _results_chat(12)
    window = _window_clearing_at(compaction.context_tokens(history, "hi", None))

    cleared = compaction.clear_tool_results(history, "hi", None, window)

    results = _results(cleared)
    assert results[:5] == [compaction.cleared_tool_result(f"t{i}") for i in range(5)]
    assert "t3" in results[3]
    assert results[5:] == _results(history)[5:]
    assert _results(history) == [call["result"] for call in _results_chat(12)[1]["tools"]]
    assert compaction.context_tokens(cleared, "hi", None) < compaction.clear_at(window)


def test_next_turn_just_above_the_trigger_keeps_the_cleared_prefix() -> None:
    """A second turn with one more small result clears the same batch, so the cached prefix stays as it was."""
    history = _results_chat(12)
    window = _window_clearing_at(compaction.context_tokens(history, "hi", None))
    first = compaction.clear_tool_results(history, "hi", None, window)
    next_turn = [
        *history,
        {"role": "user", "content": "more"},
        {"role": "assistant", "content": "ok", "tools": [{"tool": "t12", "status": "done", "result": "small"}]},
    ]

    second = compaction.clear_tool_results(next_turn, "hi", None, window)

    assert second[:2] == first
    assert _results(second)[5:] == _results(next_turn)[5:]


def test_clearing_waits_for_a_full_batch() -> None:
    """Fewer old results than one batch stay in full below the compaction threshold, so each turn does not change the prefix."""
    history = _results_chat(7)
    window = _window_clearing_at(compaction.context_tokens(history, "hi", None))

    assert compaction.clear_tool_results(history, "hi", None, window) is history


def test_clearing_never_clears_the_newest_three_results() -> None:
    """When the full batches cannot get below 80%, every older result clears but the newest three stay; compaction comes next."""
    history = _results_chat(12, chars=40_000)

    cleared = compaction.clear_tool_results(history, "hi", None, 20_000)

    results = _results(cleared)
    assert results[:9] == [compaction.cleared_tool_result(f"t{i}") for i in range(9)]
    assert results[9:] == _results(history)[9:]
    assert compaction.should_compact(compaction.context_tokens(cleared, "hi", None), 20_000)


def test_context_tokens_counts_overhead_summary_and_history() -> None:
    """The estimate adds the fixed prompt overhead, the summary turn and each entry."""
    history = [_turn("user", 400), _turn("assistant", 800)]
    base = compaction.context_tokens(history, "", None)
    assert base == compaction.PROMPT_OVERHEAD_TOKENS + 100 + 200
    assert compaction.context_tokens(history, "y" * 40, "s" * 400) > base + 10 + 100


def test_find_cut_keeps_recent_tokens_and_cuts_at_a_user_turn() -> None:
    """The cut keeps about the newest 20000 tokens and always starts at a user entry."""
    history = []
    for _ in range(20):
        history += [_turn("user", 400), _turn("assistant", 3600, tools=8)]

    cut = compaction.find_cut(history)

    assert 0 < cut < len(history)
    assert history[cut]["role"] == "user"
    kept = sum(compaction.entry_tokens(e) for e in history[cut:])
    assert kept >= compaction.KEEP_RECENT_TOKENS - compaction.entry_tokens(history[cut - 1]) - 2000


def test_find_cut_never_splits_a_tool_call_from_its_result() -> None:
    """An assistant entry with tool calls stays whole: the cut lands on the next user entry."""
    history = [_turn("user", 40), _turn("assistant", 200, tools=8), _turn("user", 40), _turn("assistant", 40)]
    # The walk back passes the keep budget inside the tool-call entry (index 1).
    keep = compaction.entry_tokens(history[3]) + compaction.entry_tokens(history[2]) + 5

    cut = compaction.find_cut(history, keep)

    assert cut == 2
    assert history[cut - 1]["tools"]


def test_find_cut_is_zero_when_everything_fits() -> None:
    """A short chat has nothing to summarize."""
    assert compaction.find_cut([_turn("user", 40), _turn("assistant", 40)]) == 0


def test_manual_cut_summarizes_all_but_the_last_exchange_of_a_short_chat() -> None:
    """``/compact`` on a short chat keeps only the latest user turn and its reply."""
    history = [_turn("user", 40), _turn("assistant", 40), _turn("user", 40), _turn("assistant", 40)]
    assert compaction.manual_cut(history) == 2
    assert compaction.manual_cut(history[:2]) == 0


def test_manual_cut_keeps_the_last_exchange_when_one_reply_fills_the_budget() -> None:
    """One huge last reply leaves no user entry in the keep budget; the cut still keeps that exchange."""
    history = [_turn("user", 40), _turn("assistant", 40), _turn("user", 40), _turn("assistant", 90000)]
    assert compaction.find_cut(history) == len(history)
    assert compaction.manual_cut(history) == 2


def test_serialize_conversation_labels_turns_and_truncates_tool_results() -> None:
    """Turns become [User]/[Assistant] blocks; a tool result is cut to 2000 characters."""
    text = compaction.serialize_conversation(
        [
            {"role": "user", "content": "fix the bug"},
            {
                "role": "assistant",
                "content": "Done.",
                "tool_calls": [
                    {"tool": "read_file", "status": "done", "result": "a" * 3000},
                    {"tool": "run", "status": "error", "result": "boom"},
                ],
            },
        ]
    )

    assert text.startswith("[User]: fix the bug\n\n[Assistant tool calls]: read_file(); run()")
    assert "[Tool result]: " + "a" * 2000 + "\n[... truncated]" in text
    assert "a" * 2001 not in text
    assert "[Tool result (error)]: boom" in text
    assert text.endswith("[Assistant]: Done.")


def test_summary_is_iterative_and_follows_instructions() -> None:
    """A previous summary switches to the update prompt; /compact instructions add a focus line."""
    lm = _FakeLM()
    turns = [{"role": "user", "content": "next step"}]

    first = compaction.summarize(lm, turns, previous_summary=None, instructions=None, window=128_000)
    compaction.summarize(lm, turns, previous_summary=first, instructions="keep the metric code", window=128_000)

    fresh, update = lm.calls[0][1]["content"], lm.calls[1][1]["content"]
    assert lm.calls[0][0]["content"] == compaction.SUMMARIZATION_SYSTEM_PROMPT
    assert fresh.endswith(compaction.SUMMARIZATION_PROMPT)
    assert "<previous-summary>" not in fresh
    assert f"<previous-summary>\n{first}\n</previous-summary>" in update
    assert compaction.UPDATE_SUMMARIZATION_PROMPT in update
    assert update.endswith("Additional focus: keep the metric code")


def test_summary_call_on_the_managed_gateway_marks_its_prompt_cache() -> None:
    """The summary call goes through the dispatcher's prompt-cache marks, like the agent's own calls."""
    lm = _FakeLM()
    lm.model = "litellm_proxy/anthropic/claude-test"

    compaction.summarize(
        lm, [{"role": "user", "content": "hi"}], previous_summary=None, instructions=None, window=128_000
    )

    system = lm.calls[0][0]["content"]
    assert system == [
        {"type": "text", "text": compaction.SUMMARIZATION_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
    ]


def test_summarize_reads_dict_outputs_and_rejects_empty_text() -> None:
    """A dict output gives its text; an empty answer raises CompactionError."""
    turns = [{"role": "user", "content": "hi"}]
    assert (
        compaction.summarize(_FakeLM({"text": " S "}), turns, previous_summary=None, instructions=None, window=128_000)
        == "S"
    )
    with pytest.raises(compaction.CompactionError):
        compaction.summarize(_FakeLM(""), turns, previous_summary=None, instructions=None, window=128_000)


def test_summarize_trims_the_oldest_text_to_fit_the_window() -> None:
    """A conversation larger than the window loses its oldest text, never its newest."""
    lm = _FakeLM()
    turns = [{"role": "user", "content": "old" + "x" * 200_000}, {"role": "user", "content": "newest"}]

    compaction.summarize(lm, turns, previous_summary=None, instructions=None, window=40_000)

    prompt = lm.calls[0][1]["content"]
    assert "newest" in prompt
    assert "[User]: old" not in prompt
    assert compaction.estimate_tokens(prompt) <= 40_000 - compaction.reserve_tokens(40_000)


def test_history_is_summary_pair_then_kept_turns() -> None:
    """The agent gets the summary as a user/assistant pair, then the kept entries."""
    kept = [_turn("user", 10), _turn("assistant", 10)]

    history = compaction.history_with_summary("## Goal\nShip it.", kept)

    assert history[0] == {
        "role": "user",
        "content": compaction.SUMMARY_PREFIX + "## Goal\nShip it." + compaction.SUMMARY_SUFFIX,
    }
    assert history[1] == {"role": "assistant", "content": compaction.SUMMARY_ACK}
    assert history[2:] == kept
    assert compaction.history_with_summary(None, kept) == kept


def test_fit_history_drops_the_oldest_turns_at_user_boundaries() -> None:
    """The fallback keeps the newest turns that fit, starting at a user entry."""
    history = []
    for _ in range(10):
        history += [_turn("user", 4000), _turn("assistant", 4000)]

    fitted = compaction.fit_history(history, "hi", None, 40_000)

    assert fitted
    assert fitted[0]["role"] == "user"
    assert fitted == history[len(history) - len(fitted) :]
    assert not compaction.should_compact(compaction.context_tokens(fitted, "hi", None), 40_000)


def test_each_non_ascii_character_counts_as_one_token() -> None:
    """Hebrew and CJK text costs about one token for each character, not one for four."""
    assert compaction.estimate_tokens("abcd") == 1
    assert compaction.estimate_tokens("שלום") == 4
    assert compaction.estimate_tokens("你好ab") == 3
    hebrew = [{"role": "user", "content": "ש" * 40000}]
    assert compaction.should_compact(compaction.context_tokens(hebrew, "", None), 60_000)


def test_summary_trim_counts_non_ascii_text_by_its_tokens() -> None:
    """The trimmed conversation of a Hebrew chat fits the window by the same estimate."""
    calls: list[list[dict[str, str]]] = []

    def lm(*, messages: list[dict[str, str]]) -> list[str]:
        """Record the call and return a summary.

        Args:
            messages: The summary call's messages.

        Returns:
            One summary.
        """
        calls.append(messages)
        return ["## Goal\nok"]

    compaction.summarize(
        lm, [{"role": "user", "content": "ש" * 80000}], previous_summary=None, instructions=None, window=40_000
    )
    prompt = calls[0][1]["content"]
    assert compaction.estimate_tokens(prompt) <= 40_000 - compaction.reserve_tokens(40_000)
    assert prompt.count("ש") > 20_000


def test_large_windows_keep_pi_budgets() -> None:
    """A window of 80k tokens or more uses Pi's reserve and kept budget unchanged."""
    assert compaction.reserve_tokens(128_000) == compaction.RESERVE_TOKENS
    assert compaction.keep_tokens(128_000) == compaction.KEEP_RECENT_TOKENS


def test_small_window_compaction_frees_room_for_the_next_turn() -> None:
    """At a 32k window the kept turns and the summary fit, so the next turn does not compact again."""
    window = 32_000
    history = []
    for _ in range(8):
        history += [_turn("user", 4000), _turn("assistant", 4000)]
    assert compaction.should_compact(compaction.context_tokens(history, "hi", None), window)

    cut = compaction.manual_cut(history, compaction.keep_tokens(window))

    assert 0 < cut < len(history)
    assert compaction.shrinks_prompt(history, cut)
    summary = "s" * 4000
    assert not compaction.should_compact(compaction.context_tokens(history[cut:], "hi", summary), window)


def test_folding_a_short_exchange_does_not_shrink_the_prompt() -> None:
    """A cut that folds fewer tokens than a summary takes is skipped."""
    history = [_turn("user", 400), _turn("assistant", 400), _turn("user", 40_000)]

    assert not compaction.shrinks_prompt(history, 2)
    assert compaction.shrinks_prompt(history, 3)


def test_summary_is_cut_to_the_request_limit() -> None:
    """A summary longer than the request fields allow is cut, so the next turn can send it back."""
    summary = compaction.summarize(
        _FakeLM("x" * (compaction.SUMMARY_MAX_CHARS + 50)),
        [{"role": "user", "content": "hi"}],
        previous_summary=None,
        instructions=None,
        window=128_000,
    )

    assert len(summary) == compaction.SUMMARY_MAX_CHARS
