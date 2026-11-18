"""Tests for the tree purge on account deletion (SKY-733).

A real step log writes packs to a local object store: one pack holds the
entries of two accounts, and one holds only the entries of the deleted
account. Runs on a SQLite file, and on PostgreSQL when ``APP_TEST_DB_URL``
names one. The row lock tests need PostgreSQL.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select, text, update
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
from core.storage.tree_purge import TreePurgeError, TreePurgeSummary, purge_user
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


def _purge(engine: Engine, delete_objects: Any) -> TreePurgeSummary:
    """Purge ``ada`` in one committed transaction.

    Args:
        engine: The test engine.
        delete_objects: The pack delete function.

    Returns:
        The purge summary.
    """
    with Session(engine) as session:
        summary = purge_user(session, "ada", ADA_RUNS, delete_objects=delete_objects)
        session.commit()
    return summary


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
    """The purge deletes the account's rows and the pack it alone used; the other account keeps all."""
    bob_before = _rows(engine, "bob", [BOB_RUN])
    deleted: list[str] = []

    summary = _purge(engine, _deleter(tmp_path / "objects", deleted))

    assert _rows(engine, "ada", ADA_RUNS) == (0, 0, 0)
    assert _rows(engine, "bob", [BOB_RUN]) == bob_before
    # Two runs in two packs: four steps, eight tree objects (a blob and a tree each), one workspace.
    assert summary == TreePurgeSummary(deleted_rows=4 + 8 + 1, deleted_packs=1)
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
    """The purge is idempotent: a second call succeeds and deletes nothing."""
    deleted: list[str] = []
    _purge(engine, _deleter(tmp_path / "objects", deleted))

    assert _purge(engine, _deleter(tmp_path / "objects", deleted)) == TreePurgeSummary(0, 0)
    assert deleted == [packs["ada"]]


def test_a_failed_pack_delete_fails_the_purge_and_keeps_the_rows(
    engine: Engine, objects: LocalObjectStore, packs: dict[str, str]
) -> None:
    """A failed S3 delete raises, and the rollback keeps every row of the account."""
    before = _rows(engine, "ada", ADA_RUNS)

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

    assert _rows(engine, "ada", ADA_RUNS) == before
    assert sorted(objects.list("")) == sorted(packs.values())
    assert _marked(engine) == set()


def test_a_purge_with_packs_and_no_bucket_fails(
    engine: Engine, packs: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no bucket set, a pack that must go fails the purge instead of staying behind."""
    monkeypatch.setattr(settings, "tree_bucket", None)

    with pytest.raises(TreePurgeError, match="SKYNET_TREE_BUCKET"):
        _purge(engine, None)


def test_a_purge_with_no_trees_needs_no_bucket(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """An account with no packs purges its workspace with no bucket set."""
    monkeypatch.setattr(settings, "tree_bucket", None)

    assert _purge(engine, None) == TreePurgeSummary(deleted_rows=1, deleted_packs=0)
    assert _rows(engine, "ada", ADA_RUNS) == (0, 0, 0)


def test_a_retry_after_a_partial_pack_delete_deletes_the_rest(
    engine: Engine, objects: LocalObjectStore, packs: dict[str, str], tmp_path: Path
) -> None:
    """The second of two pack deletes fails; the retry deletes the rows, both packs and their rows.

    The first pack object is already gone when the retry deletes it again, and that is no error.
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

    with pytest.raises(TreePurgeError):
        _purge(engine, delete_first_then_fail)
    assert objects.list("") == sorted([packs["shared"], own[1]])
    assert _rows(engine, "ada", ADA_RUNS) != (0, 0, 0)

    deleted: list[str] = []
    assert _purge(engine, _deleter(root, deleted)).deleted_packs == 2

    assert deleted == own
    assert _rows(engine, "ada", ADA_RUNS) == (0, 0, 0)
    assert objects.list("") == [packs["shared"]]
    with Session(engine) as session:
        assert set(session.scalars(select(StoragePackModel.pack_key))) == {packs["shared"]}


def test_the_compactor_rewrites_a_marked_full_pack_without_the_deleted_bytes(
    engine: Engine, objects: LocalObjectStore, packs: dict[str, str], tmp_path: Path
) -> None:
    """A shared pack that the purge marks is rewritten alone, at any size, with only the live entries."""
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


def test_the_purge_locks_the_jobs_then_the_packs_before_it_deletes(engine: Engine, packs: dict[str, str]) -> None:
    """The first statement locks the account's jobs in key order; the packs are locked before any delete."""
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


def test_an_open_purge_holds_the_account_job_and_pack_locks(engine: Engine, packs: dict[str, str]) -> None:
    """Until it commits, the purge holds the account's job rows and the pack rows; a flush or merge waits or gives up."""
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
    """The default delete function sends DeleteObjects batches of at most 1000 keys to the tree bucket."""
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
    """A DeleteObjects response with Errors raises TreePurgeError with the failed keys."""
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
    """A bucket with no boto3 fails the purge with TreePurgeError, not a bare RuntimeError."""
    monkeypatch.setattr(settings, "tree_bucket", "trees")
    monkeypatch.setattr(tree_compactor, "boto3", None)

    with pytest.raises(TreePurgeError, match="tree bucket") as error:
        _purge(engine, None)
    assert isinstance(error.value.__cause__, RuntimeError)
