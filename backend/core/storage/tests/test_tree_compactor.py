"""Tests for the tree store compactor: merge small packs, keep every row readable, survive a kill.

The tests use a SQLite file and the local-filesystem object store. The kill
points are a raised error (the transaction rolls back, as after a kill) and
one real SIGKILL of a child process inside the merge commit. The last tests
need row locks; they run only when ``APP_TEST_DB_URL`` names a disposable
PostgreSQL database, in their own schema that they drop after.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import threading
import time
import types
import uuid
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, event, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from core.config import app_env, settings
from core.storage import tree_compactor
from core.storage.models import (
    AgentConversationModel,
    AgentStepModel,
    Base,
    JobModel,
    StoragePackModel,
    TreeObjectModel,
)
from core.storage.object_store import LocalObjectStore, S3ObjectStore
from core.storage.step_log_store import StepLogStore
from core.storage.tree_compactor import (
    BROKEN_COMPACTOR_ID,
    COMPACTED_PREFIX,
    COMPACTOR_ID,
    FULL_COMPACTOR_ID,
    TreeCompactor,
    start_tree_compactor,
)
from core.storage.tree_store import TreeEntry, TreeStore

RUNS = ("job-1", "job-2", "job-3")
TEST_DB_URL = app_env("APP_TEST_DB_URL")
needs_postgres = pytest.mark.skipif(
    not TEST_DB_URL or not TEST_DB_URL.startswith("postgresql"),
    reason="Set APP_TEST_DB_URL to a disposable PostgreSQL database.",
)


class FlakyObjectStore(LocalObjectStore):
    """A local object store whose PUT of a compacted pack can fail after the write."""

    def __init__(self, root: Path) -> None:
        """Bind the store with no failure set.

        Args:
            root: The folder that holds the objects.
        """
        super().__init__(root)
        self.fail_compacted_put = False
        self.on_compacted_put: Any = None

    def put(self, key: str, data: bytes) -> None:
        """Write the object, then run the hook or fail when the key is a compacted pack.

        Args:
            key: The object key.
            data: The object bytes.

        Raises:
            OSError: When a compacted PUT is set to fail.
        """
        super().put(key, data)
        if key.startswith(f"{COMPACTED_PREFIX}/"):
            if self.on_compacted_put is not None:
                self.on_compacted_put(key)
            if self.fail_compacted_put:
                raise OSError("killed after the PUT")


def _make_engine(path: Path) -> Engine:
    """Return a SQLite file engine with the schema; two jobs of ``ada`` and one of ``bob``.

    Args:
        path: The database file.

    Returns:
        The engine.
    """
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for run_id, username in zip(RUNS, ("ada", "ada", "bob"), strict=True):
            session.add(JobModel(optimization_id=run_id, username=username, execution_generation=1))
        session.commit()
    return engine


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    """Return a fresh SQLite file engine.

    Args:
        tmp_path: Pytest's temporary folder.

    Returns:
        The engine.
    """
    return _make_engine(tmp_path / "db.sqlite")


@pytest.fixture
def objects(tmp_path: Path) -> FlakyObjectStore:
    """Return the test object store.

    Args:
        tmp_path: Pytest's temporary folder.

    Returns:
        The store.
    """
    return FlakyObjectStore(tmp_path / "objects")


@pytest.fixture
def deleted() -> list[str]:
    """Return the list of keys that the test delete function deleted, in order.

    Returns:
        The empty list.
    """
    return []


def _deleter(root: Path, deleted: list[str]) -> Any:
    """Return a delete function over a local store folder that records each key.

    Args:
        root: The object store folder.
        deleted: The list that gets each deleted key.

    Returns:
        The delete function.
    """

    def delete_object(key: str) -> None:
        """Delete one local object; a missing object is not an error.

        Args:
            key: The object key.
        """
        deleted.append(key)
        (root / key).unlink(missing_ok=True)

    return delete_object


@pytest.fixture
def make_compactor(tmp_path: Path, engine: Engine, objects: FlakyObjectStore, deleted: list[str]) -> Any:
    """Return a factory of compactors with no minimum age and a 600 s grace time by default.

    A merge needs a grace time that is longer than the pass, so only the removal steps use ``grace_seconds=0``.

    Args:
        tmp_path: Pytest's temporary folder.
        engine: The test engine.
        objects: The test object store.
        deleted: The keys the delete function deleted.

    Returns:
        ``make(**options)`` that builds a compactor.
    """

    def make(**options: Any) -> TreeCompactor:
        """Build one compactor.

        Args:
            **options: Compactor options.

        Returns:
            The compactor.
        """
        options.setdefault("min_age_seconds", 0.0)
        options.setdefault("grace_seconds", 600.0)
        delete = options.pop("delete_object", _deleter(tmp_path / "objects", deleted))
        return TreeCompactor(engine, objects, delete, **options)

    return make


def _pass(make_compactor: Any) -> tuple[int, int]:
    """Run one pass whose removal steps have no grace time and whose merge has the default one.

    Args:
        make_compactor: The compactor factory.

    Returns:
        The number of packs merged and the number of packs deleted.
    """
    sweeper = make_compactor(grace_seconds=0.0)
    removed = sweeper.remove_retired() + sweeper.remove_unrecorded()
    return make_compactor().merge(), removed


@pytest.fixture
def store(engine: Engine, objects: FlakyObjectStore) -> Iterator[StepLogStore]:
    """Yield a step log store of worker ``w1`` that the test flushes by hand.

    Args:
        engine: The test engine.
        objects: The test object store.

    Yields:
        The store.
    """
    step_log = StepLogStore(engine, objects, "w1", flush_window_seconds=60.0)
    yield step_log
    step_log.close()


def _fill(step_log: StepLogStore, flushes: int = 3) -> dict[str, Any]:
    """Write one pack per flush; each holds a step and a tree of each run.

    Args:
        step_log: The step log.
        flushes: The number of flushes, and so of packs.

    Returns:
        The step bodies by step id and the tree entries by run and tree id.
    """
    trees = TreeStore(step_log)
    bodies: dict[str, Any] = {}
    tree_entries: dict[tuple[str, str], list[TreeEntry]] = {}
    for n in range(flushes):
        for run_id in RUNS:
            blob = trees.put_blob(run_id, f"{run_id} file {n}".encode())
            entries = [TreeEntry(f"file-{n}.txt", "file", blob)]
            tree_id = trees.put_tree(run_id, entries)
            tree_entries[(run_id, tree_id)] = entries
            body = {"command": f"echo {n}", "output": f"{run_id} {n}\n"}
            step_id = step_log.commit(
                run_id=run_id,
                agent_id="agent-a",
                seq=n,
                kind="command",
                execution_generation=1,
                grant_list_id="grants-1",
                body=body,
                tree_id=tree_id,
            )
            bodies[step_id] = body
        step_log.flush()
    return {"bodies": bodies, "trees": tree_entries}


def _assert_all_readable(step_log: StepLogStore, written: dict[str, Any]) -> None:
    """Check that every step and every tree with its blob reads back as written.

    Args:
        step_log: The step log to read through.
        written: What :func:`_fill` wrote.
    """
    trees = TreeStore(step_log)
    for step_id, body in written["bodies"].items():
        assert step_log.get_step(step_id)["body"] == body
    for (run_id, tree_id), entries in written["trees"].items():
        assert trees.get_tree(run_id, tree_id) == entries
        for entry in entries:
            assert trees.get_blob(run_id, entry.object_id) is not None


def _pack_keys_of_rows(engine: Engine) -> set[str]:
    """Return the pack keys that the step and tree object rows point at.

    Args:
        engine: The test engine.

    Returns:
        The pack keys.
    """
    with Session(engine) as session:
        steps = set(session.scalars(select(AgentStepModel.pack_key)))
        tree_objects = set(session.scalars(select(TreeObjectModel.pack_key)))
    return steps | tree_objects


def _pack_rows(engine: Engine) -> set[str]:
    """Return the keys of the ``storage_packs`` rows.

    Args:
        engine: The test engine.

    Returns:
        The pack keys.
    """
    with Session(engine) as session:
        return set(session.scalars(select(StoragePackModel.pack_key)))


def _rows_of_packs(engine: Engine) -> list[str]:
    """Return the pack key of each step and tree object row.

    Args:
        engine: The test engine.

    Returns:
        One pack key for each row.
    """
    with Session(engine) as session:
        return [*session.scalars(select(AgentStepModel.pack_key)), *session.scalars(select(TreeObjectModel.pack_key))]


def _without_pack(engine: Engine, written: dict[str, Any], pack_key: str) -> dict[str, Any]:
    """Leave out of what :func:`_fill` wrote the steps and trees that need a row of one pack.

    Args:
        engine: The test engine.
        written: What :func:`_fill` wrote.
        pack_key: The pack to leave out.

    Returns:
        The steps and trees that do not read from the pack.
    """
    with Session(engine) as session:
        steps = set(session.scalars(select(AgentStepModel.step_id).where(AgentStepModel.pack_key == pack_key)))
        object_ids = set(session.scalars(select(TreeObjectModel.object_id).where(TreeObjectModel.pack_key == pack_key)))
    return {
        "bodies": {step_id: body for step_id, body in written["bodies"].items() if step_id not in steps},
        "trees": {
            (run_id, tree_id): entries
            for (run_id, tree_id), entries in written["trees"].items()
            if tree_id not in object_ids and not any(entry.object_id in object_ids for entry in entries)
        },
    }


def test_merge_puts_old_packs_into_one_object_and_moves_every_row(engine, objects, store, make_compactor) -> None:
    """Three flush packs become one compacted pack; every step and tree reads from it."""
    written = _fill(store)
    old = set(objects.list("packs/w1/"))
    assert len(old) == 3

    assert make_compactor().merge() == 3

    compacted = objects.list(f"{COMPACTED_PREFIX}/")
    assert len(compacted) == 1
    assert _pack_keys_of_rows(engine) == set(compacted)
    assert _pack_rows(engine) == old | set(compacted)
    _assert_all_readable(store, written)


def test_the_merge_moves_the_rows_in_one_transaction(engine, store, make_compactor) -> None:
    """The merge commits once: the new pack row, the moved rows and the retire times."""
    _fill(store)
    commits: list[object] = []
    event.listen(engine, "commit", commits.append)

    make_compactor().merge()

    assert len(commits) == 1


def test_young_packs_are_not_merged(engine, objects, store, make_compactor) -> None:
    """A pack younger than the minimum age stays where it is."""
    _fill(store)

    assert make_compactor(min_age_seconds=3600).merge() == 0
    assert objects.list(f"{COMPACTED_PREFIX}/") == []


def test_a_lone_pack_is_not_merged(engine, objects, store, make_compactor) -> None:
    """One old pack alone has nothing to merge with."""
    _fill(store, flushes=1)

    assert make_compactor().merge() == 0
    assert objects.list(f"{COMPACTED_PREFIX}/") == []


def test_a_large_pack_is_not_merged(engine, objects, store, make_compactor) -> None:
    """A pack over half the target size stays as it is."""
    _fill(store, flushes=2)
    size = min(len(objects.get(key)) for key in objects.list("packs/w1/"))

    assert make_compactor(target_bytes=size).merge() == 0


def test_a_read_during_compaction_finds_each_tree_in_the_old_or_the_new_pack(
    engine, objects, store, make_compactor
) -> None:
    """Reads before the commit use the old packs; a row read before the move still reads until the grace ends."""
    written = _fill(store)
    with Session(engine) as session:
        before = [(row.pack_key, row.pack_offset, row.pack_length) for row in session.scalars(select(TreeObjectModel))]
    objects.on_compacted_put = lambda key: _assert_all_readable(store, written)

    compactor = make_compactor(grace_seconds=600)
    compactor.run_once()
    _assert_all_readable(store, written)
    assert compactor.run_once() == (0, 0)
    for key, offset, length in before:
        assert len(objects.get(key, offset, length)) == length

    assert make_compactor(grace_seconds=0.0).remove_retired() == 3
    _assert_all_readable(store, written)


def test_old_packs_are_removed_only_after_no_row_points_at_them(
    engine, objects, store, deleted, make_compactor
) -> None:
    """Each delete comes after the index moved; the retired packs and their rows go, the new pack stays."""
    written = _fill(store)
    old = set(objects.list("packs/w1/"))

    def delete_object(key: str) -> None:
        """Check that no row points at the pack, then delete it.

        Args:
            key: The pack key.
        """
        assert key not in _pack_keys_of_rows(engine)
        assert key in _pack_rows(engine)
        deleted.append(key)
        objects._path(key).unlink()

    remover = make_compactor(grace_seconds=0.0, delete_object=delete_object)
    assert remover.remove_retired() == 0
    make_compactor().merge()
    assert remover.remove_retired() == 3

    assert set(deleted) == old
    assert objects.list("packs/") == []
    assert _pack_rows(engine) == set(objects.list(f"{COMPACTED_PREFIX}/"))
    _assert_all_readable(store, written)


def test_a_kill_after_the_new_pack_put_loses_nothing_and_the_next_pass_finishes(
    engine, objects, store, make_compactor
) -> None:
    """The rows stay on the old packs; the next pass deletes the new pack with no row and merges again."""
    written = _fill(store)
    old = set(objects.list("packs/w1/"))
    objects.fail_compacted_put = True
    with pytest.raises(OSError):
        make_compactor().merge()
    objects.fail_compacted_put = False

    assert len(objects.list(f"{COMPACTED_PREFIX}/")) == 1
    assert _pack_keys_of_rows(engine) == old
    assert _pack_rows(engine) == old
    _assert_all_readable(store, written)

    assert _pass(make_compactor) == (3, 1)
    assert _pass(make_compactor) == (0, 3)
    assert objects.list("packs/") == []
    compacted = objects.list(f"{COMPACTED_PREFIX}/")
    assert len(compacted) == 1
    assert _pack_rows(engine) == set(compacted) == _pack_keys_of_rows(engine)
    _assert_all_readable(store, written)


def test_a_new_pack_with_no_row_stays_during_the_grace_time(engine, objects, store, make_compactor) -> None:
    """A new pack whose pass may still commit is not deleted before the grace time ends."""
    _fill(store)
    objects.fail_compacted_put = True
    with pytest.raises(OSError):
        make_compactor().merge()

    assert make_compactor(grace_seconds=600).remove_unrecorded() == 0
    assert len(objects.list(f"{COMPACTED_PREFIX}/")) == 1


def test_a_failure_after_the_row_moves_loses_nothing(engine, objects, store, make_compactor) -> None:
    """A failure after the rows moved and before the commit leaves every row and every old pack as it was."""
    written = _fill(store)
    old = set(objects.list("packs/w1/"))

    def fail(connection: object, cursor: object, statement: str, *args: object) -> None:
        """Fail the last statement of the merge, the retire time update, as a kill there does.

        Args:
            connection: The connection.
            cursor: The cursor.
            statement: The SQL text.
            *args: The other event arguments.

        Raises:
            RuntimeError: On the update of ``storage_packs``.
        """
        if statement.startswith("UPDATE storage_packs"):
            raise RuntimeError("killed before the commit")

    event.listen(engine, "before_cursor_execute", fail)
    with pytest.raises(RuntimeError):
        make_compactor().merge()
    event.remove(engine, "before_cursor_execute", fail)

    assert _pack_keys_of_rows(engine) == old
    assert _pack_rows(engine) == old
    _assert_all_readable(store, written)
    assert _pass(make_compactor) == (3, 1)
    _assert_all_readable(store, written)


def test_a_kill_during_the_removal_loses_nothing_and_the_next_pass_finishes(
    engine, objects, store, deleted, make_compactor
) -> None:
    """A kill after some old packs went keeps their rows; recovery adds nothing; the next pass ends the work."""
    written = _fill(store)
    make_compactor().merge()
    calls: list[str] = []

    def delete_then_fail(key: str) -> None:
        """Delete the first pack, then stop the pass as a kill does.

        Args:
            key: The pack key.

        Raises:
            SystemExit: On the second call; the removal catches only ``Exception``, as a kill is not one.
        """
        if calls:
            raise SystemExit("killed during the removal")
        calls.append(key)
        objects._path(key).unlink()

    with pytest.raises(SystemExit):
        make_compactor(grace_seconds=0.0, delete_object=delete_then_fail).remove_retired()
    assert len(objects.list("packs/w1/")) == 2
    assert len(_pack_rows(engine)) == 4
    recovering = StepLogStore(engine, objects, "w1", flush_window_seconds=60.0)
    try:
        assert recovering.recover() == 0
    finally:
        recovering.close()
    _assert_all_readable(store, written)

    assert make_compactor(grace_seconds=0.0).remove_retired() == 3
    assert objects.list("packs/") == []
    assert _pack_rows(engine) == set(objects.list(f"{COMPACTED_PREFIX}/"))
    _assert_all_readable(store, written)


def test_recovery_never_applies_a_compacted_pack(engine, objects, store, make_compactor) -> None:
    """After a merge and the removal, recovery of the worker applies nothing and the rows stay on the new pack."""
    written = _fill(store)
    make_compactor().merge()
    make_compactor(grace_seconds=0.0).remove_retired()
    rows_before = _pack_keys_of_rows(engine)

    recovering = StepLogStore(engine, objects, "w1", flush_window_seconds=60.0)
    try:
        assert recovering.recover() == 0
    finally:
        recovering.close()
    assert not any(key.startswith("packs/") for key in objects.list(f"{COMPACTED_PREFIX}/"))
    assert _pack_keys_of_rows(engine) == rows_before
    _assert_all_readable(store, written)


def test_a_pack_written_but_not_applied_is_left_for_its_flush(
    engine, objects, store, make_compactor, monkeypatch
) -> None:
    """A pack with no ``storage_packs`` row is not merged or deleted; its flush applies it later."""
    _fill(store, flushes=2)
    other = StepLogStore(engine, objects, "w2", flush_window_seconds=60.0)
    try:

        def fail_apply(*args: object) -> None:
            """Fail the transaction after the PUT.

            Args:
                *args: The apply arguments.

            Raises:
                RuntimeError: Always.
            """
            raise RuntimeError("transaction failed")

        monkeypatch.setattr(other, "_apply", fail_apply)
        step_id = other.commit(
            run_id="job-1",
            agent_id="agent-b",
            seq=1,
            kind="model",
            execution_generation=1,
            grant_list_id="grants-1",
            body={"response": "hi"},
        )
        with pytest.raises(RuntimeError):
            other.flush()
        (unapplied,) = objects.list("packs/w2/")

        assert make_compactor().run_once() == (2, 0)
        assert make_compactor(grace_seconds=0.0).run_once() == (0, 2)
        assert objects.list("packs/w2/") == [unapplied]

        monkeypatch.undo()
        other.flush()
        other.durable(step_id, 1, timeout=0)
        assert other.get_step(step_id)["body"] == {"response": "hi"}
    finally:
        other.close()


def test_a_second_compactor_does_not_merge_the_packs_again(engine, objects, store, make_compactor) -> None:
    """After one compactor merged the packs, a second one finds only the new pack and merges nothing."""
    written = _fill(store)
    first, second = make_compactor(grace_seconds=600), make_compactor(grace_seconds=600)
    assert first.merge() == 3
    assert second.merge() == 0
    assert len(_pack_keys_of_rows(engine)) == 1
    _assert_all_readable(store, written)


def test_a_merge_that_another_pass_finished_first_moves_nothing(engine, objects, store, make_compactor) -> None:
    """A second pass merges the same packs during the first one's PUT; the first one moves no row and drops its pack."""
    written = _fill(store)
    results: list[int] = []

    def merge_first(key: str) -> None:
        """Run the whole second merge once, while the first one is between its PUT and its transaction.

        Args:
            key: The first merge's new pack key.
        """
        objects.on_compacted_put = None
        results.append(make_compactor().merge())

    objects.on_compacted_put = merge_first
    results.append(make_compactor().merge())

    assert results == [3, 0]
    (compacted,) = objects.list(f"{COMPACTED_PREFIX}/")
    assert _pack_keys_of_rows(engine) == {compacted}
    assert compacted in _pack_rows(engine)
    _assert_all_readable(store, written)


def test_the_merge_holds_no_connection_during_the_object_io(engine, objects, store, make_compactor) -> None:
    """The GETs and the PUT run with no database connection checked out, so no transaction stays open."""
    _fill(store)
    checked_out: list[int] = []
    objects.on_compacted_put = lambda key: checked_out.append(engine.pool.checkedout())

    assert make_compactor().merge() == 3
    assert checked_out == [0]


def test_a_merge_that_takes_half_the_grace_time_gives_up(engine, objects, store, make_compactor) -> None:
    """A merge that reaches its commit after half the grace time rolls back, deletes its new pack and raises."""
    written = _fill(store)
    old = set(objects.list("packs/w1/"))
    objects.on_compacted_put = lambda key: time.sleep(0.15)

    with pytest.raises(TimeoutError):
        make_compactor(grace_seconds=0.2).merge()

    assert objects.list(f"{COMPACTED_PREFIX}/") == []
    assert _pack_keys_of_rows(engine) == old
    assert _pack_rows(engine) == old
    _assert_all_readable(store, written)


def test_the_retire_time_is_the_commit_time(engine, objects, store, make_compactor) -> None:
    """The old packs and the new pack get a ``created_at`` from after the PUT, not from the start of the merge."""
    _fill(store)
    put_at: list[datetime] = []
    objects.on_compacted_put = lambda key: put_at.append(datetime.now(UTC).replace(tzinfo=None))

    make_compactor().merge()

    with Session(engine) as session:
        times = [created_at.replace(tzinfo=None) for created_at in session.scalars(select(StoragePackModel.created_at))]
    assert len(times) == 4
    assert min(times) >= put_at[0]


def test_old_packs_get_the_full_grace_time_after_the_merge(engine, store, make_compactor) -> None:
    """Packs that were written two hours ago are not deleted at once after the merge retires them."""
    _fill(store)
    with Session(engine) as session:
        session.execute(update(StoragePackModel).values(created_at=datetime.now(UTC) - timedelta(hours=2)))
        session.commit()
    compactor = make_compactor(grace_seconds=600)

    assert compactor.merge() == 3
    assert compactor.remove_retired() == 0


def test_a_failed_removal_does_not_skip_the_merge(engine, objects, store, make_compactor) -> None:
    """A pass whose removal steps fail still merges."""
    written = _fill(store)
    compactor = make_compactor()

    def fail() -> int:
        """Fail as a broken LIST or delete does.

        Raises:
            OSError: Always.
        """
        raise OSError("the object store is down")

    compactor.remove_retired = fail
    compactor.remove_unrecorded = fail

    assert compactor.run_once() == (3, 0)
    _assert_all_readable(store, written)


def test_remove_unrecorded_reads_no_rows_for_young_keys(engine, objects, store, make_compactor) -> None:
    """New packs younger than the grace time do not go into the database query."""
    _fill(store)
    objects.fail_compacted_put = True
    with pytest.raises(OSError):
        make_compactor().merge()
    statements: list[str] = []

    def record(connection: object, cursor: object, statement: str, *args: object) -> None:
        """Record each SQL statement.

        Args:
            connection: The connection.
            cursor: The cursor.
            statement: The SQL text.
            *args: The other event arguments.
        """
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    assert make_compactor().remove_unrecorded() == 0
    event.remove(engine, "before_cursor_execute", record)

    assert statements == []


def _record_statements(engine: Engine) -> list[tuple[str, bool]]:
    """Record each SQL statement that the engine runs from now on, with its executemany flag.

    Args:
        engine: The test engine.

    Returns:
        The list that gets each statement and flag.
    """
    statements: list[tuple[str, bool]] = []

    def record(
        connection: object, cursor: object, statement: str, parameters: object, context: object, executemany: bool
    ) -> None:
        """Record one statement.

        Args:
            connection: The connection.
            cursor: The cursor.
            statement: The SQL text.
            parameters: The parameters.
            context: The execution context.
            executemany: Whether the statement runs once for each parameter set.
        """
        statements.append((statement, executemany))

    event.listen(engine, "before_cursor_execute", record)
    return statements


def test_remove_retired_holds_no_connection_during_the_deletes(engine, objects, store, make_compactor) -> None:
    """Each object delete runs with no database connection checked out, so no transaction stays open."""
    _fill(store)
    make_compactor().merge()
    checked_out: list[int] = []

    def delete_object(key: str) -> None:
        """Record the checked-out connections, then delete the object.

        Args:
            key: The pack key.
        """
        checked_out.append(engine.pool.checkedout())
        objects._path(key).unlink()

    assert make_compactor(grace_seconds=0.0, delete_object=delete_object).remove_retired() == 3
    assert checked_out == [0, 0, 0]
    assert objects.list("packs/") == []
    assert _pack_rows(engine) == set(objects.list(f"{COMPACTED_PREFIX}/"))


def test_the_merge_moves_the_rows_with_one_statement_for_each_chunk(
    engine, objects, store, make_compactor, monkeypatch
) -> None:
    """The rows move in one UPDATE for each chunk, with no executemany and no count query."""
    written = _fill(store)
    with Session(engine) as session:
        step_rows = len(session.scalars(select(AgentStepModel.step_id)).all())
        object_rows = len(session.scalars(select(TreeObjectModel.object_id)).all())
    monkeypatch.setattr(tree_compactor, "CHUNK_SIZE", 4)
    statements = _record_statements(engine)

    assert make_compactor().merge() == 3

    assert not any(executemany for _, executemany in statements)
    assert not any("count(" in statement.lower() for statement, _ in statements)
    assert sum(statement.startswith("UPDATE agent_steps") for statement, _ in statements) == -(-step_rows // 4)
    assert sum(statement.startswith("UPDATE tree_objects") for statement, _ in statements) == -(-object_rows // 4)
    _assert_all_readable(store, written)


def _rows_per_pack(engine: Engine) -> int:
    """Return the number of rows in each pack that :func:`_fill` wrote; every pack has the same number.

    Args:
        engine: The test engine.

    Returns:
        The row count of one pack.
    """
    with Session(engine) as session:
        keys = [*session.scalars(select(AgentStepModel.pack_key)), *session.scalars(select(TreeObjectModel.pack_key))]
    (count,) = set(Counter(keys).values())
    return count


def test_the_row_cap_ends_the_pass_early(engine, objects, store, make_compactor, monkeypatch) -> None:
    """A pass stops adding packs when the next pack does not fit under the row cap."""
    written = _fill(store)
    monkeypatch.setattr(tree_compactor, "MAX_ROWS_PER_PASS", 2 * _rows_per_pack(engine))

    assert make_compactor().merge() == 2
    assert len(_pack_keys_of_rows(engine)) == 2
    _assert_all_readable(store, written)


def test_two_packs_over_the_row_cap_are_not_merged(engine, objects, store, make_compactor, monkeypatch) -> None:
    """When no two packs fit under the row cap together, the pass merges nothing and writes no pack."""
    _fill(store)
    old = _pack_keys_of_rows(engine)
    monkeypatch.setattr(tree_compactor, "MAX_ROWS_PER_PASS", 2 * _rows_per_pack(engine) - 1)

    assert make_compactor().merge() == 0
    assert objects.list(f"{COMPACTED_PREFIX}/") == []
    assert _pack_keys_of_rows(engine) == old


def test_a_pack_at_the_row_cap_is_full(engine, objects, store, make_compactor, monkeypatch) -> None:
    """The oldest pack has the cap's rows but no full mark; the pass skips it and merges the next two."""
    written = _fill(store, flushes=4)
    monkeypatch.setattr(tree_compactor, "MAX_ROWS_PER_PASS", 2 * _rows_per_pack(engine))
    assert make_compactor().merge() == 2
    (full,) = objects.list(f"{COMPACTED_PREFIX}/")
    with Session(engine) as session:
        session.execute(
            update(StoragePackModel)
            .where(StoragePackModel.pack_key == full)
            .values(created_at=datetime.now(UTC) - timedelta(hours=2), worker_id=COMPACTOR_ID)
        )
        session.commit()

    assert make_compactor().merge() == 2
    assert full in _pack_keys_of_rows(engine)
    assert len(_pack_keys_of_rows(engine)) == 2
    _assert_all_readable(store, written)


def test_full_packs_do_not_fill_the_candidate_window(engine, objects, store, make_compactor, monkeypatch) -> None:
    """More full packs than the window holds are older than two small packs; a pass still merges the small ones."""
    written = _fill(store, flushes=8)
    monkeypatch.setattr(tree_compactor, "MAX_ROWS_PER_PASS", 2 * _rows_per_pack(engine))
    for _ in range(3):
        assert make_compactor().merge() == 2
    full = set(objects.list(f"{COMPACTED_PREFIX}/"))
    with Session(engine) as session:
        session.execute(
            update(StoragePackModel)
            .where(StoragePackModel.pack_key.in_(full))
            .values(created_at=datetime.now(UTC) - timedelta(hours=2))
        )
        session.commit()
        marks = set(session.scalars(select(StoragePackModel.worker_id).where(StoragePackModel.pack_key.in_(full))))
    assert marks == {FULL_COMPACTOR_ID}
    monkeypatch.setattr(tree_compactor, "MAX_PACKS_PER_PASS", 2)

    assert make_compactor().merge() == 2

    assert len(_pack_keys_of_rows(engine)) == 4
    assert make_compactor(grace_seconds=0.0).remove_unrecorded() == 0
    assert full < set(objects.list(f"{COMPACTED_PREFIX}/"))
    recovering = StepLogStore(engine, objects, "w1", flush_window_seconds=60.0)
    try:
        assert recovering.recover() == 0
    finally:
        recovering.close()
    assert len(_pack_keys_of_rows(engine)) == 4
    _assert_all_readable(store, written)


def test_a_pass_never_goes_past_the_target_bytes(engine, objects, store, make_compactor) -> None:
    """A pack that would take the new pack past the target stays out of the pass."""
    written = _fill(store)
    with Session(engine) as session:
        total = sum(session.scalars(select(StoragePackModel.byte_size)))

    assert make_compactor(target_bytes=total - 1).merge() == 2
    _assert_all_readable(store, written)


def _fill_sizes(step_log: StepLogStore, paddings: list[int]) -> list[tuple[str, int]]:
    """Write one pack per padding, each with one step whose body holds that many random bytes.

    Args:
        step_log: The step log.
        paddings: The body padding of each pack, oldest first.

    Returns:
        Each pack key with its byte size, oldest first.
    """
    for n, padding in enumerate(paddings):
        step_log.commit(
            run_id=RUNS[0],
            agent_id="agent-a",
            seq=n,
            kind="model",
            execution_generation=1,
            grant_list_id="grants-1",
            body={"output": os.urandom(padding).hex()},
        )
        step_log.flush()
    with Session(step_log._engine) as session:
        return [
            (row.pack_key, row.byte_size)
            for row in session.execute(
                select(StoragePackModel.pack_key, StoragePackModel.byte_size).order_by(
                    StoragePackModel.created_at, StoragePackModel.pack_key
                )
            )
        ]


def test_an_old_large_pack_does_not_stop_the_newer_merges(engine, objects, store, make_compactor) -> None:
    """The oldest pack has no room for one more pack; the pass still merges the three newer small packs."""
    packs = _fill_sizes(store, [4000, 300, 300, 300])
    (large, large_size), *small = packs
    target = large_size + min(size for _, size in small) - 1
    assert 3 * max(size for _, size in small) <= target

    assert make_compactor(target_bytes=target).merge() == 3
    (compacted,) = objects.list(f"{COMPACTED_PREFIX}/")
    assert _pack_keys_of_rows(engine) == {large, compacted}


def test_a_later_smaller_pack_fills_the_target_exactly(engine, objects, store, make_compactor) -> None:
    """A pack that does not fit is skipped, and a later smaller pack that brings the total to the target joins."""
    packs = _fill_sizes(store, [1000, 1000, 600, 300])
    (_, size0), (_, size1), (skipped, size2), (_, size3) = packs
    assert size2 > size3
    target = size0 + size1 + size3

    assert make_compactor(target_bytes=target).merge() == 3
    (compacted,) = objects.list(f"{COMPACTED_PREFIX}/")
    assert _pack_keys_of_rows(engine) == {skipped, compacted}


def test_a_failed_get_leaves_the_pack_unmarked(engine, objects, store, make_compactor, monkeypatch) -> None:
    """A GET that fails with an error other than a missing object fails the merge and marks no pack."""
    _fill(store)
    failing = min(objects.list("packs/w1/"))
    get = objects.get

    def flaky_get(key: str, *args: Any) -> bytes:
        """Fail for one pack, as a network error does.

        Args:
            key: The object key.
            *args: The range arguments.

        Returns:
            The object bytes.

        Raises:
            OSError: For the failing pack.
        """
        if key == failing:
            raise OSError("connection reset")
        return get(key, *args)

    monkeypatch.setattr(objects, "get", flaky_get)

    with pytest.raises(OSError):
        make_compactor().merge()
    assert objects.list(f"{COMPACTED_PREFIX}/") == []
    with Session(engine) as session:
        assert session.get(StoragePackModel, failing).worker_id == "w1"


def test_an_unrecorded_delete_that_always_fails_does_not_stop_the_others(engine, objects, make_compactor) -> None:
    """One unrecorded pack whose delete fails stays; the unrecorded packs after it go."""
    stuck, other = (f"{COMPACTED_PREFIX}/{n:020d}-a.pack" for n in (1, 2))
    objects.put(stuck, b"x")
    objects.put(other, b"x")

    def delete_object(key: str) -> None:
        """Delete the object, but fail for one key.

        Args:
            key: The pack key.

        Raises:
            OSError: For the stuck key.
        """
        if key == stuck:
            raise OSError("access denied")
        objects._path(key).unlink()

    assert make_compactor(grace_seconds=0.0, delete_object=delete_object).remove_unrecorded() == 1
    assert objects.list(f"{COMPACTED_PREFIX}/") == [stuck]


def test_a_missing_pack_leaves_the_pass_and_the_candidates(engine, objects, store, make_compactor, monkeypatch) -> None:
    """The oldest pack has no object; the pass keeps its rows on it, and the next pass merges the other two."""
    _fill(store)
    missing = min(objects.list("packs/w1/"))
    objects._path(missing).unlink()
    monkeypatch.setattr(tree_compactor, "MAX_PACKS_PER_PASS", 2)

    assert make_compactor().merge() == 0
    assert objects.list(f"{COMPACTED_PREFIX}/") == []
    assert make_compactor().merge() == 2

    with Session(engine) as session:
        assert session.get(StoragePackModel, missing).worker_id == BROKEN_COMPACTOR_ID
        assert session.scalars(select(AgentStepModel.pack_key).where(AgentStepModel.pack_key == missing)).all()
    assert len(_pack_keys_of_rows(engine)) == 2


def test_a_missing_pack_in_the_window_leaves_the_other_packs_merged(engine, objects, store, make_compactor) -> None:
    """The last pack by key has no object; the pass merges the other two and keeps every row of it on it."""
    written = _fill(store)
    missing = max(objects.list("packs/w1/"))
    objects._path(missing).unlink()
    with Session(engine) as session:
        missing_rows = len(
            session.scalars(select(AgentStepModel.step_id).where(AgentStepModel.pack_key == missing)).all()
        )
        missing_rows += len(
            session.scalars(select(TreeObjectModel.object_id).where(TreeObjectModel.pack_key == missing)).all()
        )
    assert missing_rows >= 2

    assert make_compactor().merge() == 2

    (compacted,) = objects.list(f"{COMPACTED_PREFIX}/")
    assert Counter(_rows_of_packs(engine)) == {missing: missing_rows, compacted: 2 * missing_rows}
    _assert_all_readable(store, _without_pack(engine, written, missing))


def test_a_short_pack_leaves_the_pass_and_shifts_no_offsets(engine, objects, store, make_compactor) -> None:
    """A pack object shorter than its rows is broken; its rows stay on it, and the other two packs read back."""
    written = _fill(store)
    short = min(objects.list("packs/w1/"))
    with Session(engine) as session:
        short_rows = len(session.scalars(select(AgentStepModel.step_id).where(AgentStepModel.pack_key == short)).all())
        last = max(
            *session.scalars(select(AgentStepModel.pack_offset).where(AgentStepModel.pack_key == short)),
            *session.scalars(select(TreeObjectModel.pack_offset).where(TreeObjectModel.pack_key == short)),
        )
    # Cut inside the last row, so the rows before it copy in full and must be taken back out.
    objects.put(short, objects.get(short)[: last + 1])

    assert make_compactor().merge() == 2

    with Session(engine) as session:
        assert session.get(StoragePackModel, short).worker_id == BROKEN_COMPACTOR_ID
        assert (
            len(session.scalars(select(AgentStepModel.step_id).where(AgentStepModel.pack_key == short)).all())
            == short_rows
        )
    _assert_all_readable(store, _without_pack(engine, written, short))


def test_slow_gets_do_not_use_the_half_grace_limit(engine, objects, store, make_compactor, monkeypatch) -> None:
    """The GETs together take longer than half the grace time; the merge still commits."""
    written = _fill(store)
    get = objects.get

    def slow_get(key: str, *args: Any) -> bytes:
        """Wait before a whole-object read, as the merge does, then read the object.

        Args:
            key: The object key.
            *args: The range arguments.

        Returns:
            The object bytes.
        """
        if not args:
            time.sleep(0.4)
        return get(key, *args)

    monkeypatch.setattr(objects, "get", slow_get)

    assert make_compactor(grace_seconds=1.0).merge() == 3
    _assert_all_readable(store, written)


def test_a_lock_error_other_than_lock_not_available_is_raised(engine, objects, store, make_compactor) -> None:
    """An OperationalError at the lock step that is not 55P03 leaves the merge as an error, and no row moves."""
    _fill(store)
    old = _pack_keys_of_rows(engine)

    def fail_the_job_lock(connection: object, cursor: object, statement: str, *args: object) -> None:
        """Fail the job lock query as a lost connection does.

        Args:
            connection: The connection.
            cursor: The cursor.
            statement: The SQL text.
            *args: The other event arguments.

        Raises:
            OperationalError: For the job lock query.
        """
        if "FROM jobs" in statement:
            raise OperationalError(statement, {}, Exception("server closed the connection"))

    event.listen(engine, "before_cursor_execute", fail_the_job_lock)
    try:
        with pytest.raises(OperationalError):
            make_compactor().merge()
    finally:
        event.remove(engine, "before_cursor_execute", fail_the_job_lock)
    assert _pack_keys_of_rows(engine) == old


@pytest.mark.parametrize(("extra", "mark"), [(0, COMPACTOR_ID), (-1, FULL_COMPACTOR_ID)])
def test_the_full_mark_needs_more_than_half_the_row_cap(
    engine, objects, store, make_compactor, monkeypatch, extra, mark
) -> None:
    """A merge of exactly half the row cap is not full; one row over half is."""
    _fill(store, flushes=2)
    moved = 2 * _rows_per_pack(engine)
    monkeypatch.setattr(tree_compactor, "MAX_ROWS_PER_PASS", 2 * moved + extra)

    assert make_compactor().merge() == 2
    (compacted,) = objects.list(f"{COMPACTED_PREFIX}/")
    with Session(engine) as session:
        assert session.get(StoragePackModel, compacted).worker_id == mark


def test_the_retire_time_is_set_in_chunks(engine, objects, store, make_compactor, monkeypatch) -> None:
    """The retire time of the old packs is set with one UPDATE for each chunk of keys."""
    written = _fill(store)
    monkeypatch.setattr(tree_compactor, "CHUNK_SIZE", 2)
    statements = _record_statements(engine)

    assert make_compactor().merge() == 3

    assert sum(statement.startswith("UPDATE storage_packs") for statement, _ in statements) == 2
    _assert_all_readable(store, written)


def test_a_delete_that_always_fails_does_not_stop_the_removal(engine, objects, store, make_compactor) -> None:
    """One retired pack whose delete fails keeps its row; the other retired packs and their rows go."""
    _fill(store)
    make_compactor().merge()
    stuck = min(objects.list("packs/w1/"))

    def delete_object(key: str) -> None:
        """Delete the object, but fail for one key.

        Args:
            key: The pack key.

        Raises:
            OSError: For the stuck key.
        """
        if key == stuck:
            raise OSError("access denied")
        objects._path(key).unlink()

    assert make_compactor(grace_seconds=0.0, delete_object=delete_object).remove_retired() == 2
    assert objects.list("packs/w1/") == [stuck]
    assert _pack_rows(engine) == {stuck, *objects.list(f"{COMPACTED_PREFIX}/")}


def test_packs_merged_after_the_candidate_read_write_no_empty_pack(engine, objects, store, make_compactor) -> None:
    """Another pass merges the packs between this pass's candidate read and row read; this pass writes nothing."""
    written = _fill(store)
    old = set(objects.list("packs/w1/"))
    results: list[int] = []

    def merge_first(connection: object, cursor: object, statement: str, *args: object) -> None:
        """Run the whole second merge once, just before the first row read.

        Args:
            connection: The connection.
            cursor: The cursor.
            statement: The SQL text.
            *args: The other event arguments.
        """
        if statement.startswith("SELECT agent_steps") and not results:
            results.append(-1)
            results.append(make_compactor().merge())

    event.listen(engine, "before_cursor_execute", merge_first)
    first = make_compactor().merge()
    event.remove(engine, "before_cursor_execute", merge_first)

    assert (first, results[1]) == (0, 3)
    (compacted,) = objects.list(f"{COMPACTED_PREFIX}/")
    assert _pack_rows(engine) == old | {compacted}
    _assert_all_readable(store, written)


def test_remove_unrecorded_skips_a_key_with_no_write_time(engine, objects, make_compactor) -> None:
    """A key under the prefix that does not start with a write time is kept; the other orphans still go."""
    bad = f"{COMPACTED_PREFIX}/not-a-pack"
    orphan = f"{COMPACTED_PREFIX}/{1:020d}-a.pack"
    objects.put(bad, b"x")
    objects.put(orphan, b"x")

    assert make_compactor(grace_seconds=0.0).remove_unrecorded() == 1
    assert objects.list(f"{COMPACTED_PREFIX}/") == [bad]


def test_remove_unrecorded_queries_the_keys_in_chunks(engine, objects, store, make_compactor, monkeypatch) -> None:
    """The recorded check runs one query for each chunk of old keys and keeps the recorded pack."""
    _fill(store)
    make_compactor().merge()
    (recorded,) = objects.list(f"{COMPACTED_PREFIX}/")
    orphans = [f"{COMPACTED_PREFIX}/{n:020d}-a.pack" for n in range(1, 5)]
    for key in orphans:
        objects.put(key, b"x")
    monkeypatch.setattr(tree_compactor, "CHUNK_SIZE", 2)
    statements = _record_statements(engine)

    assert make_compactor(grace_seconds=0.0).remove_unrecorded() == 4

    assert sum("FROM storage_packs" in statement for statement, _ in statements) == 3
    assert objects.list(f"{COMPACTED_PREFIX}/") == [recorded]


def _kill_child(database: str, root: str, marker: str) -> None:
    """Run a merge that hangs inside its commit until the parent kills the process.

    Args:
        database: The SQLite file.
        root: The object store folder.
        marker: The file the child writes at the commit.
    """
    engine = create_engine(f"sqlite:///{database}")

    def hang(connection: object) -> None:
        """Signal the parent with the moves written but not committed, then hang.

        Args:
            connection: The committing connection.
        """
        Path(marker).write_text("commit")
        threading.Event().wait()

    event.listen(engine, "commit", hang)
    TreeCompactor(engine, LocalObjectStore(root), lambda key: None, min_age_seconds=0.0).merge()


def test_a_sigkill_inside_the_merge_commit_loses_nothing(tmp_path, engine, objects, store, make_compactor) -> None:
    """A real kill at the commit leaves every row on its old pack; the next pass finishes the merge."""
    written = _fill(store)
    old = set(objects.list("packs/w1/"))
    marker = tmp_path / "marker"
    child = mp.get_context("spawn").Process(
        target=_kill_child, args=(str(tmp_path / "db.sqlite"), str(tmp_path / "objects"), str(marker))
    )
    child.start()
    deadline = time.monotonic() + 60
    while not marker.exists() and child.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert marker.exists(), f"the child stopped before the commit (exit code {child.exitcode})"
    child.kill()
    child.join(30)
    assert child.exitcode == -9

    assert _pack_keys_of_rows(engine) == old
    _assert_all_readable(store, written)
    assert _pass(make_compactor) == (3, 1)
    assert _pass(make_compactor) == (0, 3)
    assert _pack_rows(engine) == set(objects.list(f"{COMPACTED_PREFIX}/")) == _pack_keys_of_rows(engine)
    _assert_all_readable(store, written)


def test_start_tree_compactor_needs_a_bucket(engine, monkeypatch) -> None:
    """With no bucket set, the worker starts no compactor."""
    monkeypatch.setattr(settings, "tree_bucket", None)

    assert start_tree_compactor(engine) is None


def test_start_tree_compactor_deletes_through_the_bucket(engine, monkeypatch) -> None:
    """With a bucket set, the compactor runs over S3 and deletes with ``delete_object`` in that bucket."""
    client = MagicMock()
    monkeypatch.setattr(settings, "tree_bucket", "trees-test")
    monkeypatch.setattr(tree_compactor, "boto3", types.SimpleNamespace(client=lambda name: client))

    compactor = start_tree_compactor(engine)
    try:
        assert isinstance(compactor._objects, S3ObjectStore)
        compactor._delete_object("packs/w1/x.pack")
    finally:
        compactor.stop()
    client.delete_object.assert_called_once_with(Bucket="trees-test", Key="packs/w1/x.pack")


@pytest.fixture
def postgres_engine() -> Iterator[Engine]:
    """Yield an engine on a fresh schema of the disposable test database, then drop the schema.

    Yields:
        The engine, with the schema and the three test jobs.
    """
    schema = f"compactor_{uuid.uuid4().hex[:10]}"
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
            for run_id, username in zip(RUNS, ("ada", "ada", "bob"), strict=True):
                session.add(JobModel(optimization_id=run_id, username=username, execution_generation=1))
            session.commit()
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def postgres_store(postgres_engine: Engine, objects: FlakyObjectStore) -> Iterator[StepLogStore]:
    """Yield a step log store of worker ``w1`` on PostgreSQL that the test flushes by hand.

    Args:
        postgres_engine: The PostgreSQL engine.
        objects: The test object store.

    Yields:
        The store.
    """
    step_log = StepLogStore(postgres_engine, objects, "w1", flush_window_seconds=60.0)
    yield step_log
    step_log.close()


@needs_postgres
def test_postgres_two_merges_at_once_do_not_overlap(tmp_path, postgres_engine, objects, postgres_store) -> None:
    """Two merges on two connections wait for each other at the PUT; one wins, the other moves nothing."""
    written = _fill(postgres_store)
    both_put = threading.Barrier(2, timeout=30)
    objects.on_compacted_put = lambda key: both_put.wait()
    results: list[int] = []

    def merge() -> None:
        """Run one merge on its own connection."""
        compactor = TreeCompactor(postgres_engine, objects, _deleter(tmp_path / "objects", []), min_age_seconds=0.0)
        results.append(compactor.merge())

    threads = [threading.Thread(target=merge) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)

    assert sorted(results) == [0, 3]
    (compacted,) = objects.list(f"{COMPACTED_PREFIX}/")
    assert _pack_keys_of_rows(postgres_engine) == {compacted}
    _assert_all_readable(postgres_store, written)


@needs_postgres
def test_postgres_a_merge_does_not_wait_for_a_locked_pack(tmp_path, postgres_engine, objects, postgres_store) -> None:
    """A merge that finds one of its packs locked by another pass gives up at once and keeps every row."""
    written = _fill(postgres_store)
    old = set(objects.list("packs/w1/"))
    results: list[int] = []
    compactor = TreeCompactor(postgres_engine, objects, _deleter(tmp_path / "objects", []), min_age_seconds=0.0)
    with postgres_engine.connect() as holder:
        holder.execute(
            select(StoragePackModel.pack_key).where(StoragePackModel.pack_key == min(old)).with_for_update()
        ).all()
        thread = threading.Thread(target=lambda: results.append(compactor.merge()))
        thread.start()
        thread.join(10)
        waited = thread.is_alive()
        holder.rollback()
    thread.join(30)

    assert not waited
    assert results == [0]
    assert objects.list(f"{COMPACTED_PREFIX}/") == []
    assert _pack_keys_of_rows(postgres_engine) == old
    _assert_all_readable(postgres_store, written)


@needs_postgres
def test_postgres_the_merge_locks_jobs_then_packs_in_key_order_with_nowait(
    tmp_path, postgres_engine, objects, postgres_store
) -> None:
    """The merge locks the jobs and then the packs, each sorted by key and with NOWAIT, so two passes cannot split them."""
    _fill(postgres_store)
    statements = _record_statements(postgres_engine)
    compactor = TreeCompactor(postgres_engine, objects, _deleter(tmp_path / "objects", []), min_age_seconds=0.0)

    assert compactor.merge() == 3

    locks = [statement for statement, _ in statements if "NOWAIT" in statement]
    assert len(locks) == 2
    assert "FROM jobs" in locks[0]
    assert "ORDER BY jobs.optimization_id FOR KEY SHARE NOWAIT" in locks[0]
    assert "FROM storage_packs" in locks[1]
    assert "ORDER BY storage_packs.pack_key FOR UPDATE NOWAIT" in locks[1]


@needs_postgres
def test_postgres_a_merge_does_not_wait_for_a_locked_job(tmp_path, postgres_engine, objects, postgres_store) -> None:
    """A job row that another transaction holds, as a job delete does, makes the merge give up at once."""
    written = _fill(postgres_store)
    old = set(objects.list("packs/w1/"))
    results: list[int] = []
    compactor = TreeCompactor(postgres_engine, objects, _deleter(tmp_path / "objects", []), min_age_seconds=0.0)
    with postgres_engine.connect() as holder:
        holder.execute(select(JobModel.optimization_id).where(JobModel.optimization_id == RUNS[1]).with_for_update())
        thread = threading.Thread(target=lambda: results.append(compactor.merge()))
        thread.start()
        thread.join(10)
        waited = thread.is_alive()
        holder.rollback()
    thread.join(30)

    assert not waited
    assert results == [0]
    assert objects.list(f"{COMPACTED_PREFIX}/") == []
    assert _pack_keys_of_rows(postgres_engine) == old
    _assert_all_readable(postgres_store, written)


@needs_postgres
def test_postgres_a_held_merge_blocks_a_job_delete_but_not_a_job_update(
    tmp_path, postgres_engine, objects, postgres_store
) -> None:
    """While a merge holds its job locks, a non-key job UPDATE finishes at once and a job DELETE waits."""
    written = _fill(postgres_store)
    at_move = threading.Event()
    release = threading.Event()

    def hold_at_move(
        connection: object, cursor: object, statement: str, parameters: object, context: object, executemany: bool
    ) -> None:
        """Stop the merge at its first row move until the test releases it.

        Args:
            connection: The connection.
            cursor: The cursor.
            statement: The SQL text.
            parameters: The parameters.
            context: The execution context.
            executemany: Whether the statement runs once for each parameter set.
        """
        if statement.startswith("UPDATE agent_steps") and not at_move.is_set():
            at_move.set()
            release.wait(30)

    event.listen(postgres_engine, "before_cursor_execute", hold_at_move)
    results: list[int] = []
    compactor = TreeCompactor(postgres_engine, objects, _deleter(tmp_path / "objects", []), min_age_seconds=0.0)
    thread = threading.Thread(target=lambda: results.append(compactor.merge()))
    thread.start()
    try:
        assert at_move.wait(30)
        with postgres_engine.connect() as other:
            other.execute(text("SET lock_timeout = '200ms'"))
            updated = other.execute(
                update(JobModel).where(JobModel.optimization_id == RUNS[0]).values(execution_generation=2)
            )
            assert updated.rowcount == 1
            other.rollback()
            other.execute(text("SET lock_timeout = '200ms'"))
            with pytest.raises(OperationalError) as blocked:
                other.execute(text("DELETE FROM jobs WHERE optimization_id = :run"), {"run": RUNS[0]})
            assert blocked.value.orig.pgcode == "55P03"
            other.rollback()
    finally:
        release.set()
        thread.join(30)
        event.remove(postgres_engine, "before_cursor_execute", hold_at_move)

    assert results == [3]
    _assert_all_readable(postgres_store, written)
