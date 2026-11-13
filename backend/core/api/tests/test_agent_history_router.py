"""Tests for the agent-history conversation router's bulk-delete route.

Mounts the conversation router on an in-memory SQLite store (the sibling
routers' pattern: a ``RemoteDBJobStore`` subclass that skips the pgvector
bootstrap so ``Base.metadata.create_all`` stands up the conversation tables).
Covers the per-id outcomes of ``POST /agent/conversations/bulk-delete``: owned
rows delete, duplicates collapse, unknown and other-users' ids are skipped, and
an empty request is a no-op. Also covers ``POST /agent/conversations/{id}/fork``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ...storage.models import AgentConversationModel, AgentMessageModel, Base
from ...storage.remote import RemoteDBJobStore
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..routers.agent_history import create_agent_history_router

_ALICE = AuthenticatedUser(username="alice", role="user", groups=())


class _MemStore(RemoteDBJobStore):
    """In-memory SQLite job store for agent-history tests (no pgvector)."""

    def __init__(self) -> None:
        """Build an in-memory SQLite engine and create the ORM tables."""
        self._engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self._engine)
        self._session_factory = sessionmaker(bind=self._engine)


def _client(user: AuthenticatedUser) -> tuple[TestClient, _MemStore]:
    """Mount the agent-history router on a fresh store, authed as ``user``.

    Args:
        user: Identity the auth dependency resolves to for every request.

    Returns:
        A ``(client, store)`` pair sharing one in-memory store.
    """
    store = _MemStore()
    app = FastAPI()
    app.include_router(create_agent_history_router(job_store=store))
    app.dependency_overrides[get_authenticated_user] = lambda: user

    @app.exception_handler(DomainError)
    async def _domain_error_handler(_request, exc: DomainError) -> JSONResponse:
        """Mirror the app-level envelope so tests can assert on ``code``."""
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "code": exc.code, "params": exc.params},
        )

    return TestClient(app), store


def _seed(store: _MemStore, *conversations: tuple[str, str]) -> None:
    """Insert ``(id, username)`` conversation rows directly into the store.

    Args:
        store: The in-memory store to seed.
        conversations: ``(id, username)`` pairs to insert.
    """
    with store._session_factory() as session:
        session.add_all(
            AgentConversationModel(id=cid, username=username, title=cid)
            for cid, username in conversations
        )
        session.commit()


def _ids(store: _MemStore) -> set[str]:
    """Return the set of conversation ids currently in the store.

    Args:
        store: The store to read.

    Returns:
        Every ``AgentConversationModel.id`` present.
    """
    with store._session_factory() as session:
        return {row.id for row in session.query(AgentConversationModel)}


def test_bulk_delete_removes_owned_conversations() -> None:
    """Bulk-delete clears every owned id and leaves none of them behind."""
    client, store = _client(_ALICE)
    _seed(store, ("c-1", "alice"), ("c-2", "alice"))

    resp = client.post("/agent/conversations/bulk-delete", json={"ids": ["c-1", "c-2"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert sorted(body["deleted"]) == ["c-1", "c-2"]
    assert body["skipped"] == []
    assert _ids(store) == set()


def test_bulk_delete_dedupes_and_skips_unknown() -> None:
    """Duplicate ids collapse to one delete; an unknown id is skipped, not fatal."""
    client, store = _client(_ALICE)
    _seed(store, ("c-1", "alice"))

    resp = client.post(
        "/agent/conversations/bulk-delete",
        json={"ids": ["c-1", "c-1", "ghost"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted"] == ["c-1"]
    assert body["skipped"] == [{"id": "ghost", "reason": "not_found"}]


def test_bulk_delete_skips_other_users_conversations() -> None:
    """A caller cannot bulk-delete another user's conversation — it is skipped."""
    client, store = _client(_ALICE)
    _seed(store, ("bob-1", "bob"))

    resp = client.post("/agent/conversations/bulk-delete", json={"ids": ["bob-1"]})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": [], "skipped": [{"id": "bob-1", "reason": "not_found"}]}
    assert _ids(store) == {"bob-1"}


def test_bulk_delete_empty_is_noop() -> None:
    """An empty id list deletes nothing and returns empty result lists."""
    client, _ = _client(_ALICE)
    resp = client.post("/agent/conversations/bulk-delete", json={"ids": []})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": [], "skipped": []}


def _seed_turns(store: _MemStore, conversation_id: str, turns: list[tuple[str, str]]) -> None:
    """Insert ordered ``(role, content)`` turns into one conversation.

    Args:
        store: The in-memory store to seed.
        conversation_id: The conversation that gets the turns.
        turns: ``(role, content)`` pairs, oldest first.
    """
    start = datetime(2026, 1, 1, tzinfo=UTC)
    with store._session_factory() as session:
        session.add_all(
            AgentMessageModel(
                conversation_id=conversation_id,
                role=role,
                content=content,
                router_metadata={
                    "stats": {"cents": index},
                    "side_cents": 2,
                    "served_model": "m",
                    "compaction": {"summary": "s", "cents": 3},
                },
                wizard_state_before={"step": index},
                created_at=start + timedelta(seconds=index),
            )
            for index, (role, content) in enumerate(turns)
        )
        session.commit()


def test_fork_saves_a_copy_that_loads_again() -> None:
    """A fork saves the turns before the chosen user turn in a new owned chat."""
    client, store = _client(_ALICE)
    _seed(store, ("c-1", "alice"))
    _seed_turns(
        store,
        "c-1",
        [("user", "first"), ("assistant", "one"), ("user", "second"), ("assistant", "two")],
    )

    resp = client.post("/agent/conversations/c-1/fork", json={"user_turns": 1})
    assert resp.status_code == 200, resp.text
    forked = resp.json()
    assert forked["id"] != "c-1"
    assert forked["title"] == "c-1"
    assert forked["pinned"] is False
    assert forked["preview"] == "first"

    detail = client.get(f"/agent/conversations/{forked['id']}")
    assert detail.status_code == 200, detail.text
    messages = detail.json()["messages"]
    assert [(m["role"], m["content"]) for m in messages] == [("user", "first"), ("assistant", "one")]
    with store._session_factory() as session:
        copies = session.query(AgentMessageModel).filter_by(conversation_id=forked["id"]).all()
        assert all(m.wizard_state_before is None for m in copies)
        # The source chat paid for these turns; the fork keeps the summary but no cost.
        assert [m.router_metadata for m in copies] == [
            {"served_model": "m", "compaction": {"summary": "s", "cents": None}}
        ] * 2
    assert len(client.get("/agent/conversations/c-1").json()["messages"]) == 4


def test_fork_past_the_last_user_turn_copies_every_turn() -> None:
    """A user-turn count past the end copies the whole chat."""
    client, store = _client(_ALICE)
    _seed(store, ("c-1", "alice"))
    _seed_turns(store, "c-1", [("user", "only"), ("assistant", "reply")])

    resp = client.post("/agent/conversations/c-1/fork", json={"user_turns": 5})
    assert resp.status_code == 200, resp.text
    detail = client.get(f"/agent/conversations/{resp.json()['id']}").json()
    assert [m["content"] for m in detail["messages"]] == ["only", "reply"]


def test_fork_checks_the_owner() -> None:
    """A fork of another user's chat is 403, and of an unknown chat is 404."""
    client, store = _client(_ALICE)
    _seed(store, ("c-bob", "bob"))

    assert client.post("/agent/conversations/c-bob/fork", json={"user_turns": 0}).status_code == 403
    assert client.post("/agent/conversations/nope/fork", json={"user_turns": 0}).status_code == 404
    assert _ids(store) == {"c-bob"}
