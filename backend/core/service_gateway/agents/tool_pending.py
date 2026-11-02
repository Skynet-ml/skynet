"""Spot tool calls in a ReAct step's stream before the step ends.

A tool's ``tool_start`` event fires only when the tool runs, after the whole
model call that asked for it has finished. A slow call, such as a local CLI
subscription that writes a large argument, leaves the chat with no sign of
work for that time. The spotter reads the step's streamed ``tool_calls``
(DSPy's text protocol) or native tool-call deltas and reports each tool name
as soon as it is complete, so the chat can show a pending tool row.
"""

from __future__ import annotations

import json

import dspy
import jiter

from .code import TOOL_ARGS_FIELD

TOOL_PENDING_EVENT = "tool_pending"
# Sent when a loop step's model call starts: a pending row from an earlier
# call (a failed parse, a dropped submit, a call cut by a steer) never runs.
TOOL_PENDING_CLEAR_EVENT = "tool_pending_clear"


class ToolCallSpotter:
    """Report each tool name a loop step's stream reveals, once per step."""

    def __init__(self, *, native: bool, skip: frozenset[str]) -> None:
        """Bind the spotter to the loop's tool protocol.

        Args:
            native: Read native tool-call deltas (:data:`TOOL_ARGS_FIELD`)
                instead of the text protocol's ``tool_calls`` field.
            skip: Tool names that get no tool row (the reply's ``submit``,
                the plan), so no pending row is reported for them.
        """
        self._native = native
        self._skip = skip
        self._buffer = ""
        self._names: dict[int, str] = {}
        self._reported: set[int] = set()

    def feed(self, chunk: dspy.streaming.StreamResponse) -> list[tuple[int, str]]:
        """Take one stream response and return the tool calls it revealed.

        Args:
            chunk: A non-reasoning ``StreamResponse`` from the loop.

        Returns:
            ``(index, tool name)`` per call whose name became complete in this
            chunk, in call order; empty for chunks of any other field.
        """
        field = TOOL_ARGS_FIELD if self._native else "tool_calls"
        if chunk.signature_field_name != field:
            return []
        if self._native:
            self._fold_native(chunk.chunk or "")
        else:
            self._buffer += chunk.chunk or ""
            self._read_text()
        found = [
            (index, name)
            for index, name in sorted(self._names.items())
            if index not in self._reported and name not in self._skip
        ]
        self._reported.update(index for index, _ in found)
        if chunk.is_last_chunk:
            self.reset()
        return found

    def reset(self) -> None:
        """Forget the current step, for a model call that starts before the last one closed its field."""
        self._buffer = ""
        self._names = {}
        self._reported = set()

    def _fold_native(self, chunk: str) -> None:
        """Record the names a batch of native tool-call deltas carries.

        A delta names its call once, in full, so a name counts at once.

        Args:
            chunk: JSON list of ``{"index", "name", "arguments"}`` deltas.
        """
        try:
            deltas = json.loads(chunk) if chunk else []
        except ValueError:
            return
        for delta in deltas if isinstance(deltas, list) else []:
            if not isinstance(delta, dict):
                continue
            index, name = delta.get("index"), delta.get("name")
            if isinstance(index, int) and isinstance(name, str) and name:
                self._names.setdefault(index, name)

    def _read_text(self) -> None:
        """Record the complete tool names in the text protocol's ``tool_calls`` JSON so far."""
        start = self._buffer.find("{")
        if start < 0:
            return
        try:
            # "on" drops an unfinished trailing string, so a name counts only
            # once its closing quote arrived.
            parsed = jiter.from_json(self._buffer[start:].encode("utf-8"), partial_mode="on")
        except ValueError:
            return
        calls = parsed.get("tool_calls") if isinstance(parsed, dict) else None
        for index, call in enumerate(calls if isinstance(calls, list) else []):
            name = call.get("name") if isinstance(call, dict) else None
            if isinstance(name, str) and name:
                self._names.setdefault(index, name)
