"""Check registered input bounds, cancellation, and bounded mailbox expiry."""

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from ...api.routers import docs_agent, generalist_agent
from ...api.tests.conftest import bypass_auth
from ...config import app_env
from ...storage.models import AgentSteerMessageModel, AgentSteerTurnModel, Base
from ..agents.steering import SteerInbox, SteerLimitError, SteerStore
from .test_pi_react import _simulated_pi


@pytest.fixture(params=["local", "sqlite", "postgres"])
def claim_store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[SteerStore]:
    """Isolate claims on each supported store and optional PostgreSQL fixture.

    Args:
        request: Selected storage backend.
        tmp_path: Isolated SQLite database directory.

    Yields:
        Mailbox with no shared application data.
    """
    store = SteerStore()
    if request.param == "local":
        yield store
        return
    schema = None
    if request.param == "postgres":
        url = app_env("APP_BUDGET_TEST_DB_URL")
        if not url or not url.startswith("postgresql"):
            pytest.skip("Set APP_BUDGET_TEST_DB_URL to a disposable PostgreSQL database.")
        engine = create_engine(url)
        schema = f"steer_claim_{uuid4().hex}"
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        scoped = engine.execution_options(schema_translate_map={None: schema})
    else:
        engine = create_engine(f"sqlite:///{tmp_path / 'steers.db'}")
        scoped = engine
    try:
        Base.metadata.create_all(scoped, tables=[AgentSteerMessageModel.__table__, AgentSteerTurnModel.__table__])
        store.bind_engine(scoped)
        yield store
    finally:
        if schema:
            with engine.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


@pytest.mark.parametrize("owner_limit", [False, True])
@pytest.mark.parametrize("characters", [False, True])
def test_claimed_messages_keep_admission_reserved(claim_store: SteerStore, owner_limit: bool, characters: bool) -> None:
    """Keep claimed input inside both pending quotas until delivery.

    Args:
        claim_store: Isolated mailbox store.
        owner_limit: Fill every key allowed to one owner.
        characters: Fill the character allowance instead of message count.
    """
    store = claim_store
    for key in range(10 if owner_limit else 1):
        for _ in range(2 if characters else 10):
            store.post("alice", str(key), "x" * (10000 if characters else 1))
    claimed = store.take("alice", "0")
    def post(index: int) -> None:
        """Attempt admission while the worker holds its claim.

        Args:
            index: Unique new key for owner-wide admission.
        """
        with pytest.raises(SteerLimitError):
            store.post("alice", f"new-{index}" if owner_limit else "0", "y")

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(post, range(8)))
    store.return_unread("alice", "0", claimed)
    assert store.take("alice", "0") == claimed


def test_claim_window_is_unknown_until_delivery(claim_store: SteerStore) -> None:
    """Do not report a cancellable claim as delivered.

    Args:
        claim_store: Isolated mailbox store.
    """
    store = claim_store
    mid = store.post("alice", "turn", "A")
    claimed = store.take("alice", "turn")
    assert store.withdraw("alice", "turn", [mid]) == ([], [])
    store.return_unread("alice", "turn", claimed)
    assert store.withdraw("alice", "turn", [mid]) == ([mid], [])
    mid = store.post("alice", "turn", "B")
    claimed = store.take("alice", "turn")
    store.mark_delivered("alice", "turn", claimed)
    store.return_unread("alice", "turn", claimed)
    assert store.withdraw("alice", "turn", [mid]) == ([], [mid])
    assert store.take("alice", "turn") == []


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
        assert store.withdraw("alice", "turn", [mid]) == ([], [])
        stop.set()
        return result

    from_module = docs_agent
    calls = []
    monkeypatch.setattr(store, "take", blocked)
    monkeypatch.setattr(from_module, "_record_spend", lambda lm: None)
    monkeypatch.setattr(from_module, "run_pi_turn", _simulated_pi)
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
