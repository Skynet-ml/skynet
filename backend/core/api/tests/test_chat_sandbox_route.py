"""Tests for the file workspace wiring of an agent chat turn (SKY-781)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from ...storage.models import Base, ChatMediaModel
from ..auth import AuthenticatedUser, get_authenticated_user
from ..routers import generalist_agent as agent_mod

_USER = "alice@example.com"
# The turn hands this runner back to the charge, as it does the real one.
_RUNNER = object()


class _Store:
    """Job-store double exposing only the engine."""

    def __init__(self, engine: Engine) -> None:
        """Store the engine.

        Args:
            engine: The SQLite engine.
        """
        self.engine = engine


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Build a file-backed SQLite engine with the app tables.

    Args:
        tmp_path: Pytest per-test temporary directory.

    Yields:
        The engine.
    """
    db = create_engine(f"sqlite:///{tmp_path / 'chat.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(db)
    yield db
    db.dispose()


@pytest.fixture
def charges(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Give every chat file tools and record the sandbox charges.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        The recorded charge calls.
    """
    monkeypatch.setattr(agent_mod, "queue_conversation_embed", lambda *a, **k: None)
    monkeypatch.setattr(agent_mod, "enforce_llm_balance", lambda job_store, username: None)
    monkeypatch.setattr(agent_mod, "chat_command_tools", lambda engine, username, conversation_id: (_RUNNER, ["tool"]))
    calls: list[dict[str, Any]] = []

    def charge(engine: Engine, username: str, runner: object, *, settlement_key: str) -> int:
        """Record one charge.

        Args:
            engine: The wallet engine.
            username: The chat's owner.
            runner: The runner that the turn used.
            settlement_key: The charge's idempotency key.

        Returns:
            No cents.
        """
        calls.append({"username": username, "runner": runner, "key": settlement_key})
        return 0

    monkeypatch.setattr(agent_mod, "charge_chat_sandbox", charge)
    return calls


def _post(engine: Engine) -> str:
    """Send one turn of a new chat and return its conversation id.

    Args:
        engine: The SQLite engine behind the store.

    Returns:
        The ``conversation_meta`` event's conversation id.
    """
    app = FastAPI()
    app.include_router(agent_mod.create_generalist_agent_router(job_store=_Store(engine)))
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser(username=_USER, role="user", groups=())
    res = TestClient(app).post(
        "/optimizations/generalist-agent",
        json={"user_message": "read it", "chat_history": [], "wizard_state": {}, "trust_mode": "ask"},
    )
    assert res.status_code == 200
    meta = next(block for block in res.text.split("\n\n") if block.startswith("event: conversation_meta"))
    return json.loads(meta.split("data: ", 1)[1])["conversation_id"]


def test_a_turn_with_file_tools_gets_one_sandbox_charge(
    monkeypatch: pytest.MonkeyPatch, engine: Engine, charges: list[dict[str, Any]]
) -> None:
    """A finished turn passes the tools to the agent and charges the box once, keyed by the turn."""
    seen: list[Any] = []

    async def turn(**kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        """Record the file tools and give a one-reply turn.

        Args:
            **kwargs: The agent's arguments.

        Yields:
            The turn's events.
        """
        seen.append(kwargs.get("file_tools"))
        yield {"event": "done", "data": {"assistant_message": "ok", "model": "test-model"}}

    monkeypatch.setattr(agent_mod, "run_generalist_agent", turn)
    conversation_id = _post(engine)
    assert seen == [["tool"]]
    assert len(charges) == 1
    assert charges[0]["username"] == _USER
    assert charges[0]["runner"] is _RUNNER
    assert charges[0]["key"].startswith(f"chat-sandbox:{conversation_id}:")
    assert charges[0]["key"] != f"chat-sandbox:{conversation_id}:"


def test_a_turn_that_fails_early_still_gets_its_sandbox_charge(
    monkeypatch: pytest.MonkeyPatch, engine: Engine, charges: list[dict[str, Any]]
) -> None:
    """A turn that raises before ``done`` still pays for the box time it used."""

    async def turn(**_kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        """Fail after one chunk.

        Args:
            **_kwargs: The agent's arguments.

        Yields:
            One chunk.

        Raises:
            RuntimeError: Always, after the chunk.
        """
        yield {"event": "message_patch", "data": {"chunk": "par"}}
        raise RuntimeError("boom")

    monkeypatch.setattr(agent_mod, "run_generalist_agent", turn)
    with pytest.raises(RuntimeError, match="boom"):
        _post(engine)
    assert len(charges) == 1
    assert charges[0]["runner"] is _RUNNER
    assert charges[0]["key"].startswith("chat-sandbox:")


def test_a_sandbox_charge_adds_its_cents_to_the_turn_cost(
    monkeypatch: pytest.MonkeyPatch, engine: Engine, charges: list[dict[str, Any]]
) -> None:
    """A charged box sends a last ``turn_stats`` event whose cents include the box charge."""
    monkeypatch.setattr(agent_mod, "charge_chat_sandbox", lambda *a, **k: 5)

    async def turn(**_kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        """Give a one-reply turn.

        Args:
            **_kwargs: The agent's arguments.

        Yields:
            The turn's events.
        """
        yield {"event": "done", "data": {"assistant_message": "ok", "model": "test-model"}}

    monkeypatch.setattr(agent_mod, "run_generalist_agent", turn)
    app = FastAPI()
    app.include_router(agent_mod.create_generalist_agent_router(job_store=_Store(engine)))
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser(username=_USER, role="user", groups=())
    res = TestClient(app).post(
        "/optimizations/generalist-agent",
        json={"user_message": "read it", "chat_history": [], "wizard_state": {}, "trust_mode": "ask"},
    )
    events = [block for block in res.text.split("\n\n") if block.startswith("event: ")]
    done = json.loads(next(b for b in events if b.startswith("event: done")).split("data: ", 1)[1])
    last = events[-1]
    assert last.startswith("event: turn_stats")
    stats = json.loads(last.split("data: ", 1)[1])["stats"]
    assert stats["cents"] == (done["stats"].get("cents") or 0) + 5


def test_reference_note_names_the_path_only_with_file_tools() -> None:
    """With file tools the note names the chat copy's path; without them it keeps the old note."""
    row = ChatMediaModel(id="m-1", kind="document", filename="a.pdf")
    with_tools = agent_mod._with_media_references("hi", [row], {"m-1": "m-2"})
    assert "attachments/m-2/" in with_tools
    assert "exec_command" in with_tools
    without = agent_mod._with_media_references("hi", [row], None)
    assert "attachments/" not in without
    assert "cannot see it" in without
