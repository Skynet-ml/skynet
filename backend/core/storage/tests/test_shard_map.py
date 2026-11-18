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
from alembic.script import ScriptDirectory
from pydantic import SecretStr
from sqlalchemy import create_engine, event, inspect, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from core.config import app_env, settings
from core.storage import shard_map as shard_map_mod
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


def test_shard_urls_from_the_environment_stay_secret(monkeypatch) -> None:
    """A shard URL carries a password: the settings hide it in logs and reprs."""
    monkeypatch.setenv("STEP_LOG_SHARDS", '{"s2": "postgresql://u:pw@h/db"}')

    shards = type(settings)().step_log_shards

    assert "pw" not in repr(shards)
    assert shards["s2"].get_secret_value() == "postgresql://u:pw@h/db"


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


def _set_shard(engine: Engine, run_id: str, name: str) -> None:
    """Record a shard for a run, as another pod with a shard map does.

    Args:
        engine: The main database.
        run_id: The job id.
        name: The shard name.
    """
    with Session(engine) as session:
        session.execute(update(JobModel).where(JobModel.optimization_id == run_id).values(step_log_shard=name))
        session.commit()


def _rows(engine: Engine, step_id: str) -> int:
    """Return the number of rows of a step in a database.

    Args:
        engine: The database.
        step_id: The step id.

    Returns:
        The row count.
    """
    with Session(engine) as session:
        return len(session.scalars(select(AgentStepModel.step_id).where(AgentStepModel.step_id == step_id)).all())


def _a_run_on(shards: ShardMap, name: str) -> str:
    """Return a test run that the map puts on a shard.

    Args:
        shards: The shard map.
        name: The shard name.

    Returns:
        The run id.
    """
    return next(run for run in RUNS if shards.shard_of(run) == name)


def _pending_ids(store: StepLogStore) -> list[str]:
    """Return the step ids in the store's buffer, in order.

    Args:
        store: The step log.

    Returns:
        The step ids.
    """
    return [item.step_id for item, _ in store._pending if hasattr(item, "step_id")]


def test_a_map_with_no_shard_refuses_a_run_that_another_pod_put_on_a_shard(
    main: Engine, objects: LocalObjectStore
) -> None:
    """A pod with no shard config must not read or write a sharded run on the main database."""
    _set_shard(main, "run-0", "s2")

    with pytest.raises(RuntimeError, match="STEP_LOG_SHARDS"):
        ShardMap(main).shard_of("run-1")
    store = StepLogStore(main, objects, "w1", flush_window_seconds=60.0, shards=ShardMap(main))
    try:
        _commit(store, "run-1", 0)
        with pytest.raises(RuntimeError, match="STEP_LOG_SHARDS"):
            store.flush()
        assert _step_runs(main) == set()
    finally:
        _set_shard(main, "run-0", MAIN)
        store.close()
    assert _step_runs(main) == {"run-1"}


@pytest.mark.parametrize(("down", "up"), [("s2", MAIN), (MAIN, "s2")])
def test_a_failed_shard_does_not_stop_the_flushes_of_the_other_shard(
    main: Engine, shard: Engine, two_shard_log: StepLogStore, monkeypatch, down: str, up: str
) -> None:
    """While one shard fails, each flush still records the runs of the other; the failed one lands once it is back.

    Both orders run, because a flush visits the shards in name order.
    """
    on_down, on_up = _a_run_on(two_shard_log.shards, down), _a_run_on(two_shard_log.shards, up)
    engines = {MAIN: main, "s2": shard}
    apply = two_shard_log._apply
    broken = True

    def fail_down(shard_name: str, *args: Any) -> Any:
        """Fail every apply on the failed shard while it is down.

        Args:
            shard_name: The shard of the pack.
            *args: The other apply arguments.

        Returns:
            The apply result.

        Raises:
            RuntimeError: For the failed shard while it is down.
        """
        if shard_name == down and broken:
            raise RuntimeError("shard down")
        return apply(shard_name, *args)

    monkeypatch.setattr(two_shard_log, "_apply", fail_down)
    held = _commit(two_shard_log, on_down, 0)
    for seq in range(2):
        step_id = _commit(two_shard_log, on_up, seq)
        with pytest.raises(RuntimeError, match="shard down"):
            two_shard_log.flush()
        two_shard_log.durable(step_id, 1, timeout=5)
    assert _rows(engines[down], held) == 0

    broken = False
    two_shard_log.flush()
    two_shard_log.durable(held, 1, timeout=5)
    assert _rows(engines[down], held) == 1


def test_a_failed_put_of_the_second_shard_keeps_the_first_and_requeues_the_second_once(
    main: Engine, shard: Engine, two_shard_log: StepLogStore, objects: LocalObjectStore, monkeypatch
) -> None:
    """The main pack lands; the ``s2`` entries go back to the buffer once, and a retry lands each step once."""
    on_main, on_s2 = _a_run_on(two_shard_log.shards, MAIN), _a_run_on(two_shard_log.shards, "s2")
    first = _commit(two_shard_log, on_main, 0)
    second = [_commit(two_shard_log, on_s2, seq) for seq in range(2)]
    put = objects.put
    calls: list[str] = []

    def fail_the_second_put(key: str, data: bytes) -> Any:
        """Fail the second pack PUT of the flush, the one of ``s2``.

        Args:
            key: The pack key.
            data: The pack bytes.

        Returns:
            The PUT result.

        Raises:
            RuntimeError: On the second call.
        """
        calls.append(key)
        if len(calls) == 2:
            raise RuntimeError("put failed")
        return put(key, data)

    monkeypatch.setattr(objects, "put", fail_the_second_put)
    with pytest.raises(RuntimeError, match="put failed"):
        two_shard_log.flush()

    two_shard_log.durable(first, 1, timeout=5)
    assert _pending_ids(two_shard_log) == second
    assert not two_shard_log._unapplied
    two_shard_log.flush()
    for step_id in second:
        two_shard_log.durable(step_id, 1, timeout=5)
    assert [_rows(main, first), *(_rows(shard, step_id) for step_id in second)] == [1, 1, 1]
    assert len(calls) == 3


def test_a_failed_apply_of_the_second_shard_keeps_its_pack_and_records_it_once(
    main: Engine, shard: Engine, two_shard_log: StepLogStore, objects: LocalObjectStore, monkeypatch
) -> None:
    """The main pack lands; ``s2`` keeps its written pack, and the retry records it with no new PUT."""
    on_main, on_s2 = _a_run_on(two_shard_log.shards, MAIN), _a_run_on(two_shard_log.shards, "s2")
    first = _commit(two_shard_log, on_main, 0)
    second = [_commit(two_shard_log, on_s2, seq) for seq in range(2)]
    apply = two_shard_log._apply
    failures: list[str] = []

    def fail_s2_once(shard_name: str, *args: Any) -> Any:
        """Fail the first apply on ``s2``.

        Args:
            shard_name: The shard of the pack.
            *args: The other apply arguments.

        Returns:
            The apply result.

        Raises:
            RuntimeError: On the first apply on ``s2``.
        """
        if shard_name == "s2" and not failures:
            failures.append(shard_name)
            raise RuntimeError("apply failed")
        return apply(shard_name, *args)

    monkeypatch.setattr(two_shard_log, "_apply", fail_s2_once)
    with pytest.raises(RuntimeError, match="apply failed"):
        two_shard_log.flush()

    two_shard_log.durable(first, 1, timeout=5)
    assert _pending_ids(two_shard_log) == []
    assert list(two_shard_log._unapplied) == ["s2"]
    packs = set(objects.list("packs/w1/"))
    assert len(packs) == 2
    two_shard_log.flush()
    for step_id in second:
        two_shard_log.durable(step_id, 1, timeout=5)
    assert set(objects.list("packs/w1/")) == packs
    assert [_rows(main, first), *(_rows(shard, step_id) for step_id in second)] == [1, 1, 1]


@pytest.mark.parametrize(("down", "up"), [("s2", MAIN), (MAIN, "s2")])
def test_recover_applies_the_packs_of_a_healthy_shard_when_another_fails(
    main: Engine, shard: Engine, objects: LocalObjectStore, monkeypatch, down: str, up: str
) -> None:
    """A crash left a pack on each shard; recovery records the healthy one although the other still fails."""
    shards = ShardMap(main, {"s2": shard})
    engines = {MAIN: main, "s2": shard}
    on_down, on_up = _a_run_on(shards, down), _a_run_on(shards, up)
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
    kept = _commit(crashed, on_up, 0)
    held = _commit(crashed, on_down, 0)
    with pytest.raises(RuntimeError, match="killed"):
        crashed.flush()
    with pytest.raises(RuntimeError, match="killed"):
        crashed.close()

    restarted = StepLogStore(main, objects, "w1", flush_window_seconds=60.0, shards=ShardMap(main, {"s2": shard}))
    apply = restarted._apply

    def fail_down(shard_name: str, *args: Any) -> Any:
        """Fail every apply on the failed shard.

        Args:
            shard_name: The shard of the pack.
            *args: The other apply arguments.

        Returns:
            The apply result.

        Raises:
            RuntimeError: For the failed shard.
        """
        if shard_name == down:
            raise RuntimeError("shard down")
        return apply(shard_name, *args)

    monkeypatch.setattr(restarted, "_apply", fail_down)
    try:
        with pytest.raises(RuntimeError, match="shard down"):
            restarted.recover()
        assert _rows(engines[up], kept) == 1
        assert _rows(engines[down], held) == 0
        monkeypatch.setattr(restarted, "_apply", apply)
        assert restarted.recover() == 1
        assert _rows(engines[down], held) == 1
    finally:
        restarted.close()


def test_a_shard_apply_connects_to_the_shard_before_it_locks_the_main_jobs_rows(
    main: Engine, shard: Engine, two_shard_log: StepLogStore
) -> None:
    """A shard that does not answer must hold no ``jobs`` row lock on the main database."""
    run_id = _a_run_on(two_shard_log.shards, "s2")
    step_id = _commit(two_shard_log, run_id, 0)
    order: list[str] = []

    def on_main(*args: Any) -> None:
        """Note a main database connection checkout.

        Args:
            *args: The pool event arguments.
        """
        order.append(MAIN)

    def on_shard(*args: Any) -> None:
        """Note a shard connection checkout.

        Args:
            *args: The pool event arguments.
        """
        order.append("s2")

    event.listen(main, "checkout", on_main)
    event.listen(shard, "checkout", on_shard)
    try:
        two_shard_log.flush()
    finally:
        event.remove(main, "checkout", on_main)
        event.remove(shard, "checkout", on_shard)
    two_shard_log.durable(step_id, 1, timeout=5)
    assert order[:2] == ["s2", MAIN]


def test_a_store_closes_only_the_shard_engines_that_it_created(
    main: Engine, objects: LocalObjectStore, tmp_path: Path, monkeypatch
) -> None:
    """A store that builds its map from the settings closes those pools; a passed map stays open."""
    _shard_engine(tmp_path / "s3.sqlite").dispose()
    monkeypatch.setattr(settings, "step_log_shards", {"s3": SecretStr(f"sqlite:///{tmp_path / 's3.sqlite'}")})
    own = StepLogStore(main, objects, "w1", flush_window_seconds=60.0)
    built = own.shards.engine("s3")
    pool = built.pool
    own.close()
    assert built.pool is not pool

    shards = ShardMap.from_settings(main)
    passed = StepLogStore(main, objects, "w2", flush_window_seconds=60.0, shards=shards)
    pool = shards.engine("s3").pool
    passed.close()
    assert shards.engine("s3").pool is pool
    shards.dispose()


def test_shard_engines_on_postgresql_get_connect_and_statement_timeouts(main: Engine, monkeypatch) -> None:
    """A PostgreSQL shard engine fails fast when the shard hangs; a SQLite one gets no PostgreSQL option."""
    made: dict[str, dict[str, Any]] = {}

    def record(url: str, **options: Any) -> Engine:
        """Record the engine options instead of building an engine.

        Args:
            url: The database URL.
            **options: The engine options.

        Returns:
            The main engine, as a stand-in.
        """
        made[url.split(":", 1)[0]] = options
        return main

    monkeypatch.setattr(shard_map_mod, "create_engine", record)
    monkeypatch.setattr(
        settings,
        "step_log_shards",
        {"s2": SecretStr("postgresql+psycopg://u@h/db"), "s3": SecretStr("sqlite:///x.sqlite")},
    )
    ShardMap.from_settings(main)

    assert made["postgresql+psycopg"]["connect_args"] == {
        "connect_timeout": shard_map_mod.SHARD_CONNECT_TIMEOUT_SECONDS,
        "options": f"-c statement_timeout={shard_map_mod.SHARD_STATEMENT_TIMEOUT_MS}",
    }
    assert "connect_args" not in made["sqlite"]


def test_the_alembic_upgrade_steps_of_the_shard_chain_still_resolve() -> None:
    """``alembic/env.py`` calls the private ``_upgrade_revs``; an alembic upgrade that renames it fails here."""
    versions = BACKEND_DIR / "alembic" / "step_log_versions"
    script = ScriptDirectory(str(versions.parent), version_locations=[str(versions)])

    steps = script._upgrade_revs("head", ())

    assert [step.revision.revision for step in steps] == ["3c4a41ece580"]


@needs_two_postgres
def test_alembic_migrates_the_main_database_and_the_step_log_tables_on_a_shard(
    objects: LocalObjectStore, monkeypatch
) -> None:
    """The boot migration stamps or upgrades main and creates only the step log tables on the shard."""
    monkeypatch.setattr(settings, "step_log_shards", {"s2": SecretStr(SHARD_DB_URL or "")})
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
