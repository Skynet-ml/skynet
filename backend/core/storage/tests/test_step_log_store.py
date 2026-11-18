"""Tests for the agent step log: pipelined commit, the durable barrier, the fence and group flush.

Most tests use a SQLite file and the local-filesystem object store. The last
test needs row locks and Postgres SQL; it runs only when ``APP_TEST_DB_URL``
names a disposable PostgreSQL database, in its own schema that it drops after.
"""

from __future__ import annotations

import gc
import sqlite3
import threading
import time
import uuid
import weakref
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, event, func, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from core.config import Settings, app_env, settings
from core.storage.models import (
    AgentConversationModel,
    AgentStepModel,
    Base,
    JobModel,
    StoragePackModel,
    TreeObjectModel,
)
from core.storage.object_store import LocalObjectStore, S3ObjectStore
from core.storage.step_log_store import StepFenceError, StepLogStore, _read_manifest

TEST_DB_URL = app_env("APP_TEST_DB_URL")
needs_postgres = pytest.mark.skipif(
    not TEST_DB_URL or not TEST_DB_URL.startswith("postgresql"),
    reason="Set APP_TEST_DB_URL to a disposable PostgreSQL database.",
)
TREE = "a" * 64


class CallerLocal:
    """A local of a caller's frame; a weak reference to it shows whether the frame is still kept."""


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
    """Return a SQLite file engine with the schema and jobs ``job-1`` and ``job-2`` at generation 1.

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


def test_durable_keeps_the_fence_verdict_when_the_step_is_committed_again(make_store, engine) -> None:
    """A retry of a fenced step between the flush and ``durable``'s verdict read does not make ``durable`` succeed."""
    store = make_store()
    step_id = _commit(store)
    waiter = store._waiters[(step_id, 1)]
    wait = waiter.wait

    def wait_then_commit_again(timeout: float | None = None) -> bool:
        """Wait for the flush, then commit as the agent thread can before ``durable`` reads its verdict.

        Args:
            timeout: The most seconds to wait.

        Returns:
            Whether the flush set the waiter.
        """
        done = wait(timeout)
        with pytest.raises(StepFenceError):
            _commit(store, seq=2)
        _commit(store)
        return done

    waiter.wait = wait_then_commit_again
    _set_generation(engine, 2)
    flusher = threading.Thread(target=store.flush)
    flusher.start()
    try:
        store.durable(step_id, 1, timeout=5)
    except StepFenceError:
        pass
    else:
        pytest.fail("durable returned success for a fenced step")
    finally:
        flusher.join()


def test_the_next_commit_of_a_fenced_agent_raises_the_fence_error_one_time(make_store, engine) -> None:
    """After a fenced flush the agent's next commit raises; a commit after that is buffered and fenced at its flush."""
    store = make_store()
    _commit(store)
    _set_generation(engine, 2)
    store.flush()

    with pytest.raises(StepFenceError):
        _commit(store, seq=2)
    later = _commit(store, seq=3)
    store.flush()

    with pytest.raises(StepFenceError, match=later):
        store.durable(later, 1, timeout=0)
    with pytest.raises(StepFenceError, match=later):
        _commit(store, seq=4)
    assert _step_rows(engine) == 0


def test_the_store_keeps_only_the_newest_fence_verdicts(make_store, engine, monkeypatch) -> None:
    """Above the cap the oldest verdicts leave: ``durable`` then asks the database, and a new commit is buffered."""
    monkeypatch.setattr("core.storage.step_log_store.FENCE_VERDICT_MAX", 2)
    store = make_store()
    steps = [_commit(store, agent_id=f"agent-{number}") for number in range(4)]
    _set_generation(engine, 2)

    store.flush()

    assert list(store._failures) == [(step_id, 1) for step_id in steps[2:]]
    assert list(store._agent_failures) == [("job-1", "agent-2", 1), ("job-1", "agent-3", 1)]
    with pytest.raises(StepFenceError, match=steps[3]):
        store.durable(steps[3], 1, timeout=0)
    with pytest.raises(KeyError):
        store.durable(steps[0], 1, timeout=0)
    again = _commit(store, seq=2, agent_id="agent-0")
    store.flush()
    with pytest.raises(StepFenceError, match=again):
        store.durable(again, 1, timeout=0)


def test_a_flush_that_fences_more_steps_than_the_cap_fails_each_waiter(make_store, engine, monkeypatch) -> None:
    """Each waiter of a flush gets its fence verdict, also when the flush fences more steps than the store keeps."""
    monkeypatch.setattr("core.storage.step_log_store.FENCE_VERDICT_MAX", 2)
    store = make_store()
    steps = [_commit(store, seq=seq) for seq in range(1, 6)]
    waiters = [store._waiters[(step_id, 1)] for step_id in steps]
    _set_generation(engine, 2)

    store.flush()

    assert [waiter.failure for waiter in waiters] == steps


def test_durable_raises_a_new_fence_error_each_time_and_keeps_no_caller_frame(make_store, engine) -> None:
    """The store keeps a step id as the verdict, so an error that ``durable`` raised keeps no frame alive in the store."""
    store = make_store()
    step_id = _commit(store)
    _set_generation(engine, 2)
    store.flush()

    def call() -> tuple[StepFenceError, weakref.ref[CallerLocal]]:
        """Call ``durable`` from a frame with a local, as an agent loop does.

        Returns:
            The raised error and a weak reference to the frame's local.
        """
        local = CallerLocal()
        try:
            store.durable(step_id, 1, timeout=0)
        except StepFenceError as error:
            return error, weakref.ref(local)
        pytest.fail("durable returned success for a fenced step")

    first, first_local = call()
    second, second_local = call()

    assert first is not second
    assert first.step_id == second.step_id == step_id
    assert store._failures == {(step_id, 1): step_id}
    del first, second
    gc.collect()
    assert first_local() is None
    assert second_local() is None


def test_durable_that_waits_for_the_flush_raises_a_new_fence_error(make_store, engine, objects) -> None:
    """A ``durable`` call that waits on the step's waiter raises its own error, not an object that the store keeps."""
    store = make_store()
    step_id = _commit(store)
    waiter = store._waiters[(step_id, 1)]
    wait = waiter.wait

    def open_the_gate_then_wait(timeout: float | None = None) -> bool:
        """Let the held PUT go on only after ``durable`` chose the waiter, then wait for the flush.

        Args:
            timeout: The most seconds to wait.

        Returns:
            Whether the flush set the waiter.
        """
        objects.gate.set()
        return wait(timeout)

    waiter.wait = open_the_gate_then_wait
    _set_generation(engine, 2)
    objects.gate.clear()
    flusher = threading.Thread(target=store.flush)
    flusher.start()

    def call() -> tuple[StepFenceError, weakref.ref[CallerLocal]]:
        """Call ``durable`` from a frame with a local.

        Returns:
            The raised error and a weak reference to the frame's local.
        """
        local = CallerLocal()
        try:
            store.durable(step_id, 1, timeout=5)
        except StepFenceError as error:
            return error, weakref.ref(local)
        finally:
            flusher.join()
        pytest.fail("durable returned success for a fenced step")

    waited, waited_local = call()

    assert waiter.failure == step_id
    assert waited.step_id == step_id
    with pytest.raises(StepFenceError) as later:
        store.durable(step_id, 1, timeout=0)
    assert later.value is not waited
    del waited, later
    gc.collect()
    assert waited_local() is None


def test_commit_raises_a_new_fence_error_and_keeps_no_caller_frame(make_store, engine) -> None:
    """The fence error of an agent's next commit keeps no frame alive, while the step's verdict stays in the store."""
    store = make_store()
    step_id = _commit(store)
    _set_generation(engine, 2)
    store.flush()
    assert store._agent_failures == {("job-1", "agent-a", 1): step_id}

    def call() -> tuple[StepFenceError, weakref.ref[CallerLocal]]:
        """Commit the agent's next step from a frame with a local.

        Returns:
            The raised error and a weak reference to the frame's local.
        """
        local = CallerLocal()
        try:
            _commit(store, seq=2)
        except StepFenceError as error:
            return error, weakref.ref(local)
        pytest.fail("the commit of a fenced agent did not raise")

    raised, raised_local = call()

    assert raised.step_id == step_id
    assert store._failures == {(step_id, 1): step_id}
    del raised
    gc.collect()
    assert raised_local() is None
    with pytest.raises(StepFenceError, match=step_id):
        store.durable(step_id, 1, timeout=0)


def test_a_new_fence_verdict_of_a_kept_agent_becomes_the_newest(make_store, engine, objects, monkeypatch) -> None:
    """An agent that a later flush fences again moves to the newest place, so the cap removes another agent first."""
    monkeypatch.setattr("core.storage.step_log_store.FENCE_VERDICT_MAX", 2)
    store = make_store()
    _commit(store, agent_id="agent-0")
    _commit(store, agent_id="agent-1")
    _set_generation(engine, 2)
    put = objects.put

    def put_then_commit(key: str, data: bytes) -> None:
        """Write the pack, then commit the next step of the first agent before the flush sets its verdict.

        Args:
            key: The object key.
            data: The object bytes.
        """
        put(key, data)
        monkeypatch.setattr(objects, "put", put)
        _commit(store, seq=2, agent_id="agent-0")

    monkeypatch.setattr(objects, "put", put_then_commit)
    store.flush()
    assert list(store._agent_failures) == [("job-1", "agent-0", 1), ("job-1", "agent-1", 1)]

    store.flush()

    assert list(store._agent_failures) == [("job-1", "agent-1", 1), ("job-1", "agent-0", 1)]


def test_tree_objects_are_inserted_in_sorted_key_order(make_store, engine) -> None:
    """Objects put in reverse id order reach the ``tree_objects`` insert sorted, so every flush locks them in one order."""
    ids = [letter * 64 for letter in "fedcb"]
    inserted: list[list[str]] = []

    def capture_tree_insert(connection, cursor, statement, parameters, context, executemany) -> None:
        """Record the object ids of each ``tree_objects`` insert, in parameter order.

        Args:
            connection: The connection.
            cursor: The DB-API cursor.
            statement: The SQL text.
            parameters: The statement parameters.
            context: The execution context.
            executemany: Whether the call is an executemany.
        """
        if statement.startswith("INSERT INTO tree_objects"):
            inserted.append([value for value in parameters if value in ids])

    store = make_store()
    for object_id in ids:
        store.put_object("job-1", object_id, b"blob\0" + object_id.encode())
    event.listen(engine, "before_cursor_execute", capture_tree_insert)
    try:
        store.flush()
    finally:
        event.remove(engine, "before_cursor_execute", capture_tree_insert)

    assert inserted == [sorted(ids)]


def test_recover_counts_only_the_packs_it_applied(make_store, engine, objects, monkeypatch) -> None:
    """A pack that another store of the same worker applies while recovery reads it is not counted."""
    store = make_store()
    recovering = make_store()
    other = make_store()
    step_id = _commit(store)
    put, get = objects.put, objects.get

    def get_after_the_other_store_applies(key: str, *location: Any) -> bytes:
        """Let the other store apply the pack first, then read the object.

        Args:
            key: The object key.
            *location: The offset and length.

        Returns:
            The object bytes.
        """
        monkeypatch.setattr(objects, "get", get)
        assert other.recover() == 1
        return get(key, *location)

    def put_then_recover(key: str, data: bytes) -> None:
        """Write the pack, then recover it before this flush's transaction.

        Args:
            key: The object key.
            data: The object bytes.
        """
        put(key, data)
        monkeypatch.setattr(objects, "get", get_after_the_other_store_applies)
        assert recovering.recover() == 0

    monkeypatch.setattr(objects, "put", put_then_recover)
    store.flush()

    store.durable(step_id, 1, timeout=0)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(StoragePackModel)) == 1
    assert _step_rows(engine) == 1


def test_a_failed_database_write_puts_no_new_pack_while_the_written_pack_waits(make_store, engine, objects) -> None:
    """New commits after a failed database write do not PUT a new pack while the written pack waits for its retry."""
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

        assert objects.puts == 1
    finally:
        database_down.clear()
        event.remove(engine, "before_cursor_execute", fail_pack_insert)


def _fail_the_next_apply(store: StepLogStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the store's next database write fail one time, as a short database outage does.

    Args:
        store: The step log.
        monkeypatch: Pytest's monkeypatch fixture.
    """
    apply = store._apply

    def fail_once(*args: Any) -> Any:
        """Restore the real write, then fail this call.

        Args:
            *args: The pack key, size and manifest.

        Raises:
            OperationalError: Always.
        """
        monkeypatch.setattr(store, "_apply", apply)
        raise OperationalError("INSERT INTO storage_packs", {}, Exception("database is down"))

    monkeypatch.setattr(store, "_apply", fail_once)


def test_a_failed_database_write_keeps_the_written_pack_for_the_next_flush(
    make_store, engine, objects, monkeypatch
) -> None:
    """After the PUT and a failed transaction, the next flush records that pack and does not PUT its steps again."""
    store = make_store()
    _fail_the_next_apply(store, monkeypatch)
    first = _commit(store)
    with pytest.raises(OperationalError):
        store.flush()

    second = _commit(store, seq=2)
    store.flush()

    holders = [
        key
        for key in objects.list("packs/w1/")
        if first in [entry["step_id"] for entry in _read_manifest(objects.get(key))["steps"]]
    ]
    assert len(holders) == 1
    store.durable(first, 1, timeout=0)
    store.durable(second, 1, timeout=0)
    assert _step_rows(engine) == 2


def test_durable_returns_when_the_flusher_records_the_kept_pack(make_store, engine, objects, monkeypatch) -> None:
    """With no new commit, the flusher records the kept pack after the window, and ``durable`` returns."""
    store = make_store(flush_window_seconds=0.2)
    _fail_the_next_apply(store, monkeypatch)
    step_id = _commit(store)

    store.durable(step_id, 1, timeout=5)

    assert objects.puts == 1
    assert _step_rows(engine) == 1


def test_a_kept_pack_that_recovery_applied_settles_at_the_next_flush(make_store, engine, objects, monkeypatch) -> None:
    """Recovery in the same process can apply the kept pack first; the next flush then opens its waiters."""
    store = make_store()
    _fail_the_next_apply(store, monkeypatch)
    step_id = _commit(store)
    with pytest.raises(OperationalError):
        store.flush()

    assert store.recover() == 1
    store.flush()

    store.durable(step_id, 1, timeout=0)
    assert objects.puts == 1
    assert _step_rows(engine) == 1


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
        {"grant_list_id": ""},
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


@pytest.mark.parametrize(
    "data",
    [None, 7, "blob\0x", [b"blob\0x"], bytearray(b"blob\0x"), memoryview(b"blob\0x")],
)
def test_a_put_object_with_data_that_is_not_bytes_is_refused(make_store, engine, data: Any) -> None:
    """Data that is not ``bytes`` is refused at the call; the same object id and a step land after it."""
    store = make_store()

    with pytest.raises(TypeError, match="data is not bytes"):
        store.put_object("job-1", "c" * 64, data)
    store.put_object("job-1", "c" * 64, b"blob\0x")
    _commit(store)
    store.flush()

    assert _step_rows(engine) == 1
    assert store.get_object("job-1", "c" * 64) == b"blob\0x"


@pytest.mark.parametrize("worker_id", ["", "w" * 65, "w/1", "w\x00", "w-é", ".", ".."])
def test_a_bad_worker_id_is_refused(make_store, worker_id: str) -> None:
    """An empty, too long or not printable ASCII worker id, or one that changes the pack folder, is refused."""
    with pytest.raises(ValueError, match="bad worker id"):
        make_store(worker_id)


@pytest.mark.parametrize(
    "options",
    [
        {"flush_window_seconds": 0.0},
        {"flush_window_seconds": -1.0},
        {"flush_window_seconds": float("inf")},
        {"flush_window_seconds": threading.TIMEOUT_MAX * 2},
        {"flush_max_bytes": 0},
        {"flush_max_bytes": -1},
    ],
)
def test_a_flush_window_or_size_cap_outside_its_bounds_is_refused(make_store, options: dict[str, float]) -> None:
    """A window that is not above 0 or that no thread can wait, or a size cap below 1, fails at construction."""
    with pytest.raises(ValueError, match="bad flush window or size cap"):
        make_store(**options)


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


def test_a_batch_above_the_sqlite_bind_limit_lands_in_one_flush(make_store, engine, objects) -> None:
    """A batch of 3,001 steps needs more bind variables than SQLite allows in one statement; it lands at once (SKY-807)."""
    commits: list[object] = []

    def default_bind_limit(dbapi_connection: Any, record: Any) -> None:
        """Set SQLite's default bind-variable limit; some builds raise it, and production may not.

        Args:
            dbapi_connection: The new SQLite connection.
            record: The pool record.
        """
        dbapi_connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 32_766)

    def count_commit(connection: Any) -> None:
        """Count one transaction commit.

        Args:
            connection: The committing connection.
        """
        commits.append(connection)

    event.listen(engine, "connect", default_bind_limit)
    engine.dispose()
    store = make_store()
    step_ids = [_commit(store, seq=seq) for seq in range(1, 3_002)]
    event.listen(engine, "commit", count_commit)

    store.flush()
    event.remove(engine, "commit", count_commit)

    store.durable(step_ids[-1], 1, timeout=0)
    assert objects.puts == 1
    assert len(commits) == 1
    assert _step_rows(engine) == 3_001


def test_each_flush_statement_splits_into_chunks_under_the_bind_limit(make_store, engine, monkeypatch) -> None:
    """With a small bind limit, the step insert, the tree object insert and the id query each split into chunks."""
    limit = 28
    monkeypatch.setattr("core.storage.step_log_store.SQL_PARAMS_MAX", limit)
    ids = [f"{number:064x}" for number in range(9, 0, -1)]
    statements: list[tuple[str, list[Any]]] = []

    def capture(connection, cursor, statement, parameters, context, executemany) -> None:
        """Record the SQL text and the parameters of each statement.

        Args:
            connection: The connection.
            cursor: The DB-API cursor.
            statement: The SQL text.
            parameters: The statement parameters.
            context: The execution context.
            executemany: Whether the call is an executemany.
        """
        statements.append((statement, list(parameters)))

    store = make_store()
    for seq in range(1, 31):
        _commit(store, seq=seq)
    for object_id in ids:
        store.put_object("job-1", object_id, b"blob\0" + object_id.encode())
    event.listen(engine, "before_cursor_execute", capture)
    try:
        store.flush()
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    step_inserts = [params for sql, params in statements if sql.startswith("INSERT INTO agent_steps")]
    object_inserts = [params for sql, params in statements if sql.startswith("INSERT INTO tree_objects")]
    id_queries = [params for sql, params in statements if sql.startswith("SELECT agent_steps.step_id")]
    assert len(step_inserts) > 1
    assert len(object_inserts) > 1
    assert len(id_queries) > 1
    assert all(len(params) <= limit for params in step_inserts + object_inserts + id_queries)
    assert [value for params in object_inserts for value in params if value in ids] == sorted(ids)
    assert _step_rows(engine) == 30
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(TreeObjectModel)) == 9


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


def test_new_commits_after_a_failed_put_do_not_start_a_retry_early(make_store, objects, monkeypatch) -> None:
    """After a failed PUT, the flusher waits one window: the commits in that window do not each PUT the batch again."""
    attempts: list[str] = []

    def failing_put(key: str, data: bytes) -> None:
        """Count the PUT, then fail it.

        Args:
            key: The object key.
            data: The object bytes.

        Raises:
            OSError: Always.
        """
        attempts.append(key)
        raise OSError("object store is down")

    monkeypatch.setattr(objects, "put", failing_put)
    # The size cap makes each commit due at once; the long window keeps a slow loop inside the wait.
    store = make_store(flush_window_seconds=5, flush_max_bytes=1)
    _commit(store)
    deadline = time.monotonic() + 2
    while not attempts and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(attempts) == 1

    for seq in range(2, 12):
        _commit(store, seq=seq)
        time.sleep(0.02)

    assert len(attempts) == 1
    monkeypatch.undo()


def test_a_failed_outside_flush_wakes_the_flusher(make_store, engine, objects, monkeypatch) -> None:
    """A batch that a failed outside ``flush`` puts back lands with no new commit to wake the flusher."""
    store = make_store(flush_window_seconds=0.1)
    step_id = _commit(store)
    put = objects.put

    def slow_failing_put(key: str, data: bytes) -> None:
        """Fail after the flusher's window ends, so it sleeps on an empty buffer first.

        Args:
            key: The object key.
            data: The object bytes.

        Raises:
            OSError: Always.
        """
        time.sleep(0.4)
        raise OSError("disk full")

    monkeypatch.setattr(objects, "put", slow_failing_put)
    with pytest.raises(OSError):
        store.flush()
    monkeypatch.setattr(objects, "put", put)

    store.durable(step_id, 1, timeout=3)
    assert _step_rows(engine) == 1


def test_a_flush_that_fails_again_logs_the_count_and_only_the_error_type(make_store, caplog, monkeypatch) -> None:
    """Each repeat of a failed flush logs one line with its count and error type, never the error text."""
    caplog.set_level("WARNING", logger="core.storage.step_log_store")
    store = make_store(flush_window_seconds=0.05)

    def failing_apply(*args: Any) -> Any:
        """Fail the database write with row data in the error text.

        Args:
            *args: The ``_apply`` arguments.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("row data: secret-output")

    monkeypatch.setattr(store, "_apply", failing_apply)
    _commit(store)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not any("3 times" in r.getMessage() for r in caplog.records):
        time.sleep(0.01)
    monkeypatch.undo()

    repeats = [r for r in caplog.records if "times in a row" in r.getMessage()]
    assert any("3 times" in r.getMessage() for r in repeats)
    assert all("RuntimeError" in r.getMessage() and "secret" not in r.getMessage() for r in repeats)
    assert all(r.exc_info is None for r in repeats)


def test_a_flush_that_fails_with_a_new_error_type_logs_the_full_error_again(make_store, caplog, monkeypatch) -> None:
    """When a repeat fails with another error type, the log shows that error's full trace, and the count starts again."""
    caplog.set_level("WARNING", logger="core.storage.step_log_store")
    store = make_store(flush_window_seconds=0.05)
    errors: list[Exception] = [RuntimeError("first"), RuntimeError("first"), ValueError("second"), ValueError("second")]

    def failing_apply(*args: Any) -> Any:
        """Fail the database write with the next error of the list, then with the last one.

        Args:
            *args: The ``_apply`` arguments.

        Raises:
            Exception: Always.
        """
        raise errors.pop(0) if len(errors) > 1 else errors[0]

    monkeypatch.setattr(store, "_apply", failing_apply)
    _commit(store)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not any("ValueError" in r.getMessage() for r in caplog.records):
        time.sleep(0.01)
    monkeypatch.undo()

    traced = [r.exc_info[0] for r in caplog.records if r.exc_info]
    assert traced[:2] == [RuntimeError, ValueError]
    repeats = [r.getMessage() for r in caplog.records if "times in a row" in r.getMessage()]
    assert repeats[:2] == [
        "step log flush failed 2 times in a row (RuntimeError); it tries again after the window",
        "step log flush failed 2 times in a row (ValueError); it tries again after the window",
    ]


def test_a_flush_that_fails_again_after_a_success_logs_the_full_error_again(
    make_store, engine, caplog, monkeypatch
) -> None:
    """A successful flush resets the count, so the next failure with the same cause logs its full trace."""
    caplog.set_level("WARNING", logger="core.storage.step_log_store")
    store = make_store(flush_window_seconds=0.05)
    real_apply = store._apply
    outcomes: list[Exception | None] = [RuntimeError("x"), None, RuntimeError("x")]

    def flaky_apply(*args: Any) -> Any:
        """Fail the database write, then write it once, then fail with the same error each time.

        Args:
            *args: The ``_apply`` arguments.

        Returns:
            The result of the real ``_apply`` on the one success.

        Raises:
            RuntimeError: On every call but the second.
        """
        outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if outcome is None:
            return real_apply(*args)
        raise outcome

    monkeypatch.setattr(store, "_apply", flaky_apply)
    _commit(store)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _step_rows(engine) < 1:
        time.sleep(0.01)
    _commit(store, seq=2)
    while time.monotonic() < deadline and len(caplog.records) < 2:
        time.sleep(0.01)
    monkeypatch.undo()

    first, second = caplog.records[:2]
    assert [r.exc_info[0] if r.exc_info else None for r in (first, second)] == [RuntimeError, RuntimeError]
    assert "times in a row" not in second.getMessage()


def test_a_flush_that_fails_with_a_new_driver_error_logs_the_full_error_again(make_store, caplog, monkeypatch) -> None:
    """Both SQLite errors are ``sqlite3.OperationalError``; a new SQLite error code still logs its full trace."""
    caplog.set_level("WARNING", logger="core.storage.step_log_store")
    store = make_store(flush_window_seconds=0.05)
    too_many_orig = sqlite3.OperationalError("too many SQL variables")
    too_many_orig.sqlite_errorcode = sqlite3.SQLITE_ERROR
    locked_orig = sqlite3.OperationalError("database is locked")
    locked_orig.sqlite_errorcode = sqlite3.SQLITE_BUSY
    too_many = OperationalError("INSERT", {"output": "secret"}, too_many_orig)
    locked = OperationalError("INSERT", {"output": "secret"}, locked_orig)
    errors: list[Exception] = [too_many, too_many, locked, locked]

    def failing_apply(*args: Any) -> Any:
        """Fail the database write with the next error of the list, then with the last one.

        Args:
            *args: The ``_apply`` arguments.

        Raises:
            OperationalError: Always.
        """
        raise errors.pop(0) if len(errors) > 1 else errors[0]

    monkeypatch.setattr(store, "_apply", failing_apply)
    _commit(store)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and sum("times in a row" in r.getMessage() for r in caplog.records) < 2:
        time.sleep(0.01)
    monkeypatch.undo()

    traced = [r.exc_info[1].orig.sqlite_errorcode for r in caplog.records if r.exc_info]
    assert traced[:2] == [sqlite3.SQLITE_ERROR, sqlite3.SQLITE_BUSY]
    repeats = [r.getMessage() for r in caplog.records if "times in a row" in r.getMessage()]
    assert repeats[:2] == [
        "step log flush failed 2 times in a row (OperationalError); it tries again after the window",
        "step log flush failed 2 times in a row (OperationalError); it tries again after the window",
    ]


def test_a_closed_store_refuses_new_steps_and_objects(make_store) -> None:
    """After ``close``, no flusher is left, so a new step or object fails at once instead of waiting forever."""
    store = make_store()
    store.close()

    with pytest.raises(RuntimeError, match="closed"):
        _commit(store)
    with pytest.raises(RuntimeError, match="closed"):
        store.put_object("job-1", "c" * 64, b"blob\0hello")


def test_a_failed_flush_at_close_opens_the_waiters_with_an_error(make_store, objects, monkeypatch) -> None:
    """A waiter with no time limit does not block forever when the last flush of ``close`` fails."""
    store = make_store()
    step_id = _commit(store)
    errors: list[BaseException] = []

    def wait() -> None:
        """Wait for the step without a time limit and keep the error."""
        try:
            store.durable(step_id, 1)
        except BaseException as exc:
            errors.append(exc)

    waiting = threading.Thread(target=wait, daemon=True)
    waiting.start()
    monkeypatch.setattr(objects, "put", lambda key, data: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError):
        store.close()

    waiting.join(timeout=5)
    assert not waiting.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    with pytest.raises(RuntimeError, match="closed"):
        store.durable(step_id, 1, timeout=5)
    monkeypatch.undo()


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


def test_the_local_object_store_does_not_list_a_half_written_file(tmp_path) -> None:
    """A temporary file that a crash left during ``put`` is not a key, so recovery never reads it as a pack."""
    store = LocalObjectStore(tmp_path)
    store.put("packs/w1/a.pack", b"0123456789")
    (tmp_path / "packs" / "w1" / ".put-abc123").write_bytes(b"half")

    assert store.list("packs/") == ["packs/w1/a.pack"]


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


class _NoSuchKeyError(Exception):
    """Stands in for the boto3 ``NoSuchKey`` error."""


class _ReadableFakeS3(_FakeS3):
    """Serves ranged GETs and paged listings from a dict of objects; no network."""

    exceptions = type("Exceptions", (), {"NoSuchKey": _NoSuchKeyError})

    def __init__(self, objects: dict[str, bytes], page_size: int) -> None:
        """Hold the objects to serve.

        Args:
            objects: The object bytes by key.
            page_size: The number of keys in one listing page.
        """
        super().__init__()
        self.objects = objects
        self.page_size = page_size

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        """Serve one ranged GET with the inclusive HTTP range end.

        Args:
            **kwargs: The call arguments.

        Returns:
            A response whose body holds the range.

        Raises:
            _NoSuchKeyError: When no object is filed under the key.
        """
        self.calls.append(kwargs)
        if kwargs["Key"] not in self.objects:
            raise _NoSuchKeyError(kwargs["Key"])
        start, end = kwargs["Range"].removeprefix("bytes=").split("-")
        data = self.objects[kwargs["Key"]]
        body = data[int(start) : int(end) + 1] if end else data[int(start) :]
        return {"Body": type("Body", (), {"read": lambda self: body})()}

    def get_paginator(self, name: str) -> Any:
        """Return a paginator that splits the matching keys into pages.

        Args:
            name: The operation name.

        Returns:
            An object whose ``paginate`` yields the pages.
        """
        assert name == "list_objects_v2"
        fake = self

        class _Paginator:
            """Yields ``list_objects_v2`` pages."""

            def paginate(self, Bucket: str, Prefix: str) -> Iterator[dict[str, Any]]:  # noqa: N803 (boto3 names)
                """Yield the matching keys, ``page_size`` at a time, in reverse order.

                Args:
                    Bucket: The bucket name.
                    Prefix: The key prefix.

                Yields:
                    One page; an empty page has no ``Contents``.
                """
                keys = sorted((key for key in fake.objects if key.startswith(Prefix)), reverse=True)
                for start in range(0, len(keys), fake.page_size):
                    yield {"Contents": [{"Key": key} for key in keys[start : start + fake.page_size]]}
                yield {}

        return _Paginator()


def test_the_s3_store_reads_ranges_and_lists_every_page() -> None:
    """The S3 store reads exact byte ranges, maps a missing key to KeyError, and joins every listing page in key order."""
    client = _ReadableFakeS3(
        {"packs/w1/b.pack": b"0123456789", "packs/w1/a.pack": b"x", "packs/w2/c.pack": b"y", "other/d": b"z"},
        page_size=2,
    )
    store = S3ObjectStore(bucket="trees-test", client=client)

    assert store.get("packs/w1/b.pack", 2, 3) == b"234"
    assert client.calls[-1]["Range"] == "bytes=2-4"
    assert store.get("packs/w1/b.pack", 7) == b"789"
    calls = len(client.calls)
    assert store.get("packs/w1/b.pack", 4, 0) == b""
    assert len(client.calls) == calls
    with pytest.raises(KeyError):
        store.get("packs/missing")
    assert store.list("packs/") == ["packs/w1/a.pack", "packs/w1/b.pack", "packs/w2/c.pack"]


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
    engine = create_engine(TEST_DB_URL, connect_args={"options": f"-csearch_path={schema}"})
    try:
        Base.metadata.create_all(
            engine,
            tables=[
                AgentConversationModel.__table__,
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
