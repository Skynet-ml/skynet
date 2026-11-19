"""Tests for the agent run store: stored rows read back as the run view's records."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from core.storage.agent_run_store import PostgresAgentRunStore
from core.storage.models import Base, BlackboxAgentRunModel


@pytest.fixture
def engine() -> Engine:
    """Return an engine over a fresh in-memory SQLite schema."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def store(engine: Engine) -> PostgresAgentRunStore:
    """Return a store over the test engine."""
    return PostgresAgentRunStore(engine)


def _seed(engine: Engine, optimization_id: str, **overrides: object) -> None:
    """Write one stored agent run row, as a past text or agent run left it.

    Args:
        engine: The test engine.
        optimization_id: Owning job id.
        **overrides: Column overrides.
    """
    columns: dict[str, object] = {
        "run_id": 1,
        "phase": "version",
        "trial": 2,
        "example_id": "0",
        "case_id": "case-a",
        "label": "v2 · case 1",
        "status": "running",
        "started_at": datetime(2026, 9, 1, 10, tzinfo=UTC),
        "model": "openai/gpt-5",
        "usage": {},
        "transcript": "",
    }
    columns.update(overrides)
    with Session(engine) as session:
        session.add(BlackboxAgentRunModel(optimization_id=optimization_id, **columns))
        session.commit()


def test_get_reads_a_stored_row_back_whole(engine: Engine, store: PostgresAgentRunStore) -> None:
    """A stored row reads back whole, with ISO timestamps and the check under ``check``."""
    _seed(engine, "job-1", status="finished", exit_code=0, output="answer", check_result={"exit_code": 0})

    record = store.get("job-1", 1)

    assert record is not None
    assert record["run_id"] == 1
    assert record["phase"] == "version"
    assert record["trial"] == 2
    assert record["example_id"] == "0"
    assert record["case_id"] == "case-a"
    assert record["status"] == "finished"
    assert record["started_at"] == "2026-09-01T10:00:00+00:00"
    assert record["finished_at"] is None
    assert record["model"] == "openai/gpt-5"
    assert record["output"] == "answer"
    assert record["check"] == {"exit_code": 0}
    assert record["transcript"] == ""


def test_get_returns_none_for_an_unknown_run(store: PostgresAgentRunStore) -> None:
    """Missing rows read as ``None``."""
    assert store.get("job-1", 99) is None


def test_records_are_scoped_to_their_job(engine: Engine, store: PostgresAgentRunStore) -> None:
    """Two jobs may both have run 1."""
    _seed(engine, "job-1", output="a")
    _seed(engine, "job-2", output="b")

    first = store.get("job-1", 1)
    second = store.get("job-2", 1)

    assert first is not None
    assert first["output"] == "a"
    assert second is not None
    assert second["output"] == "b"
