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

import asyncio
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import blake2b
from itertools import islice
from typing import Any
from uuid import uuid4

import dspy
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ...storage.models import AgentSteerMessageModel, AgentSteerTurnModel

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


class SteerInputError(ValueError):
    """Reject text that the registered turn cannot accept."""


class SteerStore:
    """Hold steer messages per ``(owner, steer_key)`` until taken once."""

    def __init__(self) -> None:
        """Start with no engine bound and an empty in-process fallback."""
        self._engine: Any = None
        self._lock = threading.Lock()
        self._local: dict[tuple[str, str], list[tuple[str, str]]] = {}
        self._claimed: dict[tuple[str, str], set[str]] = {}
        self._delivered: dict[tuple[str, str], dict[str, None]] = {}
        self._local_updated: dict[tuple[str, str], datetime] = {}
        self._limits: dict[tuple[str, str], int] = {}

    def _purge_local(self, now: datetime) -> None:
        """Expire inactive fallback keys while the store lock is held.

        Args:
            now: Current UTC time.
        """
        for key in list(islice(self._local_updated, _PURGE_BATCH_SIZE)):
            self._expire_local_key(key, now)
            if key in self._local_updated:
                self._local_updated[key] = self._local_updated.pop(key)

    def _expire_local_key(self, key: tuple[str, str], now: datetime) -> None:
        """Expire a directly accessed key even if the cleanup batch did not reach it.

        Args:
            key: Owner and turn key.
            now: Current UTC time.
        """
        if self._local_updated.get(key, now) < now - _STALE_AFTER:
            self._local.pop(key, None)
            self._claimed.pop(key, None)
            self._delivered.pop(key, None)
            self._local_updated.pop(key, None)
            self._limits.pop(key, None)

    def open_turn(self, owner: str, steer_key: str, max_chars: int) -> None:
        """Register the turn limit before its steering channel is announced.

        Args:
            owner: Authenticated mailbox owner.
            steer_key: Key announced by the running turn.
            max_chars: Maximum text accepted by that turn.
        """
        now = datetime.now(UTC)
        if self._engine is None:
            with self._lock:
                self._purge_local(now)
                self._expire_local_key((owner, steer_key), now)
                self._limits[(owner, steer_key)] = max_chars
                self._local_updated[(owner, steer_key)] = now
            return
        with Session(self._engine) as session:
            self._lock_owner(session, owner)
            session.merge(AgentSteerTurnModel(owner=owner, steer_key=steer_key, max_chars=max_chars, created_at=now))
            stale = select(AgentSteerTurnModel.owner, AgentSteerTurnModel.steer_key).where(
                AgentSteerTurnModel.created_at < now - _STALE_AFTER
            ).limit(_PURGE_BATCH_SIZE).with_for_update(skip_locked=True)
            for old_owner, old_key in session.execute(stale).all():
                session.execute(delete(AgentSteerTurnModel).where(
                    AgentSteerTurnModel.owner == old_owner, AgentSteerTurnModel.steer_key == old_key))
            session.commit()

    @staticmethod
    def _lock_owner(session: Session, owner: str) -> None:
        """Serialize admission across one owner's keys in PostgreSQL.

        Args:
            session: Current storage transaction.
            owner: Authenticated owner whose input allowance is checked.
        """
        if session.get_bind().dialect.name == "postgresql":
            lock_key = int.from_bytes(blake2b(owner.encode(), digest_size=8).digest(), signed=True)
            session.execute(select(func.pg_advisory_xact_lock(lock_key)))

    @staticmethod
    def _check_input(text: str, max_chars: int) -> None:
        """Reject blank or non-fitting text before it can block the FIFO head.

        Args:
            text: Proposed steer message.
            max_chars: Registered turn limit or the default shared limit.

        Raises:
            SteerInputError: The message cannot serve as input to the turn.
        """
        if not text.strip() or len(text) > max_chars:
            raise SteerInputError("Steer text must be nonblank and fit the turn's input limit.")

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
            SteerInputError: Text is blank or exceeds the registered turn limit.
            SteerLimitError: The key's pending input allowance is full.
        """
        message_id = uuid4().hex
        if self._engine is None:
            with self._lock:
                now = datetime.now(UTC)
                self._purge_local(now)
                key = (owner, steer_key)
                self._expire_local_key(key, now)
                self._check_input(text, self._limits.get(key, STEER_TEXT_MAX_CHARS))
                self._check_pending(
                    [message for (username, key_name), waiting in self._local.items() if username == owner
                     and self._local_updated.get((username, key_name), now) >= now - _STALE_AFTER
                     for _, message in waiting], text,
                    max_messages=STEER_OWNER_PENDING_MAX_MESSAGES, max_chars=STEER_OWNER_PENDING_MAX_CHARS,
                )
                self._check_pending([message for _, message in self._local.get(key, [])], text)
                self._local.setdefault(key, []).append((message_id, text))
                self._local_updated[key] = now
            return message_id
        now = datetime.now(UTC)
        with Session(self._engine) as session:
            self._lock_owner(session, owner)
            turn = session.get(AgentSteerTurnModel, (owner, steer_key))
            self._check_input(text, turn.max_chars if turn else STEER_TEXT_MAX_CHARS)
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
        """Claim unread messages for ``steer_key``, oldest first.

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
                self._expire_local_key(key, now)
                waiting = self._local.get(key, [])
                claimed = self._claimed.get(key, set())
                wanted = set(ids) if ids is not None else None
                taken = self._fitting_prefix(
                    [m for m in waiting if m[0] not in claimed and (wanted is None or m[0] in wanted)], max_chars,
                )
                taken_ids = {mid for mid, _ in taken}
                if taken:
                    self._claimed.setdefault(key, set()).update(taken_ids)
                    self._local_updated[key] = now
                return taken
        conditions = [
            AgentSteerMessageModel.steer_key == steer_key,
            AgentSteerMessageModel.owner == owner,
            AgentSteerMessageModel.taken_at.is_(None),
            AgentSteerMessageModel.claimed_at.is_(None),
            AgentSteerMessageModel.created_at >= datetime.now(UTC) - _STALE_AFTER,
        ]
        if ids is not None:
            conditions.append(AgentSteerMessageModel.id.in_(ids))
        with Session(self._engine) as session:
            self._lock_owner(session, owner)
            rows = (
                session.execute(
                    select(AgentSteerMessageModel)
                    .where(*conditions)
                    .order_by(AgentSteerMessageModel.created_at, AgentSteerMessageModel.id)
                    .limit(STEER_PENDING_MAX_MESSAGES)
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
                    row.claimed_at = now
            session.commit()
            return taken

    def mark_delivered(self, owner: str, steer_key: str, messages: list[tuple[str, str]]) -> None:
        """Commit delivery proof immediately before the model consumes claimed input.

        Args:
            owner: Mailbox owner.
            steer_key: Running turn's key.
            messages: Claimed messages passed to this model step.
        """
        ids = {mid for mid, _ in messages}
        if self._engine is None:
            with self._lock:
                key = (owner, steer_key)
                delivered = ids & self._claimed.get(key, set())
                self._claimed.get(key, set()).difference_update(delivered)
                if not self._claimed.get(key):
                    self._claimed.pop(key, None)
                waiting = [m for m in self._local.get(key, []) if m[0] not in delivered]
                if waiting:
                    self._local[key] = waiting
                else:
                    self._local.pop(key, None)
                self._delivered.setdefault(key, {}).update((mid, None) for mid, _ in messages if mid in delivered)
            return
        with Session(self._engine) as session:
            self._lock_owner(session, owner)
            for row in session.scalars(select(AgentSteerMessageModel).where(
                AgentSteerMessageModel.owner == owner, AgentSteerMessageModel.steer_key == steer_key,
                AgentSteerMessageModel.id.in_(ids), AgentSteerMessageModel.claimed_at.is_not(None),
                AgentSteerMessageModel.taken_at.is_(None)).with_for_update()):
                row.taken_at = datetime.now(UTC)
                row.claimed_at = None
            session.commit()

    def return_unread(self, owner: str, steer_key: str, messages: list[tuple[str, str]]) -> None:
        """Restore text taken for a cancelled step that never called the model.

        Args:
            owner: Mailbox owner.
            steer_key: Cancelled turn's key.
            messages: Messages read for the model step that did not run.
        """
        if self._engine is None:
            with self._lock:
                key = (owner, steer_key)
                self._claimed.get(key, set()).difference_update(mid for mid, _ in messages)
                if not self._claimed.get(key):
                    self._claimed.pop(key, None)
            return
        with Session(self._engine) as session:
            self._lock_owner(session, owner)
            for row in session.scalars(select(AgentSteerMessageModel).where(
                AgentSteerMessageModel.owner == owner, AgentSteerMessageModel.steer_key == steer_key,
                AgentSteerMessageModel.id.in_([mid for mid, _ in messages]),
                AgentSteerMessageModel.taken_at.is_(None)).with_for_update()):
                row.claimed_at = None
            session.commit()

    def withdraw(self, owner: str, steer_key: str, ids: list[str] | None = None) -> tuple[list[str], list[str]]:
        """Delete unread messages and report messages already read by the agent.

        Args:
            owner: Username the turn belongs to.
            steer_key: The turn's steer key.
            ids: Requested ids, or all messages when omitted.

        Returns:
            Newly withdrawn ids and delivered ids. Claimed or missing ids are unknown.
        """
        if self._engine is None:
            with self._lock:
                self._purge_local(datetime.now(UTC))
                key = (owner, steer_key)
                self._expire_local_key(key, datetime.now(UTC))
                waiting = self._local.get(key, [])
                wanted = set(ids) if ids is not None else None
                claimed = self._claimed.get(key, set())
                withdrawn = [mid for mid, _ in waiting if mid not in claimed and (wanted is None or mid in wanted)]
                kept = [m for m in waiting if m[0] not in withdrawn]
                if kept:
                    self._local[key] = kept
                else:
                    self._local.pop(key, None)
                delivered = self._delivered.get(key, {})
                if not kept and not delivered and key not in self._limits:
                    self._local_updated.pop(key, None)
                return withdrawn, [mid for mid in delivered if wanted is None or mid in wanted]
        conditions = [AgentSteerMessageModel.owner == owner, AgentSteerMessageModel.steer_key == steer_key]
        if ids is not None:
            conditions.append(AgentSteerMessageModel.id.in_(ids))
        with Session(self._engine) as session:
            self._lock_owner(session, owner)
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
                elif row.claimed_at is None:
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


async def open_steer_inbox(
    owner: str,
    steer_key: str,
    input_field: str,
    emit: Callable[[dict[str, Any]], None],
    store: SteerStore,
    max_chars: int | None = None,
) -> SteerInbox | None:
    """Register a turn's steer bound and return its inbox.

    The bound is written before the caller announces ``steer_ready``, so a
    steer posted right after it meets the turn's limit. The write runs off
    the event loop. A failed write leaves the turn unsteerable instead of
    failing it: the caller sends no ``steer_ready`` and the client queues.

    Args:
        owner: The signed-in caller who owns the turn.
        steer_key: The turn's steer key from the client.
        input_field: The step input that carries a delivered steer.
        emit: Sends one stream event.
        store: The steer store.
        max_chars: The turn's per-message bound, or ``None`` for the default.

    Returns:
        The inbox, or ``None`` when the turn could not be registered.
    """
    inbox = SteerInbox(owner, steer_key, input_field, emit, store, max_chars)
    try:
        await asyncio.to_thread(store.open_turn, owner, steer_key, max_chars or STEER_TEXT_MAX_CHARS)
    except Exception:
        logger.exception("Failed to open a steerable turn; the turn runs without steering")
        return None
    return inbox


def claim_delivery(inbox: SteerInbox, taken: list[tuple[str, str]]) -> bool:
    """Mark taken steers delivered, or give them back when that fails.

    A steer the store cannot mark delivered must not reach the model: a later
    withdraw would report it unread and the client would send it again.

    Args:
        inbox: The turn's inbox.
        taken: The ``(id, text)`` pairs this step took.

    Returns:
        True when the step may use the steers.
    """
    try:
        inbox.store.mark_delivered(inbox.owner, inbox.steer_key, taken)
    except Exception:
        logger.exception("Failed to mark steer messages delivered; returning them unread")
        try:
            inbox.store.return_unread(inbox.owner, inbox.steer_key, taken)
        except Exception:
            logger.exception("Failed to return undelivered steer messages")
        return False
    return True


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
    if not claim_delivery(inbox, taken):
        return
    text = "\n\n".join(message for _, message in taken)
    kwargs[inbox.input_field] = text
    inbox.unrecorded = text
    inbox.emit({"event": STEER_APPLIED_EVENT, "data": {"ids": [message_id for message_id, _ in taken], "text": text}})
