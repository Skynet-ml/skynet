"""Tests for the agent step log: pipelined commit, the durable barrier, the fence and group flush.

Most tests use a SQLite file and the local-filesystem object store. The last
test needs row locks and Postgres SQL; it runs only when ``APP_TEST_DB_URL``
names a disposable PostgreSQL database, in its own schema that it drops after.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, event, func, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from core.config import Settings, app_env, settings
from core.storage.models import AgentStepModel, Base, JobModel, StoragePackModel, TreeObjectModel
from core.storage.object_store import LocalObjectStore, S3ObjectStore
from core.storage.step_log_store import StepFenceError, StepLogStore

TEST_DB_URL = app_env("APP_TEST_DB_URL")
needs_postgres = pytest.mark.skipif(
    not TEST_DB_URL or not TEST_DB_URL.startswith("postgresql"),
    reason="Set APP_TEST_DB_URL to a disposable PostgreSQL database.",
)
TREE = "a" * 64


class CountingObjectStore(LocalObjectStore):
    """A local object store that counts PUTs and can hold a PUT until a gate opens."""

    def __init__(self, root: Path) -> None:
        """Bind the store to a folder with an open gate.

        Args:
            root: The folder that holds the objects.
        """
        super().__init__(root)
        self.puts = 0
        self.gate = threading.Event()
        self.gate.set()
        self.entered = threading.Event()

    def put(self, key: str, data: bytes) -> None:
        """Wait for the gate, then write the object and count the PUT.

        Args:
            key: The object key.
            data: The object bytes.
        """
        self.entered.set()
        self.gate.wait()
        super().put(key, data)
        self.puts += 1


def _make_engine(path: Path) -> Engine:
    """Return a SQLite file engine with the schema and job ``job-1`` at generation 1.

    Args:
        path: The database file.

    Returns:
        The engine.
    """
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(JobModel(optimization_id="job-1", username="ada", execution_generation=1))
        session.add(JobModel(optimization_id="job-2", username="ada", execution_generation=1))
        session.commit()
    return engine


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    """Return a fresh SQLite file engine with two jobs.

    Args:
        tmp_path: Pytest's temporary folder.

    Returns:
        The engine.
    """
    return _make_engine(tmp_path / "db.sqlite")


@pytest.fixture
def objects(tmp_path: Path) -> CountingObjectStore:
    """Return a counting local object store.

    Args:
        tmp_path: Pytest's temporary folder.

    Returns:
        The store.
    """
    return CountingObjectStore(tmp_path / "objects")


@pytest.fixture
def make_store(engine: Engine, objects: CountingObjectStore) -> Iterator[Any]:
    """Yield a factory of step log stores that closes each one after the test.

    Args:
        engine: The test engine.
        objects: The test object store.

    Yields:
        ``make(worker_id="w1", **options)`` that builds a store.
    """
    stores: list[StepLogStore] = []

    def make(worker_id: str = "w1", **options: Any) -> StepLogStore:
        """Build one store.

        Args:
            worker_id: The store's worker id.
            **options: Store options; the window defaults to 60 seconds.

        Returns:
            The store.
        """
        options.setdefault("flush_window_seconds", 60.0)
        store = StepLogStore(engine, objects, worker_id, **options)
        stores.append(store)
        return store

    yield make
    objects.gate.set()
    for store in stores:
        store.close()


def _commit(store: StepLogStore, seq: int = 1, **overrides: Any) -> str:
    """Commit one command step with test defaults.

    Args:
        store: The step log.
        seq: The step position.
        **overrides: Field overrides.

    Returns:
        The step id.
    """
    fields: dict[str, Any] = {
        "run_id": "job-1",
        "agent_id": "agent-a",
        "seq": seq,
        "kind": "command",
        "execution_generation": 1,
        "grant_list_id": "grants-1",
        "body": {"command": f"echo {seq}", "output": f"{seq}\n"},
        "tree_id": TREE,
    }
    fields.update(overrides)
    return store.commit(**fields)


def _step_rows(engine: Engine) -> int:
    """Return the number of committed step rows.

    Args:
        engine: The test engine.

    Returns:
        The row count.
    """
    with Session(engine) as session:
        return int(session.scalar(select(func.count()).select_from(AgentStepModel)) or 0)


def _set_generation(engine: Engine, generation: int, run_id: str = "job-1") -> None:
    """Move a job to another execution generation, as a resume does.

    Args:
        engine: The test engine.
        generation: The new generation.
        run_id: The job.
    """
    with Session(engine) as session:
        session.execute(
            update(JobModel).where(JobModel.optimization_id == run_id).values(execution_generation=generation)
        )
        session.commit()


def test_commit_returns_the_step_id_before_any_write(make_store, engine, objects) -> None:
    """A commit only buffers: no PUT, no row, and ``durable`` is not done yet."""
    store = make_store()

    step_id = _commit(store)

    assert step_id == "job-1:agent-a:1"
    assert objects.puts == 0
    assert _step_rows(engine) == 0
    with pytest.raises(TimeoutError):
        store.durable(step_id, 1, timeout=0.05)
    store.flush()
    store.durable(step_id, 1, timeout=0)
    assert _step_rows(engine) == 1


def test_durable_waits_for_the_object_put(make_store, engine, objects) -> None:
    """While the PUT is held, ``durable`` does not return; after it, the pack and row exist."""
    objects.gate.clear()
    store = make_store(flush_window_seconds=0.05)
    step_id = _commit(store)
    assert objects.entered.wait(5)

    with pytest.raises(TimeoutError):
        store.durable(step_id, 1, timeout=0.2)
    objects.gate.set()
    store.durable(step_id, 1, timeout=5)

    assert objects.puts == 1
    assert len(objects.list("packs/w1/")) == 1
    assert _step_rows(engine) == 1


def test_durable_waits_for_the_postgres_commit(make_store, engine, objects) -> None:
    """With the pack already written, ``durable`` still waits until the transaction commits."""
    committing = threading.Event()
    release = threading.Event()

    def hold_commit(connection: Any) -> None:
        """Hold the flush transaction's commit until the test releases it.

        Args:
            connection: The committing connection.
        """
        committing.set()
        release.wait(5)

    event.listen(engine, "commit", hold_commit)
    store = make_store(flush_window_seconds=0.05)
    step_id = _commit(store)
    assert committing.wait(5)

    assert objects.puts == 1
    with pytest.raises(TimeoutError):
        store.durable(step_id, 1, timeout=0.2)
    release.set()
    store.durable(step_id, 1, timeout=5)
    event.remove(engine, "commit", hold_commit)

    assert _step_rows(engine) == 1


def test_a_stale_generation_fails_the_step_with_a_fence_error(make_store, engine) -> None:
    """A generation bump before the flush fails the step on ``durable`` and on the next commit."""
    store = make_store()
    step_id = _commit(store)
    with Session(engine) as session:
        session.execute(update(JobModel).where(JobModel.optimization_id == "job-1").values(execution_generation=2))
        session.commit()

    store.flush()

    with pytest.raises(StepFenceError, match="job-1:agent-a:1") as raised:
        store.durable(step_id, 1)
    assert raised.value.step_id == step_id
    assert _step_rows(engine) == 0
    assert store.get_step(step_id) is None
    with pytest.raises(StepFenceError, match="job-1:agent-a:1"):
        _commit(store, seq=2)


def test_a_fresh_run_lands_while_a_stale_run_in_the_same_flush_fails(make_store, engine) -> None:
    """The fence is per run: one flush keeps the current run's step and fails only the stale one."""
    store = make_store()
    fresh = _commit(store, agent_id="agent-a")
    stale = _commit(store, agent_id="agent-b", run_id="job-2")
    with Session(engine) as session:
        session.execute(update(JobModel).where(JobModel.optimization_id == "job-2").values(execution_generation=5))
        session.commit()

    store.flush()

    store.durable(fresh, 1)
    with pytest.raises(StepFenceError):
        store.durable(stale, 1)
    with Session(engine) as session:
        assert session.scalars(select(AgentStepModel.step_id)).all() == [fresh]


def test_a_retried_commit_with_the_same_step_id_is_a_no_op(make_store, engine, objects) -> None:
    """A retry in the buffer, after the flush, and from a new worker leaves one row with the first body."""
    store = make_store()
    first = _commit(store, body={"command": "ls", "output": "a\n"})
    assert _commit(store, body={"command": "ls", "output": "a\n"}) == first
    store.flush()
    assert objects.puts == 1

    assert _commit(store, body={"command": "other", "output": ""}) == first
    assert store.get_step(first)["body"] == {"command": "ls", "output": "a\n"}
    store.flush()
    restarted = make_store()
    assert _commit(restarted, body={"command": "again", "output": ""}) == first
    restarted.flush()

    assert _step_rows(engine) == 1
    assert restarted.get_step(first)["body"] == {"command": "ls", "output": "a\n"}


def test_a_retry_after_the_flush_reads_the_first_body(make_store) -> None:
    """Between a retry and its flush, a reader sees the stored first body, not the retry's."""
    store = make_store()
    first = _commit(store, body={"command": "ls", "output": "a\n"})
    store.flush()

    _commit(store, body={"command": "retry", "output": ""})

    assert store.get_step(first)["body"] == {"command": "ls", "output": "a\n"}


def test_after_a_generation_bump_only_the_new_generation_retry_of_a_durable_step_succeeds(make_store, engine) -> None:
    """The old generation's retry fails the fence; the new one's retry is a no-op, and its next seq lands."""
    store = make_store()
    first = _commit(store)
    store.flush()
    _commit(store, body={"command": "old retry", "output": ""})
    _set_generation(engine, 2)
    assert _commit(store, execution_generation=2, body={"command": "new retry", "output": ""}) == first

    store.flush()

    with pytest.raises(StepFenceError, match=first):
        store.durable(first, 1, timeout=0)
    store.durable(first, 2, timeout=0)
    assert store.get_step(first)["body"] == {"command": "echo 1", "output": "1\n"}
    second = _commit(store, seq=2, execution_generation=2)
    store.flush()
    store.durable(second, 2, timeout=0)
    assert _step_rows(engine) == 2


def test_a_stale_generation_commit_after_the_current_one_landed_fails_the_fence(make_store, engine) -> None:
    """Generation 2 lands seq 5; a later generation 1 commit of seq 5 fails, and the row stays generation 2's."""
    store = make_store()
    _set_generation(engine, 2)
    current = _commit(store, seq=5, execution_generation=2, body={"command": "new", "output": ""})
    store.flush()

    stale = _commit(store, seq=5, execution_generation=1, body={"command": "old", "output": ""})
    store.flush()

    assert stale == current
    store.durable(current, 2, timeout=0)
    with pytest.raises(StepFenceError, match=stale):
        store.durable(stale, 1, timeout=0)
    step = store.get_step(current)
    assert (step["execution_generation"], step["body"]["command"]) == (2, "new")
    assert _step_rows(engine) == 1


def test_a_stale_generation_commit_on_another_worker_fails_the_fence(make_store, engine) -> None:
    """A generation 1 commit of a seq that generation 2 landed fails on a second store with another worker id."""
    store = make_store()
    other = make_store(worker_id="w2")
    _set_generation(engine, 2)
    current = _commit(store, seq=5, execution_generation=2, body={"command": "new", "output": ""})
    store.flush()

    stale = _commit(other, seq=5, execution_generation=1, body={"command": "old", "output": ""})
    other.flush()

    with pytest.raises(StepFenceError, match=stale):
        other.durable(stale, 1, timeout=0)
    step = other.get_step(current)
    assert (step["execution_generation"], step["body"]["command"]) == (2, "new")
    assert _step_rows(engine) == 1


def test_two_generations_of_one_step_in_one_flush_land_only_the_current_one(make_store, engine) -> None:
    """Generations 1 and 2 commit seq 5 in one window: generation 2 lands its body, generation 1 fails."""
    store = make_store()
    old = _commit(store, seq=5, execution_generation=1, body={"command": "old", "output": ""})
    _set_generation(engine, 2)
    new = _commit(store, seq=5, execution_generation=2, body={"command": "new", "output": ""})

    store.flush()

    store.durable(new, 2, timeout=0)
    with pytest.raises(StepFenceError, match=old):
        store.durable(old, 1, timeout=0)
    with Session(engine) as session:
        row = session.get(AgentStepModel, new)
        assert row.execution_generation == 2
    assert store.get_step(new)["body"]["command"] == "new"
    assert _step_rows(engine) == 1


def test_a_fenced_step_stays_fenced_after_the_new_generation_lands_the_same_seq(make_store, engine) -> None:
    """Generation 1's fenced seq 5 still fails after generation 2 commits and lands seq 5."""
    store = make_store()
    old = _commit(store, seq=5, execution_generation=1)
    _set_generation(engine, 2)
    store.flush()

    new = _commit(store, seq=5, execution_generation=2)
    store.flush()

    store.durable(new, 2, timeout=0)
    with pytest.raises(StepFenceError, match=old):
        store.durable(old, 1, timeout=0)


def test_durable_without_a_buffered_entry_checks_the_current_generation(make_store, engine) -> None:
    """With nothing buffered, ``durable`` fails an old generation's landed step and does not know an unknown id."""
    store = make_store()
    step_id = _commit(store)
    store.flush()
    store.durable(step_id, 1, timeout=0)

    _set_generation(engine, 2)

    with pytest.raises(StepFenceError, match=step_id):
        store.durable(step_id, 1, timeout=0)
    with pytest.raises(KeyError):
        store.durable("job-1:agent-a:99", 1, timeout=0)


def test_a_fence_error_of_an_old_generation_does_not_fail_the_new_generation(make_store, engine) -> None:
    """A fenced old incarnation stops; the run resumes at a new generation and its first commit lands."""
    store = make_store()
    _commit(store, execution_generation=1)
    with Session(engine) as session:
        session.execute(update(JobModel).where(JobModel.optimization_id == "job-1").values(execution_generation=2))
        session.commit()
    store.flush()

    step_id = _commit(store, execution_generation=2)
    store.flush()

    store.durable(step_id, 2, timeout=0)
    assert store.get_step(step_id)["execution_generation"] == 2
    with pytest.raises(StepFenceError):
        _commit(store, seq=2, execution_generation=1)


def test_a_pack_that_another_store_applied_reports_its_fenced_step(make_store, engine, objects, monkeypatch) -> None:
    """When recovery by another store applies the pack first, ``durable`` still fails the step it fenced."""
    store = make_store()
    recovering = make_store()
    fenced = _commit(store)
    applied = _commit(store, run_id="job-2")
    put = objects.put

    def put_then_recover(key: str, data: bytes) -> None:
        """Write the pack, then let the other store fence and apply it before this flush's transaction.

        Args:
            key: The object key.
            data: The object bytes.
        """
        put(key, data)
        with Session(engine) as session:
            session.execute(update(JobModel).where(JobModel.optimization_id == "job-1").values(execution_generation=2))
            session.commit()
        assert recovering.recover() == 1

    monkeypatch.setattr(objects, "put", put_then_recover)
    store.flush()

    store.durable(applied, 1, timeout=0)
    with pytest.raises(StepFenceError, match=fenced):
        store.durable(fenced, 1, timeout=0)
    assert _step_rows(engine) == 1


def test_a_pack_that_another_store_applied_fences_a_stale_retry_of_a_landed_step(
    make_store, engine, objects, monkeypatch
) -> None:
    """A generation 1 retry of a landed step fails when another store applies its pack after a bump."""
    store = make_store()
    recovering = make_store()
    step_id = _commit(store)
    store.flush()
    _commit(store, body={"command": "retry", "output": ""})
    _set_generation(engine, 2)
    put = objects.put

    def put_then_recover(key: str, data: bytes) -> None:
        """Write the pack, then let the other store apply it before this flush's transaction.

        Args:
            key: The object key.
            data: The object bytes.
        """
        put(key, data)
        assert recovering.recover() == 1

    monkeypatch.setattr(objects, "put", put_then_recover)
    store.flush()

    with pytest.raises(StepFenceError, match=step_id):
        store.durable(step_id, 1, timeout=0)
    assert _step_rows(engine) == 1


def test_a_failed_database_write_retries_once_per_window(make_store, engine, objects) -> None:
    """New commits after a failed flush do not start a retry early, so each window costs at most one pack PUT."""
    database_down = threading.Event()
    database_down.set()

    def fail_pack_insert(connection, cursor, statement, parameters, context, executemany) -> None:
        """Fail every statement on ``storage_packs`` while the database is down.

        Args:
            connection: The connection.
            cursor: The DB-API cursor.
            statement: The SQL text.
            parameters: The statement parameters.
            context: The execution context.
            executemany: Whether the call is an executemany.

        Raises:
            OperationalError: While ``database_down`` is set.
        """
        if database_down.is_set() and "storage_packs" in statement:
            raise OperationalError(statement, parameters, Exception("database is down"))

    event.listen(engine, "before_cursor_execute", fail_pack_insert)
    try:
        store = make_store(flush_window_seconds=0.5)
        _commit(store)
        deadline = time.monotonic() + 2
        while objects.puts == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert objects.puts == 1

        for seq in range(2, 12):
            _commit(store, seq=seq)
            time.sleep(0.02)

        assert objects.puts <= 2
    finally:
        database_down.clear()
        event.remove(engine, "before_cursor_execute", fail_pack_insert)


def test_run_agent_and_seq_are_unique_in_the_database(engine) -> None:
    """The schema refuses a second step at the same run, agent and position, but not in another run."""
    row = {
        "run_id": "job-1",
        "agent_id": "agent-a",
        "seq": 1,
        "kind": "model",
        "execution_generation": 1,
        "grant_list_id": "g",
        "pack_key": "p",
        "pack_offset": 0,
        "pack_length": 0,
    }
    with Session(engine) as session:
        session.add(AgentStepModel(step_id="one", **row))
        session.add(AgentStepModel(step_id="other-run", **{**row, "run_id": "job-2"}))
        session.commit()
        session.add(AgentStepModel(step_id="two", **row))
        with pytest.raises(IntegrityError):
            session.commit()


def test_two_runs_with_the_same_agent_and_seq_keep_their_own_steps(make_store, engine) -> None:
    """In one buffer, the same agent and seq in two runs give two ids, two rows and two bodies."""
    store = make_store()
    first = _commit(store, body={"command": "first", "output": ""})
    second = _commit(store, run_id="job-2", body={"command": "second", "output": ""})

    assert first != second
    store.flush()
    store.durable(first, 1, timeout=0)
    store.durable(second, 1, timeout=0)
    assert _step_rows(engine) == 2
    assert (store.get_step(first)["run_id"], store.get_step(first)["body"]["command"]) == ("job-1", "first")
    assert (store.get_step(second)["run_id"], store.get_step(second)["body"]["command"]) == ("job-2", "second")


def test_after_a_restart_another_run_with_the_same_agent_and_seq_lands(make_store, engine) -> None:
    """A new store with an empty buffer commits the same agent and seq for another run, and the row lands."""
    store = make_store()
    _commit(store, body={"command": "first", "output": ""})
    store.flush()
    restarted = make_store()

    second = _commit(restarted, run_id="job-2", body={"command": "second", "output": ""})
    restarted.flush()

    restarted.durable(second, 1, timeout=0)
    with Session(engine) as session:
        assert session.get(AgentStepModel, second).run_id == "job-2"
    assert restarted.get_step(second)["body"]["command"] == "second"


def test_a_fence_failure_in_one_run_does_not_fail_the_same_agent_id_in_another_run(make_store, engine) -> None:
    """The failure an agent sees on its next commit belongs to its own run only."""
    store = make_store()
    stale = _commit(store, run_id="job-2")
    with Session(engine) as session:
        session.execute(update(JobModel).where(JobModel.optimization_id == "job-2").values(execution_generation=2))
        session.commit()
    store.flush()

    _commit(store, seq=2)
    with pytest.raises(StepFenceError, match=stale):
        _commit(store, run_id="job-2", seq=2)


def test_an_object_read_with_an_unknown_run_is_none(make_store) -> None:
    """A run id that names no job reads no object, before or after the flush."""
    store = make_store()
    store.put_object("job-1", "c" * 64, b"blob\0hello")

    assert store.get_object("no-such-run", "c" * 64) is None
    store.flush()
    assert store.get_object("job-1", "c" * 64) == b"blob\0hello"
    assert store.get_object("no-such-run", "c" * 64) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"run_id": ""},
        {"run_id": "job:1"},
        {"seq": -1},
        {"seq": 2**31},
        {"seq": 1.0},
        {"execution_generation": True},
        {"execution_generation": 1.0},
        {"run_id": "job\x001"},
        {"agent_id": "agent\x00a"},
        {"agent_id": "agent-é"},
        {"agent_id": "agent-\ud800"},
        {"grant_list_id": "grants\x001"},
        {"tree_id": "a" * 65},
        {"tree_id": "A" * 64},
        {"tree_id": "g" * 64},
        {"tree_id": "a" * 64 + "\n"},
    ],
)
def test_a_commit_outside_the_step_id_bounds_is_refused(make_store, objects, overrides: dict[str, Any]) -> None:
    """An id, seq or generation that the database or the step id would refuse is refused before the buffer."""
    store = make_store()

    with pytest.raises(ValueError):
        _commit(store, **overrides)
    store.flush()
    assert objects.list("packs/") == []


@pytest.mark.parametrize(
    ("run_id", "object_id"),
    [
        ("job-1", "c" * 65),
        ("job-1", "C" * 64),
        ("job-1", "z" * 64),
        ("job-1", "c" * 64 + "\n"),
        ("", "c" * 64),
        ("j" * 37, "c" * 64),
        ("job:1", "c" * 64),
        ("job\x001", "c" * 64),
    ],
)
def test_a_put_object_with_an_id_the_database_refuses_is_refused(make_store, objects, run_id, object_id) -> None:
    """A bad object id or run id is refused at the call, and nothing reaches a pack."""
    store = make_store()

    with pytest.raises(ValueError):
        store.put_object(run_id, object_id, b"blob\0x")
    store.flush()
    assert objects.list("packs/") == []


def test_the_buffer_flushes_when_the_window_ends(make_store, engine) -> None:
    """With no explicit flush, the step is durable after the window."""
    store = make_store(flush_window_seconds=0.2)
    started = time.monotonic()
    step_id = _commit(store)

    store.durable(step_id, 1, timeout=10)

    assert time.monotonic() - started >= 0.2
    assert _step_rows(engine) == 1


def test_the_buffer_flushes_at_the_size_cap_before_the_window(make_store, engine) -> None:
    """A batch over the size cap flushes at once, long before a 60 second window."""
    store = make_store(flush_window_seconds=60.0, flush_max_bytes=100)
    step_id = _commit(store, body={"command": "cat big", "output": "x" * 200})

    store.durable(step_id, 1, timeout=10)

    assert _step_rows(engine) == 1


def test_one_flush_is_one_put_and_one_transaction(make_store, engine, objects) -> None:
    """Steps of two agents in two runs, plus a tree object, go out in one PUT and one commit."""
    commits: list[object] = []

    def count_commit(connection: Any) -> None:
        """Count one transaction commit.

        Args:
            connection: The committing connection.
        """
        commits.append(connection)

    store = make_store()
    for seq in (1, 2, 3):
        _commit(store, seq=seq, agent_id="agent-a")
        _commit(store, seq=seq, agent_id="agent-b", run_id="job-2", kind="model", body={"response": "ok"}, tree_id=None)
    store.put_object("job-1", "b" * 64, b"blob\0data")
    event.listen(engine, "commit", count_commit)

    store.flush()
    event.remove(engine, "commit", count_commit)

    assert objects.puts == 1
    assert len(commits) == 1
    assert _step_rows(engine) == 6


def test_a_read_before_the_flush_returns_the_buffered_bytes(make_store, objects) -> None:
    """Before the flush the step and object come from the buffer; after it, from the pack."""
    store = make_store()
    step_id = _commit(store, body={"command": "pwd", "output": "/w\n"})
    store.put_object("job-1", "c" * 64, b"blob\0hello")

    assert objects.list("packs/") == []
    assert store.get_step(step_id)["body"] == {"command": "pwd", "output": "/w\n"}
    assert store.get_object("job-1", "c" * 64) == b"blob\0hello"

    store.flush()
    reader = make_store()
    step = reader.get_step(step_id)
    assert step["body"] == {"command": "pwd", "output": "/w\n"}
    assert step["tree_id"] == TREE
    assert step["grant_list_id"] == "grants-1"
    assert step["execution_generation"] == 1
    assert reader.get_object("job-2", "c" * 64) == b"blob\0hello"


def test_a_failed_put_keeps_the_batch_for_the_next_flush(make_store, engine, objects, monkeypatch) -> None:
    """A PUT error loses nothing: the next flush writes the same steps once."""
    store = make_store()
    step_id = _commit(store)
    monkeypatch.setattr(objects, "put", lambda key, data: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        store.flush()
    monkeypatch.undo()

    store.flush()

    store.durable(step_id, 1, timeout=0)
    assert _step_rows(engine) == 1


def test_the_flush_window_defaults_to_half_a_second() -> None:
    """The window and the size cap are settings; the window is 0.5 seconds by default."""
    assert Settings.model_fields["step_log_flush_window_seconds"].default == 0.5
    assert Settings.model_fields["step_log_flush_max_bytes"].default > 0


def test_the_local_object_store_reads_ranges_and_lists_by_prefix(tmp_path) -> None:
    """The local store reads a byte range, lists keys by prefix and refuses keys outside its root."""
    store = LocalObjectStore(tmp_path)
    store.put("packs/w1/a.pack", b"0123456789")
    store.put("other/b", b"x")

    assert store.get("packs/w1/a.pack", 2, 3) == b"234"
    assert store.get("packs/w1/a.pack", 7) == b"789"
    assert store.list("packs/") == ["packs/w1/a.pack"]
    with pytest.raises(KeyError):
        store.get("packs/missing")
    with pytest.raises(ValueError):
        store.put("../escape", b"x")


class _FakeS3:
    """Records the S3 calls the store makes; no network."""

    def __init__(self) -> None:
        """Start with no calls."""
        self.calls: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> None:
        """Record one PUT.

        Args:
            **kwargs: The call arguments.
        """
        self.calls.append(kwargs)


def test_the_s3_store_reads_its_bucket_from_the_setting(monkeypatch) -> None:
    """The S3 store writes to the ``SKYNET_TREE_BUCKET`` bucket, and refuses to start without one."""
    client = _FakeS3()
    monkeypatch.setattr(settings, "tree_bucket", "trees-test")

    S3ObjectStore(client=client).put("packs/w1/a.pack", b"data")

    assert client.calls == [
        {"Bucket": "trees-test", "Key": "packs/w1/a.pack", "Body": b"data", "StorageClass": "STANDARD"}
    ]
    monkeypatch.setattr(settings, "tree_bucket", None)
    with pytest.raises(ValueError, match="SKYNET_TREE_BUCKET"):
        S3ObjectStore(client=client)


@pytest.fixture
def postgres_engine() -> Iterator[Engine]:
    """Yield an engine on a fresh schema of the disposable test database, then drop the schema.

    Yields:
        The engine, with the schema and two jobs at generation 1.
    """
    schema = f"steplog_{uuid.uuid4().hex[:10]}"
    admin = create_engine(TEST_DB_URL)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(TEST_DB_URL, connect_args={"options": f"-csearch_path={schema},public"})
    try:
        # ``public`` is on the path only for the vector type; a check would find its tables and skip ours.
        Base.metadata.create_all(
            engine,
            tables=[
                JobModel.__table__,
                AgentStepModel.__table__,
                StoragePackModel.__table__,
                TreeObjectModel.__table__,
            ],
            checkfirst=False,
        )
        with Session(engine) as session:
            session.add(JobModel(optimization_id="job-1", username="ada", execution_generation=1))
            session.add(JobModel(optimization_id="job-2", username="ada", execution_generation=1))
            session.commit()
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@needs_postgres
def test_postgres_flush_fences_and_dedupes(postgres_engine, tmp_path) -> None:
    """On Postgres, one flush keeps the fresh step, fails the stale one, and a retry adds no row."""
    store = StepLogStore(postgres_engine, LocalObjectStore(tmp_path), "w1", flush_window_seconds=60.0)
    try:
        fresh = _commit(store, agent_id="agent-a")
        stale = _commit(store, agent_id="agent-b", run_id="job-2")
        store.put_object("job-1", "d" * 64, b"blob\0x")
        store.put_object("job-1", "d" * 64, b"blob\0x")
        with Session(postgres_engine) as session:
            session.execute(update(JobModel).where(JobModel.optimization_id == "job-2").values(execution_generation=2))
            session.commit()
        store.flush()
        _commit(store, agent_id="agent-a", body={"command": "retry", "output": ""})
        store.flush()

        store.durable(fresh, 1)
        with pytest.raises(StepFenceError):
            store.durable(stale, 1)
        assert _step_rows(postgres_engine) == 1
        assert store.get_object("job-1", "d" * 64) == b"blob\0x"
        assert store.get_step(fresh)["body"]["command"] == "echo 1"
    finally:
        store.close()
