"""Tests for the lazy per-workspace data migration on ``workspaces.storage_version``.

Most tests use a SQLite file. SQLite has no row locks, so the two-workers
test runs only when ``APP_TEST_DB_URL`` names a disposable PostgreSQL
database, in its own schema that it drops after.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from core.config import app_env
from core.storage import storage_version
from core.storage.models import Base, WorkspaceModel
from core.storage.storage_version import (
    BASE_VERSION,
    StorageUpgradeError,
    StorageVersionAheadError,
    StorageVersionBelowBaseError,
    current_version,
    ensure_current,
)

TEST_DB_URL = app_env("APP_TEST_DB_URL")
needs_postgres = pytest.mark.skipif(
    not TEST_DB_URL or not TEST_DB_URL.startswith("postgresql"),
    reason="Set APP_TEST_DB_URL to a disposable PostgreSQL database.",
)


def _add_workspace(engine: Engine, workspace_id: str = "ws-1", version: int = BASE_VERSION) -> None:
    """Insert one workspace at a storage version.

    Args:
        engine: The database.
        workspace_id: The workspace id.
        version: Its ``storage_version``.
    """
    with Session(engine) as session:
        session.add(WorkspaceModel(id=workspace_id, username="ada", policy={}, storage_version=version))
        session.commit()


def _workspace(engine: Engine, workspace_id: str = "ws-1") -> WorkspaceModel:
    """Return a workspace row as the database holds it now.

    Args:
        engine: The database.
        workspace_id: The workspace id.

    Returns:
        The detached row.
    """
    with Session(engine, expire_on_commit=False) as session:
        return session.execute(select(WorkspaceModel).where(WorkspaceModel.id == workspace_id)).scalar_one()


def _recording_step(calls: list[str], name: str) -> Callable[[Session, str], None]:
    """Return an upgrade step that records its name and writes it into the workspace policy.

    Args:
        calls: The list that gets one name per run.
        name: The step name.

    Returns:
        The step.
    """

    def step(session: Session, workspace_id: str) -> None:
        """Record the run and write the step name in the workspace's transaction.

        Args:
            session: The upgrade transaction.
            workspace_id: The workspace to upgrade.
        """
        calls.append(name)
        row = session.get(WorkspaceModel, workspace_id)
        row.policy = {**row.policy, name: True}

    return step


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Yield a fresh SQLite file engine with the schema, then dispose of it.

    Args:
        tmp_path: Pytest's temporary folder.

    Yields:
        The engine.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def _duplicate_row_step(session: Session, workspace_id: str) -> None:
    """Add a second row with the workspace's id, without a flush, so only a later flush fails.

    Args:
        session: The upgrade transaction.
        workspace_id: The workspace to upgrade.
    """
    session.add(WorkspaceModel(id=workspace_id, username="dup", policy={}, storage_version=BASE_VERSION))


def test_the_registry_starts_empty_at_version_1() -> None:
    """Version 1 is the step log and tree format, and no upgrade step exists yet."""
    assert BASE_VERSION == 1
    assert storage_version.UPGRADES == ()
    assert current_version() == 1


def test_a_current_workspace_runs_no_step(engine) -> None:
    """A workspace at the current version stays as it is."""
    _add_workspace(engine)
    assert ensure_current(engine, "ws-1") == 1
    assert _workspace(engine).storage_version == 1


def test_only_the_missing_steps_run_in_order(engine, monkeypatch) -> None:
    """A workspace at version 2 of 4 runs the steps to 3 and to 4, in that order, and ends at 4."""
    calls: list[str] = []
    steps = tuple(_recording_step(calls, name) for name in ("to2", "to3", "to4"))
    monkeypatch.setattr(storage_version, "UPGRADES", steps)
    _add_workspace(engine, version=2)

    assert ensure_current(engine, "ws-1") == 4

    assert calls == ["to3", "to4"]
    row = _workspace(engine)
    assert row.storage_version == 4
    assert row.policy == {"to3": True, "to4": True}


def test_a_second_call_runs_no_step_again(engine, monkeypatch) -> None:
    """After the upgrade, the next wake finds the workspace current and runs nothing."""
    calls: list[str] = []
    monkeypatch.setattr(storage_version, "UPGRADES", (_recording_step(calls, "to2"),))
    _add_workspace(engine)

    ensure_current(engine, "ws-1")
    ensure_current(engine, "ws-1")

    assert calls == ["to2"]


def test_a_failed_step_keeps_the_old_version_and_raises_a_typed_error(engine, monkeypatch) -> None:
    """The step to 3 fails: the step to 2 stays, the failed step's writes roll back, and the version stays 2."""
    calls: list[str] = []
    boom = RuntimeError("boom")

    def failing(session: Session, workspace_id: str) -> None:
        """Write into the workspace, then fail.

        Args:
            session: The upgrade transaction.
            workspace_id: The workspace to upgrade.
        """
        _recording_step(calls, "to3")(session, workspace_id)
        session.flush()
        raise boom

    monkeypatch.setattr(storage_version, "UPGRADES", (_recording_step(calls, "to2"), failing))
    _add_workspace(engine)

    with pytest.raises(StorageUpgradeError) as caught:
        ensure_current(engine, "ws-1")

    assert caught.value.workspace_id == "ws-1"
    assert caught.value.from_version == 2
    assert caught.value.__cause__ is boom
    row = _workspace(engine)
    assert row.storage_version == 2
    assert row.policy == {"to2": True}


def test_an_unflushed_step_write_that_fails_raises_a_typed_error(engine, monkeypatch) -> None:
    """A step's write that fails only at the flush raises ``StorageUpgradeError`` and keeps the version."""
    monkeypatch.setattr(storage_version, "UPGRADES", (_duplicate_row_step,))
    _add_workspace(engine)

    with pytest.raises(StorageUpgradeError) as caught:
        ensure_current(engine, "ws-1")

    assert caught.value.from_version == 1
    row = _workspace(engine)
    assert row.storage_version == 1
    assert row.username == "ada"


def test_a_failed_commit_raises_a_typed_error(engine, monkeypatch) -> None:
    """Only the commit fails: ``StorageUpgradeError`` names it as the cause, and the version and writes stay."""
    calls: list[str] = []
    monkeypatch.setattr(storage_version, "UPGRADES", (_recording_step(calls, "to2"),))
    _add_workspace(engine)
    boom = RuntimeError("commit lost")
    real_commit = Session.commit
    commits = [0]

    def commit_fails_once(self: Session) -> None:
        """Fail the first commit, then commit as normal.

        Args:
            self: The session.
        """
        commits[0] += 1
        if commits[0] == 1:
            raise boom
        real_commit(self)

    monkeypatch.setattr(Session, "commit", commit_fails_once)

    with pytest.raises(StorageUpgradeError) as caught:
        ensure_current(engine, "ws-1")

    assert calls == ["to2"]
    assert caught.value.from_version == 1
    assert caught.value.__cause__ is boom
    row = _workspace(engine)
    assert row.storage_version == 1
    assert row.policy == {}


def test_a_version_below_the_base_raises_before_any_step(engine, monkeypatch) -> None:
    """A stored version 0 has no step; a negative index would run the last step, so nothing runs."""
    calls: list[str] = []
    monkeypatch.setattr(storage_version, "UPGRADES", (_recording_step(calls, "to2"),))
    _add_workspace(engine, version=0)

    with pytest.raises(StorageVersionBelowBaseError) as caught:
        ensure_current(engine, "ws-1")

    assert caught.value.workspace_id == "ws-1"
    assert caught.value.stored_version == 0
    assert calls == []
    assert _workspace(engine).storage_version == 0


def test_a_retry_after_a_failed_step_runs_only_that_step(engine, monkeypatch) -> None:
    """Once the failed step works, the retry runs it and not the step that already landed."""
    calls: list[str] = []
    runs = [0]

    def flaky(session: Session, workspace_id: str) -> None:
        """Fail on the first run, then record like a normal step.

        Args:
            session: The upgrade transaction.
            workspace_id: The workspace to upgrade.
        """
        runs[0] += 1
        if runs[0] == 1:
            raise RuntimeError("first run fails")
        _recording_step(calls, "to3")(session, workspace_id)

    monkeypatch.setattr(storage_version, "UPGRADES", (_recording_step(calls, "to2"), flaky))
    _add_workspace(engine)

    with pytest.raises(StorageUpgradeError):
        ensure_current(engine, "ws-1")
    assert ensure_current(engine, "ws-1") == 3

    assert calls == ["to2", "to3"]


def test_data_newer_than_this_code_raises(engine) -> None:
    """A worker with older code does not touch data that a newer format wrote."""
    _add_workspace(engine, version=2)
    with pytest.raises(StorageVersionAheadError) as caught:
        ensure_current(engine, "ws-1")
    assert caught.value.workspace_id == "ws-1"
    assert caught.value.stored_version == 2


def test_a_locked_read_newer_than_this_code_raises(engine, monkeypatch) -> None:
    """Newer code upgrades the data between the unlocked and the locked read: no step runs, and the call raises."""
    calls: list[str] = []
    monkeypatch.setattr(storage_version, "UPGRADES", (_recording_step(calls, "to2"),))
    _add_workspace(engine)

    def stored_version(session: Session, workspace_id: str, *, lock: bool) -> int:
        """Return version 1 to the unlocked read and version 3 to the locked read.

        Args:
            session: The open session.
            workspace_id: The workspace.
            lock: Whether the caller asks for the row lock.

        Returns:
            The version that the read sees.
        """
        return 3 if lock else 1

    monkeypatch.setattr(storage_version, "_stored_version", stored_version)

    with pytest.raises(StorageVersionAheadError) as caught:
        ensure_current(engine, "ws-1")

    assert caught.value.stored_version == 3
    assert calls == []


def test_an_unknown_workspace_raises_lookup_error(engine) -> None:
    """A missing workspace row is an error, not a silent no-op."""
    with pytest.raises(LookupError, match="ws-missing"):
        ensure_current(engine, "ws-missing")


@pytest.fixture
def postgres_engine() -> Iterator[Engine]:
    """Yield an engine on a fresh schema of the disposable test database, then drop the schema.

    Yields:
        The engine, with the workspaces table.
    """
    schema = f"storver_{uuid.uuid4().hex[:10]}"
    admin = create_engine(TEST_DB_URL)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    # The application name marks this test's sessions, so a lock count sees no other session of the database.
    engine = create_engine(TEST_DB_URL, connect_args={"options": f"-csearch_path={schema} -capplication_name={schema}"})
    try:
        Base.metadata.create_all(engine, tables=[WorkspaceModel.__table__], checkfirst=False)
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def _lock_waiters(engine: Engine) -> int:
    """Return how many sessions of this engine wait for a lock now.

    Args:
        engine: The database, with an application name that only this test uses.

    Returns:
        The number of waiting sessions.
    """
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                " AND application_name = current_setting('application_name') AND wait_event_type = 'Lock'"
            )
        ).scalar_one()


@needs_postgres
def test_postgres_two_workers_run_each_step_once(postgres_engine, monkeypatch) -> None:
    """Two workers wake the same workspace at once; the row lock lets each step run one time."""
    calls: list[str] = []
    in_first_step = threading.Event()
    release = threading.Event()

    def held(session: Session, workspace_id: str) -> None:
        """Record the run, then hold the row lock until the test releases it.

        Args:
            session: The upgrade transaction.
            workspace_id: The workspace to upgrade.
        """
        _recording_step(calls, "to2")(session, workspace_id)
        in_first_step.set()
        assert release.wait(30)

    monkeypatch.setattr(storage_version, "UPGRADES", (held, _recording_step(calls, "to3")))
    _add_workspace(postgres_engine)
    results: dict[str, int] = {}
    errors: list[BaseException] = []

    def wake(name: str) -> None:
        """Run ``ensure_current`` as one worker and keep its result.

        Args:
            name: The worker name.
        """
        try:
            results[name] = ensure_current(postgres_engine, "ws-1")
        except BaseException as error:  # The main thread asserts on it; a thread error alone does not fail the test.
            errors.append(error)

    first = threading.Thread(target=wake, args=("first",))
    first.start()
    assert in_first_step.wait(10)
    second = threading.Thread(target=wake, args=("second",))
    second.start()
    deadline = time.monotonic() + 10
    try:
        while _lock_waiters(postgres_engine) < 1:
            assert time.monotonic() < deadline, "the second worker never waited for the row lock"
            time.sleep(0.05)
    finally:
        # A failed assert must not leave the first worker to hold the lock for its full wait.
        release.set()
    first.join(30)
    second.join(30)
    assert not first.is_alive(), "the first worker did not stop"
    assert not second.is_alive(), "the second worker did not stop"

    assert errors == []
    assert results == {"first": 3, "second": 3}
    assert sorted(calls) == ["to2", "to3"]
    assert _workspace(postgres_engine).storage_version == 3


@needs_postgres
def test_postgres_a_failed_step_keeps_the_old_version(postgres_engine, monkeypatch) -> None:
    """On PostgreSQL, a failed step rolls back its writes and leaves the version."""

    def failing(session: Session, workspace_id: str) -> None:
        """Change the workspace, then fail.

        Args:
            session: The upgrade transaction.
            workspace_id: The workspace to upgrade.
        """
        session.execute(update(WorkspaceModel).where(WorkspaceModel.id == workspace_id).values(username="bob"))
        raise RuntimeError("boom")

    monkeypatch.setattr(storage_version, "UPGRADES", (failing,))
    _add_workspace(postgres_engine)

    with pytest.raises(StorageUpgradeError):
        ensure_current(postgres_engine, "ws-1")

    row = _workspace(postgres_engine)
    assert row.storage_version == 1
    assert row.username == "ada"


@needs_postgres
def test_postgres_an_unflushed_step_write_that_fails_raises_a_typed_error(postgres_engine, monkeypatch) -> None:
    """On PostgreSQL, a step's write that fails only at the flush raises ``StorageUpgradeError``."""
    monkeypatch.setattr(storage_version, "UPGRADES", (_duplicate_row_step,))
    _add_workspace(postgres_engine)

    with pytest.raises(StorageUpgradeError):
        ensure_current(postgres_engine, "ws-1")

    assert _workspace(postgres_engine).storage_version == 1
