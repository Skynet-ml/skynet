"""Carry the user's extra approval choices from the confirm call to the waiting tool.

The approval table keeps one boolean for each call. The user can also pick a
scope ("allow for this chat") and write a note when they decline (SKY-706).
The confirm call posts these extras to the steer store under
:func:`approval_choice_key` before it resolves the boolean, so the tool reads
them on any replica after its wait ends.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Literal

from .steering import SteerInputError, SteerLimitError, get_steer_store

logger = logging.getLogger(__name__)

ApprovalScope = Literal["once", "conversation"]

# The note reaches the agent as user text, so a long paste must not fill its context.
DECLINE_NOTE_MAX_CHARS = 500
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


@dataclass(frozen=True)
class ApprovalChoice:
    """The scope and the decline note the user sent with one decision."""

    scope: ApprovalScope = "once"
    note: str = ""


def approval_choice_key(call_id: str) -> str:
    """Return the steer-store key that holds the extras of one approval.

    Args:
        call_id: The short id from the ``pending_approval`` event.

    Returns:
        The key, at most 33 characters for a 24-character id.
    """
    return f"approval:{call_id}"


def clean_note(note: str | None) -> str:
    """Strip control characters and clip a decline note.

    Args:
        note: The note the user typed, or ``None``.

    Returns:
        The note without control characters (newlines stay), trimmed and at
        most :data:`DECLINE_NOTE_MAX_CHARS` characters.
    """
    return _CONTROL_CHARS.sub("", note or "").strip()[:DECLINE_NOTE_MAX_CHARS].strip()


def post_approval_choice(owner: str, call_id: str, scope: ApprovalScope, note: str | None) -> None:
    """Store the extras of one decision for the tool that waits on it.

    A failed write only drops the extras: the decision itself still goes
    through, as a one-time choice with no note.

    Args:
        owner: Username of the caller, the same owner the approval key uses.
        call_id: The short id from the ``pending_approval`` event.
        scope: ``"conversation"`` asks to allow the tool for the rest of the chat.
        note: The decline note, or ``None``.
    """
    text = clean_note(note)
    if scope == "once" and not text:
        return
    try:
        get_steer_store().post(owner, approval_choice_key(call_id), json.dumps({"scope": scope, "note": text}))
    except (SteerInputError, SteerLimitError):
        logger.warning("approval extras for %s were not stored", call_id, exc_info=True)


def take_approval_choice(owner: str | None, call_id: str) -> ApprovalChoice:
    """Read and consume the extras the confirm call stored for one decision.

    Args:
        owner: Username the approval is bound to; ``None`` has no extras.
        call_id: The short id from the ``pending_approval`` event.

    Returns:
        The latest stored choice, or the default one-time choice with no note
        when nothing usable was stored.
    """
    if not owner:
        return ApprovalChoice()
    store = get_steer_store()
    key = approval_choice_key(call_id)
    taken = store.take(owner, key)
    if not taken:
        return ApprovalChoice()
    store.mark_delivered(owner, key, taken)
    try:
        value = json.loads(taken[-1][1])
    except ValueError:
        return ApprovalChoice()
    if not isinstance(value, dict):
        return ApprovalChoice()
    scope: ApprovalScope = "conversation" if value.get("scope") == "conversation" else "once"
    note = value.get("note")
    return ApprovalChoice(scope=scope, note=clean_note(note if isinstance(note, str) else None))


def declined_result(note: str) -> str:
    """Build the tool result the agent reads after the user declines.

    Args:
        note: The cleaned decline note, or ``""``.

    Returns:
        ``"User declined"``, followed by the note as quoted user text when
        there is one.
    """
    if not note:
        return "User declined"
    return f"User declined. The user said instead: {json.dumps(note, ensure_ascii=False)}"
