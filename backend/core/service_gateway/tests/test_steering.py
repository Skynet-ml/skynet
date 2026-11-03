"""Tests for delivering mid-turn user messages in :mod:`...agents.steering`.

Pins that a message posted while a turn runs reaches the model at the loop's
next step as the newest user message, is recorded in the loop history where it
was read, and goes either to the loop or back to the client, never both.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import Mock

import dspy
import pytest
from sqlalchemy import create_engine

from ...storage.models import AgentSteerMessageModel, AgentSteerTurnModel
from ..agents import steering as steering_mod
from ..agents.acting_react import ActingReActV2
from ..agents.conversation_react import ConversationReAct
from ..agents.steering import STEER_APPLIED_EVENT, SteerInbox, SteerStore, attach_steering
from .test_conversation_react import _ScriptedLM

_OWNER = "alice"
_KEY = "turn-1"


@pytest.mark.parametrize("database", [False, True])
def test_pending_steer_count_and_character_caps(database: bool) -> None:
    """Limit pending input by count and characters, releasing room after take.

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
    assert len(store.take(_OWNER, _KEY)) == 10
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
    store.take(_OWNER, _KEY)
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
    store.take(_OWNER, _KEY)
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
    store.take(_OWNER, _KEY)
    unread = store.post(_OWNER, _KEY, "return")
    store.post("mallory", _KEY, "private")
    assert store.withdraw(_OWNER, _KEY, [unread, delivered, "missing"]) == ([unread], [delivered])
    assert store.withdraw(_OWNER, _KEY, [unread, delivered]) == ([], [delivered])
    assert store.withdraw("mallory", "other", [delivered]) == ([], [])
    assert store.take(_OWNER, _KEY) == []
    assert store.withdraw(_OWNER, _KEY, []) == ([], [])


def _user_texts(messages: list[dict[str, Any]]) -> list[str]:
    """Return the text of every user message in one request.

    Args:
        messages: The rendered chat messages.

    Returns:
        Each user message's content, in order.
    """
    return [str(m["content"]) for m in messages if m["role"] == "user"]


def _steered_program(
    store: SteerStore, events: list[dict[str, Any]], *, posts: dict[str, str], conversation: bool
) -> dspy.Module:
    """Build a one-tool loop whose tool posts a steer message when called.

    Args:
        store: Where steer messages are kept.
        events: Receives every event the inbox emits.
        posts: Maps a tool argument to the steer text posted when it is used.
        conversation: Build a :class:`ConversationReAct` instead of an
            :class:`ActingReActV2`.

    Returns:
        The program, with steering attached.
    """

    def lookup(name: str) -> str:
        """Look a name up.

        Args:
            name: The thing to look up.

        Returns:
            A constant string.
        """
        if name in posts:
            store.post(_OWNER, _KEY, posts[name])
        return f"value of {name}"

    signature = dspy.Signature("user_message: str -> reply: str", "Be helpful.")
    loop_cls = ConversationReAct if conversation else ActingReActV2
    program = loop_cls(signature, tools=[lookup], max_iters=5)
    attach_steering(
        program,
        SteerInbox(owner=_OWNER, steer_key=_KEY, input_field="user_message", emit=events.append, store=store),
    )
    return program


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


def test_steer_reaches_the_next_step_as_the_newest_user_message() -> None:
    """A message posted during a tool call is read by the very next step."""
    store = SteerStore()
    events: list[dict[str, Any]] = []
    lm = _ScriptedLM([("lookup", {"name": "a"}), ("lookup", {"name": "b"}), ("submit", {"reply": "done"})])
    program = _steered_program(store, events, posts={"a": "use blue instead"}, conversation=False)

    with dspy.context(lm=lm, adapter=dspy.ChatAdapter(use_native_function_calling=True)):
        result = program(user_message="make it red")

    assert result.reply == "done"
    second_request = lm.requests[1]["messages"]
    assert second_request[-1]["role"] == "user"
    assert "use blue instead" in second_request[-1]["content"]
    assert [e["event"] for e in events] == [STEER_APPLIED_EVENT]
    assert events[0]["data"]["text"] == "use blue instead"
    assert len(events[0]["data"]["ids"]) == 1
    third_users = _user_texts(lm.requests[2]["messages"])
    assert sum("use blue instead" in text for text in third_users) == 1
    assert not any(m["role"] == "assistant" and not m.get("tool_calls") for m in lm.requests[2]["messages"])


def test_steered_calls_keep_extending_the_previous_prompt() -> None:
    """Steering keeps the append-only prompt shape the prompt cache relies on."""
    store = SteerStore()
    lm = _ScriptedLM([("lookup", {"name": "a"}), ("lookup", {"name": "b"}), ("submit", {"reply": "done"})])
    program = _steered_program(store, [], posts={"a": "use blue instead"}, conversation=True)

    with dspy.context(lm=lm):
        program(user_message="make it red")

    for earlier, later in zip(lm.requests, lm.requests[1:], strict=False):
        assert later["messages"][: len(earlier["messages"])] == earlier["messages"]


def test_steer_read_by_the_final_step_is_kept_in_history() -> None:
    """A steer the submitting step read is recorded on that step's event."""
    store = SteerStore()
    lm = _ScriptedLM([("lookup", {"name": "a"}), ("submit", {"reply": "done"})])
    program = _steered_program(store, [], posts={"a": "also add a title"}, conversation=True)

    with dspy.context(lm=lm):
        result = program(user_message="make it red")

    steered = [event for event in result.history.messages if event.get("user_message") == "also add a title"]
    assert len(steered) == 1
    assert "tool_calls" in steered[0]


def test_unread_steer_stays_for_the_client_to_withdraw() -> None:
    """A message posted after the last step is never applied and can be withdrawn."""
    store = SteerStore()
    events: list[dict[str, Any]] = []
    lm = _ScriptedLM([("submit", {"reply": "done"})])
    program = _steered_program(store, events, posts={}, conversation=False)
    store.post(_OWNER, _KEY, "too late")

    with dspy.context(lm=lm, adapter=dspy.ChatAdapter(use_native_function_calling=True)):
        program(user_message="make it red")

    assert events == []
    assert [text for _, text in store.take(_OWNER, _KEY)] == ["too late"]


def test_loop_without_an_inbox_is_unchanged() -> None:
    """Attaching no inbox leaves the loop exactly as it was."""
    signature = dspy.Signature("user_message: str -> reply: str", "Be helpful.")
    program = ConversationReAct(signature, tools=[], max_iters=2)
    attach_steering(program, None)
    lm = _ScriptedLM([("submit", {"reply": "done"})])

    with dspy.context(lm=lm):
        assert program(user_message="hi").reply == "done"
    assert not hasattr(program.react, "steer_inbox")
