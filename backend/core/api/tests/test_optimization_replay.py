"""Tests for the read-only replay route ``GET /optimizations/{id}/steps`` (SKY-726)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ...storage import step_log_reader
from ...storage.models import AgentStepModel, Base, JobModel
from ...storage.object_store import LocalObjectStore
from ...storage.shard_map import ShardMap
from ...storage.step_log_store import StepLogStore
from ..auth import AuthenticatedUser
from ..errors import DomainError
from ..routers.optimizations import create_optimizations_router
from ..routers.optimizations import replay as replay_module
from .conftest import bypass_auth
from .mocks import FakeJobStore

RUN = "run-1"
TREE = "a" * 64
OWNER = AuthenticatedUser(username="alice", role="user", groups=())
STRANGER = AuthenticatedUser(username="bob", role="user", groups=())


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    """Return a SQLite engine with the schema and the run's job row.

    Args:
        tmp_path: Pytest's temporary folder.

    Returns:
        The engine.
    """
    database = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    Base.metadata.create_all(database)
    with Session(database) as session:
        session.add(JobModel(optimization_id=RUN, username="alice", execution_generation=1))
        session.commit()
    return database


@pytest.fixture
def objects(tmp_path: Path) -> LocalObjectStore:
    """Return a local pack store.

    Args:
        tmp_path: Pytest's temporary folder.

    Returns:
        The store.
    """
    return LocalObjectStore(tmp_path / "objects")


@pytest.fixture
def step_log(engine: Engine, objects: LocalObjectStore) -> Iterator[StepLogStore]:
    """Yield a step log writer over the test engine, and close it after the test.

    Args:
        engine: The test engine.
        objects: The pack store.

    Yields:
        The writer.
    """
    store = StepLogStore(engine, objects, "w1", flush_window_seconds=60.0, shards=ShardMap(engine))
    yield store
    store.close()


def _commit(store: StepLogStore, agent_id: str, seq: int, kind: str, body: dict) -> None:
    """Commit one step at generation 1.

    Args:
        store: The writer.
        agent_id: The agent.
        seq: The step's position.
        kind: ``command`` or ``model``.
        body: The step body.
    """
    store.commit(
        run_id=RUN,
        agent_id=agent_id,
        seq=seq,
        kind=kind,
        execution_generation=1,
        grant_list_id="grants-1",
        body=body,
        tree_id=TREE if kind == "command" else None,
    )


def _client(
    engine: Engine,
    objects: LocalObjectStore | None,
    monkeypatch: pytest.MonkeyPatch,
    user: AuthenticatedUser = OWNER,
) -> TestClient:
    """Mount the optimizations router over a fake job store that carries the test engine.

    Args:
        engine: The test engine.
        objects: The pack store the route reads, or ``None`` for a pod without one.
        monkeypatch: Pytest's monkeypatch fixture.
        user: The caller.

    Returns:
        The client.
    """
    store = FakeJobStore()
    store.seed_job(RUN, payload_overview={"username": "alice"})
    store.engine = engine
    store.step_log_shards = ShardMap(engine)
    monkeypatch.setattr(replay_module, "_pack_store", lambda: objects)
    app = FastAPI()
    app.include_router(create_optimizations_router(job_store=store, get_worker_ref=lambda: None))
    bypass_auth(app, user=user)

    @app.exception_handler(DomainError)
    async def _domain_error_handler(_request, exc: DomainError) -> JSONResponse:
        """Mirror the app-level envelope so tests can assert on ``code``.

        Args:
            _request: Ignored request.
            exc: The raised error.

        Returns:
            The envelope.
        """
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, "code": exc.code})

    return TestClient(app, raise_server_exceptions=False)


def test_steps_page_each_agent_oldest_first(
    engine: Engine, objects: LocalObjectStore, step_log: StepLogStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route lists the agents, pages one agent's steps by seq, and returns each step's fields."""
    _commit(step_log, "agent-b", 0, "command", {"command": "ls", "stdout": "a\n", "stderr": "warn"})
    _commit(step_log, "agent-a", 2, "model", {"response": "third"})
    _commit(step_log, "agent-a", 0, "command", {"command": "make", "output": "ok\n"})
    _commit(step_log, "agent-a", 1, "model", {"generation": 1, "champion": "x"})
    step_log.flush()
    client = _client(engine, objects, monkeypatch)

    first = client.get(f"/optimizations/{RUN}/steps", params={"limit": 2})
    assert first.status_code == 200, first.text
    page = first.json()
    assert page["agents"] == [{"agent_id": "agent-a", "step_count": 3}, {"agent_id": "agent-b", "step_count": 1}]
    assert page["agent_id"] == "agent-a"
    assert [step["seq"] for step in page["steps"]] == [0, 1]
    command, model = page["steps"]
    assert command["step_id"] == f"{RUN}:agent-a:0"
    assert (command["kind"], command["command"], command["output"], command["tree_id"]) == (
        "command",
        "make",
        "ok\n",
        TREE,
    )
    assert command["response"] is None
    assert model["tree_id"] is None
    assert '"champion": "x"' in model["response"]
    assert page["has_more"] is True
    assert page["next_after_seq"] == 1

    rest = client.get(f"/optimizations/{RUN}/steps", params={"limit": 2, "after_seq": 1}).json()
    assert [step["response"] for step in rest["steps"]] == ["third"]
    assert rest["has_more"] is False

    other = client.get(f"/optimizations/{RUN}/steps", params={"agent_id": "agent-b"}).json()
    assert other["steps"][0]["output"] == "a\n\nwarn"


def test_steps_cut_long_fields_and_skip_huge_bodies(
    engine: Engine, objects: LocalObjectStore, step_log: StepLogStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long field is cut to the cap, and a body above the read cap is not read at all."""
    monkeypatch.setattr(step_log_reader, "BODY_READ_MAX", 4 * step_log_reader.FIELD_MAX)
    _commit(step_log, "agent-a", 0, "command", {"command": "cat", "output": "x" * (step_log_reader.FIELD_MAX + 5)})
    _commit(step_log, "agent-a", 1, "command", {"command": "big", "output": "y" * (5 * step_log_reader.FIELD_MAX)})
    step_log.flush()

    long, huge = _client(engine, objects, monkeypatch).get(f"/optimizations/{RUN}/steps").json()["steps"]

    assert len(long["output"]) == step_log_reader.FIELD_MAX
    assert long["truncated"] is True
    assert long["command"] == "cat"
    assert (huge["command"], huge["output"], huge["truncated"]) == (None, None, True)


def test_steps_without_pack_store_list_rows_with_no_body(
    engine: Engine, step_log: StepLogStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pod with no tree bucket still lists the steps, with no body fields."""
    _commit(step_log, "agent-a", 0, "command", {"command": "make"})
    step_log.flush()

    (step,) = _client(engine, None, monkeypatch).get(f"/optimizations/{RUN}/steps").json()["steps"]

    assert (step["seq"], step["tree_id"], step["command"], step["truncated"]) == (0, TREE, None, False)


def test_steps_of_a_run_with_no_steps_are_empty(
    engine: Engine, objects: LocalObjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run with no flushed step returns no agents and no steps, and the read records no shard."""
    response = _client(engine, objects, monkeypatch).get(f"/optimizations/{RUN}/steps")

    assert response.status_code == 200, response.text
    assert response.json() == {"agents": [], "agent_id": None, "steps": [], "has_more": False, "next_after_seq": -1}
    with Session(engine) as session:
        assert session.scalar(select(JobModel.step_log_shard).where(JobModel.optimization_id == RUN)) is None
        assert session.scalar(select(func.count()).select_from(AgentStepModel)) == 0


def test_steps_are_owner_only(engine: Engine, objects: LocalObjectStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """Another user gets 404, the same as for an unknown run."""
    client = _client(engine, objects, monkeypatch, user=STRANGER)

    response = client.get(f"/optimizations/{RUN}/steps")
    unknown = _client(engine, objects, monkeypatch).get("/optimizations/nope/steps")

    assert response.status_code == 404
    assert response.json()["code"] == "optimization.not_found"
    assert unknown.status_code == 404
