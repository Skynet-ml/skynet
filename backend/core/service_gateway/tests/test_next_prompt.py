"""Tests for the next-message suggestion prompt and its cleanup."""

from __future__ import annotations

from ..agents import next_prompt
from ..agents.next_prompt import (
    MAX_SUGGESTION_CHARS,
    PROMPT_BUDGET_CHARS,
    build_messages,
    clean_suggestion,
    suggest_next_prompt,
    writes_lowercase,
)

_SAMPLES = "The user's own messages, oldest first"


def _turns(count: int) -> list[tuple[str, str]]:
    """Build alternating user and assistant turns, oldest first.

    Args:
        count: Number of turns.

    Returns:
        ``(role, content)`` pairs whose content names the turn's index.
    """
    return [("user" if i % 2 == 0 else "assistant", f"turn {i} text") for i in range(count)]


def test_recent_turns_are_verbatim_and_last() -> None:
    """The last turns come verbatim, after the older background, marked most important."""
    messages = build_messages(_turns(10), ["set_column_roles: done"], "English")
    body = messages[1]["content"]
    assert "English" in messages[0]["content"]
    for i in (7, 8, 9):
        assert f"turn {i} text" in body
    assert body.index(_SAMPLES) < body.index("Latest turns") < body.index("What just happened")
    assert "most important" in body.split("Latest turns", 1)[1]
    assert body.rstrip().endswith("The user's next message, written the way the user writes (all lowercase):")


def test_user_messages_are_passed_as_style_samples() -> None:
    """The user's own messages reach the model as style samples; assistant turns do not."""
    turns = [
        ("user", "hey so my bot is kinda bad at refunds " + "x" * 500),
        ("assistant", "old reply"),
        ("user", "its a gpt prompt"),
        *_turns(3),
    ]
    messages = build_messages(turns, [], "English")
    body = messages[1]["content"]
    samples = body.split(_SAMPLES, 1)[1].split("\n\n", 1)[0]
    lines = samples.splitlines()[1:]
    assert lines[0].startswith("> hey so my bot is kinda bad at refunds")
    assert len(lines[0]) <= next_prompt.STYLE_SAMPLE_CHARS + 2
    assert "> its a gpt prompt" in lines
    assert "old reply" not in samples
    system = messages[0]["content"]
    assert "samples of how the user writes" in system
    assert "never add spelling mistakes" in system
    assert "You are the user, not the assistant" in system


def test_lowercase_writer_is_detected_and_named_in_the_prompt() -> None:
    """A user who never types capitals gets an explicit all-lowercase request; Hebrew does not."""
    casual = [("user", "hey can u help w my prompt"), ("assistant", "Sure. Upload the CSV.")]
    formal = [("user", "Good afternoon. Please help."), ("assistant", "Upload the CSV.")]
    hebrew = [("user", "היי, אפשר עזרה?"), ("assistant", "תעלה CSV.")]
    assert writes_lowercase(casual)
    assert not writes_lowercase(formal)
    assert not writes_lowercase(hebrew)
    assert not writes_lowercase([])
    assert build_messages(casual, [], "English")[1]["content"].endswith("(all lowercase):")
    assert not build_messages(formal, [], "English")[1]["content"].endswith("(all lowercase):")


def test_no_user_messages_asks_for_a_short_casual_line() -> None:
    """With no user messages there are no samples, and the prompt asks for short and casual."""
    messages = build_messages([("assistant", "Hi! What do you want to optimize?")], [], "English")
    assert _SAMPLES not in messages[1]["content"]
    assert "short and casual, in English" in messages[0]["content"]


def test_last_reply_keeps_its_closing_question() -> None:
    """A long final reply keeps its tail, where the question or next step sits."""
    reply = "intro " * 1000 + "Which column is the label?"
    body = build_messages([("user", "hi"), ("assistant", reply)], [], "English")[1]["content"]
    assert "Which column is the label?" in body


def test_prompt_stays_in_budget_and_keeps_newest() -> None:
    """A huge transcript is cut from the oldest side to the character budget."""
    turns = [("user", "u" * 5000), ("assistant", "a" * 5000)] * 20 + [("assistant", "newest reply")]
    actions = [f"action {i} " + "z" * 300 for i in range(30)]
    body = build_messages(turns, actions, "English")[1]["content"]
    assert len(body) <= PROMPT_BUDGET_CHARS + len("\n\nThe user's next message, written the way the user writes:")
    assert "newest reply" in body
    assert "action 29" in body
    assert "action 21" not in body


def test_clean_suggestion_trims_quotes_labels_and_length() -> None:
    """Quotes, labels and extra lines go; a long line is cut at a word."""
    assert clean_suggestion('"Run the optimization now"\nextra', []) == "Run the optimization now"
    assert clean_suggestion("User: Show me the results", []) == "Show me the results"
    assert (
        clean_suggestion('# User\'s Likely Next Message\n\n"Yes, start with the dataset."', [])
        == "Yes, start with the dataset."
    )
    assert clean_suggestion("Here's my prediction of the user's next message:\n\nUpload the CSV now", []) == (
        "Upload the CSV now"
    )
    long = ("word " * 30).strip()
    cleaned = clean_suggestion(long, [])
    assert 0 < len(cleaned) <= MAX_SUGGESTION_CHARS
    assert not cleaned.endswith(" ")


def test_clean_suggestion_strips_markdown_and_assistant_filler() -> None:
    """Markdown markers and assistant openers go; the user's words stay."""
    assert clean_suggestion("**Upload the CSV now**", []) == "Upload the CSV now"
    assert clean_suggestion("- can i upload the csv", []) == "can i upload the csv"
    assert clean_suggestion("Sure, I'll upload the CSV now.", []) == "I'll upload the CSV now."
    assert clean_suggestion("great! can i upload it", []) == "can i upload it"
    assert clean_suggestion("Sure", []) == "Sure"


def test_clean_suggestion_lowercases_for_a_lowercase_writer() -> None:
    """A user who writes lowercase gets a lowercase suggestion."""
    assert clean_suggestion("I can export the CSV now", [], lowercase=True) == "i can export the csv now"
    assert clean_suggestion("I can export the CSV now", []) == "I can export the CSV now"


def test_clean_suggestion_drops_empty_paragraphs_and_repeats() -> None:
    """An empty answer, a paragraph, or a message already sent gives no suggestion."""
    assert clean_suggestion("   ", []) == ""
    assert clean_suggestion("x" * 400, []) == ""
    assert clean_suggestion("Start the run", ["start   the run"]) == ""


def test_suggest_next_prompt_calls_the_model_once() -> None:
    """The model gets the built messages and its answer is cleaned."""
    calls: list[list[dict[str, str]]] = []

    def fake_lm(*, messages: list[dict[str, str]]) -> list[str]:
        """Record the prompt and answer like a DSPy LM.

        Args:
            messages: The chat messages.

        Returns:
            One completion.
        """
        calls.append(messages)
        return ["'כן, תריץ את האופטימיזציה'"]

    out = suggest_next_prompt(fake_lm, [("user", "שלום"), ("assistant", "להריץ?")], [], "Hebrew")
    assert out == "כן, תריץ את האופטימיזציה"
    assert len(calls) == 1
    assert "Hebrew" in calls[0][0]["content"]
