"""Stream events for tool calls the model asked for but that have not run yet.

A tool's ``tool_start`` event fires only when the tool runs, after the whole
model call that asked for it has finished. A slow call leaves the chat with
no sign of work for that time, so the Pi event translator reports each tool
name as soon as the model's stream reveals it, and the chat shows a pending
tool row.
"""

from __future__ import annotations

TOOL_PENDING_EVENT = "tool_pending"
# Sent when a loop step's model call starts: a pending row from an earlier
# call (a failed parse, a dropped submit, a call cut by a steer) never runs.
TOOL_PENDING_CLEAR_EVENT = "tool_pending_clear"
