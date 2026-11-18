"""Tests for the tree purge on account deletion (SKY-733).

A real step log writes packs to a local object store: one pack holds the
entries of two accounts, and one holds only the entries of the deleted
account. Runs on a SQLite file, and on PostgreSQL when ``APP_TEST_DB_URL``
names one.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from core.config import app_env, settings
from core.storage import tree_purge
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
    """Return a delete function over the local store folder that records each key.

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


def _purge(engine: Engine, delete_object: Any) -> TreePurgeSummary:
    """Purge ``ada`` in one committed transaction.

    Args:
        engine: The test engine.
        delete_object: The pack delete function.

    Returns:
        The purge summary.
    """
    with Session(engine) as session:
        summary = purge_user(session, "ada", ADA_RUNS, delete_object=delete_object)
        session.commit()
    return summary


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

    def fail(key: str) -> None:
        """Fail like an S3 outage.

        Args:
            key: The object key.

        Raises:
            OSError: Always.
        """
        raise OSError("S3 is down")

    with pytest.raises(TreePurgeError, match=packs["ada"]):
        _purge(engine, fail)

    assert _rows(engine, "ada", ADA_RUNS) == before
    assert sorted(objects.list("")) == sorted(packs.values())


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


def test_bucket_deleter_deletes_from_the_tree_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default delete function deletes the key from the bucket that SKYNET_TREE_BUCKET names."""
    calls: list[dict[str, str]] = []
    client = type("Client", (), {"delete_object": lambda self, **kwargs: calls.append(kwargs)})()
    monkeypatch.setattr(settings, "tree_bucket", "trees")
    monkeypatch.setattr(tree_purge, "boto3", type("Boto3", (), {"client": staticmethod(lambda name: client)}))

    tree_purge.bucket_deleter()("packs/w1/p")

    assert calls == [{"Bucket": "trees", "Key": "packs/w1/p"}]
