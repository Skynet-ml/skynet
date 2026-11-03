"""Deliver messages a user sends into an agent turn that is still running.

A steerable turn request carries a client-chosen ``steer_key``. While the turn
streams, the chat posts follow-up messages under that key; the running loop
takes them at its next step boundary and appends them to its history as a new
user message, so the model reads them before its next decision. When the turn
ends, the client withdraws whatever the loop never read and sends it as the
next turn.

Messages live in the shared database because the steer POST and the streaming
turn can land on different backend replicas. Without an engine (tests, local
runs without Postgres) an in-process store stands in.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import dspy
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ...storage.models import AgentSteerMessageModel

logger = logging.getLogger(__name__)

STEER_APPLIED_EVENT = "steer_applied"
STEER_KEY_MAX_CHARS = 64
STEER_TEXT_MAX_CHARS = 20_000
# A turn never runs this long; older rows belong to turns that ended without
# a withdraw (a closed tab) and are purged on the next post.
_STALE_AFTER = timedelta(hours=6)


class SteerStore:
    """Hold steer messages per ``(owner, steer_key)`` until taken once."""

    def __init__(self) -> None:
        """Start with no engine bound and an empty in-process fallback."""
        self._engine: Any = None
        self._lock = threading.Lock()
        self._local: dict[tuple[str, str], list[tuple[str, str]]] = {}
        self._delivered: dict[tuple[str, str], set[str]] = {}

    def bind_engine(self, engine: Any) -> None:
        """Attach the shared database engine that backs cross-replica delivery.

        Args:
            engine: SQLAlchemy engine for the application database.
        """
        self._engine = engine

    def post(self, owner: str, steer_key: str, text: str) -> str:
        """Store one message for the turn running under ``steer_key``.

        Args:
            owner: Username of the caller.
            steer_key: The running turn's steer key.
            text: The message text.

        Returns:
            The new message's id.
        """
        message_id = uuid4().hex
        if self._engine is None:
            with self._lock:
                self._local.setdefault((owner, steer_key), []).append((message_id, text))
            return message_id
        now = datetime.now(UTC)
        with Session(self._engine) as session:
            session.add(
                AgentSteerMessageModel(id=message_id, steer_key=steer_key, owner=owner, text=text, created_at=now)
            )
            session.execute(
                delete(AgentSteerMessageModel).where(AgentSteerMessageModel.created_at < now - _STALE_AFTER)
            )
            session.commit()
        return message_id

    def take(self, owner: str, steer_key: str, ids: list[str] | None = None) -> list[tuple[str, str]]:
        """Take unread messages for ``steer_key``, oldest first.

        The row lock serializes reads with withdrawal.

        Args:
            owner: Username the turn belongs to.
            steer_key: The turn's steer key.
            ids: Take only these messages; ``None`` takes every unread one.

        Returns:
            ``(id, text)`` pairs, oldest first; empty when nothing is waiting.
        """
        if self._engine is None:
            with self._lock:
                waiting = self._local.pop((owner, steer_key), [])
                if ids is None:
                    self._delivered.setdefault((owner, steer_key), set()).update(m[0] for m in waiting)
                    return waiting
                wanted = set(ids)
                kept = [m for m in waiting if m[0] not in wanted]
                if kept:
                    self._local[(owner, steer_key)] = kept
                taken = [m for m in waiting if m[0] in wanted]
                self._delivered.setdefault((owner, steer_key), set()).update(m[0] for m in taken)
                return taken
        conditions = [
            AgentSteerMessageModel.steer_key == steer_key,
            AgentSteerMessageModel.owner == owner,
            AgentSteerMessageModel.taken_at.is_(None),
        ]
        if ids is not None:
            conditions.append(AgentSteerMessageModel.id.in_(ids))
        with Session(self._engine) as session:
            rows = (
                session.execute(
                    select(AgentSteerMessageModel)
                    .where(*conditions)
                    .order_by(AgentSteerMessageModel.created_at, AgentSteerMessageModel.id)
                    .with_for_update()
                )
                .scalars()
                .all()
            )
            if not rows:
                return []
            now = datetime.now(UTC)
            taken = [(row.id, row.text) for row in rows]
            for row in rows:
                row.taken_at = now
            session.commit()
            return taken

    def withdraw(self, owner: str, steer_key: str, ids: list[str] | None = None) -> tuple[list[str], list[str]]:
        """Delete unread messages and report messages already read by the agent.

        Args:
            owner: Username the turn belongs to.
            steer_key: The turn's steer key.
            ids: Requested ids, or all messages when omitted.

        Returns:
            Newly withdrawn ids and delivered ids. Missing ids were already withdrawn.
        """
        if self._engine is None:
            with self._lock:
                key = (owner, steer_key)
                waiting = self._local.get(key, [])
                wanted = set(ids) if ids is not None else None
                withdrawn = [mid for mid, _ in waiting if wanted is None or mid in wanted]
                self._local[key] = [m for m in waiting if m[0] not in withdrawn]
                delivered = self._delivered.get(key, set())
                return withdrawn, sorted(delivered if wanted is None else delivered & wanted)
        conditions = [AgentSteerMessageModel.owner == owner, AgentSteerMessageModel.steer_key == steer_key]
        if ids is not None:
            conditions.append(AgentSteerMessageModel.id.in_(ids))
        with Session(self._engine) as session:
            rows = session.scalars(
                select(AgentSteerMessageModel)
                .where(*conditions)
                .order_by(AgentSteerMessageModel.created_at, AgentSteerMessageModel.id)
                .with_for_update()
            ).all()
            withdrawn = []
            delivered = []
            for row in rows:
                if row.taken_at is not None:
                    delivered.append(row.id)
                else:
                    withdrawn.append(row.id)
                    session.delete(row)
            session.commit()
            return withdrawn, delivered


_store = SteerStore()


def get_steer_store() -> SteerStore:
    """Return the process-wide :class:`SteerStore`.

    Returns:
        The shared store used by the steer endpoints and the agent loops.
    """
    return _store


@dataclass
class SteerInbox:
    """One running turn's view of its steer messages."""

    owner: str
    steer_key: str
    input_field: str
    emit: Callable[[dict[str, Any]], None]
    store: SteerStore
    # Text handed to a step as its input but not yet written into the history
    # event ReActV2 appends for that step.
    unrecorded: str | None = None


def attach_steering(react: dspy.Module, inbox: SteerInbox | None) -> None:
    """Let a ReActV2 loop read steer messages between its steps.

    Args:
        react: The loop; its step predictor gets the inbox.
        inbox: The turn's inbox, or ``None`` for a turn that cannot be steered.
    """
    if inbox is None:
        return
    react.react.steer_inbox = inbox
    # Read through the class so DSPy does not flag a direct ``forward`` access.
    run = type(react).forward.__get__(react)

    def forward(**input_args: Any) -> Any:
        """Run the loop, then record a steer the final step consumed.

        Args:
            **input_args: The loop's inputs.

        Returns:
            The loop's prediction.
        """
        prediction = run(**input_args)
        history = getattr(prediction, "history", None)
        if isinstance(history, dspy.History):
            _record_unrecorded(inbox, history)
        return prediction

    react.forward = forward


def _record_unrecorded(inbox: SteerInbox, history: dspy.History) -> None:
    """Write the last steered text into the history event of the step that read it.

    ReActV2 records a step's inputs from its own bookkeeping, which never saw
    the steer, so the event it appended right after that step lacks it.

    Args:
        inbox: The turn's inbox.
        history: The loop history; its last event belongs to the steered step.
    """
    if inbox.unrecorded is None:
        return
    if history.messages and inbox.input_field not in history.messages[-1]:
        history.messages[-1][inbox.input_field] = inbox.unrecorded
    inbox.unrecorded = None


def deliver_steering(predictor: Any, kwargs: dict[str, Any]) -> None:
    """Hand waiting steer messages to the step about to run as its user input.

    The steer becomes the step's ``input_field`` value, so the model reads it
    as the newest user message right after the last tool result. A separate
    history event would replay as an empty assistant message after it.

    The first step still carries the turn's own message, so delivery starts
    at the second step.

    Args:
        predictor: The loop's step predictor, possibly carrying an inbox.
        kwargs: The step's inputs, updated in place.
    """
    inbox: SteerInbox | None = getattr(predictor, "steer_inbox", None)
    if inbox is None:
        return
    history = kwargs.get("history")
    if not isinstance(history, dspy.History):
        return
    _record_unrecorded(inbox, history)
    if inbox.input_field in kwargs:
        return
    try:
        taken = inbox.store.take(inbox.owner, inbox.steer_key)
    except Exception:
        logger.exception("Failed to read steer messages for an agent turn")
        return
    if not taken:
        return
    text = "\n\n".join(message for _, message in taken)
    kwargs[inbox.input_field] = text
    inbox.unrecorded = text
    inbox.emit({"event": STEER_APPLIED_EVENT, "data": {"ids": [message_id for message_id, _ in taken], "text": text}})
