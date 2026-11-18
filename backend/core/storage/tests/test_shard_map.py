"""Tests for the step log shard map and the step log on two shards (SKY-731).

Most tests use SQLite files: one main database with the ``jobs`` table and
one shard database with only the step log tables. The last test needs two
disposable PostgreSQL databases: set ``APP_TEST_DB_URL`` (main) and
``APP_TEST_SHARD_DB_URL`` (shard). It runs the Alembic env on both.
"""

from __future__ import annotations

import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, inspect, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from core.config import app_env, settings
from core.storage.migrate import sync_migration_head
from core.storage.models import AgentStepModel, Base, JobModel, StoragePackModel, TreeObjectModel
from core.storage.object_store import LocalObjectStore
from core.storage.shard_map import MAIN, ShardMap
from core.storage.step_log_store import StepFenceError, StepLogStore

BACKEND_DIR = Path(__file__).resolve().parents[3]
MAIN_DB_URL = app_env("APP_TEST_DB_URL")
SHARD_DB_URL = app_env("APP_TEST_SHARD_DB_URL")
needs_two_postgres = pytest.mark.skipif(
    not (MAIN_DB_URL or "").startswith("postgresql") or not (SHARD_DB_URL or "").startswith("postgresql"),
    reason="Set APP_TEST_DB_URL and APP_TEST_SHARD_DB_URL to two disposable PostgreSQL databases.",
)
RUNS = [f"run-{index}" for index in range(12)]
SHARD_TABLES = [AgentStepModel.__table__, TreeObjectModel.__table__, StoragePackModel.__table__]
TREE = "b" * 64


def _main_engine(path: Path) -> Engine:
    """Return a SQLite main database with the full schema and the test jobs at generation 1.

    Args:
        path: The database file.

    Returns:
        The engine.
    """
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for run_id in RUNS:
            session.add(JobModel(optimization_id=run_id, username="ada", execution_generation=1))
        session.commit()
    return engine


def _shard_engine(path: Path) -> Engine:
    """Return a SQLite shard database with only the step log tables, as the shard migration makes it.

    Args:
        path: The database file.

    Returns:
        The engine.
    """
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine, tables=SHARD_TABLES)
    return engine


def _recorded(engine: Engine, run_id: str) -> str | None:
    """Return the shard that the main database records for a run.

    Args:
        engine: The main database.
        run_id: The job id.

    Returns:
        The recorded shard name, or ``None``.
    """
    with Session(engine) as session:
        return session.scalar(select(JobModel.step_log_shard).where(JobModel.optimization_id == run_id))


def _step_runs(engine: Engine) -> set[str]:
    """Return the runs that have step rows in a database.

    Args:
        engine: The database.

    Returns:
        The run ids.
    """
    with Session(engine) as session:
        return set(session.scalars(select(AgentStepModel.run_id)))


def _commit(store: StepLogStore, run_id: str, seq: int, generation: int = 1) -> str:
    """Commit one command step for agent ``a1``.

    Args:
        store: The step log.
        run_id: The job id.
        seq: The step position.
        generation: The execution generation.

    Returns:
        The step id.
    """
    return store.commit(
        run_id=run_id,
        agent_id="a1",
        seq=seq,
        kind="command",
        execution_generation=generation,
        grant_list_id="g1",
        body={"command": f"echo {run_id} {seq}"},
        tree_id=TREE,
    )


@pytest.fixture
def main(tmp_path: Path) -> Engine:
    """Return the SQLite main database.

    Args:
        tmp_path: Pytest's temporary folder.

    Returns:
        The engine.
    """
    return _main_engine(tmp_path / "main.sqlite")


@pytest.fixture
def shard(tmp_path: Path) -> Engine:
    """Return the SQLite shard database ``s2``.

    Args:
        tmp_path: Pytest's temporary folder.

    Returns:
        The engine.
    """
    return _shard_engine(tmp_path / "s2.sqlite")


@pytest.fixture
def objects(tmp_path: Path) -> LocalObjectStore:
    """Return a local object store.

    Args:
        tmp_path: Pytest's temporary folder.

    Returns:
        The store.
    """
    return LocalObjectStore(tmp_path / "objects")


@pytest.fixture
def two_shard_log(main: Engine, shard: Engine, objects: LocalObjectStore) -> Iterator[StepLogStore]:
    """Yield a step log over the main database and shard ``s2``.

    Args:
        main: The main database.
        shard: The shard database.
        objects: The object store.

    Yields:
        The open step log; the fixture closes it.
    """
    store = StepLogStore(main, objects, "w1", flush_window_seconds=60.0, shards=ShardMap(main, {"s2": shard}))
    try:
        yield store
    finally:
        store.close()


def test_with_no_shard_config_every_run_uses_the_main_database_and_nothing_is_recorded(main: Engine) -> None:
    """With no shard config the map answers ``main`` and writes no row, as before the shard map."""
    shards = ShardMap(main)

    assert shards.single
    assert shards.shard_of("run-0") == MAIN
    with shards.for_run("run-0") as session:
        assert session.get_bind() is main
    assert _recorded(main, "run-0") is None


def test_settings_with_no_shards_give_the_main_database_only(main: Engine, monkeypatch) -> None:
    """``from_settings`` with an empty ``STEP_LOG_SHARDS`` map gives a single-shard map on the main engine."""
    monkeypatch.setattr(settings, "step_log_shards", {})
    shards = ShardMap.from_settings(main)

    assert shards.single
    assert shards.names() == (MAIN,)


def test_for_run_records_the_shard_once_and_the_same_run_always_maps_to_it(main: Engine, shard: Engine) -> None:
    """The first use records the shard on the job row; each later map, also a new one, returns the same shard."""
    first = ShardMap(main, {"s2": shard})
    names = {run_id: first.shard_of(run_id) for run_id in RUNS}

    assert {_recorded(main, run_id) for run_id in RUNS} == set(names.values()) == {MAIN, "s2"}
    again = ShardMap(main, {"s2": shard})
    assert {run_id: again.shard_of(run_id) for run_id in RUNS} == names
    with again.for_run(RUNS[0]) as session:
        assert session.get_bind() is (main if names[RUNS[0]] == MAIN else shard)


def test_adding_a_shard_never_moves_a_run_that_has_its_shard(main: Engine, shard: Engine, tmp_path: Path) -> None:
    """A third shard gets only new runs: each recorded run keeps its shard."""
    names = {run_id: ShardMap(main, {"s2": shard}).shard_of(run_id) for run_id in RUNS}
    third = _shard_engine(tmp_path / "s3.sqlite")
    grown = ShardMap(main, {"s2": shard, "s3": third})

    assert {run_id: grown.shard_of(run_id) for run_id in RUNS} == names


def test_a_run_with_steps_on_main_from_before_the_shard_config_stays_on_main(main: Engine, shard: Engine) -> None:
    """A run that wrote steps with no shard config keeps them: its first sharded use records ``main``."""
    names = (MAIN, "s2")
    run_id = next(run for run in RUNS if names[zlib.crc32(run.encode()) % 2] == "s2")
    with Session(main) as session:
        session.add(
            AgentStepModel(
                step_id=f"{run_id}:a1:0",
                run_id=run_id,
                agent_id="a1",
                seq=0,
                kind="model",
                execution_generation=1,
                grant_list_id="g1",
                pack_key="packs/w0/old.pack",
                pack_offset=0,
                pack_length=2,
            )
        )
        session.commit()

    assert ShardMap(main, {"s2": shard}).shard_of(run_id) == MAIN
    assert _recorded(main, run_id) == MAIN


def test_a_recorded_shard_that_is_not_configured_raises(main: Engine, shard: Engine) -> None:
    """A run on a shard that the config lost fails loudly, never falls back to another database."""
    with Session(main) as session:
        session.execute(update(JobModel).where(JobModel.optimization_id == "run-0").values(step_log_shard="gone"))
        session.commit()

    with pytest.raises(KeyError, match="gone"):
        ShardMap(main, {"s2": shard}).shard_of("run-0")


@pytest.mark.parametrize("name", [MAIN, "", "Bad Name", "x" * 65])
def test_a_bad_shard_name_is_refused(main: Engine, shard: Engine, name: str) -> None:
    """The name ``main`` is the main database; other names are short lowercase ids."""
    with pytest.raises(ValueError):
        ShardMap(main, {name: shard})


def test_runs_land_on_both_shards_and_each_reads_back_its_full_step_log(
    main: Engine, shard: Engine, two_shard_log: StepLogStore
) -> None:
    """Each run's steps go to its shard only, and every step reads back from there."""
    steps = {run_id: [_commit(two_shard_log, run_id, seq) for seq in range(3)] for run_id in RUNS}
    two_shard_log.flush()
    for ids in steps.values():
        for step_id in ids:
            two_shard_log.durable(step_id, 1, timeout=5)

    on_main, on_shard = _step_runs(main), _step_runs(shard)
    assert on_main
    assert on_shard
    assert not on_main & on_shard
    assert on_main | on_shard == set(RUNS)
    for run_id, ids in steps.items():
        assert run_id in (on_main if _recorded(main, run_id) == MAIN else on_shard)
        assert [two_shard_log.get_step(step_id)["body"] for step_id in ids] == [
            {"command": f"echo {run_id} {seq}"} for seq in range(3)
        ]
    assert not inspect(shard).has_table("jobs")


def test_a_stale_step_on_a_shard_fails_the_fence_of_the_main_jobs_row(
    main: Engine, shard: Engine, two_shard_log: StepLogStore
) -> None:
    """The fence reads the generation on the main database also for a run on another shard."""
    run_id = next(run for run in RUNS if two_shard_log._shards.shard_of(run) == "s2")
    with Session(main) as session:
        session.execute(update(JobModel).where(JobModel.optimization_id == run_id).values(execution_generation=2))
        session.commit()
    stale = _commit(two_shard_log, run_id, 0, generation=1)
    fresh = _commit(two_shard_log, run_id, 1, generation=2)
    two_shard_log.flush()

    with pytest.raises(StepFenceError):
        two_shard_log.durable(stale, 1, timeout=5)
    two_shard_log.durable(fresh, 2, timeout=5)
    assert _step_runs(shard) == {run_id}
    with Session(shard) as session:
        assert set(session.scalars(select(AgentStepModel.step_id))) == {fresh}
    # A durable call with no waiter asks the databases: the row on the shard, the generation on main.
    two_shard_log.durable(fresh, 2)


def test_each_shard_gets_its_own_pack_with_its_own_pack_row(
    main: Engine, shard: Engine, two_shard_log: StepLogStore, objects: LocalObjectStore
) -> None:
    """One flush of runs on two shards writes one pack for each shard; each pack row is on its shard."""
    for run_id in RUNS:
        _commit(two_shard_log, run_id, 0)
    two_shard_log.flush()

    packs = list(objects.list("packs/w1/"))
    assert len(packs) == 2
    with Session(main) as session:
        main_packs = set(session.scalars(select(StoragePackModel.pack_key)))
    with Session(shard) as session:
        shard_packs = set(session.scalars(select(StoragePackModel.pack_key)))
    assert len(main_packs) == len(shard_packs) == 1
    assert main_packs | shard_packs == set(packs)


def test_recover_applies_a_crashed_pack_to_its_own_shard(
    main: Engine, shard: Engine, objects: LocalObjectStore, monkeypatch
) -> None:
    """A pack whose transaction never ran lands on the shard its manifest names."""
    shards = ShardMap(main, {"s2": shard})
    run_id = next(run for run in RUNS if shards.shard_of(run) == "s2")
    crashed = StepLogStore(main, objects, "w1", flush_window_seconds=60.0, shards=shards)

    def crash(*args: Any) -> Any:
        """Stand for the process dying between the pack PUT and its transaction.

        Args:
            *args: The apply arguments.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("killed")

    monkeypatch.setattr(crashed, "_apply", crash)
    step_id = _commit(crashed, run_id, 0)
    with pytest.raises(RuntimeError, match="killed"):
        crashed.flush()
    with pytest.raises(RuntimeError, match="killed"):
        crashed.close()

    restarted = StepLogStore(main, objects, "w1", flush_window_seconds=60.0, shards=ShardMap(main, {"s2": shard}))
    try:
        assert restarted.recover() == 1
        assert restarted.recover() == 0
        assert restarted.get_step(step_id)["body"] == {"command": f"echo {run_id} 0"}
    finally:
        restarted.close()
    assert _step_runs(shard) == {run_id}
    assert _step_runs(main) == set()


def test_a_tree_object_on_one_shard_is_readable_by_every_run_of_the_account(
    main: Engine, shard: Engine, two_shard_log: StepLogStore
) -> None:
    """After its flush, a run on the other shard of the same account reads the object."""
    shards = two_shard_log._shards
    writer = next(run for run in RUNS if shards.shard_of(run) == "s2")
    reader = next(run for run in RUNS if shards.shard_of(run) == MAIN)
    two_shard_log.put_object(writer, "c" * 64, b"tree bytes")
    two_shard_log.flush()

    with Session(shard) as session:
        assert session.scalar(select(TreeObjectModel.object_id)) == "c" * 64
    assert two_shard_log.get_object(reader, "c" * 64) == b"tree bytes"
    assert two_shard_log.get_object("no-such-run", "c" * 64) is None


@needs_two_postgres
def test_alembic_migrates_the_main_database_and_the_step_log_tables_on_a_shard(
    objects: LocalObjectStore, monkeypatch
) -> None:
    """The boot migration stamps or upgrades main and creates only the step log tables on the shard."""
    monkeypatch.setattr(settings, "step_log_shards", {"s2": SHARD_DB_URL})
    main = create_engine(MAIN_DB_URL or "")
    shard = create_engine(SHARD_DB_URL or "")
    try:
        # The boot path of a new database: create_all, then a stamp; the second sync is the upgrade of an adopted one.
        # Prod boot skips the pgvector tables when the extension is absent.
        Base.metadata.create_all(main, tables=[t for t in Base.metadata.sorted_tables if "embedding" not in t.name])
        sync_migration_head(main)
        sync_migration_head(main)
        main_tables = set(inspect(main).get_table_names())
        shard_tables = set(inspect(shard).get_table_names())
        assert {"jobs", "agent_steps"} <= main_tables
        assert "step_log_shard" in {column["name"] for column in inspect(main).get_columns("jobs")}
        assert shard_tables == {"agent_steps", "tree_objects", "storage_packs", "alembic_version"}
        assert inspect(shard).get_foreign_keys("agent_steps") == []

        with Session(main) as session:
            for run_id in RUNS:
                session.add(JobModel(optimization_id=run_id, username="ada", execution_generation=1))
            session.commit()
        store = StepLogStore(main, objects, "w1", flush_window_seconds=60.0, shards=ShardMap(main, {"s2": shard}))
        try:
            steps = {run_id: _commit(store, run_id, 0) for run_id in RUNS}
            store.flush()
            for run_id, step_id in steps.items():
                store.durable(step_id, 1, timeout=5)
                assert store.get_step(step_id)["body"] == {"command": f"echo {run_id} 0"}
        finally:
            store.close()
        assert _step_runs(main)
        assert _step_runs(shard)
        assert _step_runs(main) | _step_runs(shard) == set(RUNS)
    finally:
        with main.begin() as connection:
            connection.execute(text("DELETE FROM jobs WHERE optimization_id LIKE 'run-%'"))
        with shard.begin() as connection:
            connection.execute(text("DELETE FROM agent_steps"))
            connection.execute(text("DELETE FROM tree_objects"))
            connection.execute(text("DELETE FROM storage_packs"))
        main.dispose()
        shard.dispose()
