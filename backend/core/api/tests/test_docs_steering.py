"""Exercise docs steering at existing metered model steps without paid calls."""

import threading
from pathlib import Path
from typing import Any

import fakeredis
import pytest

from ...service_gateway.agents.steering import SteerInbox, SteerStore
from ..auth import AuthenticatedUser
from ..routers import docs_agent
from .test_docs_agent_router import _client, _events, _ScriptedLM, _tool_output, docs_root, redis_double

__all__ = ["docs_root", "redis_double"]


@pytest.mark.parametrize("text", ["Focus on run budgets", "x" * 8000], ids=["normal", "at-limit"])
def test_docs_consumes_steer_at_next_metered_step(
    monkeypatch: pytest.MonkeyPatch, docs_root: Path, redis_double: fakeredis.FakeStrictRedis, text: str
) -> None:
    """Consume the caller's pending text on the second call and count both calls' spend."""
    store = SteerStore()
    monkeypatch.setattr(docs_agent, "get_steer_store", lambda: store, raising=False)
    other = store.post("other", "turn", "private")
    queued: list[str] = []
    lm = _ScriptedLM(_tool_output("run budgets"), {"text": "The budget limits the run."})
    original = lm.__class__.__call__

    def call(self: Any, **kwargs: Any) -> list[Any]:
        """Post during the first call so only the next existing step can consume it."""
        output = original(self, **kwargs)
        if len(self.calls) == 1:
            queued.append(store.post("alice", "turn", text))
        return output

    monkeypatch.setattr(_ScriptedLM, "__call__", call)
    user = AuthenticatedUser(username="alice", role="user", groups=())
    events = _events(_client(docs_root, lm, user=user).post(
        "/docs-agent/ask", json={"question": "balance", "steer_key": "turn"}
    ))
    assert ("steer_ready", {"steer_key": "turn"}) in events
    assert ("steer_applied", {"ids": queued, "text": text}) in events
    assert len(lm.calls) == 2
    assert text not in str(lm.calls[0]["messages"])
    assert any(text in m["content"] for m in lm.calls[1]["messages"])
    assert store.withdraw("alice", "turn", None) == ([], queued)
    assert store.withdraw("other", "turn", None) == ([other], [])
    key = next(k for k in redis_double.keys("skynet:platform-budget:docs_agent:*") if not k.endswith(":alerted"))
    assert float(redis_double.get(key)) == pytest.approx(0.02)


@pytest.mark.parametrize("user", [None, AuthenticatedUser(username="alice", role="user", groups=())])
def test_docs_empty_inbox_adds_no_model_step(
    monkeypatch: pytest.MonkeyPatch, docs_root: Path, redis_double: fakeredis.FakeStrictRedis, user: Any
) -> None:
    """Leave anonymous callers queue-only and avoid additional calls with an empty inbox."""
    store = SteerStore()
    monkeypatch.setattr(docs_agent, "get_steer_store", lambda: store, raising=False)
    private = store.post("alice", "turn", "private") if user is None else None
    lm = _ScriptedLM({"text": "Answer."})
    events = _events(_client(docs_root, lm, user=user).post(
        "/docs-agent/ask", json={"question": "balance", "steer_key": "turn"}
    ))
    assert any(n == "steer_ready" for n, _ in events) == (user is not None)
    assert not any(n == "steer_applied" for n, _ in events)
    assert len(lm.calls) == 1
    if private:
        assert store.withdraw("alice", "turn", None) == ([private], [])


def test_docs_oversize_steer_stays_withdrawable(
    monkeypatch: pytest.MonkeyPatch, docs_root: Path, redis_double: fakeredis.FakeStrictRedis
) -> None:
    """Keep a non-fitting message unread rather than truncating it at a model boundary."""
    store = SteerStore()
    monkeypatch.setattr(docs_agent, "get_steer_store", lambda: store, raising=False)
    pending = store.post("alice", "turn", "x" * 8001)
    user = AuthenticatedUser(username="alice", role="user", groups=())
    lm = _ScriptedLM({"text": "Answer."})
    events = _events(_client(docs_root, lm, user=user).post(
        "/docs-agent/ask", json={"question": "balance", "steer_key": "turn"}
    ))
    assert ("steer_ready", {"steer_key": "turn"}) in events
    assert not any(n == "steer_applied" for n, _ in events)
    assert store.withdraw("alice", "turn", None) == ([pending], [])


def test_docs_mailbox_error_keeps_existing_answer_step(
    monkeypatch: pytest.MonkeyPatch, docs_root: Path, redis_double: fakeredis.FakeStrictRedis
) -> None:
    """Do not fail a metered answer when its optional steer mailbox cannot be read."""
    store = SteerStore()

    def fail(*args: Any, **kwargs: Any) -> Any:
        """Simulate a mailbox read error."""
        raise RuntimeError("mailbox unavailable")

    monkeypatch.setattr(store, "take", fail)
    monkeypatch.setattr(docs_agent, "get_steer_store", lambda: store, raising=False)
    user = AuthenticatedUser(username="alice", role="user", groups=())
    lm = _ScriptedLM({"text": "Answer."})
    events = _events(_client(docs_root, lm, user=user).post(
        "/docs-agent/ask", json={"question": "balance", "steer_key": "turn"}
    ))
    assert ("steer_ready", {"steer_key": "turn"}) in events
    assert events[-1][0] == "done"
    assert len(lm.calls) == 1


def test_docs_stop_preserves_unread_input_without_an_extra_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop after the current metered step and leave later steering unread."""
    store = SteerStore()
    stop = threading.Event()
    pending: list[str] = []
    spent: list[Any] = []
    lm = _ScriptedLM(_tool_output("budget"))
    original = _ScriptedLM.__call__

    def call(self: Any, **kwargs: Any) -> list[Any]:
        """Stop while the first call runs, after a new message enters the mailbox."""
        output = original(self, **kwargs)
        pending.append(store.post("alice", "turn", "keep me"))
        stop.set()
        return output

    monkeypatch.setattr(_ScriptedLM, "__call__", call)
    monkeypatch.setattr(docs_agent, "_record_spend", spent.append)
    inbox = SteerInbox("alice", "turn", "question", lambda event: None, store, max_chars=8000)
    result = docs_agent._run_answer(lm, [], lambda q: {"results": []}, lambda n, d: None, stop, inbox)
    assert result == ""
    assert len(lm.calls) == 1
    assert spent == [lm]
    assert store.withdraw("alice", "turn", None) == (pending, [])


def test_docs_turn_runs_unsteerable_when_turn_registration_fails(
    monkeypatch: pytest.MonkeyPatch, docs_root: Path, redis_double: fakeredis.FakeStrictRedis
) -> None:
    """Answer normally and announce no steering when the turn bound cannot be stored."""
    store = SteerStore()

    def fail(*args: Any, **kwargs: Any) -> None:
        """Simulate a database error while the turn registers."""
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(store, "open_turn", fail)
    monkeypatch.setattr(docs_agent, "get_steer_store", lambda: store, raising=False)
    user = AuthenticatedUser(username="alice", role="user", groups=())
    lm = _ScriptedLM({"text": "Answer."})
    events = _events(_client(docs_root, lm, user=user).post(
        "/docs-agent/ask", json={"question": "balance", "steer_key": "turn"}
    ))
    assert not any(n == "steer_ready" for n, _ in events)
    assert events[-1][0] == "done"
    assert len(lm.calls) == 1


def test_docs_steer_stays_unread_when_delivery_cannot_be_recorded(
    monkeypatch: pytest.MonkeyPatch, docs_root: Path, redis_double: fakeredis.FakeStrictRedis
) -> None:
    """Keep a steer out of the model call and give it back when it cannot be marked delivered."""
    store = SteerStore()
    pending = store.post("alice", "turn", "Focus on run budgets")

    def fail(*args: Any, **kwargs: Any) -> None:
        """Simulate a database error while the steer is marked delivered."""
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(store, "mark_delivered", fail)
    monkeypatch.setattr(docs_agent, "get_steer_store", lambda: store, raising=False)
    user = AuthenticatedUser(username="alice", role="user", groups=())
    lm = _ScriptedLM({"text": "Answer."})
    events = _events(_client(docs_root, lm, user=user).post(
        "/docs-agent/ask", json={"question": "balance", "steer_key": "turn"}
    ))
    assert not any(n == "steer_applied" for n, _ in events)
    assert events[-1][0] == "done"
    assert "Focus on run budgets" not in str(lm.calls[0]["messages"])
    assert store.withdraw("alice", "turn", None) == ([pending], [])
