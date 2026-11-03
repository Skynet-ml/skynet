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
from hashlib import blake2b
from typing import Any
from uuid import uuid4

import dspy
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ...storage.models import AgentSteerMessageModel

logger = logging.getLogger(__name__)

STEER_APPLIED_EVENT = "steer_applied"
STEER_KEY_MAX_CHARS = 64
STEER_TEXT_MAX_CHARS = 20_000
STEER_PENDING_MAX_MESSAGES = 10
STEER_PENDING_MAX_CHARS = 20_000
STEER_OWNER_PENDING_MAX_MESSAGES = 100
STEER_OWNER_PENDING_MAX_CHARS = 200_000
_PURGE_BATCH_SIZE = 100
# A turn never runs this long; older rows belong to turns that ended without
# a withdraw (a closed tab) and are purged on the next post.
_STALE_AFTER = timedelta(hours=6)


class SteerLimitError(ValueError):
    """Reject a steer that exceeds its turn's pending input allowance."""


class SteerStore:
    """Hold steer messages per ``(owner, steer_key)`` until taken once."""

    def __init__(self) -> None:
        """Start with no engine bound and an empty in-process fallback."""
        self._engine: Any = None
        self._lock = threading.Lock()
        self._local: dict[tuple[str, str], list[tuple[str, str]]] = {}
        self._delivered: dict[tuple[str, str], dict[str, None]] = {}
        self._local_updated: dict[tuple[str, str], datetime] = {}

    def _purge_local(self, now: datetime) -> None:
        """Expire inactive fallback keys while the store lock is held.

        Args:
            now: Current UTC time.
        """
        stale = [key for key, updated in self._local_updated.items() if updated < now - _STALE_AFTER]
        for key in stale:
            self._local.pop(key, None)
            self._delivered.pop(key, None)
            self._local_updated.pop(key, None)

    @staticmethod
    def _check_pending(
        messages: list[str], text: str, *, max_messages: int = STEER_PENDING_MAX_MESSAGES,
        max_chars: int = STEER_PENDING_MAX_CHARS,
    ) -> None:
        """Reject input that would exceed the pending count or character cap.

        Args:
            messages: Text of pending messages for this owner and key.
            text: Proposed new message.
            max_messages: Maximum pending message count.
            max_chars: Maximum pending character count.

        Raises:
            SteerLimitError: The pending allowance would be exceeded.
        """
        if len(messages) >= max_messages or sum(map(len, messages)) + len(text) > max_chars:
            raise SteerLimitError("The pending steer limit for this turn was reached; queue this message instead.")

    @staticmethod
    def _fitting_prefix(messages: list[tuple[str, str]], max_chars: int | None) -> list[tuple[str, str]]:
        """Keep the longest ordered prefix within one step's input allowance.

        Args:
            messages: Ordered unread messages.
            max_chars: Maximum joined length, or no limit.

        Returns:
            Messages that fit without consuming or truncating the rest.
        """
        if max_chars is None:
            return messages
        length = 0
        for index, (_, text) in enumerate(messages):
            length += len(text) + (2 if index else 0)
            if length > max_chars:
                return messages[:index]
        return messages

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

        Raises:
            SteerLimitError: The key's pending input allowance is full.
        """
        message_id = uuid4().hex
        if self._engine is None:
            with self._lock:
                now = datetime.now(UTC)
                self._purge_local(now)
                key = (owner, steer_key)
                self._check_pending(
                    [message for (username, _), waiting in self._local.items() if username == owner
                     for _, message in waiting], text,
                    max_messages=STEER_OWNER_PENDING_MAX_MESSAGES, max_chars=STEER_OWNER_PENDING_MAX_CHARS,
                )
                self._check_pending([message for _, message in self._local.get(key, [])], text)
                self._local.setdefault(key, []).append((message_id, text))
                self._local_updated[key] = now
            return message_id
        now = datetime.now(UTC)
        with Session(self._engine) as session:
            if session.get_bind().dialect.name == "postgresql":
                # Production uses Postgres; SQLite is only a sequential test fallback.
                # Serialize all this owner's keys, including keys with no rows yet.
                lock_key = int.from_bytes(blake2b(owner.encode(), digest_size=8).digest(), signed=True)
                session.execute(select(func.pg_advisory_xact_lock(lock_key)))
            stale_ids = (
                select(AgentSteerMessageModel.id)
                .where(AgentSteerMessageModel.created_at < now - _STALE_AFTER)
                .order_by(AgentSteerMessageModel.created_at)
                .limit(_PURGE_BATCH_SIZE)
                .with_for_update(skip_locked=True)
            )
            session.execute(
                delete(AgentSteerMessageModel).where(AgentSteerMessageModel.id.in_(stale_ids))
            )
            pending = session.execute(
                select(AgentSteerMessageModel.steer_key, AgentSteerMessageModel.text).where(
                    AgentSteerMessageModel.owner == owner,
                    AgentSteerMessageModel.taken_at.is_(None),
                    AgentSteerMessageModel.created_at >= now - _STALE_AFTER,
                ).limit(STEER_OWNER_PENDING_MAX_MESSAGES)
            ).all()
            self._check_pending(
                [message for _, message in pending], text,
                max_messages=STEER_OWNER_PENDING_MAX_MESSAGES, max_chars=STEER_OWNER_PENDING_MAX_CHARS,
            )
            self._check_pending([message for key, message in pending if key == steer_key], text)
            session.add(
                AgentSteerMessageModel(id=message_id, steer_key=steer_key, owner=owner, text=text, created_at=now)
            )
            session.commit()
        return message_id

    def take(
        self, owner: str, steer_key: str, ids: list[str] | None = None, *, max_chars: int | None = None,
    ) -> list[tuple[str, str]]:
        """Take unread messages for ``steer_key``, oldest first.

        The row lock serializes reads with withdrawal.

        Args:
            owner: Username the turn belongs to.
            steer_key: The turn's steer key.
            ids: Take only these messages; ``None`` takes every unread one.
            max_chars: Maximum joined input length; leave the remaining messages unread.

        Returns:
            ``(id, text)`` pairs, oldest first; empty when nothing is waiting.
        """
        if self._engine is None:
            with self._lock:
                now = datetime.now(UTC)
                self._purge_local(now)
                key = (owner, steer_key)
                waiting = self._local.get(key, [])
                wanted = set(ids) if ids is not None else None
                taken = self._fitting_prefix([m for m in waiting if wanted is None or m[0] in wanted], max_chars)
                taken_ids = {mid for mid, _ in taken}
                kept = [m for m in waiting if m[0] not in taken_ids]
                if kept:
                    self._local[key] = kept
                else:
                    self._local.pop(key, None)
                if taken:
                    self._delivered.setdefault(key, {}).update((mid, None) for mid, _ in taken)
                    self._local_updated[key] = now
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
            taken = self._fitting_prefix([(row.id, row.text) for row in rows], max_chars)
            taken_ids = {mid for mid, _ in taken}
            for row in rows:
                if row.id in taken_ids:
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
                self._purge_local(datetime.now(UTC))
                key = (owner, steer_key)
                waiting = self._local.get(key, [])
                wanted = set(ids) if ids is not None else None
                withdrawn = [mid for mid, _ in waiting if wanted is None or mid in wanted]
                kept = [m for m in waiting if m[0] not in withdrawn]
                if kept:
                    self._local[key] = kept
                else:
                    self._local.pop(key, None)
                delivered = self._delivered.get(key, {})
                if not kept and not delivered:
                    self._local_updated.pop(key, None)
                return withdrawn, [mid for mid in delivered if wanted is None or mid in wanted]
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
    max_chars: int | None = None
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
        taken = inbox.store.take(inbox.owner, inbox.steer_key, max_chars=inbox.max_chars)
    except Exception:
        logger.exception("Failed to read steer messages for an agent turn")
        return
    if not taken:
        return
    text = "\n\n".join(message for _, message in taken)
    kwargs[inbox.input_field] = text
    inbox.unrecorded = text
    inbox.emit({"event": STEER_APPLIED_EVENT, "data": {"ids": [message_id for message_id, _ in taken], "text": text}})
