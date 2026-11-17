"""Tests for the content-addressed tree store and its share of the stored-bytes footprint."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from core.storage.models import Base, JobModel, TreeObjectModel
from core.storage.object_store import LocalObjectStore
from core.storage.step_log_store import StepLogStore
from core.storage.tree_store import TreeEntry, TreeStore, encode_blob, encode_tree, object_id_of
from core.storage.usage import compute_user_storage


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    """Return a SQLite file engine with the schema and three jobs of two accounts.

    Args:
        tmp_path: Pytest's temporary folder.

    Returns:
        The engine.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(JobModel(optimization_id="job-ada", username="ada", execution_generation=1))
        session.add(JobModel(optimization_id="job-ada-2", username="ada", execution_generation=1))
        session.add(JobModel(optimization_id="job-bob", username="bob", execution_generation=1))
        session.commit()
    return engine


@pytest.fixture
def step_log(engine: Engine, tmp_path: Path) -> Iterator[StepLogStore]:
    """Yield a step log with a long window, so only explicit flushes write.

    Args:
        engine: The test engine.
        tmp_path: Pytest's temporary folder.

    Yields:
        The step log; it closes after the test.
    """
    store = StepLogStore(engine, LocalObjectStore(tmp_path / "objects"), "w1", flush_window_seconds=60.0)
    yield store
    store.close()


def _tree_rows(engine: Engine) -> int:
    """Return the number of tree object rows.

    Args:
        engine: The test engine.

    Returns:
        The row count.
    """
    with Session(engine) as session:
        return int(session.scalar(select(func.count()).select_from(TreeObjectModel)) or 0)


def test_the_same_content_gets_the_same_id_in_any_entry_order() -> None:
    """Ids are SHA-256 hashes of the stored bytes; entry order does not change a tree id."""
    blob = object_id_of(encode_blob(b"print(1)\n"))
    a = TreeEntry("a.py", "file", blob)
    b = TreeEntry("run.sh", "exec", blob)

    assert blob == object_id_of(encode_blob(b"print(1)\n"))
    assert blob != object_id_of(encode_blob(b"print(2)\n"))
    assert object_id_of(encode_tree([a, b])) == object_id_of(encode_tree([b, a]))
    assert object_id_of(encode_tree([a])) != object_id_of(encode_blob(encode_tree([a])[5:]))


@pytest.mark.parametrize(
    "entry",
    [
        TreeEntry("", "file", "a" * 64),
        TreeEntry("..", "file", "a" * 64),
        TreeEntry("a/b", "file", "a" * 64),
        TreeEntry("a", "socket", "a" * 64),
        TreeEntry("a", "file", "not-a-hash"),
    ],
)
def test_a_bad_tree_entry_is_refused(entry: TreeEntry) -> None:
    """A tree refuses unsafe names, unknown modes and ids that are not hashes."""
    with pytest.raises(ValueError):
        encode_tree([entry])


def test_a_tree_refuses_the_same_name_twice() -> None:
    """Two entries with one name make no tree."""
    with pytest.raises(ValueError, match="same name"):
        encode_tree([TreeEntry("a", "file", "a" * 64), TreeEntry("a", "exec", "b" * 64)])


def test_a_tree_reads_from_the_buffer_and_then_from_its_pack(step_log, engine, tmp_path) -> None:
    """Before the flush the blob and tree come from the buffer; after it, a new reader gets them from the pack."""
    trees = TreeStore(step_log)
    blob = trees.put_blob("job-ada", b"hello\n")
    sub = trees.put_tree("job-ada", [TreeEntry("greeting.txt", "file", blob)])
    root = trees.put_tree("job-ada", [TreeEntry("docs", "tree", sub), TreeEntry("main.txt", "file", blob)])

    assert _tree_rows(engine) == 0
    assert trees.get_blob("job-ada", blob) == b"hello\n"
    assert [entry.name for entry in trees.get_tree("job-ada", root)] == ["docs", "main.txt"]

    step_log.flush()
    reader_log = StepLogStore(engine, LocalObjectStore(tmp_path / "objects"), "w2", flush_window_seconds=60.0)
    try:
        reader = TreeStore(reader_log)
        assert _tree_rows(engine) == 3
        assert reader.get_tree("job-ada", sub) == [TreeEntry("greeting.txt", "file", blob)]
        assert reader.get_blob("job-ada", blob) == b"hello\n"
        assert reader.get_blob("job-ada", "f" * 64) is None
    finally:
        reader_log.close()


def test_another_account_cannot_read_the_object_before_or_after_the_flush(step_log) -> None:
    """Before its flush only the writing run reads an object; after it, every run of the owner and no other account."""
    trees = TreeStore(step_log)
    blob = trees.put_blob("job-ada", b"secret")
    root = trees.put_tree("job-ada", [TreeEntry("secret.txt", "file", blob)])

    assert trees.get_blob("job-ada", blob) == b"secret"
    assert trees.get_blob("job-bob", blob) is None
    assert trees.get_tree("job-bob", root) is None
    step_log.flush()

    assert trees.get_blob("job-bob", blob) is None
    assert trees.get_tree("job-bob", root) is None
    assert trees.get_blob("job-ada-2", blob) == b"secret"
    assert trees.get_tree("job-ada-2", root) == [TreeEntry("secret.txt", "file", blob)]


def test_a_repeated_object_is_stored_once_per_account(step_log, engine) -> None:
    """The same blob twice in one account is one row; another account gets its own row."""
    trees = TreeStore(step_log)
    first = trees.put_blob("job-ada", b"same")
    assert trees.put_blob("job-ada", b"same") == first
    step_log.flush()
    trees.put_blob("job-ada", b"same")
    trees.put_blob("job-bob", b"same")
    step_log.flush()

    with Session(engine) as session:
        owners = sorted(session.scalars(select(TreeObjectModel.username).where(TreeObjectModel.object_id == first)))
    assert owners == ["ada", "bob"]


def test_a_blob_read_as_a_tree_or_a_changed_object_is_refused(step_log) -> None:
    """A read checks the hash and the object type."""
    trees = TreeStore(step_log)
    blob = trees.put_blob("job-ada", b"x")
    step_log.put_object("job-ada", "e" * 64, encode_blob(b"y"))

    with pytest.raises(ValueError, match="not a tree"):
        trees.get_tree("job-ada", blob)
    with pytest.raises(ValueError, match="does not match"):
        trees.get_blob("job-ada", "e" * 64)


def test_tree_bytes_count_toward_the_owner_footprint(step_log, engine) -> None:
    """Flushed tree bytes add to the owner's stored bytes, and only to that owner's."""
    before_ada = compute_user_storage(engine, "ada")
    before_bob = compute_user_storage(engine, "bob")
    trees = TreeStore(step_log)
    blob = trees.put_blob("job-ada", b"z" * 1000)
    trees.put_tree("job-ada", [TreeEntry("z.bin", "file", blob)])
    step_log.flush()

    with Session(engine) as session:
        stored = int(session.scalar(select(func.sum(TreeObjectModel.byte_size))) or 0)
    after_ada = compute_user_storage(engine, "ada")
    assert stored > 1000
    assert after_ada.total - before_ada.total == stored
    assert after_ada.breakdown["optimizations"] - before_ada.breakdown["optimizations"] == stored
    assert compute_user_storage(engine, "bob").total == before_bob.total
