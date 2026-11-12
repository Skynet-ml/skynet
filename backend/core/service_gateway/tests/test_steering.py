"""Tests for the mid-turn user message store in :mod:`...agents.steering`.

Pins the store's caps, its hand-out and withdraw rules and the turn bound. The
Pi turn's steer delivery is in ``test_pi_react.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine

from ...storage.models import AgentSteerMessageModel, AgentSteerTurnModel
from ..agents import steering as steering_mod
from ..agents.steering import SteerStore

_OWNER = "alice"
_KEY = "turn-1"


@pytest.mark.parametrize("database", [False, True])
def test_pending_steer_count_and_character_caps(database: bool) -> None:
    """Limit pending input by count and characters, releasing room after delivery.

    Args:
        database: Exercise SQL storage instead of the local fallback.
    """
    store = SteerStore()
    if database:
        engine = create_engine("sqlite://")
        AgentSteerMessageModel.__table__.create(engine)
        AgentSteerTurnModel.__table__.create(engine)
        store.bind_engine(engine)
    for _ in range(10):
        store.post(_OWNER, _KEY, "one")
    with pytest.raises(ValueError, match="pending steer"):
        store.post(_OWNER, _KEY, "over count")
    taken = store.take(_OWNER, _KEY)
    assert len(taken) == 10
    store.mark_delivered(_OWNER, _KEY, taken)
    first = store.post(_OWNER, _KEY, "a" * 12000)
    store.post(_OWNER, _KEY, "b" * 8000)
    with pytest.raises(ValueError, match="pending steer"):
        store.post(_OWNER, _KEY, "c")
    store.withdraw(_OWNER, _KEY, [first])
    store.post(_OWNER, _KEY, "c")
    assert len(store.take(_OWNER, _KEY)) == 2


def test_local_withdraw_drops_empty_keys_and_keeps_delivery_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop empty waiting lists and preserve delivery order rather than UUID order.

    Args:
        monkeypatch: Supply ids whose lexical order differs from insertion order.
    """
    store = SteerStore()
    monkeypatch.setattr(steering_mod, "uuid4", Mock(side_effect=[Mock(hex="b"), Mock(hex="a"), Mock(hex="c")]))
    first = store.post(_OWNER, _KEY, "one")
    second = store.post(_OWNER, _KEY, "two")
    store.mark_delivered(_OWNER, _KEY, store.take(_OWNER, _KEY))
    unread = store.post(_OWNER, _KEY, "three")
    assert store.withdraw(_OWNER, _KEY) == ([unread], [first, second])
    assert (_OWNER, _KEY) not in store._local
    store.take(_OWNER, "missing")
    assert (_OWNER, "missing") not in store._delivered


def test_local_fallback_expires_old_waiting_and_delivery_records(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expire inactive local keys while retaining recent delivery tombstones.

    Args:
        monkeypatch: Advance the storage clock without waiting six hours.
    """
    clock = Mock(wraps=datetime)
    now = datetime.now(UTC)
    clock.now.return_value = now
    monkeypatch.setattr(steering_mod, "datetime", clock)
    store = SteerStore()
    delivered = store.post(_OWNER, _KEY, "read")
    store.mark_delivered(_OWNER, _KEY, store.take(_OWNER, _KEY))
    store.post(_OWNER, _KEY, "unread")
    assert store.withdraw(_OWNER, _KEY, [delivered]) == ([], [delivered])
    clock.now.return_value = now + timedelta(hours=7)
    store.post(_OWNER, "fresh", "new")
    assert (_OWNER, _KEY) not in store._local
    assert (_OWNER, _KEY) not in store._delivered


@pytest.mark.parametrize("database", [False, True])
def test_withdraw_distinguishes_delivered_and_is_idempotent(database: bool) -> None:
    """Return unread ids once and preserve delivered outcomes on retries.

    Args:
        database: Exercise SQL storage instead of the in-memory fallback.
    """
    store = SteerStore()
    if database:
        engine = create_engine("sqlite://")
        AgentSteerMessageModel.__table__.create(engine)
        AgentSteerTurnModel.__table__.create(engine)
        store.bind_engine(engine)
    delivered = store.post(_OWNER, _KEY, "read")
    store.mark_delivered(_OWNER, _KEY, store.take(_OWNER, _KEY))
    unread = store.post(_OWNER, _KEY, "return")
    store.post("mallory", _KEY, "private")
    assert store.withdraw(_OWNER, _KEY, [unread, delivered, "missing"]) == ([unread], [delivered])
    assert store.withdraw(_OWNER, _KEY, [unread, delivered]) == ([], [delivered])
    assert store.withdraw("mallory", "other", [delivered]) == ([], [])
    assert store.take(_OWNER, _KEY) == []
    assert store.withdraw(_OWNER, _KEY, []) == ([], [])


def test_store_hands_each_message_out_once() -> None:
    """A taken message is gone, and keys and owners do not mix."""
    store = SteerStore()
    first = store.post(_OWNER, _KEY, "one")
    second = store.post(_OWNER, _KEY, "two")
    store.post("mallory", _KEY, "not yours")

    assert store.take(_OWNER, _KEY) == [(first, "one"), (second, "two")]
    assert store.take(_OWNER, _KEY) == []
    assert store.take(_OWNER, "other-turn") == []


def test_store_takes_back_only_the_named_messages() -> None:
    """Withdrawing one steer leaves the others for the running turn."""
    store = SteerStore()
    first = store.post(_OWNER, _KEY, "one")
    second = store.post(_OWNER, _KEY, "two")

    assert store.take(_OWNER, _KEY, [second]) == [(second, "two")]
    assert store.take(_OWNER, _KEY, [second]) == []
    assert store.take(_OWNER, _KEY) == [(first, "one")]


async def test_open_steer_inbox_returns_none_when_registration_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A turn whose bound cannot be stored runs without steering instead of failing."""
    store = SteerStore()

    def fail(*args: Any, **kwargs: Any) -> None:
        """Simulate a database error while the turn registers."""
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(store, "open_turn", fail)
    inbox = await steering_mod.open_steer_inbox(_OWNER, _KEY, "user_message", lambda event: None, store, 100)
    assert inbox is None


async def test_open_steer_inbox_registers_the_turn_bound() -> None:
    """The bound is stored before the inbox is returned, so later posts meet it."""
    store = SteerStore()
    inbox = await steering_mod.open_steer_inbox(_OWNER, _KEY, "user_message", lambda event: None, store, 5)
    assert inbox is not None
    with pytest.raises(steering_mod.SteerInputError):
        store.post(_OWNER, _KEY, "x" * 6)
