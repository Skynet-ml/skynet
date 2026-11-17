"""Tests for the effect log: an outside effect of a step runs once, and only after the step is durable.

Most tests use a SQLite file and the local-filesystem object store. The last
tests need Postgres row locks; they run only when ``APP_TEST_DB_URL`` names a
disposable PostgreSQL database, each in its own schema that it drops after.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from core.config import app_env
from core.storage.effect_store import EffectStore, StepEffects
from core.storage.models import (
    AgentConversationModel,
    AgentEffectModel,
    AgentStepModel,
    Base,
    JobModel,
    StoragePackModel,
    TreeObjectModel,
)
from core.storage.object_store import LocalObjectStore
from core.storage.step_log_store import StepFenceError, StepLogStore

TEST_DB_URL = app_env("APP_TEST_DB_URL")
needs_postgres = pytest.mark.skipif(
    not TEST_DB_URL or not TEST_DB_URL.startswith("postgresql"),
    reason="Set APP_TEST_DB_URL to a disposable PostgreSQL database.",
)


class GatedObjectStore(LocalObjectStore):
    """A local object store that holds each PUT until its gate opens."""

    def __init__(self, root: Path) -> None:
        """Bind the store to a folder with an open gate.

        Args:
            root: The folder that holds the objects.
        """
        super().__init__(root)
        self.gate = threading.Event()
        self.gate.set()
        self.entered = threading.Event()

    def put(self, key: str, data: bytes) -> None:
        """Wait for the gate, then write the object.

        Args:
            key: The object key.
            data: The object bytes.
        """
        self.entered.set()
        self.gate.wait()
        super().put(key, data)


class Counter:
    """An effect that counts its calls and returns a fixed result."""

    def __init__(self, result: dict[str, Any] | None = None) -> None:
        """Set the result that each call returns.

        Args:
            result: The effect's result; ``None`` gives ``{"pr": 7}``.
        """
        self.calls = 0
        self.result = {"pr": 7} if result is None else result

    def __call__(self) -> dict[str, Any]:
        """Count the call and return the result.

        Returns:
            The fixed result.
        """
        self.calls += 1
        return self.result


def _seed(engine: Engine) -> Engine:
    """Create the schema and job ``job-1`` at generation 1.

    Args:
        engine: The empty database.

    Returns:
        The same engine.
    """
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(JobModel(optimization_id="job-1", username="ada", execution_generation=1))
        session.commit()
    return engine


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    """Return a fresh SQLite file engine with one job.

    Args:
        tmp_path: Pytest's temporary folder.

    Returns:
        The engine.
    """
    return _seed(create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}"))


@pytest.fixture
def objects(tmp_path: Path) -> GatedObjectStore:
    """Return a gated local object store.

    Args:
        tmp_path: Pytest's temporary folder.

    Returns:
        The store.
    """
    return GatedObjectStore(tmp_path / "objects")


@pytest.fixture
def steps(engine: Engine, objects: GatedObjectStore) -> Iterator[StepLogStore]:
    """Yield a step log with a long window, so only an explicit flush writes a pack.

    Args:
        engine: The database.
        objects: The object store.

    Yields:
        The step log; it closes after the test.
    """
    store = StepLogStore(engine, objects, "w1", flush_window_seconds=60.0)
    yield store
    store.close()


def _step(steps: StepLogStore, seq: int = 0) -> str:
    """Commit one model step of ``job-1`` at generation 1.

    Args:
        steps: The step log.
        seq: The step's position in the agent's log.

    Returns:
        The step id.
    """
    return steps.commit(
        run_id="job-1", agent_id="agent-a", seq=seq, kind="model", execution_generation=1, grant_list_id="g", body={}
    )


def _effect_rows(engine: Engine) -> list[tuple[str, str, str | None, dict[str, Any] | None]]:
    """Return every effect row's key, state, step id and result.

    Args:
        engine: The database.

    Returns:
        The rows, by key.
    """
    with Session(engine) as session:
        rows = session.scalars(select(AgentEffectModel).order_by(AgentEffectModel.effect_key))
        return [(row.effect_key, row.state, row.step_id, row.result) for row in rows]


def _in_thread(target: Callable[[], Any]) -> tuple[threading.Thread, list[Any]]:
    """Start a thread that keeps the target's result or error.

    Args:
        target: The call to run.

    Returns:
        The started thread and the list that gets its result or error.
    """
    out: list[Any] = []

    def run() -> None:
        """Run the target and keep what it returns or raises."""
        try:
            out.append(target())
        except Exception as error:
            out.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, out


def test_once_runs_the_effect_once_and_returns_the_stored_result(engine, steps) -> None:
    """A second call with the same key, also from a new store, returns the stored result and does not run the effect."""
    step_id = _step(steps)
    steps.flush()
    effect = Counter()

    first = EffectStore(engine, steps).once("job-1", f"{step_id}:pr", effect, step_id=step_id, execution_generation=1)
    again = EffectStore(engine, steps).once("job-1", f"{step_id}:pr", effect, step_id=step_id, execution_generation=1)

    assert first == again == {"pr": 7}
    assert effect.calls == 1
    assert _effect_rows(engine) == [(f"{step_id}:pr", "done", step_id, {"pr": 7})]


def test_two_keys_of_one_step_are_two_effects(engine, steps) -> None:
    """A push and a pull request of the same step each run once, under their own keys."""
    step_id = _step(steps)
    steps.flush()
    push, pr = Counter({"commit": "c"}), Counter()
    bound = StepEffects(EffectStore(engine, steps), "job-1", step_id, 1)

    assert bound.once("push", push) == {"commit": "c"}
    assert bound.once("pull_request", pr) == {"pr": 7}
    assert bound.once("push", push) == {"commit": "c"}

    assert (push.calls, pr.calls) == (1, 1)
    assert [key for key, *_ in _effect_rows(engine)] == [f"{step_id}:pull_request", f"{step_id}:push"]


def test_the_effect_does_not_run_before_the_step_is_durable(engine, objects, steps) -> None:
    """While the step's pack PUT waits, the effect does not run; it runs after the flush commits."""
    objects.gate.clear()
    step_id = _step(steps)
    flusher, _ = _in_thread(steps.flush)
    assert objects.entered.wait(5)
    effect = Counter()
    caller, out = _in_thread(
        lambda: EffectStore(engine, steps).once("job-1", "k", effect, step_id=step_id, execution_generation=1)
    )

    time.sleep(0.3)
    assert effect.calls == 0
    assert caller.is_alive()

    objects.gate.set()
    flusher.join(5)
    caller.join(5)
    assert out == [{"pr": 7}]
    assert effect.calls == 1


def test_a_durable_timeout_runs_no_effect(engine, steps) -> None:
    """When the step is not durable in time, the call raises and stores nothing."""
    step_id = _step(steps)
    effect = Counter()

    with pytest.raises(TimeoutError):
        EffectStore(engine, steps).once("job-1", "k", effect, step_id=step_id, execution_generation=1, timeout=0.05)

    assert effect.calls == 0
    assert _effect_rows(engine) == []


def test_a_fenced_step_runs_no_effect(engine, steps) -> None:
    """A step whose generation went stale before its flush never reaches the outside."""
    step_id = _step(steps)
    with Session(engine) as session:
        session.execute(update(JobModel).values(execution_generation=2))
        session.commit()
    steps.flush()
    effect = Counter()

    with pytest.raises(StepFenceError):
        EffectStore(engine, steps).once("job-1", "k", effect, step_id=step_id, execution_generation=1)

    assert effect.calls == 0
    assert _effect_rows(engine) == []


def test_a_failed_effect_stores_nothing_and_a_retry_runs_it(engine, steps) -> None:
    """An effect that raises leaves no row, so the next call runs it again."""
    step_id = _step(steps)
    steps.flush()
    store = EffectStore(engine, steps)

    def refuse() -> dict[str, Any]:
        """Fail as a refused GitHub call does.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("GitHub refused")

    with pytest.raises(RuntimeError, match="refused"):
        store.once("job-1", "k", refuse, step_id=step_id, execution_generation=1)
    assert _effect_rows(engine) == []

    effect = Counter()
    assert store.once("job-1", "k", effect, step_id=step_id, execution_generation=1) == {"pr": 7}
    assert effect.calls == 1


def test_a_crash_after_the_effect_and_before_its_commit_runs_it_again(engine, steps, monkeypatch) -> None:
    """The effect ran, then the worker died before the row commit: no row is left, and the retry runs it once more.

    The effect log is at-least-once across this crash, so each effect checks
    the outside state itself; ``github_publish`` does that.
    """
    step_id = _step(steps)
    steps.flush()
    effect = Counter()
    commit = Session.commit

    def crash(session: Session) -> None:
        """Die in place of the first commit after the effect ran, as a worker crash does.

        Args:
            session: The committing session.

        Raises:
            ConnectionError: On the first commit after the effect.
        """
        if effect.calls == 1:
            raise ConnectionError("worker died")
        commit(session)

    monkeypatch.setattr(Session, "commit", crash)
    with pytest.raises(ConnectionError):
        EffectStore(engine, steps).once("job-1", "k", effect, step_id=step_id, execution_generation=1)
    assert effect.calls == 1
    assert _effect_rows(engine) == []

    assert EffectStore(engine, steps).once("job-1", "k", effect, step_id=step_id, execution_generation=1) == {"pr": 7}
    assert effect.calls == 2
    assert EffectStore(engine, steps).once("job-1", "k", effect, step_id=step_id, execution_generation=1) == {"pr": 7}
    assert effect.calls == 2


def _race(first: EffectStore, second: EffectStore, step_id: str, *, first_fails: bool = False) -> None:
    """Call ``once`` with one key from two stores at the same time and check that the effect runs once.

    The first store's effect holds until the second call is waiting, so the
    two calls overlap. With ``first_fails``, the first effect then raises, so
    its insert rolls back under the waiting second call, which runs the effect.

    Args:
        first: The store whose call starts first.
        second: The other store.
        step_id: The durable step.
        first_fails: Whether the first effect raises after the second call waits.
    """
    entered, release = threading.Event(), threading.Event()
    calls: list[str] = []

    def effect(name: str) -> Callable[[], dict[str, Any]]:
        """Return an effect that records its caller and holds until released.

        Args:
            name: The caller's name.

        Returns:
            The effect.
        """

        def run() -> dict[str, Any]:
            """Record the call, then hold until the test releases it.

            Returns:
                The caller's result.

            Raises:
                RuntimeError: For the first caller when ``first_fails`` is set.
            """
            calls.append(name)
            entered.set()
            assert release.wait(5)
            if first_fails and name == "a":
                raise RuntimeError("GitHub refused")
            return {"by": name}

        return run

    a, out_a = _in_thread(lambda: first.once("job-1", "k", effect("a"), step_id=step_id, execution_generation=1))
    assert entered.wait(5)
    b, out_b = _in_thread(lambda: second.once("job-1", "k", effect("b"), step_id=step_id, execution_generation=1))
    time.sleep(0.3)
    assert b.is_alive()
    release.set()
    a.join(10)
    b.join(10)

    if first_fails:
        assert calls == ["a", "b"]
        assert [type(error) for error in out_a] == [RuntimeError]
        assert out_b == [{"by": "b"}]
    else:
        assert calls == ["a"]
        assert out_a == out_b == [{"by": "a"}]


def test_sqlite_workers_with_the_same_key_serialize_within_the_busy_timeout(tmp_path: Path, steps) -> None:
    """Two workers, each with its own engine on one SQLite file, overlap on one key: one runs it, one waits.

    This proves the order only while the first call ends inside SQLite's busy
    timeout (5 s); a longer effect makes the second call raise
    ``OperationalError``. The PostgreSQL tests below prove the real wait.
    """
    step_id = _step(steps)
    steps.flush()
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    other = StepLogStore(create_engine(url), LocalObjectStore(tmp_path / "objects"), "w2", flush_window_seconds=60.0)
    try:
        _race(EffectStore(create_engine(url), steps), EffectStore(create_engine(url), other), step_id)
    finally:
        other.close()


@pytest.fixture
def postgres_engine() -> Iterator[Engine]:
    """Yield an engine on a fresh schema of the disposable test database, then drop the schema.

    Yields:
        The engine, with the schema and job ``job-1`` at generation 1.
    """
    schema = f"effects_{uuid.uuid4().hex[:10]}"
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
                AgentEffectModel.__table__,
            ],
            checkfirst=False,
        )
        with Session(engine) as session:
            session.add(JobModel(optimization_id="job-1", username="ada", execution_generation=1))
            session.commit()
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@needs_postgres
def test_postgres_workers_with_the_same_key_run_the_effect_once(postgres_engine, tmp_path) -> None:
    """On Postgres, the second insert of a key waits for the first call's commit, then returns its result."""
    objects = LocalObjectStore(tmp_path)
    one = StepLogStore(postgres_engine, objects, "w1", flush_window_seconds=60.0)
    two = StepLogStore(postgres_engine, objects, "w2", flush_window_seconds=60.0)
    try:
        step_id = _step(one)
        one.flush()
        _race(EffectStore(postgres_engine, one), EffectStore(postgres_engine, two), step_id)
    finally:
        one.close()
        two.close()


@needs_postgres
def test_postgres_a_waiting_worker_runs_the_effect_when_the_first_rolls_back(postgres_engine, tmp_path) -> None:
    """On Postgres, the first effect raises while the second insert waits: the second then runs it and stores it."""
    objects = LocalObjectStore(tmp_path)
    one = StepLogStore(postgres_engine, objects, "w1", flush_window_seconds=60.0)
    two = StepLogStore(postgres_engine, objects, "w2", flush_window_seconds=60.0)
    try:
        step_id = _step(one)
        one.flush()
        _race(EffectStore(postgres_engine, one), EffectStore(postgres_engine, two), step_id, first_fails=True)
        assert _effect_rows(postgres_engine) == [("k", "done", step_id, {"by": "b"})]
    finally:
        one.close()
        two.close()
