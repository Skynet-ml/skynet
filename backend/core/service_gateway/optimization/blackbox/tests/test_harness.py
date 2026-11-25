"""Tests for the harness catalog: per-harness launches, overrides and output parsers."""

from __future__ import annotations

import json

from ..harness import (
    MCP_HARNESSES,
)
from ..harness_bridge import (
    parse_codex_output,
    parse_opencode_output,
    parse_pi_output,
    parse_plain_output,
)


def test_parse_plain_output_strips_and_reports_none_when_empty() -> None:
    """Plain output is the stripped text, or None when blank, with no usage."""
    assert parse_plain_output("  hello \n") == ("hello", {})
    assert parse_plain_output("   ") == (None, {})


def test_parse_pi_output_takes_the_last_assistant_message_and_sums_usage() -> None:
    """Pi parsing keeps the final assistant text, joins its parts, and sums usage across turns."""
    stdout = "\n".join(
        [
            "not json",
            json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "first"}],
                        "usage": {"input": 10, "output": 5},
                    },
                }
            ),
            json.dumps({"type": "message_end", "message": {"role": "user", "content": []}}),
            json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "second"}, {"type": "text", "text": "line"}],
                        "usage": {"input": 3, "output": 7},
                    },
                }
            ),
        ]
    )

    text, usage = parse_pi_output(stdout)

    assert text == "second\nline"
    assert usage == {"input_tokens": 13, "output_tokens": 12}


def test_parse_codex_output_reads_agent_message_and_turn_usage() -> None:
    """Codex parsing keeps the final agent message and the turn's token usage."""
    stdout = "\n".join(
        [
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "the answer"}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 8, "output_tokens": 4}}),
        ]
    )

    assert parse_codex_output(stdout) == ("the answer", {"input_tokens": 8, "output_tokens": 4})


def test_parse_opencode_output_reads_text_parts_and_step_usage() -> None:
    """The last completed text part is the answer; every step_finish adds its tokens."""
    stdout = "\n".join(
        [
            json.dumps({"type": "step_start", "part": {"type": "step-start"}}),
            json.dumps({"type": "tool_use", "part": {"type": "tool", "tool": "read"}}),
            json.dumps({"type": "step_finish", "part": {"tokens": {"input": 5, "output": 2, "reasoning": 0}}}),
            json.dumps({"type": "text", "part": {"type": "text", "text": "draft"}}),
            json.dumps({"type": "text", "part": {"type": "text", "text": " the answer \n"}}),
            json.dumps({"type": "step_finish", "part": {"tokens": {"input": 3, "output": 2}}}),
            "not json",
        ]
    )
    assert parse_opencode_output(stdout) == ("the answer", {"input_tokens": 8, "output_tokens": 4})
    assert parse_opencode_output("") == (None, {})


def test_the_mcp_harness_list_names_every_harness_that_loads_the_relay_urls() -> None:
    """Name exactly the harnesses that write the relay urls into their config (SKY-618, SKY-619, SKY-620, SKY-621)."""
    assert MCP_HARNESSES == (
        "codex",
        "opencode",
        "opencode_v2",
        "qwen_code",
        "cline",
        "kimi_code",
        "openhands",
        "hermes",
        "goose",
        "openhands_sdk",
        "antigravity_sdk",
        "deerflow",
        "eve",
        "fabric",
        "code_puppy",
        "grok_build",
    )
