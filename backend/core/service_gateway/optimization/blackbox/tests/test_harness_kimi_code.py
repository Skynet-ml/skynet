"""Tests for the Kimi Code harness: registry entry, gateway-only launch, effort names and output parsing."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ..harness_bridge import parse_pi_output, tool_call_ids
from ..harnesses.kimi_code import (
    KIMI_CODE_TO_PI,
)


def _loop_event(event: dict[str, object]) -> dict[str, object]:
    """Wrap a loop event as a Kimi Code 2.5.3 ``wire.jsonl`` record.

    Args:
        event: The loop event.

    Returns:
        The journal record.
    """
    return {"type": "context.append_loop_event", "agentId": "main", "event": event}


def _text(step: str, text: str) -> dict[str, object]:
    """Build a journal record for one text part of a model step.

    Args:
        step: The step uuid.
        text: The text.

    Returns:
        The journal record.
    """
    return _loop_event({"type": "content.part", "stepUuid": step, "part": {"type": "text", "text": text}})


def _parse_main_journal(tmp_path: Path, records: list[dict[str, object]]) -> str | None:
    """Write a main agent journal and return the answer :data:`KIMI_CODE_TO_PI` reads from it.

    Args:
        tmp_path: Test folder.
        records: The journal records.

    Returns:
        The answer the Pi parser reads.
    """
    sessions = tmp_path / "sessions"
    journal = sessions / "ws1" / "sess1" / "agents" / "main" / "wire.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text("".join(json.dumps(record) + "\n" for record in records))
    result = subprocess.run(
        ["python3", "-I", "-c", KIMI_CODE_TO_PI, str(sessions)],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return parse_pi_output(result.stdout)[0]


@pytest.mark.parametrize(
    ("last", "expected"),
    [
        ([_text("s2", "  One. "), _text("s2", "Two.  ")], "One. Two."),
        (
            [_loop_event({"type": "content.part", "stepUuid": "s2", "part": {"type": "think", "think": "x"}})],
            "Earlier.",
        ),
        ([_loop_event({"type": "tool.call", "stepUuid": "s2", "toolCallId": "c1", "name": "Read"})], "Earlier."),
        ([_text("s2", "   ")], "Earlier."),
    ],
)
def test_answer_is_the_text_of_the_last_model_step_with_text(
    tmp_path: Path, last: list[dict[str, object]], expected: str
) -> None:
    """Join the text parts of the last model step that has text, and keep an earlier answer otherwise."""
    assert _parse_main_journal(tmp_path, [_text("s1", "Earlier."), *last]) == expected


def test_hook_results_never_replace_the_answer(tmp_path: Path) -> None:
    """Read the answer only from model step text, so a hook result message after it never replaces it."""
    hook = {
        "type": "context.append_message",
        "agentId": "main",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "Stop hook\n\nAll checks passed."}],
            "origin": {"kind": "hook_result", "event": "Stop", "blocked": True},
        },
    }

    assert _parse_main_journal(tmp_path, [_text("s1", "The answer."), hook]) == "The answer."


def test_tool_calls_count_every_agent_like_the_token_usage(tmp_path: Path) -> None:
    """Count the tool calls of every agent journal, once each, and keep a call id that two agents share."""
    sessions = tmp_path / "sessions"
    agents = sessions / "ws1" / "sess1" / "agents"
    call = {"type": "tool.call", "stepUuid": "s1", "toolCallId": "call_1", "name": "Read"}
    for agent, records in {
        "main": [_loop_event(call), _loop_event(call), _text("s1", "Done.")],
        "sub1": [{"type": "context.append_loop_event", "agentId": "sub1", "event": call}],
    }.items():
        (agents / agent).mkdir(parents=True)
        (agents / agent / "wire.jsonl").write_text("".join(json.dumps(record) + "\n" for record in records))
    result = subprocess.run(
        ["python3", "-I", "-c", KIMI_CODE_TO_PI, str(sessions)],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )

    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert parse_pi_output(result.stdout)[0] == "Done."
    assert len({call for event in events for call in tool_call_ids("pi", event)}) == 2


def test_no_journal_gives_no_answer(tmp_path: Path) -> None:
    """Print an empty answer with zero usage when Kimi Code wrote no journal."""
    result = subprocess.run(
        ["python3", "-I", "-c", KIMI_CODE_TO_PI, str(tmp_path / "sessions")],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )

    assert parse_pi_output(result.stdout) == (None, {"input_tokens": 0, "output_tokens": 0})
