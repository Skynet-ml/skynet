"""Tests for spotting tool calls in a ReAct step's stream before the step ends."""

from __future__ import annotations

import json

from dspy.streaming import StreamResponse

from core.service_gateway.agents.code import TOOL_ARGS_FIELD
from core.service_gateway.agents.tool_pending import ToolCallSpotter

SKIP = frozenset({"submit", "share_plan"})


def _chunk(field: str, text: str, *, last: bool = False) -> StreamResponse:
    """Build one stream response of the loop predictor.

    Args:
        field: The signature field the chunk belongs to.
        text: The chunk text.
        last: Whether the chunk ends the field.

    Returns:
        The stream response.
    """
    return StreamResponse(predict_name="react", signature_field_name=field, chunk=text, is_last_chunk=last)


def test_text_protocol_reports_each_tool_once_its_name_is_complete() -> None:
    """Report a text-protocol call when its name closes, once, and skip ``submit``."""
    spotter = ToolCallSpotter(native=False, skip=SKIP)
    text = (
        '{"tool_calls": [{"name": "update_wizard_state", "args": {"step": "data"}}, '
        '{"name": "submit", "args": {"assistant_message": "Done"}}]}'
    )
    seen: list[tuple[int, str]] = []
    for start in range(0, len(text), 3):
        piece = text[start : start + 3]
        found = spotter.feed(_chunk("tool_calls", piece))
        if found:
            assert '"update_wizard_state"' in text[: start + 3]
        seen += found
    assert seen == [(0, "update_wizard_state")]


def test_text_protocol_ignores_an_unfinished_name_and_resets_per_step() -> None:
    """Wait for the closing quote, and report the same tool again in the next step."""
    spotter = ToolCallSpotter(native=False, skip=SKIP)
    assert spotter.feed(_chunk("tool_calls", '{"tool_calls": [{"name": "sea')) == []
    assert spotter.feed(_chunk("tool_calls", 'rch", "args": {}}]}', last=True)) == [(0, "search")]
    assert spotter.feed(_chunk("tool_calls", '{"tool_calls": [{"name": "search"')) == [(0, "search")]


def test_other_fields_and_skipped_tools_report_nothing() -> None:
    """Ignore chunks of other fields, and the plan and reply tools."""
    spotter = ToolCallSpotter(native=False, skip=SKIP)
    assert spotter.feed(_chunk("next_thought", '{"tool_calls": [{"name": "search"}]}')) == []
    assert spotter.feed(_chunk("tool_calls", '{"tool_calls": [{"name": "share_plan", "args": {}}]}')) == []


def test_native_deltas_report_each_named_call() -> None:
    """Report native calls by index as their name delta arrives, skipping ``submit``."""
    spotter = ToolCallSpotter(native=True, skip=SKIP)
    first = json.dumps([{"index": 0, "name": "search", "arguments": '{"q'}])
    second = json.dumps(
        [{"index": 0, "name": "", "arguments": '": 1}'}, {"index": 1, "name": "submit", "arguments": ""}]
    )
    assert spotter.feed(_chunk(TOOL_ARGS_FIELD, first)) == [(0, "search")]
    assert spotter.feed(_chunk(TOOL_ARGS_FIELD, second)) == []
    assert spotter.feed(_chunk("tool_calls", first)) == []
