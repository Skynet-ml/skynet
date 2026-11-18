"""Tests for the tree purge on account deletion (SKY-733).

A real step log writes packs to a local object store: one pack holds the
entries of two accounts, and one holds only the entries of the deleted
account. Runs on a SQLite file, and on PostgreSQL when ``APP_TEST_DB_URL``
names one. The row lock tests need PostgreSQL.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, event, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from core.config import app_env, settings
from core.storage import tree_compactor, tree_purge
from core.storage.models import (
    AgentStepModel,
    Base,
    JobModel,
    StoragePackModel,
    TreeObjectModel,
    WorkspaceModel,
)
from core.storage.object_store import LocalObjectStore
from core.storage.step_log_store import StepLogStore
from core.storage.tree_compactor import TreeCompactor
from core.storage.tree_purge import TreePurgeError, delete_unused_packs, purge_user
from core.storage.tree_store import TreeEntry, TreeStore

ADA_RUNS = ["job-1", "job-2"]
BOB_RUN = "job-3"
TEST_DB_URL = app_env("APP_TEST_DB_URL")


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param(
            "postgresql",
            marks=pytest.mark.skipif(
                not TEST_DB_URL or not TEST_DB_URL.startswith("postgresql"),
                reason="Set APP_TEST_DB_URL to a disposable PostgreSQL database.",
            ),
        ),
    ]
)
def engine(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Engine]:
    """Yield an engine with the schema and the jobs of ``ada`` and ``bob``.

    The PostgreSQL engine works in its own schema, which it drops at the end.

    Args:
        request: Selects the database dialect.
        tmp_path: Pytest's temporary folder.

    Yields:
        The engine.
    """
    schema = f"tree_purge_test_{uuid4().hex}"
    if request.param == "sqlite":
        eng = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    else:
        admin = create_engine(TEST_DB_URL)
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        eng = create_engine(TEST_DB_URL, connect_args={"options": f"-csearch_path={schema},public"})
    try:
        Base.metadata.create_all(eng)
        with Session(eng) as session:
            for run_id in ADA_RUNS:
                session.add(JobModel(optimization_id=run_id, username="ada", execution_generation=1))
            session.add(JobModel(optimization_id=BOB_RUN, username="bob", execution_generation=1))
            session.add(WorkspaceModel(id="ws-ada", username="ada"))
            session.add(WorkspaceModel(id="ws-bob", username="bob"))
            session.commit()
        yield eng
    finally:
        eng.dispose()
        if request.param == "postgresql":
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()


@pytest.fixture
def objects(tmp_path: Path) -> LocalObjectStore:
    """Return the local object store of the packs.

    Args:
        tmp_path: Pytest's temporary folder.

    Returns:
        The store.
    """
    return LocalObjectStore(tmp_path / "objects")


def _write(step_log: StepLogStore, run_ids: list[str], label: str) -> None:
    """Write one pack with a tree and a step of each run.

    Args:
        step_log: The step log.
        run_ids: The runs that write into the pack.
        label: Makes each pack's content its own.
    """
    trees = TreeStore(step_log)
    for run_id in run_ids:
        blob = trees.put_blob(run_id, f"{run_id} {label}".encode())
        tree_id = trees.put_tree(run_id, [TreeEntry("file.txt", "file", blob)])
        step_log.commit(
            run_id=run_id,
            agent_id="agent-a",
            seq=len(label),
            kind="command",
            execution_generation=1,
            grant_list_id="grants-1",
            body={"output": label},
            tree_id=tree_id,
        )
    step_log.flush()


@pytest.fixture
def packs(engine: Engine, objects: LocalObjectStore) -> dict[str, str]:
    """Write a pack shared by ``ada`` and ``bob``, then a pack of ``ada`` alone.

    Args:
        engine: The test engine.
        objects: The test object store.

    Returns:
        The two pack keys, as ``shared`` and ``ada``.
    """
    step_log = StepLogStore(engine, objects, "w1", flush_window_seconds=60.0)
    try:
        _write(step_log, [*ADA_RUNS, BOB_RUN], "a")
        with Session(engine) as session:
            shared = session.scalars(select(StoragePackModel.pack_key)).one()
        _write(step_log, ADA_RUNS, "bb")
    finally:
        step_log.close()
    with Session(engine) as session:
        (ada,) = set(session.scalars(select(StoragePackModel.pack_key))) - {shared}
    return {"shared": shared, "ada": ada}


def _deleter(root: Path, deleted: list[str]) -> Any:
    """Return a batch delete function over the local store folder that records each key.

    Args:
        root: The object store folder.
        deleted: The list that gets each deleted key.

    Returns:
        The delete function.
    """

    def delete_objects(keys: list[str]) -> None:
        """Delete local objects; a missing object is not an error.

        Args:
            keys: The object keys.
        """
        for key in keys:
            deleted.append(key)
            (root / key).unlink(missing_ok=True)

    return delete_objects


def _purge(engine: Engine, delete_objects: Any, grace_seconds: float = 0.0) -> tuple[int, int]:
    """Purge ``ada`` in one committed transaction, then delete its unused packs, as the account delete does.

    Args:
        engine: The test engine.
        delete_objects: The pack delete function.
        grace_seconds: The age a pack needs before the purge lists it for deletion.

    Returns:
        The counts of deleted rows and deleted packs.
    """
    with Session(engine) as session:
        summary = purge_user(session, "ada", ADA_RUNS, delete_objects=delete_objects, grace_seconds=grace_seconds)
        session.commit()
    return summary.deleted_rows, delete_unused_packs(engine, summary)


def _delete_local(root: Path) -> Any:
    """Return a one-key delete function over the local store folder, as the compactor takes.

    Args:
        root: The object store folder.

    Returns:
        The delete function.
    """

    def delete_object(key: str) -> None:
        """Delete one local object; a missing object is not an error.

        Args:
            key: The object key.
        """
        (root / key).unlink(missing_ok=True)

    return delete_object


def _marked(engine: Engine) -> set[str]:
    """Return the pack keys that ``needs_rewrite`` marks.

    Args:
        engine: The test engine.

    Returns:
        The marked keys.
    """
    with Session(engine) as session:
        return set(session.scalars(select(StoragePackModel.pack_key).where(StoragePackModel.needs_rewrite)))


def _rows(engine: Engine, username: str, run_ids: list[str]) -> tuple[int, int, int]:
    """Count an account's tree object, step and workspace rows.

    Args:
        engine: The test engine.
        username: The account.
        run_ids: The account's runs.

    Returns:
        The counts of tree object, step and workspace rows.
    """
    with Session(engine) as session:
        return (
            len(session.scalars(select(TreeObjectModel).where(TreeObjectModel.username == username)).all()),
            len(session.scalars(select(AgentStepModel).where(AgentStepModel.run_id.in_(run_ids))).all()),
            len(session.scalars(select(WorkspaceModel).where(WorkspaceModel.username == username)).all()),
        )


def test_purge_deletes_the_account_rows_and_its_own_pack(
    engine: Engine, objects: LocalObjectStore, packs: dict[str, str], tmp_path: Path
) -> None:
    """The purge deletes the account's rows and the pack it alone used; the other account keeps all.

    Args:
        engine: The test engine.
        objects: The test object store.
        packs: The shared pack and the pack of ``ada``.
        tmp_path: Pytest's temporary folder.
    """
    bob_before = _rows(engine, "bob", [BOB_RUN])
    deleted: list[str] = []

    summary = _purge(engine, _deleter(tmp_path / "objects", deleted))

    assert _rows(engine, "ada", ADA_RUNS) == (0, 0, 0)
    assert _rows(engine, "bob", [BOB_RUN]) == bob_before
    # Two runs in two packs: four steps, eight tree objects (a blob and a tree each), one workspace.
    assert summary == (4 + 8 + 1, 1)
    assert deleted == [packs["ada"]]
    assert objects.list("") == [packs["shared"]]
    with Session(engine) as session:
        assert set(session.scalars(select(StoragePackModel.pack_key))) == {packs["shared"]}
    assert _marked(engine) == {packs["shared"]}
    step_log = StepLogStore(engine, objects, "w2", flush_window_seconds=60.0)
    try:
        bob_step = f"{BOB_RUN}:agent-a:1"
        assert step_log.get_step(bob_step)["body"] == {"output": "a"}
    finally:
        step_log.close()


def test_a_second_purge_deletes_nothing(engine: Engine, packs: dict[str, str], tmp_path: Path) -> None:
    """The purge is idempotent: a second call succeeds and deletes nothing.

    Args:
        engine: The test engine.
        packs: The shared pack and the pack of ``ada``.
        tmp_path: Pytest's temporary folder.
    """
    deleted: list[str] = []
    _purge(engine, _deleter(tmp_path / "objects", deleted))

    assert _purge(engine, _deleter(tmp_path / "objects", deleted)) == (0, 0)
    assert deleted == [packs["ada"]]


def test_a_failed_pack_delete_after_the_commit_keeps_the_pack_for_the_compactor(
    engine: Engine, objects: LocalObjectStore, packs: dict[str, str], tmp_path: Path
) -> None:
    """A failed S3 delete after the commit raises and keeps the pack and its row; the compactor deletes both.

    Args:
        engine: The test engine.
        objects: The test object store.
        packs: The shared pack and the pack of ``ada``.
        tmp_path: Pytest's temporary folder.
    """

    def fail(keys: list[str]) -> None:
        """Fail like an S3 outage.

        Args:
            keys: The object keys.

        Raises:
            OSError: Always.
        """
        raise OSError("S3 is down")

    with pytest.raises(TreePurgeError, match=packs["ada"]):
        _purge(engine, fail)

    assert _rows(engine, "ada", ADA_RUNS) == (0, 0, 0)
    assert sorted(objects.list("")) == sorted(packs.values())
    with Session(engine) as session:
        assert set(session.scalars(select(StoragePackModel.pack_key))) == set(packs.values())
    assert TreeCompactor(engine, objects, _delete_local(tmp_path / "objects"), grace_seconds=0).remove_retired() == 1
    assert objects.list("") == [packs["shared"]]
    with Session(engine) as session:
        assert set(session.scalars(select(StoragePackModel.pack_key))) == {packs["shared"]}


def test_a_rollback_after_the_purge_keeps_the_pack_objects_and_rows(
    engine: Engine, objects: LocalObjectStore, packs: dict[str, str], tmp_path: Path
) -> None:
    """A failure after the purge and before the commit deletes no pack object, and every row stays.

    A late call of delete_unused_packs with the rolled-back summary deletes nothing either, because
    the rows still point at the pack.

    Args:
        engine: The test engine.
        objects: The test object store.
        packs: The shared pack and the pack of ``ada``.
        tmp_path: Pytest's temporary folder.
    """
    before = _rows(engine, "ada", ADA_RUNS)
    deleted: list[str] = []

    with Session(engine) as session:
        summary = purge_user(
            session, "ada", ADA_RUNS, delete_objects=_deleter(tmp_path / "objects", deleted), grace_seconds=0.0
        )
        assert summary.unused_packs == [packs["ada"]]
        session.rollback()

    assert deleted == []
    assert sorted(objects.list("")) == sorted(packs.values())
    assert _rows(engine, "ada", ADA_RUNS) == before
    assert delete_unused_packs(engine, summary) == 0
    assert deleted == []
    with Session(engine) as session:
        assert set(session.scalars(select(StoragePackModel.pack_key))) == set(packs.values())


def test_a_purge_with_packs_and_no_bucket_fails(
    engine: Engine, packs: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no bucket set, a pack that must go fails the purge instead of staying behind.

    Args:
        engine: The test engine.
        packs: The shared pack and the pack of ``ada``.
        monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(settings, "tree_bucket", None)

    with pytest.raises(TreePurgeError, match="SKYNET_TREE_BUCKET"):
        _purge(engine, None)


def test_a_purge_with_no_trees_needs_no_bucket(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """An account with no packs purges its workspace with no bucket set.

    Args:
        engine: The test engine.
        monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(settings, "tree_bucket", None)

    assert _purge(engine, None) == (1, 0)
    assert _rows(engine, "ada", ADA_RUNS) == (0, 0, 0)


def test_a_retry_after_a_partial_pack_delete_deletes_the_rest(
    engine: Engine, objects: LocalObjectStore, packs: dict[str, str], tmp_path: Path
) -> None:
    """The second of two pack deletes after the commit fails; a retry deletes both packs and their rows.

    The first pack object is already gone when the retry deletes it again, and that is no error.

    Args:
        engine: The test engine.
        objects: The test object store.
        packs: The shared pack and the pack of ``ada``.
        tmp_path: Pytest's temporary folder.
    """
    step_log = StepLogStore(engine, objects, "w3", flush_window_seconds=60.0)
    try:
        _write(step_log, ADA_RUNS, "ccc")
    finally:
        step_log.close()
    own = sorted(set(objects.list("")) - {packs["shared"]})
    assert len(own) == 2
    root = tmp_path / "objects"

    def delete_first_then_fail(keys: list[str]) -> None:
        """Delete the first object, then fail on the second like an S3 error.

        Args:
            keys: The object keys.

        Raises:
            OSError: On the second key.
        """
        (root / keys[0]).unlink()
        raise OSError(f"S3 could not delete {keys[1]}")

    with Session(engine) as session:
        summary = purge_user(session, "ada", ADA_RUNS, delete_objects=delete_first_then_fail, grace_seconds=0.0)
        session.commit()
    with pytest.raises(TreePurgeError):
        delete_unused_packs(engine, summary)
    assert objects.list("") == sorted([packs["shared"], own[1]])
    assert _rows(engine, "ada", ADA_RUNS) == (0, 0, 0)

    deleted: list[str] = []
    summary.delete_objects = _deleter(root, deleted)
    assert delete_unused_packs(engine, summary) == 2

    assert deleted == own
    with Session(engine) as session:
        assert set(session.scalars(select(StoragePackModel.pack_key))) == {packs["shared"]}
    assert objects.list("") == [packs["shared"]]
    with Session(engine) as session:
        assert set(session.scalars(select(StoragePackModel.pack_key))) == {packs["shared"]}


def test_a_row_that_points_at_the_pack_during_the_object_delete_keeps_the_pack_row(
    engine: Engine, packs: dict[str, str], tmp_path: Path
) -> None:
    """A tree object row that points at the pack while its object goes keeps the pack's row.

    Args:
        engine: The test engine.
        packs: The shared pack and the pack of ``ada``.
        tmp_path: Pytest's temporary folder.
    """
    deleted: list[str] = []
    delete_local = _deleter(tmp_path / "objects", deleted)

    def delete_and_point(keys: list[str]) -> None:
        """Delete the objects, then add a row that points at the first pack.

        Args:
            keys: The object keys.
        """
        delete_local(keys)
        with Session(engine) as session:
            session.add(
                TreeObjectModel(
                    username="bob",
                    object_id="0" * 64,
                    byte_size=1,
                    pack_key=keys[0],
                    pack_offset=0,
                    pack_length=1,
                )
            )
            session.commit()

    with Session(engine) as session:
        summary = purge_user(session, "ada", ADA_RUNS, delete_objects=delete_and_point, grace_seconds=0.0)
        session.commit()

    assert delete_unused_packs(engine, summary) == 1
    assert deleted == [packs["ada"]]
    with Session(engine) as session:
        assert set(session.scalars(select(StoragePackModel.pack_key))) == set(packs.values())


def test_the_compactor_rewrites_a_marked_full_pack_without_the_deleted_bytes(
    engine: Engine, objects: LocalObjectStore, packs: dict[str, str], tmp_path: Path
) -> None:
    """A shared pack that the purge marks is rewritten alone, at any size, with only the live entries.

    Args:
        engine: The test engine.
        objects: The test object store.
        packs: The shared pack and the pack of ``ada``.
        tmp_path: Pytest's temporary folder.
    """
    root = tmp_path / "objects"
    assert b"job-1 a" in objects.get(packs["shared"])
    _purge(engine, _deleter(root, []))

    def delete_object(key: str) -> None:
        """Delete one local object; a missing object is not an error.

        Args:
            key: The object key.
        """
        (root / key).unlink(missing_ok=True)

    # A target of one byte makes every pack full, so only the mark makes the shared pack a candidate.
    compactor = TreeCompactor(engine, objects, delete_object, min_age_seconds=0, target_bytes=1)
    with Session(engine) as session:
        session.execute(update(StoragePackModel).values(needs_rewrite=False))
        session.commit()
    assert compactor.merge() == 0
    with Session(engine) as session:
        session.execute(update(StoragePackModel).values(needs_rewrite=True))
        session.commit()

    assert compactor.merge() == 1
    assert TreeCompactor(engine, objects, delete_object, grace_seconds=0).remove_retired() == 1

    (new_key,) = objects.list("")
    assert new_key.startswith(f"{tree_compactor.COMPACTED_PREFIX}/")
    rewritten = objects.get(new_key)
    assert b"job-3 a" in rewritten
    assert b"job-1" not in rewritten
    assert b"job-2" not in rewritten
    assert _marked(engine) == set()
    step_log = StepLogStore(engine, objects, "w2", flush_window_seconds=60.0)
    try:
        assert step_log.get_step(f"{BOB_RUN}:agent-a:1")["body"] == {"output": "a"}
    finally:
        step_log.close()


def _compact(engine: Engine, objects: LocalObjectStore, root: Path) -> int:
    """Run one merge pass with every pack full, then delete the retired packs at once.

    Args:
        engine: The test engine.
        objects: The test object store.
        root: The object store folder.

    Returns:
        The number of packs merged.
    """

    def delete_object(key: str) -> None:
        """Delete one local object; a missing object is not an error.

        Args:
            key: The object key.
        """
        (root / key).unlink(missing_ok=True)

    # A target of one byte makes every pack full, so only the mark makes a pack a candidate.
    merged = TreeCompactor(engine, objects, delete_object, min_age_seconds=0, target_bytes=1).merge()
    TreeCompactor(engine, objects, delete_object, grace_seconds=0).remove_retired()
    return merged


def test_a_pack_where_every_entry_lands_is_not_marked(engine: Engine, packs: dict[str, str]) -> None:
    """Each step and object of both packs gets its row, so no pack gets ``needs_rewrite``.

    Args:
        engine: The test engine.
        packs: The shared pack and the pack of ``ada``.
    """
    assert _marked(engine) == set()


def test_a_mixed_pack_applied_after_an_account_is_gone_is_marked_and_rewritten(
    engine: Engine, objects: LocalObjectStore, tmp_path: Path
) -> None:
    """A pack written before the account deletion and applied after it loses the deleted account's bytes.

    The deleted account's entries get no row, so only the mark makes the compactor drop their bytes.

    Args:
        engine: The test engine.
        objects: The test object store.
        tmp_path: Pytest's temporary folder.
    """
    step_log = StepLogStore(engine, objects, "w1", flush_window_seconds=60.0)
    try:
        trees = TreeStore(step_log)
        for run_id in [*ADA_RUNS, BOB_RUN]:
            trees.put_blob(run_id, f"{run_id} late".encode())
        with Session(engine) as session:
            session.execute(delete(JobModel).where(JobModel.optimization_id.in_(ADA_RUNS)))
            session.commit()
        step_log.flush()
    finally:
        step_log.close()
    (pack,) = objects.list("")
    assert b"job-1 late" in objects.get(pack)
    assert _rows(engine, "ada", ADA_RUNS)[0] == 0
    assert _marked(engine) == {pack}

    assert _compact(engine, objects, tmp_path / "objects") == 1

    (new_key,) = objects.list("")
    rewritten = objects.get(new_key)
    assert b"job-3 late" in rewritten
    assert b"job-1" not in rewritten
    assert b"job-2" not in rewritten
    assert _marked(engine) == set()


def test_a_pack_with_an_object_that_already_has_a_row_is_marked(
    engine: Engine, objects: LocalObjectStore, tmp_path: Path
) -> None:
    """A second run stores the same blob again: the copy gets no row, the pack gets the mark, and a rewrite drops it.

    Args:
        engine: The test engine.
        objects: The test object store.
        tmp_path: Pytest's temporary folder.
    """
    step_log = StepLogStore(engine, objects, "w1", flush_window_seconds=60.0)
    try:
        trees = TreeStore(step_log)
        blob = trees.put_blob("job-1", b"same bytes")
        step_log.flush()
        (first,) = objects.list("")
        assert trees.put_blob("job-2", b"same bytes") == blob
        trees.put_blob("job-2", b"new bytes")
        step_log.flush()
    finally:
        step_log.close()
    (second,) = set(objects.list("")) - {first}
    assert _marked(engine) == {second}

    assert _compact(engine, objects, tmp_path / "objects") == 1

    with Session(engine) as session:
        rows = session.execute(select(TreeObjectModel.pack_key, TreeObjectModel.pack_length)).all()
    (new_key,) = {key for key, _ in rows} - {first}
    # The new pack holds only the bytes of its one row: the duplicate copy is gone.
    assert [length for key, length in rows if key == new_key] == [len(objects.get(new_key))]
    assert set(objects.list("")) == {first, new_key}
    assert _marked(engine) == set()


def test_a_pack_with_a_stale_step_is_marked_and_rewritten_without_it(
    engine: Engine, objects: LocalObjectStore, tmp_path: Path
) -> None:
    """A step with an old generation gets no row, so its pack gets the mark and a rewrite drops its bytes.

    Args:
        engine: The test engine.
        objects: The test object store.
        tmp_path: Pytest's temporary folder.
    """
    step_log = StepLogStore(engine, objects, "w1", flush_window_seconds=60.0)
    try:
        for generation, output in ((0, "stale bytes"), (1, "live bytes")):
            step_log.commit(
                run_id=BOB_RUN,
                agent_id=f"agent-{generation}",
                seq=1,
                kind="model",
                execution_generation=generation,
                grant_list_id="grants-1",
                body={"output": output},
            )
        step_log.flush()
    finally:
        step_log.close()
    (pack,) = objects.list("")
    assert b"stale bytes" in objects.get(pack)
    assert _marked(engine) == {pack}

    assert _compact(engine, objects, tmp_path / "objects") == 1

    (new_key,) = objects.list("")
    rewritten = objects.get(new_key)
    assert b"live bytes" in rewritten
    assert b"stale bytes" not in rewritten
    assert _marked(engine) == set()


def test_a_pack_with_a_step_that_already_has_a_row_is_marked(engine: Engine, objects: LocalObjectStore) -> None:
    """A second store commits a step id that already landed: the first commit wins and the copy marks its pack.

    Args:
        engine: The test engine.
        objects: The test object store.
    """
    step = {
        "run_id": BOB_RUN,
        "agent_id": "agent-a",
        "kind": "model",
        "execution_generation": 1,
        "grant_list_id": "grants-1",
    }
    first_log = StepLogStore(engine, objects, "w1", flush_window_seconds=60.0)
    try:
        first_log.commit(seq=1, body={"output": "first"}, **step)
        first_log.flush()
    finally:
        first_log.close()
    (first,) = objects.list("")
    second_log = StepLogStore(engine, objects, "w2", flush_window_seconds=60.0)
    try:
        second_log.commit(seq=1, body={"output": "again"}, **step)
        second_log.commit(seq=2, body={"output": "new"}, **step)
        second_log.flush()
        assert second_log.get_step(f"{BOB_RUN}:agent-a:1")["body"] == {"output": "first"}
    finally:
        second_log.close()
    (second,) = set(objects.list("")) - {first}
    assert _marked(engine) == {second}


def test_the_purge_keeps_a_young_unused_pack_until_the_grace_time_ends(
    engine: Engine, objects: LocalObjectStore, packs: dict[str, str], tmp_path: Path
) -> None:
    """After a merge moves bob's rows off the shared pack, a reader may still GET it inside the grace time.

    The purge leaves the young unused packs; the compactor deletes them once the grace time ends.

    Args:
        engine: The test engine.
        objects: The test object store.
        packs: The shared pack and the pack of ``ada``.
        tmp_path: Pytest's temporary folder.
    """
    shared = packs["shared"]
    new = f"{tree_compactor.COMPACTED_PREFIX}/{uuid4().hex}.pack"
    objects.put(new, objects.get(shared))
    with Session(engine) as session:
        session.add(StoragePackModel(pack_key=new, worker_id=tree_compactor.COMPACTOR_ID, byte_size=1))
        session.execute(
            update(AgentStepModel)
            .where(AgentStepModel.pack_key == shared, AgentStepModel.run_id == BOB_RUN)
            .values(pack_key=new)
        )
        session.execute(
            update(TreeObjectModel)
            .where(TreeObjectModel.pack_key == shared, TreeObjectModel.username == "bob")
            .values(pack_key=new)
        )
        session.execute(
            update(StoragePackModel).where(StoragePackModel.pack_key == shared).values(created_at=datetime.now(UTC))
        )
        session.commit()
    deleted: list[str] = []

    summary = _purge(engine, _deleter(tmp_path / "objects", deleted), grace_seconds=tree_compactor.GRACE_SECONDS)

    assert summary[1] == 0
    assert deleted == []
    assert _rows(engine, "ada", ADA_RUNS) == (0, 0, 0)
    assert sorted(objects.list("")) == sorted([shared, packs["ada"], new])
    assert _marked(engine) == set()
    root = tmp_path / "objects"

    def delete_object(key: str) -> None:
        """Delete one local object; a missing object is not an error.

        Args:
            key: The object key.
        """
        (root / key).unlink(missing_ok=True)

    assert TreeCompactor(engine, objects, delete_object).remove_retired() == 0
    assert TreeCompactor(engine, objects, delete_object, grace_seconds=0).remove_retired() == 2
    assert objects.list("") == [new]


def test_the_purge_locks_the_jobs_then_the_packs_before_it_deletes(engine: Engine, packs: dict[str, str]) -> None:
    """The first statement locks the account's jobs in key order; the packs are locked before any delete.

    Args:
        engine: The test engine.
        packs: The shared pack and the pack of ``ada``.
    """
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        """Record each SQL statement.

        Args:
            conn: The connection.
            cursor: The cursor.
            statement: The SQL text.
            *args: The other event arguments.
        """
        statements.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", record)
    try:
        _purge(engine, lambda keys: None)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    first_pack_lock = next(i for i, sql in enumerate(statements) if "FROM storage_packs" in sql)
    first_delete = next(i for i, sql in enumerate(statements) if sql.startswith("DELETE"))
    assert "FROM jobs" in statements[0]
    assert "ORDER BY jobs.optimization_id" in statements[0]
    assert first_pack_lock < first_delete
    if engine.dialect.name == "postgresql":
        assert statements[0].endswith("FOR UPDATE")
        assert statements[first_pack_lock].endswith("FOR UPDATE")


def _record_with_parameters(engine: Engine) -> tuple[list[tuple[str, list[Any]]], Any]:
    """Record each SQL statement that the engine runs from now on, with its parameter values.

    Args:
        engine: The test engine.

    Returns:
        The list that gets each statement, with its whitespace made single, and its parameter values,
        and a function that stops the recording.
    """
    statements: list[tuple[str, list[Any]]] = []

    def record(conn: Any, cursor: Any, statement: str, parameters: Any, *args: Any) -> None:
        """Record one statement and its parameter values.

        Args:
            conn: The connection.
            cursor: The cursor.
            statement: The SQL text.
            parameters: The parameters, a tuple or a dict by dialect.
            *args: The other event arguments.
        """
        values = list(parameters.values()) if isinstance(parameters, dict) else list(parameters or ())
        statements.append((" ".join(statement.split()), values))

    event.listen(engine, "before_cursor_execute", record)
    return statements, lambda: event.remove(engine, "before_cursor_execute", record)


def test_the_pack_lock_takes_each_chunk_in_key_order_for_update(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pack lock sorts the keys over the chunks, and each lock statement orders by key FOR UPDATE.

    Args:
        engine: The test engine.
        monkeypatch: Pytest monkeypatch fixture.
    """
    keys = {f"packs/w1/{index:02d}" for index in range(12)}
    with Session(engine) as session:
        for key in keys:
            session.add(StoragePackModel(pack_key=key, worker_id="w1", byte_size=1))
        session.commit()
    monkeypatch.setattr(tree_purge, "CHUNK_SIZE", 5)
    statements, stop = _record_with_parameters(engine)
    try:
        with Session(engine) as session:
            tree_purge._lock_packs(session, keys)
            session.rollback()
    finally:
        stop()
    locks = [(sql, values) for sql, values in statements if "FROM storage_packs" in sql]

    assert len(locks) == 3
    for sql, _ in locks:
        assert "ORDER BY storage_packs.pack_key" in sql
        if engine.dialect.name == "postgresql":
            assert sql.endswith("FOR UPDATE")
    assert [value for _, values in locks for value in values] == sorted(keys)


def _move_to_a_new_pack(engine: Engine, objects: LocalObjectStore, old: str) -> str:
    """Move every row of a pack to a new pack and retire the old one, as a compactor merge commits it.

    Args:
        engine: The test engine.
        objects: The test object store.
        old: The pack whose rows move.

    Returns:
        The new pack key.
    """
    new = f"{tree_compactor.COMPACTED_PREFIX}/{uuid4().hex}.pack"
    objects.put(new, objects.get(old))
    with Session(engine) as session:
        session.add(StoragePackModel(pack_key=new, worker_id=tree_compactor.COMPACTOR_ID, byte_size=1))
        for model in (AgentStepModel, TreeObjectModel):
            session.execute(update(model).where(model.pack_key == old).values(pack_key=new))
        session.execute(
            update(StoragePackModel).where(StoragePackModel.pack_key == old).values(created_at=datetime.now(UTC))
        )
        session.commit()
    return new


def _purge_after_a_merge_moves_ada(
    engine: Engine, objects: LocalObjectStore, packs: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> tuple[str, list[tuple[list[str], int]], list[str]]:
    """Purge ``ada`` while a merge moves the rows of her own pack to a new pack between the key read and the lock.

    Args:
        engine: The test engine.
        objects: The test object store.
        packs: The pack keys.
        monkeypatch: Pytest's monkeypatch.

    Returns:
        The new pack key, each pack lock call with the deletes run before it, and the deleted objects.
    """
    read = tree_purge._read_pack_keys
    lock = tree_purge._lock_packs
    moved: list[str] = []
    locks: list[tuple[list[str], int]] = []
    deletes: list[str] = []

    def read_then_merge(session: Session, username: str, run_ids: list[str]) -> set[str]:
        """Read the pack keys, then let a merge commit before the purge locks them.

        Args:
            session: The purge transaction.
            username: The account.
            run_ids: The account's runs.

        Returns:
            The keys as read before the merge.
        """
        keys = read(session, username, run_ids)
        if not moved:
            moved.append(_move_to_a_new_pack(engine, objects, packs["ada"]))
        return keys

    def record_lock(session: Session, keys: set[str]) -> None:
        """Record the keys of each lock call and the deletes before it.

        Args:
            session: The purge transaction.
            keys: The pack keys.
        """
        locks.append((sorted(keys), len(deletes)))
        lock(session, keys)

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        """Record each DELETE statement.

        Args:
            conn: The connection.
            cursor: The cursor.
            statement: The SQL text.
            *args: The other event arguments.
        """
        if statement.lstrip().startswith("DELETE"):
            deletes.append(statement)

    monkeypatch.setattr(tree_purge, "_read_pack_keys", read_then_merge)
    monkeypatch.setattr(tree_purge, "_lock_packs", record_lock)
    deleted: list[str] = []
    event.listen(engine, "before_cursor_execute", record)
    try:
        _purge(engine, deleted.extend)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return moved[0], locks, deleted


def test_a_merge_between_the_key_read_and_the_lock_makes_the_purge_lock_the_union_in_order(
    engine: Engine, objects: LocalObjectStore, packs: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The purge finds the new pack after its lock, locks the union again in key order, and never locks after a delete.

    Args:
        engine: The test engine.
        objects: The test object store.
        packs: The shared pack and the pack of ``ada``.
        monkeypatch: Pytest monkeypatch fixture.
    """
    new, locks, deleted = _purge_after_a_merge_moves_ada(engine, objects, packs, monkeypatch)

    assert locks == [
        (sorted([packs["shared"], packs["ada"]]), 0),
        (sorted([packs["shared"], packs["ada"], new]), 0),
    ]
    assert deleted == [new]
    assert _rows(engine, "ada", ADA_RUNS) == (0, 0, 0)


def test_the_purge_keeps_a_pack_that_a_merge_retired(
    engine: Engine, objects: LocalObjectStore, packs: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pack that the purge locked but whose rows a merge moved away stays for the compactor's grace time.

    Args:
        engine: The test engine.
        objects: The test object store.
        packs: The shared pack and the pack of ``ada``.
        monkeypatch: Pytest monkeypatch fixture.
    """
    _, _, deleted = _purge_after_a_merge_moves_ada(engine, objects, packs, monkeypatch)

    assert packs["ada"] not in deleted
    assert packs["ada"] in objects.list("")
    assert _marked(engine) == {packs["shared"]}
    with Session(engine) as session:
        assert set(session.scalars(select(StoragePackModel.pack_key))) == {packs["shared"], packs["ada"]}


def test_a_purge_whose_packs_keep_moving_fails_and_can_be_retried(
    engine: Engine, objects: LocalObjectStore, packs: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the keys change on each read, the purge gives up after a few tries and deletes nothing.

    A retry when the keys stay still deletes the rows and the pack.

    Args:
        engine: The test engine.
        objects: The test object store.
        packs: The shared pack and the pack of ``ada``.
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Pytest's temporary folder.
    """
    read = tree_purge._read_pack_keys
    before = _rows(engine, "ada", ADA_RUNS)
    reads: list[int] = []

    def read_a_new_key(session: Session, username: str, run_ids: list[str]) -> set[str]:
        """Read the pack keys, plus one new key on each read, as if a merge committed before each read.

        Args:
            session: The purge transaction.
            username: The account.
            run_ids: The account's runs.

        Returns:
            The keys with one new key.
        """
        reads.append(len(reads))
        return read(session, username, run_ids) | {f"{tree_compactor.COMPACTED_PREFIX}/{len(reads):04d}.pack"}

    monkeypatch.setattr(tree_purge, "_read_pack_keys", read_a_new_key)

    with pytest.raises(TreePurgeError, match="try again"):
        _purge(engine, lambda keys: None)
    assert len(reads) == tree_purge.PACK_LOCK_TRIES + 1
    assert _rows(engine, "ada", ADA_RUNS) == before

    monkeypatch.setattr(tree_purge, "_read_pack_keys", read)
    deleted: list[str] = []
    _purge(engine, _deleter(tmp_path / "objects", deleted))
    assert _rows(engine, "ada", ADA_RUNS) == (0, 0, 0)
    assert deleted == [packs["ada"]]
    assert objects.list("") == [packs["shared"]]
    with Session(engine) as session:
        assert set(session.scalars(select(StoragePackModel.pack_key))) == {packs["shared"]}


def test_an_open_purge_holds_the_account_job_and_pack_locks(engine: Engine, packs: dict[str, str]) -> None:
    """Until it commits, the purge holds the account's job rows and the pack rows; a flush or merge waits or gives up.

    Args:
        engine: The test engine.
        packs: The shared pack and the pack of ``ada``.
    """
    if engine.dialect.name != "postgresql":
        pytest.skip("Row locks need PostgreSQL.")
    with Session(engine) as purge, engine.connect() as other:
        other.execute(text("SET lock_timeout = '200ms'"))
        other.commit()
        purge_user(purge, "ada", ADA_RUNS, delete_objects=lambda keys: None)
        for locked in (
            select(JobModel.optimization_id).where(JobModel.optimization_id == ADA_RUNS[0]).with_for_update(),
            select(StoragePackModel.pack_key)
            .where(StoragePackModel.pack_key == packs["shared"])
            .with_for_update(nowait=True),
        ):
            with pytest.raises(OperationalError) as error:
                other.execute(locked)
            assert error.value.orig.pgcode == "55P03"
            other.rollback()
        assert other.execute(
            select(JobModel.optimization_id).where(JobModel.optimization_id == BOB_RUN).with_for_update()
        ).all() == [(BOB_RUN,)]
        other.rollback()
        purge.rollback()


def test_bucket_deleter_deletes_from_the_tree_bucket_in_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default delete function sends DeleteObjects batches of at most 1000 keys to the tree bucket.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    calls: list[dict[str, Any]] = []

    def delete_objects(**kwargs: Any) -> dict[str, Any]:
        """Record one DeleteObjects call and report no error.

        Args:
            **kwargs: The call arguments.

        Returns:
            An empty response.
        """
        calls.append(kwargs)
        return {}

    client = type("Client", (), {"delete_objects": staticmethod(delete_objects)})()
    monkeypatch.setattr(settings, "tree_bucket", "trees")
    monkeypatch.setattr(tree_compactor, "boto3", type("Boto3", (), {"client": staticmethod(lambda name: client)}))
    keys = [f"packs/w1/{index:04d}" for index in range(1001)]

    tree_purge.bucket_deleter()(keys)

    assert [call["Bucket"] for call in calls] == ["trees", "trees"]
    assert [[item["Key"] for item in call["Delete"]["Objects"]] for call in calls] == [keys[:1000], keys[1000:]]
    assert all(call["Delete"]["Quiet"] for call in calls)


def test_bucket_deleter_names_the_keys_that_s3_did_not_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DeleteObjects response with Errors raises TreePurgeError with the failed keys.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    response = {
        "Errors": [{"Key": "packs/w1/b", "Code": "AccessDenied"}, {"Key": "packs/w1/a", "Code": "InternalError"}]
    }
    client = type("Client", (), {"delete_objects": lambda self, **kwargs: response})()
    monkeypatch.setattr(settings, "tree_bucket", "trees")
    monkeypatch.setattr(tree_compactor, "boto3", type("Boto3", (), {"client": staticmethod(lambda name: client)}))

    with pytest.raises(TreePurgeError, match="packs/w1/a, packs/w1/b"):
        tree_purge.bucket_deleter()(["packs/w1/a", "packs/w1/b", "packs/w1/c"])


def test_a_bucket_that_cannot_open_fails_the_purge(
    engine: Engine, packs: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bucket with no boto3 fails the purge with TreePurgeError, not a bare RuntimeError.

    Args:
        engine: The test engine.
        packs: The shared pack and the pack of ``ada``.
        monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(settings, "tree_bucket", "trees")
    monkeypatch.setattr(tree_compactor, "boto3", None)

    with pytest.raises(TreePurgeError, match="tree bucket") as error:
        _purge(engine, None)
    assert isinstance(error.value.__cause__, RuntimeError)


def test_the_purge_deletes_rows_with_no_returning_list(engine: Engine, packs: dict[str, str], tmp_path: Path) -> None:
    """The row deletes return no pack key for each row; the counts come from the row counts.

    Args:
        engine: The test engine.
        packs: The shared pack and the pack of ``ada``.
        tmp_path: Pytest's temporary folder.
    """
    statements, stop = _record_with_parameters(engine)
    try:
        summary = _purge(engine, _deleter(tmp_path / "objects", []))
    finally:
        stop()

    assert summary == (4 + 8 + 1, 1)
    deletes = [sql for sql, _ in statements if sql.startswith("DELETE")]
    assert deletes
    assert not any("RETURNING" in sql for sql in deletes)


def test_the_error_names_the_first_keys_and_the_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DeleteObjects response with many Errors names the count and only the first few keys.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    failed = [f"packs/w1/{index:02d}" for index in range(12)]
    response = {"Errors": [{"Key": key, "Code": "InternalError"} for key in reversed(failed)]}
    client = type("Client", (), {"delete_objects": lambda self, **kwargs: response})()
    monkeypatch.setattr(settings, "tree_bucket", "trees")
    monkeypatch.setattr(tree_compactor, "boto3", type("Boto3", (), {"client": staticmethod(lambda name: client)}))

    with pytest.raises(TreePurgeError) as error:
        tree_purge.bucket_deleter()(failed)

    message = str(error.value)
    assert "12 tree packs" in message
    assert ", ".join(failed[: tree_purge.ERROR_KEYS_SHOWN]) in message
    assert failed[tree_purge.ERROR_KEYS_SHOWN] not in message


def test_a_job_delete_marks_the_packs_of_its_steps(engine: Engine, packs: dict[str, str]) -> None:
    """Marking the packs of one deleted run marks both packs that hold its steps, before the steps go.

    Args:
        engine: The test engine.
        packs: The shared pack and the pack of ``ada``.
    """
    with Session(engine) as session:
        assert _marked(engine) == set()
        tree_purge.mark_run_packs(session, [ADA_RUNS[0], ADA_RUNS[0], "no-such-job"])
        session.execute(delete(AgentStepModel).where(AgentStepModel.run_id == ADA_RUNS[0]))
        session.execute(delete(JobModel).where(JobModel.optimization_id == ADA_RUNS[0]))
        session.commit()

    assert _marked(engine) == {packs["shared"], packs["ada"]}


def test_a_job_delete_rewrites_the_shared_pack_without_the_run(
    engine: Engine, objects: LocalObjectStore, packs: dict[str, str], tmp_path: Path
) -> None:
    """After a run is deleted, the compactor rewrites the shared pack with only the entries that rows still use.

    Args:
        engine: The test engine.
        objects: The test object store.
        packs: The shared pack and the pack of ``ada``.
        tmp_path: Pytest's temporary folder.
    """
    with Session(engine) as session:
        tree_purge.mark_run_packs(session, [BOB_RUN])
        session.execute(delete(AgentStepModel).where(AgentStepModel.run_id == BOB_RUN))
        session.execute(delete(JobModel).where(JobModel.optimization_id == BOB_RUN))
        session.commit()
        kept = sum(
            sum(session.scalars(select(model.pack_length).where(model.pack_key == packs["shared"])))
            for model in (AgentStepModel, TreeObjectModel)
        )

    assert _compact(engine, objects, tmp_path / "objects") == 1

    with Session(engine) as session:
        new = session.scalars(
            select(StoragePackModel.pack_key).where(StoragePackModel.pack_key.startswith("compacted/"))
        ).one()
    # A merged pack is the kept entries back to back, so its size shows that the run's step bytes went.
    assert len(objects.get(new)) == kept
    assert objects.list("packs/") == [packs["ada"]]


def test_a_job_delete_locks_the_jobs_then_the_packs_in_key_order(engine: Engine, packs: dict[str, str]) -> None:
    """The mark locks the run's job first, then its packs ordered by key, before it marks any pack.

    Args:
        engine: The test engine.
        packs: The shared pack and the pack of ``ada``.
    """
    statements, stop = _record_with_parameters(engine)
    try:
        with Session(engine) as session:
            tree_purge.mark_run_packs(session, ADA_RUNS)
            session.rollback()
    finally:
        stop()

    sqls = [sql for sql, _ in statements]
    first_job_lock = next(i for i, sql in enumerate(sqls) if "FROM jobs" in sql)
    pack_lock = next(i for i, sql in enumerate(sqls) if sql.startswith("SELECT storage_packs.pack_key"))
    mark = next(i for i, sql in enumerate(sqls) if sql.startswith("UPDATE storage_packs"))
    assert first_job_lock < pack_lock < mark
    assert "ORDER BY jobs.optimization_id" in sqls[first_job_lock]
    assert "ORDER BY storage_packs.pack_key" in sqls[pack_lock]
    if engine.dialect.name == "postgresql":
        assert sqls[first_job_lock].endswith("FOR UPDATE")
        assert sqls[pack_lock].endswith("FOR UPDATE")
