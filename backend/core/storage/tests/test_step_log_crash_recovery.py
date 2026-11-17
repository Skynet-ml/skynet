"""Chaos test for the step log: a worker killed between the pack PUT and the Postgres commit.

A spawned child process commits a step and flushes it. The child stops at a
chosen point (after the PUT, or inside the transaction just before its
commit) and the parent kills it with SIGKILL. The parent then checks that no
step is visible, and that recovery makes it appear exactly once.
"""

from __future__ import annotations

import multiprocessing as mp
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from core.storage.models import AgentStepModel, Base, JobModel, StoragePackModel, TreeObjectModel
from core.storage.object_store import LocalObjectStore
from core.storage.step_log_store import StepLogStore

TREE = "a" * 64
OBJECT = "b" * 64


class _StopAfterPut(LocalObjectStore):
    """A local store that writes the pack, signals the parent, then hangs until it is killed."""

    def __init__(self, root: Path, marker: Path) -> None:
        """Bind the store and the marker file.

        Args:
            root: The folder that holds the objects.
            marker: The file that tells the parent the PUT is done.
        """
        super().__init__(root)
        self._marker = marker

    def put(self, key: str, data: bytes) -> None:
        """Write the object, then signal and hang.

        Args:
            key: The object key.
            data: The object bytes.
        """
        super().put(key, data)
        self._marker.write_text(key)
        threading.Event().wait()


def _crash_child(database: str, root: str, marker: str, stop_at: str) -> None:
    """Commit and flush one step, and hang at ``stop_at`` until the parent kills the process.

    Args:
        database: The SQLite file.
        root: The object store folder.
        marker: The file the child writes when it reaches the stop point.
        stop_at: ``after_put`` or ``before_commit``.
    """
    engine = create_engine(f"sqlite:///{database}")
    if stop_at == "after_put":
        objects: LocalObjectStore = _StopAfterPut(Path(root), Path(marker))
    else:
        objects = LocalObjectStore(root)

        def hang(connection: object) -> None:
            """Signal the parent with the rows written but not committed, then hang.

            Args:
                connection: The committing connection.
            """
            Path(marker).write_text("commit")
            threading.Event().wait()

        event.listen(engine, "commit", hang)
    store = StepLogStore(engine, objects, "w1", flush_window_seconds=60.0)
    store.commit(
        run_id="job-1",
        agent_id="agent-a",
        seq=1,
        kind="command",
        execution_generation=1,
        grant_list_id="grants-1",
        body={"command": "make", "output": "ok\n"},
        tree_id=TREE,
    )
    store.put_object("job-1", OBJECT, b"blob\0data")
    store.flush()


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
        session.commit()
    return engine


def _count(engine: Engine, model: type) -> int:
    """Return the number of rows of one table.

    Args:
        engine: The test engine.
        model: The table's model.

    Returns:
        The row count.
    """
    with Session(engine) as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _kill_at(tmp_path: Path, stop_at: str) -> None:
    """Run the child until it reaches ``stop_at``, then kill it with SIGKILL.

    Args:
        tmp_path: Pytest's temporary folder.
        stop_at: ``after_put`` or ``before_commit``.
    """
    marker = tmp_path / "marker"
    child = mp.get_context("spawn").Process(
        target=_crash_child,
        args=(str(tmp_path / "db.sqlite"), str(tmp_path / "objects"), str(marker), stop_at),
    )
    child.start()
    deadline = time.monotonic() + 60
    while not marker.exists() and child.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert marker.exists(), f"the child stopped before {stop_at} (exit code {child.exitcode})"
    child.kill()
    child.join(30)
    assert child.exitcode == -9


@pytest.mark.parametrize("stop_at", ["after_put", "before_commit"])
def test_a_kill_between_the_put_and_the_commit_leaves_no_step_and_recovery_adds_it_once(tmp_path, stop_at) -> None:
    """After the kill the pack exists but no row does; recovery adds the step once, and again adds nothing."""
    engine = _make_engine(tmp_path / "db.sqlite")
    _kill_at(tmp_path, stop_at)

    objects = LocalObjectStore(tmp_path / "objects")
    assert len(objects.list("packs/w1/")) == 1
    assert _count(engine, AgentStepModel) == 0
    assert _count(engine, StoragePackModel) == 0
    assert _count(engine, TreeObjectModel) == 0

    store = StepLogStore(engine, objects, "w1", flush_window_seconds=60.0)
    try:
        assert store.get_step("agent-a:1") is None
        assert store.recover() == 1
        store.durable("agent-a:1", timeout=0)
        assert store.get_step("agent-a:1")["body"] == {"command": "make", "output": "ok\n"}
        assert store.get_object(OBJECT) == b"blob\0data"

        assert store.recover() == 0
        store.commit(
            run_id="job-1",
            agent_id="agent-a",
            seq=1,
            kind="command",
            execution_generation=1,
            grant_list_id="grants-1",
            body={"command": "make", "output": "ok\n"},
            tree_id=TREE,
        )
        store.flush()
    finally:
        store.close()
    assert _count(engine, AgentStepModel) == 1
    assert _count(engine, TreeObjectModel) == 1


def test_recovery_keeps_the_fence(tmp_path) -> None:
    """A pack left by a crashed worker of an older generation adds no step on recovery."""
    engine = _make_engine(tmp_path / "db.sqlite")
    _kill_at(tmp_path, "after_put")
    with Session(engine) as session:
        session.execute(update(JobModel).where(JobModel.optimization_id == "job-1").values(execution_generation=2))
        session.commit()

    store = StepLogStore(engine, LocalObjectStore(tmp_path / "objects"), "w1", flush_window_seconds=60.0)
    try:
        assert store.recover() == 1
        with pytest.raises(KeyError):
            store.durable("agent-a:1")
    finally:
        store.close()
    assert _count(engine, AgentStepModel) == 0
    assert _count(engine, StoragePackModel) == 1
