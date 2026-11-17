"""Tests for the tree store compactor: merge small packs, keep every row readable, survive a kill.

The tests use a SQLite file and the local-filesystem object store. The kill
points are a raised error (the transaction rolls back, as after a kill) and
one real SIGKILL of a child process inside the merge commit.
"""

from __future__ import annotations

import multiprocessing as mp
import threading
import time
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from core.config import settings
from core.storage import tree_compactor
from core.storage.models import AgentStepModel, Base, JobModel, StoragePackModel, TreeObjectModel
from core.storage.object_store import LocalObjectStore, S3ObjectStore
from core.storage.step_log_store import StepLogStore
from core.storage.tree_compactor import COMPACTED_PREFIX, TreeCompactor, start_tree_compactor
from core.storage.tree_store import TreeEntry, TreeStore

RUNS = ("job-1", "job-2", "job-3")


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
    """Return a factory of compactors with no minimum age and no grace time by default.

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
        options.setdefault("grace_seconds", 0.0)
        delete = options.pop("delete_object", _deleter(tmp_path / "objects", deleted))
        return TreeCompactor(engine, objects, delete, **options)

    return make


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
    """A pack at the target size or above stays as it is."""
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

    assert make_compactor().remove_retired() == 3
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

    compactor = make_compactor(delete_object=delete_object)
    assert compactor.remove_retired() == 0
    compactor.merge()
    assert compactor.remove_retired() == 3

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

    compactor = make_compactor()
    assert compactor.run_once() == (3, 1)
    assert compactor.run_once() == (0, 3)
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
    assert make_compactor().run_once() == (3, 1)
    _assert_all_readable(store, written)


def test_a_kill_during_the_removal_loses_nothing_and_the_next_pass_finishes(
    engine, objects, store, deleted, make_compactor
) -> None:
    """A kill after some old packs went keeps their rows; recovery adds nothing; the next pass ends the work."""
    written = _fill(store)
    make_compactor().merge()
    calls: list[str] = []

    def delete_then_fail(key: str) -> None:
        """Delete the first pack, then fail.

        Args:
            key: The pack key.

        Raises:
            OSError: On the second call.
        """
        if calls:
            raise OSError("killed during the removal")
        calls.append(key)
        objects._path(key).unlink()

    with pytest.raises(OSError):
        make_compactor(delete_object=delete_then_fail).remove_retired()
    assert len(objects.list("packs/w1/")) == 2
    assert len(_pack_rows(engine)) == 4
    recovering = StepLogStore(engine, objects, "w1", flush_window_seconds=60.0)
    try:
        assert recovering.recover() == 0
    finally:
        recovering.close()
    _assert_all_readable(store, written)

    assert make_compactor().remove_retired() == 3
    assert objects.list("packs/") == []
    assert _pack_rows(engine) == set(objects.list(f"{COMPACTED_PREFIX}/"))
    _assert_all_readable(store, written)


def test_recovery_never_applies_a_compacted_pack(engine, objects, store, make_compactor) -> None:
    """After a merge and the removal, recovery of the worker applies nothing and the rows stay on the new pack."""
    written = _fill(store)
    compactor = make_compactor()
    compactor.merge()
    compactor.remove_retired()
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

        compactor = make_compactor()
        assert compactor.run_once() == (2, 0)
        assert compactor.run_once() == (0, 2)
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
    compactor = make_compactor()
    assert compactor.run_once() == (3, 1)
    assert compactor.run_once() == (0, 3)
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
