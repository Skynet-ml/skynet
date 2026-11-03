"""Check registered input bounds, cancellation, and bounded mailbox expiry."""

import threading
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ...api.routers import docs_agent, generalist_agent
from ...api.tests.conftest import bypass_auth
from ...storage.models import AgentSteerMessageModel, Base
from ..agents.steering import SteerInbox, SteerStore


@pytest.mark.parametrize("sql", [False, True])
def test_registered_limit_rejects_head_before_it_blocks_later_steers(sql: bool) -> None:
    """Reject a non-fitting head at admission on either storage path."""
    store = SteerStore()
    if sql:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        store.bind_engine(engine)
    getattr(store, "open_turn", lambda *args: None)("alice", "turn", 2000)
    with pytest.raises(ValueError):
        store.post("alice", "turn", "x" * 2001)
    mid = store.post("alice", "turn", "fits")
    assert store.take("alice", "turn", max_chars=2000) == [(mid, "fits")]


def test_expired_rows_never_join_delivery_after_bounded_cleanup() -> None:
    """Exclude expired rows left behind by a bounded purge."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([AgentSteerMessageModel(id=str(i), owner="alice", steer_key="turn", text="old",
                                              created_at=datetime.now(UTC) - timedelta(days=1)) for i in range(501)])
        session.commit()
    store = SteerStore()
    store.bind_engine(engine)
    mid = store.post("alice", "turn", "new")
    assert store.take("alice", "turn") == [(mid, "new")]


def test_local_expiry_work_is_bounded() -> None:
    """Avoid deleting every expired fallback key during one withdrawal."""
    store = SteerStore()
    old = datetime.now(UTC) - timedelta(days=1)
    for i in range(501):
        key = (str(i), "turn")
        store._local[key] = [(str(i), "old")]
        store._local_updated[key] = old
    store.withdraw("missing", "turn")
    assert len(store._local_updated) >= 401


def test_stop_during_docs_mailbox_read_restores_unread_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not start a paid model call after a blocked read observes Stop."""
    store = SteerStore()
    mid = store.post("alice", "turn", "keep")
    stop = threading.Event()
    take = store.take

    def blocked(*args, **kwargs):
        """Finish the read after cancellation arrives."""
        result = take(*args, **kwargs)
        stop.set()
        return result

    from_module = docs_agent
    calls = []
    monkeypatch.setattr(store, "take", blocked)
    monkeypatch.setattr(from_module, "_record_spend", lambda lm: None)
    inbox = SteerInbox("alice", "turn", "question", lambda ev: None, store, 8000)
    from_module._run_answer(lambda **kw: calls.append(kw) or [{"text": "answer"}], [], lambda q: {},
                            lambda n, d: None, stop, inbox)
    assert calls == []
    assert store.withdraw("alice", "turn", [mid]) == ([mid], [])


@pytest.mark.parametrize("text", [" ", "x" * 2001], ids=["blank", "over-turn-limit"])
def test_steer_route_rejects_unusable_input(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    """Return 422 before storing text the active turn cannot use."""
    store = SteerStore()
    getattr(store, "open_turn", lambda *args: None)("alice", "turn", 2000)
    monkeypatch.setattr(generalist_agent, "get_steer_store", lambda: store)
    app = FastAPI()
    app.include_router(generalist_agent.create_generalist_agent_router())
    bypass_auth(app)
    response = TestClient(app).post("/optimizations/agent-steer", json={"steer_key": "turn", "text": text})
    assert response.status_code == 422


def test_withdraw_response_keeps_old_constructor_compatible() -> None:
    """Keep delivered optional for callers that only supply withdrawn ids."""
    assert generalist_agent.SteerWithdrawResponse(ids=[]).delivered == []
