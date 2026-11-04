"""Tests for resumable wizard-agent turns (``core.api.agent_turns``).

Covers replay after a sequence number (same replica and, through a shared
SQLite store, another replica), the response window that forces a reconnect,
explicit and cross-replica cancellation, abandonment, a vanished owner, and
that a turn read over several connections is metered exactly once. The
router tests drive the resume/cancel endpoints end to end.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ...storage.models import AgentTurnModel, Base
from .. import agent_turns
from ..agent_turns import TURN_END_EVENT, TURN_STARTED_EVENT, AgentTurnRegistry
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..routers import _helpers
from ..routers import code_agent as code_agent_router
from ..routers._helpers import stream_with_llm_metering
from ..routers.code_agent import create_code_agent_router


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Yield a file-backed SQLite engine (safe across worker threads) with the ORM schema."""
    eng = create_engine(f"sqlite:///{tmp_path / 'turns.db'}")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def fast_timers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the registry's poll/heartbeat intervals so tests run in milliseconds."""
    monkeypatch.setattr(agent_turns, "_FLUSH_SECONDS", 0.01)
    monkeypatch.setattr(agent_turns, "_REMOTE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(agent_turns, "_HEARTBEAT_SECONDS", 0.01)


async def _numbers(count: int) -> AsyncIterator[dict[str, Any]]:
    """Yield ``count`` message events, pausing between them like a model would."""
    for i in range(count):
        await asyncio.sleep(0)
        yield {"event": "message_patch", "data": {"chunk": str(i)}}


async def _collect(stream: AsyncIterator[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drain a turn stream into a list."""
    return [event async for event in stream]


def _chunks(events: list[dict[str, Any]]) -> list[str]:
    """Pull the message chunks out of a list of turn events."""
    return [e["data"]["chunk"] for e in events if e["event"] == "message_patch"]


async def test_local_resume_returns_only_later_events() -> None:
    """Reading after seq N yields exactly the events numbered above N, then turn_end."""
    registry = AgentTurnRegistry()
    turn_id = await registry.start(_numbers(5), owner="alice")
    full = await _collect(registry.stream(turn_id))
    assert full[0] == {"id": 1, "event": TURN_STARTED_EVENT, "data": {"turn_id": turn_id}}
    assert [e["id"] for e in full] == list(range(1, len(full) + 1))
    assert full[-1]["event"] == TURN_END_EVENT
    assert full[-1]["data"] == {"status": "finished"}

    resumed = await _collect(registry.stream(turn_id, after_seq=3))
    assert resumed == full[3:]
    assert _chunks(resumed) == ["2", "3", "4"]


async def test_window_ends_response_and_resume_has_no_gaps_or_duplicates() -> None:
    """A response cut by the window resumes exactly where it stopped."""
    registry = AgentTurnRegistry()
    gate = asyncio.Event()

    async def source() -> AsyncIterator[dict[str, Any]]:
        """Emit two chunks, wait for the test, then emit two more."""
        yield {"event": "message_patch", "data": {"chunk": "a"}}
        yield {"event": "message_patch", "data": {"chunk": "b"}}
        await gate.wait()
        yield {"event": "message_patch", "data": {"chunk": "c"}}
        yield {"event": "message_patch", "data": {"chunk": "d"}}

    turn_id = await registry.start(source(), owner="alice")
    first = await _collect(registry.stream(turn_id, window_seconds=0.1))
    assert first[-1]["event"] != TURN_END_EVENT
    gate.set()
    rest = await _collect(registry.stream(turn_id, after_seq=first[-1]["id"]))
    assert _chunks(first + rest) == ["a", "b", "c", "d"]
    assert [e["id"] for e in first + rest] == list(range(1, len(first) + len(rest) + 1))


async def test_other_replica_resumes_from_shared_store(engine: Engine) -> None:
    """A reconnect landing on another replica replays the persisted events after N."""
    owner_replica, other_replica = AgentTurnRegistry(), AgentTurnRegistry()
    owner_replica.bind_engine(engine)
    other_replica.bind_engine(engine)
    turn_id = await owner_replica.start(_numbers(6), owner="alice")
    local = await _collect(owner_replica.stream(turn_id))

    await other_replica.authorize(turn_id, "alice")
    remote = await _collect(other_replica.stream(turn_id, after_seq=2))
    assert remote == local[2:]
    assert remote[-1]["event"] == TURN_END_EVENT


async def test_other_replica_tails_a_running_turn(engine: Engine) -> None:
    """A remote reader attached mid-turn receives the live remainder in order."""
    owner_replica, other_replica = AgentTurnRegistry(), AgentTurnRegistry()
    owner_replica.bind_engine(engine)
    other_replica.bind_engine(engine)
    gate = asyncio.Event()

    async def source() -> AsyncIterator[dict[str, Any]]:
        """Emit one chunk, wait for the test, then finish."""
        yield {"event": "message_patch", "data": {"chunk": "x"}}
        await gate.wait()
        yield {"event": "message_patch", "data": {"chunk": "y"}}

    turn_id = await owner_replica.start(source(), owner="alice")
    reader = asyncio.create_task(_collect(other_replica.stream(turn_id)))
    await asyncio.sleep(0.1)
    gate.set()
    events = await asyncio.wait_for(reader, timeout=5)
    assert _chunks(events) == ["x", "y"]
    assert events[-1]["event"] == TURN_END_EVENT


async def test_authorize_rejects_foreign_and_unknown_turns(engine: Engine) -> None:
    """Only the owner may read a turn, on its replica or any other."""
    owner_replica, other_replica = AgentTurnRegistry(), AgentTurnRegistry()
    owner_replica.bind_engine(engine)
    other_replica.bind_engine(engine)
    turn_id = await owner_replica.start(_numbers(1), owner="alice")
    for registry in (owner_replica, other_replica):
        with pytest.raises(DomainError) as excinfo:
            await registry.authorize(turn_id, "mallory")
        assert excinfo.value.status_code == 404
    with pytest.raises(DomainError):
        await other_replica.authorize("missing", "alice")
    await _collect(owner_replica.stream(turn_id))


async def test_explicit_cancel_stops_the_task_and_ends_the_stream() -> None:
    """Cancel interrupts the source, runs its cleanup and closes readers with turn_end."""
    registry = AgentTurnRegistry()
    cleaned_up = asyncio.Event()

    async def endless() -> AsyncIterator[dict[str, Any]]:
        """Emit one chunk and then wait forever."""
        try:
            yield {"event": "message_patch", "data": {"chunk": "x"}}
            await asyncio.Event().wait()
        finally:
            cleaned_up.set()

    turn_id = await registry.start(endless(), owner="alice")
    reader = asyncio.create_task(_collect(registry.stream(turn_id)))
    await asyncio.sleep(0.05)
    assert await registry.cancel(turn_id, "mallory") is False
    assert await registry.cancel(turn_id, "alice") is True
    events = await asyncio.wait_for(reader, timeout=5)
    assert cleaned_up.is_set()
    assert [e["event"] for e in events[-2:]] == ["error", TURN_END_EVENT]
    assert events[-1]["data"] == {"status": "cancelled"}
    assert await registry.cancel(turn_id, "alice") is False


async def test_cancel_from_another_replica_reaches_the_owner(engine: Engine) -> None:
    """A Stop landing on another replica is picked up by the owner's heartbeat."""
    owner_replica, other_replica = AgentTurnRegistry(), AgentTurnRegistry()
    owner_replica.bind_engine(engine)
    other_replica.bind_engine(engine)
    cleaned_up = asyncio.Event()

    async def endless() -> AsyncIterator[dict[str, Any]]:
        """Wait forever, recording cleanup."""
        try:
            await asyncio.Event().wait()
            yield {"event": "never", "data": {}}
        finally:
            cleaned_up.set()

    turn_id = await owner_replica.start(endless(), owner="alice")
    assert await other_replica.cancel(turn_id, "mallory") is False
    assert await other_replica.cancel(turn_id, "alice") is True
    await asyncio.wait_for(cleaned_up.wait(), timeout=5)
    events = await asyncio.wait_for(_collect(other_replica.stream(turn_id)), timeout=5)
    assert events[-1] == {"id": events[-1]["id"], "event": TURN_END_EVENT, "data": {"status": "cancelled"}}


async def test_abandoned_turn_is_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    """A turn nobody reads past the abandonment window stops (and cleans up)."""
    monkeypatch.setattr(agent_turns, "_ABANDON_SECONDS", 0.05)
    registry = AgentTurnRegistry()
    cleaned_up = asyncio.Event()

    async def endless() -> AsyncIterator[dict[str, Any]]:
        """Wait forever, recording cleanup."""
        try:
            await asyncio.Event().wait()
            yield {"event": "never", "data": {}}
        finally:
            cleaned_up.set()

    await registry.start(endless(), owner="alice")
    await asyncio.wait_for(cleaned_up.wait(), timeout=5)


async def test_remote_reader_gives_up_on_a_vanished_owner(engine: Engine) -> None:
    """A running turn whose owner stopped beating ends with an error and turn_end."""
    registry = AgentTurnRegistry()
    registry.bind_engine(engine)
    old = datetime.now(UTC) - timedelta(minutes=10)
    with Session(engine) as session:
        session.add(
            AgentTurnModel(
                turn_id="ghost",
                owner="alice",
                status="running",
                cancel_requested=False,
                created_at=old,
                heartbeat_at=old,
                last_read_at=old,
            )
        )
        session.commit()
    events = await asyncio.wait_for(_collect(registry.stream("ghost")), timeout=5)
    assert [e["event"] for e in events] == ["error", TURN_END_EVENT]


class _StubStore:
    """Job-store double whose (opaque) engine switches metering on."""

    engine = object()


@pytest.mark.parametrize("cancel", [False, True])
async def test_turn_is_metered_exactly_once_across_reconnects(monkeypatch: pytest.MonkeyPatch, cancel: bool) -> None:
    """Dropped and resumed connections never re-meter; a cancelled turn still meters once."""
    calls: list[str] = []
    monkeypatch.setattr(_helpers, "meter_llm_run", lambda _engine, username, *_a, **_k: calls.append(username))
    registry = AgentTurnRegistry()
    gate = asyncio.Event()

    async def source() -> AsyncIterator[dict[str, Any]]:
        """Emit a chunk, wait for the test, emit another."""
        yield {"event": "message_patch", "data": {"chunk": "a"}}
        await gate.wait()
        yield {"event": "message_patch", "data": {"chunk": "b"}}

    metered = stream_with_llm_metering(
        source(), job_store=_StubStore(), username="alice", description="Code authoring", usage_sink=[object()]
    )
    turn_id = await registry.start(metered, owner="alice")
    first = registry.stream(turn_id)
    await anext(first)
    await first.aclose()
    first_window = await _collect(registry.stream(turn_id, window_seconds=0.05))
    if cancel:
        assert await registry.cancel(turn_id, "alice") is True
    else:
        gate.set()
    rest = await _collect(registry.stream(turn_id, after_seq=first_window[-1]["id"]))
    assert rest[-1]["event"] == TURN_END_EVENT
    assert calls == ["alice"]


_ALICE = AuthenticatedUser(username="alice", role="user", groups=())
_MALLORY = AuthenticatedUser(username="mallory", role="user", groups=())
_INTAKE_BODY = {"phase": "billing"}


def _app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Mount the code-agent router on a fresh registry with a three-event intake turn."""

    async def fake_stream(**_kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        """Yield a short interview stream ending in ``done``."""
        yield {"event": "message_patch", "data": {"chunk": "hi"}}
        yield {"event": "done", "data": {"message": "hi"}}

    monkeypatch.setattr(code_agent_router, "intake_turn_stream", fake_stream)
    monkeypatch.setattr(code_agent_router, "gate_intake_turn", lambda *_args: (None, False))
    monkeypatch.setattr(code_agent_router, "_intake_catalog", lambda: ([], []))
    monkeypatch.setattr(code_agent_router, "route_interview_model", lambda model: model or "m")
    app = FastAPI()
    app.include_router(create_code_agent_router())
    app.dependency_overrides[get_authenticated_user] = lambda: _ALICE

    @app.exception_handler(DomainError)
    async def _domain_error_handler(_request, exc: DomainError) -> JSONResponse:
        """Mirror the app-level envelope so tests can assert on ``code``."""
        return JSONResponse(status_code=exc.status_code, content={"code": exc.code})

    return app


def test_router_streams_ids_and_resumes_by_query_or_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """The POST frames a turn with ids; GET resumes after ?after= or Last-Event-ID."""
    app = _app(monkeypatch)
    with TestClient(app) as client:
        resp = client.post("/account/intake-interview", json=_INTAKE_BODY)
        assert resp.status_code == 200
        text = resp.text
        assert text.startswith("id: 1\nevent: turn_started\n")
        assert "id: 3\nevent: done\n" in text
        assert "event: turn_end" in text
        turn_id = text.split('"turn_id": "', 1)[1].split('"', 1)[0]

        by_query = client.get(f"/optimizations/agent-turns/{turn_id}/stream", params={"after": 2})
        assert by_query.status_code == 200
        assert by_query.text.startswith("id: 3\nevent: done\n")
        assert "turn_started" not in by_query.text
        assert "message_patch" not in by_query.text

        by_header = client.get(f"/optimizations/agent-turns/{turn_id}/stream", headers={"Last-Event-ID": "3"})
        assert by_header.text.startswith("id: 4\nevent: turn_end\n")

        assert client.post(f"/optimizations/agent-turns/{turn_id}/cancel").json() == {"cancelled": False}

        app.dependency_overrides[get_authenticated_user] = lambda: _MALLORY
        foreign = client.get(f"/optimizations/agent-turns/{turn_id}/stream")
        assert foreign.status_code == 404
        assert foreign.json() == {"code": "agent.turn.not_found"}
        assert client.post(f"/optimizations/agent-turns/{turn_id}/cancel").json() == {"cancelled": False}


def test_router_cancel_stops_a_running_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /cancel from the owner stops a turn that is still running."""
    registry = AgentTurnRegistry()
    app = _app_with_registry(monkeypatch, registry)
    with TestClient(app) as client:
        turn_id = client.portal.call(partial(registry.start, _endless(), owner="alice"))
        assert client.post(f"/optimizations/agent-turns/{turn_id}/cancel").json() == {"cancelled": True}
        resumed = client.get(f"/optimizations/agent-turns/{turn_id}/stream")
        assert '"status": "cancelled"' in resumed.text


async def _endless() -> AsyncIterator[dict[str, Any]]:
    """Wait forever without emitting."""
    await asyncio.Event().wait()
    yield {"event": "never", "data": {}}


def _app_with_registry(monkeypatch: pytest.MonkeyPatch, registry: AgentTurnRegistry) -> FastAPI:
    """Mount the code-agent router bound to ``registry``."""
    monkeypatch.setattr(code_agent_router, "AgentTurnRegistry", lambda: registry)
    app = FastAPI()
    app.include_router(create_code_agent_router())
    app.dependency_overrides[get_authenticated_user] = lambda: _ALICE
    return app


def test_stale_cancel_flag_is_ignored_for_finished_turns(engine: Engine) -> None:
    """A cancel request for a finished turn flags nothing."""
    registry = AgentTurnRegistry()
    registry.bind_engine(engine)
    with Session(engine) as session:
        now = datetime.now(UTC)
        session.add(
            AgentTurnModel(
                turn_id="done",
                owner="alice",
                status="finished",
                cancel_requested=False,
                created_at=now,
                heartbeat_at=now,
                last_read_at=now,
            )
        )
        session.commit()
    assert asyncio.run(registry.cancel("done", "alice")) is False
    with Session(engine) as session:
        session.execute(update(AgentTurnModel).values(status="running"))
        session.commit()
    assert asyncio.run(registry.cancel("done", "alice")) is True
