"""Tests for the model-written agent chat title."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from ..agents import chat_title
from ..agents.chat_title import (
    MAX_TITLE_CHARS,
    MAX_TITLE_WORDS,
    MAX_UNSPACED_TITLE_CHARS,
    build_messages,
    clean_title,
    with_title_event,
    write_chat_title,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Optimize the support bot prompt", "Optimize the support bot prompt"),
        ('"Optimize the support bot."', "Optimize the support bot"),
        ("Title: Fix the CSV upload", "Fix the CSV upload"),
        ("Here is a title:\n**Fix the CSV upload**", "Fix the CSV upload"),
        ("# Fix the CSV upload\nmore text", "Fix the CSV upload"),
        ("«שיפור הבוט של התמיכה»", "שיפור הבוט של התמיכה"),
        ("Here is a title: Fix the CSV upload", "Fix the CSV upload"),
        ("Suggested title for this chat: Fix the CSV upload", "Fix the CSV upload"),
        ("#1 crash on save", "#1 crash on save"),
        ("## Fix the CSV upload", "Fix the CSV upload"),
        ("Fix: CSV upload crash", "Fix: CSV upload crash"),
        ("Page title: missing on mobile", "Page title: missing on mobile"),
        ("Fix the title: CSV import", "Fix the title: CSV import"),
        ("Here's a short title: Fix the CSV upload", "Fix the CSV upload"),
        ("Suggested title: Fix the CSV upload", "Fix the CSV upload"),
        ("Title:Fix upload", "Fix upload"),
        ("'90s music playlist", "'90s music playlist"),
        ('"Fix" the upload', '"Fix" the upload'),
        ("Rock 'n' roll", "Rock 'n' roll"),
        ("「サポートボットの改善」", "サポートボットの改善"),
        ("Translate the 你好 greeting into English for the docs page", "Translate the 你好"),
        ("修复 CSV 导入", "修复 CSV 导入"),
        ("`npm install` fails on CI", "`npm install` fails on CI"),
        ("Fix the `npm install` step", "Fix the `npm install` step"),
        ("*Fix the CSV upload*", "Fix the CSV upload"),
        ("__Fix the CSV upload__", "Fix the CSV upload"),
        ("`Fix the CSV upload`", "Fix the CSV upload"),
        ("_init_ fails on start", "_init_ fails on start"),
        ("`ls` or `dir` hangs", "`ls` or `dir` hangs"),
        ('"Fix" vs "Break"', '"Fix" vs "Break"'),
        ("«Fix» and «Break»", "«Fix» and «Break»"),
        ('"Fix the upload"', "Fix the upload"),
        ("修复 ABCDEFGHIJKLMNOPQRSTU xyz", "修复 ABCDEFGHIJKLMNOPQRSTU"),
        (
            "One two three four five six seven eight nine ten. eleven",
            "One two three four five six seven eight nine ten",
        ),
        (
            "Internationalization configuration troubleshooting documentation implementations now",
            "Internationalization configuration troubleshooting documentation implementations",
        ),
        ("**Title:** Fix the upload", "Fix the upload"),
        ("**Title**: Fix the upload", "Fix the upload"),
        ("**Title: Fix the upload**", "Fix the upload"),
        ("Title: **Fix the upload**", "Fix the upload"),
        ("```\nFix the CSV upload\n```", "Fix the CSV upload"),
        ("```text\nFix the CSV upload\n```", "Fix the CSV upload"),
        ("---\nFix the CSV upload", "Fix the CSV upload"),
        ("***", ""),
        ("# ", ""),
        ('"Fix the CSV upload".', "Fix the CSV upload"),
        ("**Fix the CSV upload**.", "Fix the CSV upload"),
        ("`Fix it`.", "Fix it"),
        ("   \n  ", ""),
        ("", ""),
    ],
)
def test_clean_title_gives_one_short_line(raw: str, expected: str) -> None:
    """Quotes, labels, Markdown and a final period go; Hebrew stays intact."""
    assert clean_title(raw) == expected


def test_clean_title_keeps_at_most_ten_words() -> None:
    """A long answer is cut to the word limit."""
    raw = " ".join(f"word{i}" for i in range(20))
    assert clean_title(raw).split() == [f"word{i}" for i in range(MAX_TITLE_WORDS)]


@pytest.mark.parametrize(
    "raw",
    [
        "修复客户支持机器人的提示词并提高回答质量和速度以及准确性和用户满意度",
        "サポートボットのプロンプトを改善して回答の品質と速度を上げる方法について",
        "ปรับปรุงพรอมต์ของบอทฝ่ายสนับสนุนลูกค้าให้ตอบได้ดีขึ้นและเร็วขึ้น",
    ],
)
def test_clean_title_cuts_chinese_and_japanese_by_characters(raw: str) -> None:
    """Chinese, Japanese and other unspaced text get the short character limit."""
    assert clean_title(raw) == raw[:MAX_UNSPACED_TITLE_CHARS].strip()


def test_clean_title_keeps_the_word_rule_for_korean() -> None:
    """Korean puts spaces between words, so it keeps the word and character limits."""
    raw = "고객 지원 봇의 프롬프트를 개선하고 응답 품질을 높이는 방법에 대한 대화 내용 정리"
    title = clean_title(raw)
    assert title.split() == raw.split()[:MAX_TITLE_WORDS]
    assert MAX_UNSPACED_TITLE_CHARS < len(title) <= MAX_TITLE_CHARS


def test_prompt_is_short_and_clips_the_message() -> None:
    """The system prompt stays short and a long paste is clipped."""
    messages = build_messages("fix   my\nbot " + "x" * 5000)
    assert len(messages[0]["content"]) < 300
    assert messages[1]["content"].startswith("fix my bot ")
    assert len(messages[1]["content"]) == chat_title.MESSAGE_CHARS


def test_write_chat_title_reads_the_first_output() -> None:
    """The first completion, as text or a dict, is cleaned into the title."""
    assert write_chat_title(lambda *, messages: [{"text": '"Tune the bot"'}], "hi") == "Tune the bot"
    assert write_chat_title(lambda *, messages: [], "hi") == ""


async def _events(names: list[str], delay: float = 0.0) -> AsyncIterator[dict[str, Any]]:
    """Yield one event for each name.

    Args:
        names: The event names.
        delay: Seconds to wait before each event.

    Yields:
        ``{event, data}`` mappings.
    """
    for name in names:
        await asyncio.sleep(delay)
        yield {"event": name, "data": {}}


async def _collect(source: AsyncIterator[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drain a stream.

    Args:
        source: The stream.

    Returns:
        Its events.
    """
    return [event async for event in source]


async def test_title_event_comes_after_the_first_event_even_when_ready_first() -> None:
    """A title ready before the turn starts never goes before the turn's own meta event."""
    task: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    task.set_result("Tune the bot")
    events = await _collect(with_title_event(_events(["conversation_meta", "done"]), task, "c1", "cut"))
    assert [e["event"] for e in events] == ["conversation_meta", "conversation_title", "done"]
    assert events[1]["data"] == {"conversation_id": "c1", "title": "Tune the bot", "cut_title": "cut"}


async def test_title_event_goes_out_between_slow_events() -> None:
    """The title does not wait for the turn to end."""

    async def title() -> str:
        """Finish quickly.

        Returns:
            The title.
        """
        await asyncio.sleep(0.01)
        return "Tune the bot"

    events = await _collect(
        with_title_event(_events(["a", "b", "c"], delay=0.05), asyncio.ensure_future(title()), "c1", "cut")
    )
    assert [e["event"] for e in events] == ["a", "conversation_title", "b", "c"]


async def test_stream_waits_briefly_for_a_late_title(monkeypatch: pytest.MonkeyPatch) -> None:
    """A title that ends after the turn still goes out; one that is too late is dropped."""
    monkeypatch.setattr(chat_title, "TAIL_WAIT_SECONDS", 0.2)

    async def title(delay: float) -> str:
        """Finish after ``delay``.

        Args:
            delay: Seconds to wait.

        Returns:
            The title.
        """
        await asyncio.sleep(delay)
        return "Late title"

    late = await _collect(with_title_event(_events(["done"]), asyncio.ensure_future(title(0.05)), "c1", "cut"))
    assert [e["event"] for e in late] == ["done", "turn_billed", "conversation_title"]
    slow = asyncio.ensure_future(title(1.0))
    too_late = await _collect(with_title_event(_events(["done"]), slow, "c1", "cut"))
    assert [e["event"] for e in too_late] == ["done", "turn_billed"]
    slow.cancel()


async def test_turn_billed_goes_out_after_the_source_settles_and_before_the_title() -> None:
    """The balance marker follows the source's ``finally`` (the debit) and precedes the tail wait."""
    order: list[str] = []

    async def metered() -> AsyncIterator[dict[str, Any]]:
        """Yield ``done``, then debit in ``finally`` like the metering wrapper.

        Yields:
            The ``done`` event.
        """
        try:
            yield {"event": "done", "data": {}}
        finally:
            order.append("debited")

    async def title() -> str:
        """Finish after the turn.

        Returns:
            The title.
        """
        await asyncio.sleep(0.05)
        return "Late title"

    # Append inside the loop: ``list.extend`` would collect every event before the debit line.
    async for event in with_title_event(metered(), asyncio.ensure_future(title()), "c1", "cut"):
        order.append(event["event"])  # noqa: PERF401
    assert order == ["done", "debited", "turn_billed", "conversation_title"]


async def test_failed_or_empty_title_sends_an_empty_title() -> None:
    """A failed or empty title task sends a title event with ``title: ""``, so the panel stops waiting."""
    loop = asyncio.get_running_loop()
    failed: asyncio.Future[str] = loop.create_future()
    failed.set_exception(RuntimeError("model down"))
    empty: asyncio.Future[str] = loop.create_future()
    empty.set_result("")
    for task in (failed, empty):
        events = await _collect(with_title_event(_events(["conversation_meta", "done"]), task, "c1", "cut"))
        assert [e["event"] for e in events] == ["conversation_meta", "conversation_title", "done"]
        assert events[1]["data"] == {"conversation_id": "c1", "title": "", "cut_title": "cut"}
