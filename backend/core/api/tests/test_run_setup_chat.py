"""Tests for the run-to-setup-chat binding.

A submit binds the new run to the setup chat that the request names, but only
when the submitter owns that chat. ``GET /optimizations/{id}/chat`` then serves
the full chat up to the run to the chat's user and admins. Any other member of
the run, also a new owner after a transfer, or a user with no access, gets no
chat. The stale-chat purge keeps a bound chat.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ...storage.models import (
    AgentConversationModel,
    AgentMessageModel,
    Base,
    JobModel,
    OptimizationShareGrantModel,
)
from ...storage.remote import RemoteDBJobStore
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..routers.agent_history import purge_stale_conversations
from ..routers.agent_settings import MASK
from ..routers.generalist_agent import _persist_user_turn
from ..routers.optimizations import create_optimizations_router
from ..routers.share import create_share_router
from ..routers.submissions import _bind_setup_chat

_SECRET = "sk-ant-api03-" + "a" * 40


class _MemStore(RemoteDBJobStore):
    """In-memory SQLite job store (skips the pgvector bootstrap)."""

    def __init__(self) -> None:
        """Build an in-memory SQLite engine and create the ORM tables."""
        self._engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self._engine)
        self._session_factory = sessionmaker(bind=self._engine)


def _store() -> _MemStore:
    """Build a store with alice's run ``opt-1``, her chat ``c-alice`` and bob's chat ``c-bob``.

    ``c-alice`` holds an earlier setup, the turn that started the run, the
    agent's reply saved after the run, and one later turn.

    Returns:
        The seeded store. Bob holds a viewer grant on ``opt-1`` and eve an
        editor grant.
    """
    store = _MemStore()
    now = datetime.now(UTC)
    with Session(store.engine) as session:
        session.add(
            JobModel(
                optimization_id="opt-1",
                status="success",
                created_at=now,
                latest_metrics={},
                payload_overview={"optimization_type": "blackbox", "username": "alice"},
                payload={"username": "alice"},
                username="alice",
            )
        )
        session.add(AgentConversationModel(id="c-alice", username="alice", title="Mail alice@example.test"))
        session.add(AgentConversationModel(id="c-bob", username="bob", title="bob chat"))
        session.flush()
        turns = [
            ("user", -600, "earlier setup", None, None),
            (
                "assistant",
                -590,
                f"Key {_SECRET} for alice@example.test",
                {"stats": {"cost_usd": 0.5}, "thinking": [{"summary": "private"}]},
                [
                    {
                        "id": "t1",
                        "tool": "get_wallet_for_agent",
                        "status": "done",
                        "payload": {"arguments": {}, "result": {"balance_cents": 4200}},
                    }
                ],
            ),
            ("user", -60, "start the run", None, None),
            ("assistant", 2, "run started", None, None),
            ("user", 300, "later private turn", None, None),
            ("assistant", 310, "later reply", None, None),
        ]
        for role, offset, content, meta, tools in turns:
            session.add(
                AgentMessageModel(
                    conversation_id="c-alice",
                    role=role,
                    created_at=now + timedelta(seconds=offset),
                    content=content,
                    router_metadata=meta,
                    tool_calls=tools,
                )
            )
        for grantee, role in (("bob", "viewer"), ("eve", "editor")):
            session.add(
                OptimizationShareGrantModel(
                    optimization_id="opt-1",
                    grantee_username=grantee,
                    role=role,
                    created_by="alice",
                    created_at=now,
                )
            )
        session.commit()
    return store


def _bound(store: _MemStore) -> str | None:
    """Return the chat id bound to ``opt-1``.

    Args:
        store: The store to read.

    Returns:
        The ``jobs.conversation_id`` value of ``opt-1``.
    """
    with Session(store.engine) as session:
        return session.get(JobModel, "opt-1").conversation_id


def _client(store: _MemStore, username: str, role: str = "user") -> TestClient:
    """Mount the optimizations and share routers over ``store`` as ``username``.

    Args:
        store: The store the router reads.
        username: The caller.
        role: The caller's role.

    Returns:
        A test client that maps ``DomainError`` to its status code.
    """
    app = FastAPI()
    app.include_router(create_optimizations_router(job_store=store, get_worker_ref=lambda: None))
    app.include_router(create_share_router(job_store=store))
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser(
        username=username, role=role, groups=()
    )

    @app.exception_handler(DomainError)
    async def _domain_error_handler(_request, exc: DomainError) -> JSONResponse:
        """Return the app-level error envelope so tests can check ``code``."""
        return JSONResponse(status_code=exc.status_code, content={"code": exc.code})

    return TestClient(app)


def test_bind_keeps_a_chat_the_submitter_owns() -> None:
    """The run keeps the setup chat when the submitter owns it."""
    store = _store()
    _bind_setup_chat(store, "opt-1", "alice", "c-alice")
    assert _bound(store) == "c-alice"


def test_bind_ignores_another_users_chat_and_unknown_ids() -> None:
    """A chat of another user, or an unknown id, leaves the run unbound."""
    store = _store()
    _bind_setup_chat(store, "opt-1", "alice", "c-bob")
    _bind_setup_chat(store, "opt-1", "alice", "ghost")
    assert _bound(store) is None


@pytest.mark.parametrize(("username", "role", "key"), [("alice", "user", _SECRET), ("root", "admin", MASK)])
def test_owner_and_admin_read_the_full_chat_up_to_the_run(username: str, role: str, key: str) -> None:
    """The owner and an admin get the chat up to the run's final reply; only the owner sees keys."""
    store = _store()
    _bind_setup_chat(store, "opt-1", "alice", "c-alice")
    resp = _client(store, username, role).get("/optimizations/opt-1/chat")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == "c-alice"
    assert body["title"] == "Mail alice@example.test"
    assert [m["content"] for m in body["messages"]] == [
        "earlier setup",
        f"Key {key} for alice@example.test",
        "start the run",
        "run started",
    ]
    assert (_SECRET in resp.text) == (username == "alice")
    earlier = body["messages"][1]
    assert earlier["stats"] == {"cost_usd": 0.5}
    assert earlier["thinking"] == [{"summary": "private"}]
    assert earlier["tool_calls"][0]["payload"]["result"] == {"balance_cents": 4200}
    assert body["updated_at"] == body["messages"][-1]["created_at"]


def _contents(store: _MemStore) -> list[str]:
    """Return the turn texts that an admin reads from ``opt-1``.

    Args:
        store: The store the router reads.

    Returns:
        The ``content`` of each turn, in order.
    """
    return [m["content"] for m in _client(store, "root", "admin").get("/optimizations/opt-1/chat").json()["messages"]]


def test_a_steer_before_the_final_reply_keeps_the_reply() -> None:
    """A steer saved between the run start and the final reply does not cut the reply off."""
    store = _store()
    _bind_setup_chat(store, "opt-1", "alice", "c-alice")
    with Session(store.engine) as session:
        started = session.get(JobModel, "opt-1").created_at
        session.add(
            AgentMessageModel(
                conversation_id="c-alice",
                role="user",
                created_at=started + timedelta(seconds=1),
                content="steer",
                router_metadata={"steer": True},
            )
        )
        session.commit()
    assert _contents(store)[-3:] == ["start the run", "steer", "run started"]


def test_a_saved_steer_stays_in_the_turn_that_started_the_run() -> None:
    """A steer saved by the chat stream stays in the setup; a new user turn does not."""
    store = _store()
    _bind_setup_chat(store, "opt-1", "alice", "c-alice")
    with Session(store.engine) as session:
        session.query(AgentMessageModel).filter(
            AgentMessageModel.created_at > session.get(JobModel, "opt-1").created_at
        ).delete()
        session.commit()
    _persist_user_turn(store, "c-alice", "steer", steer=True)
    _persist_user_turn(store, "c-alice", "new turn")
    assert _contents(store)[-2:] == ["start the run", "steer"]


def test_a_lost_reply_does_not_show_the_next_turn() -> None:
    """When the start turn saved no reply, the owner's next turn and its reply stay hidden."""
    store = _store()
    _bind_setup_chat(store, "opt-1", "alice", "c-alice")
    with Session(store.engine) as session:
        session.query(AgentMessageModel).filter(AgentMessageModel.content == "run started").delete()
        session.commit()
    assert _contents(store)[-1] == "start the run"


def test_a_run_started_after_the_reply_ends_at_the_next_user_turn() -> None:
    """A run started after the agent replied keeps that reply and hides the next turn."""
    store = _store()
    _bind_setup_chat(store, "opt-1", "alice", "c-alice")
    with Session(store.engine) as session:
        job = session.get(JobModel, "opt-1")
        job.created_at = job.created_at + timedelta(seconds=5)
        session.commit()
    assert _contents(store)[-2:] == ["start the run", "run started"]


def test_chat_shows_no_later_pin_or_archive() -> None:
    """The run's chat shows no pin or archive that the owner set after the run."""
    store = _store()
    _bind_setup_chat(store, "opt-1", "alice", "c-alice")
    with Session(store.engine) as session:
        row = session.get(AgentConversationModel, "c-alice")
        row.pinned = True
        row.archived_at = datetime.now(UTC) + timedelta(days=1)
        session.commit()
    body = _client(store, "root", "admin").get("/optimizations/opt-1/chat").json()
    assert body["pinned"] is False
    assert body["archived_at"] is None


@pytest.mark.parametrize("username", ["bob", "eve"])
def test_viewer_and_editor_get_no_chat(username: str) -> None:
    """A viewer (bob) or an editor (eve) of the run gets the chat-not-found error."""
    store = _store()
    _bind_setup_chat(store, "opt-1", "alice", "c-alice")
    resp = _client(store, username).get("/optimizations/opt-1/chat")
    assert resp.status_code == 404
    assert resp.json()["code"] == "agent.conversation.not_found"
    assert _SECRET not in resp.text


def test_a_transfer_keeps_the_chat_with_its_user() -> None:
    """After alice transfers the run to eve, eve gets no chat and alice, now an editor, keeps hers."""
    store = _store()
    _bind_setup_chat(store, "opt-1", "alice", "c-alice")
    moved = _client(store, "alice").post("/optimizations/opt-1/sharing/transfer", json={"username": "eve"})
    assert moved.status_code == 200, moved.text
    assert moved.json()["owner"] == "eve"
    new_owner = _client(store, "eve").get("/optimizations/opt-1/chat")
    assert new_owner.status_code == 404
    assert new_owner.json()["code"] == "agent.conversation.not_found"
    assert _SECRET not in new_owner.text
    old_owner = _client(store, "alice").get("/optimizations/opt-1/chat")
    assert old_owner.status_code == 200, old_owner.text
    assert old_owner.json()["id"] == "c-alice"
    assert _SECRET in old_owner.text


def test_public_visitor_gets_no_chat() -> None:
    """A user with no access to the run, such as an Explore visitor, gets no chat."""
    store = _store()
    _bind_setup_chat(store, "opt-1", "alice", "c-alice")
    resp = _client(store, "carol").get("/optimizations/opt-1/chat")
    assert resp.status_code == 404
    assert resp.json()["code"] == "optimization.not_found"


def test_unbound_run_gets_404() -> None:
    """A run with no setup chat returns the chat-not-found error."""
    resp = _client(_store(), "alice").get("/optimizations/opt-1/chat")
    assert resp.status_code == 404
    assert resp.json()["code"] == "agent.conversation.not_found"


def test_purge_keeps_a_bound_setup_chat() -> None:
    """The stale purge deletes an old unbound chat and keeps an old bound one."""
    store = _store()
    _bind_setup_chat(store, "opt-1", "alice", "c-alice")
    old = datetime.now(UTC) - timedelta(days=400)
    with Session(store.engine) as session:
        for row in session.query(AgentConversationModel):
            row.updated_at = old
        session.commit()
    assert purge_stale_conversations(store.engine, threshold_days=30) == 1
    with Session(store.engine) as session:
        assert {row.id for row in session.query(AgentConversationModel)} == {"c-alice"}
