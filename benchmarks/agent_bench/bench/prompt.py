"""Build the identical instructions and user message every harness receives."""

from __future__ import annotations

import json
from pathlib import Path

from bench.task import Task

BRIEF_PATH = Path(__file__).resolve().parent.parent / "brief.md"


def brief() -> str:
    """Return the shared agent instructions (the system prompt)."""
    return BRIEF_PATH.read_text().strip()


def user_message(task: Task) -> str:
    """Render the task as one user message: context, earlier turns, then the request.

    Args:
        task: The task to render.

    Returns:
        The user message text.
    """
    parts: list[str] = []
    if task.wizard_state:
        parts.append("Current wizard state (JSON):\n" + json.dumps(task.wizard_state, ensure_ascii=False, indent=1))
    if task.history:
        lines = [f"{role.upper()}: {text}" for role, text in task.history]
        parts.append("Conversation so far:\n" + "\n".join(lines))
    parts.append(("USER: " if task.history else "") + task.prompt)
    return "\n\n".join(parts)
