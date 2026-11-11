"""Tests for the worker's sandbox sweep through the async providers.

A fake provider stands in for Vercel and a fake Docker CLI call stands in for
the daemon, so no test opens a network connection or starts a container.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, cast

import pytest
from sqlalchemy import create_engine, insert
from sqlalchemy.pool import StaticPool

import core.worker.engine as engine_module
from core.sandbox import factory
from core.sandbox.errors import SandboxFatalError
from core.sandbox.factory import PROVIDERS, ProviderEntry
from core.sandbox.providers import docker as docker_provider
from core.sandbox.providers.docker import _CliResult
from core.sandbox.tests.fake_provider import InMemorySandbox
from core.storage.base import JobStore
from core.storage.models import JobModel
from core.worker.engine import BackgroundWorker, reset_worker_for_tests

from .conftest import FakeJobStore


class SweepSandbox(InMemorySandbox):
    """Fake Vercel provider whose sweep returns a fixed count, raises, or runs a hook."""

    swept: ClassVar[list[str]] = []
    stopped: ClassVar[int] = 1
    error: ClassVar[Exception | None] = None
    unavailable: ClassVar[bool] = False
    on_sweep: ClassVar[Any] = None

    @classmethod
    def preflight(cls) -> None:
        """Refuse to run when the test marks the provider unavailable.

        Raises:
            SandboxFatalError: The test marked the provider unavailable.
        """
        if cls.unavailable:
            raise SandboxFatalError("The fake credentials are not configured.")

    @classmethod
    async def stop_job_sandboxes(cls, job_id: str) -> int:
        """Record the job and report the fixed outcome.

        Args:
            job_id: The job whose boxes to stop.

        Returns:
            The fixed count.

        Raises:
            Exception: The error the test set.
        """
        if cls.on_sweep is not None:
            cls.on_sweep()
        cls.swept.append(job_id)
        if cls.error is not None:
            raise cls.error
        return cls.stopped


class _FakeDocker:
    """Record Docker CLI calls and answer them from a script."""

    def __init__(self, answers: dict[str, _CliResult]) -> None:
        """Keep the scripted answers.

        Args:
            answers: Maps a space-joined argument prefix to its answer.
        """
        self.calls: list[list[str]] = []
        self.answers = answers

    async def __call__(
        self, docker_bin: str, args: Sequence[str], *, stdin: bytes | None = None, cli_seconds: float = 0
    ) -> _CliResult:
        """Record one call and return the answer for the longest matching prefix.

        Args:
            docker_bin: The Docker CLI path.
            args: The CLI arguments.
            stdin: The stdin bytes.
            cli_seconds: The CLI time limit.

        Returns:
            The scripted answer, or a success with no output.
        """
        del docker_bin, stdin, cli_seconds
        self.calls.append(list(args))
        joined = " ".join(args)
        matches = [prefix for prefix in self.answers if joined.startswith(prefix)]
        if not matches:
            return _CliResult(0, b"", b"")
        return self.answers[max(matches, key=len)]


@pytest.fixture(autouse=True)
def _fake_vercel(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Put the fake provider in the Vercel slot, outside production, and reset the worker singleton.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Yields:
        Control to the test.
    """
    monkeypatch.setattr(factory.settings, "app_environment", "")
    monkeypatch.setattr(engine_module.settings, "local_mode", False)
    monkeypatch.setitem(PROVIDERS, "vercel", ProviderEntry(__name__, "SweepSandbox", None, True))
    monkeypatch.setattr(SweepSandbox, "swept", [])
    monkeypatch.setattr(SweepSandbox, "stopped", 1)
    monkeypatch.setattr(SweepSandbox, "error", None)
    monkeypatch.setattr(SweepSandbox, "unavailable", False)
    monkeypatch.setattr(SweepSandbox, "on_sweep", None)
    reset_worker_for_tests()
    yield
    reset_worker_for_tests()


@pytest.fixture
def store() -> FakeJobStore:
    """Return a fresh in-memory job store.

    Returns:
        The store.
    """
    return FakeJobStore()


@pytest.fixture
def worker(store: FakeJobStore) -> BackgroundWorker:
    """Build an unstarted worker bound to the test store.

    Args:
        store: The test job store.

    Returns:
        The worker.
    """
    return BackgroundWorker(
        job_store=cast(JobStore, store), num_workers=2, poll_interval=1.0, _allow_unprotected_test_execution=True
    )


def test_stop_blackbox_sandboxes_logs_the_boxes_it_stopped(worker: BackgroundWorker, store: FakeJobStore) -> None:
    """A sweep that stopped boxes leaves one job log line saying so."""
    SweepSandbox.stopped = 2
    store.seed_job("opt-1")

    worker._stop_blackbox_sandboxes("opt-1")

    assert SweepSandbox.swept == ["opt-1"]
    assert [entry["message"] for entry in store.append_log_calls] == ["Stopped 2 sandbox(es) the job left running"]


def test_stop_blackbox_sandboxes_stays_quiet_without_a_provider_or_boxes(
    worker: BackgroundWorker, store: FakeJobStore
) -> None:
    """A provider that cannot run here, or nothing left running, adds no log line."""
    SweepSandbox.unavailable = True
    worker._stop_blackbox_sandboxes("opt-1")

    SweepSandbox.unavailable = False
    SweepSandbox.stopped = 0
    worker._stop_blackbox_sandboxes("opt-2")

    assert SweepSandbox.swept == ["opt-2"]
    assert store.append_log_calls == []


def test_stop_blackbox_sandboxes_swallows_a_failed_sweep(worker: BackgroundWorker, store: FakeJobStore) -> None:
    """A sweep the API refuses never surfaces past the job's own outcome."""
    SweepSandbox.error = RuntimeError("vercel down")

    worker._stop_blackbox_sandboxes("opt-1")

    assert SweepSandbox.swept == ["opt-1"]
    assert store.append_log_calls == []


def test_stop_blackbox_sandboxes_in_local_mode_removes_the_job_boxes_by_label(
    worker: BackgroundWorker, store: FakeJobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local mode removes a killed job's containers and networks with no image probe."""
    monkeypatch.setattr(engine_module.settings, "local_mode", True)
    fake = _FakeDocker({"ps": _CliResult(0, b"box1 \n", b""), "network ls": _CliResult(0, b"net1\n", b"")})
    monkeypatch.setattr(docker_provider, "_docker", fake)
    store.seed_job("opt-1")

    worker._stop_blackbox_sandboxes("opt-1")

    assert [call[0] for call in fake.calls] == ["ps", "rm", "network", "network"]
    assert ["rm", "-f", "box1"] in fake.calls
    assert fake.calls[-1] == ["network", "rm", "net1"]
    assert SweepSandbox.swept == []
    assert [entry["message"] for entry in store.append_log_calls] == ["Stopped 1 sandbox(es) the job left running"]


def test_sweep_stops_the_boxes_of_terminal_jobs_and_keeps_running_ones(
    worker: BackgroundWorker, store: FakeJobStore
) -> None:
    """The sweep stops boxes only for recently finished jobs that are still terminal."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    JobModel.__table__.create(engine)
    now = datetime.now(UTC)
    rows = {
        "done": ("success", now),
        "cancelled": ("cancelled", now),
        "live": ("running", None),
        "paused": ("paused", now),
        "resumed": ("failed", now),
        "old": ("success", now - timedelta(days=3)),
    }
    with engine.begin() as conn:
        conn.execute(
            insert(JobModel),
            [
                {"optimization_id": job_id, "status": status, "completed_at": ended}
                for job_id, (status, ended) in rows.items()
            ],
        )
    for job_id, (status, _) in rows.items():
        store.seed_job(job_id, status=status)
    store.update_job("resumed", status="running")
    store.engine = engine  # type: ignore[attr-defined]

    swept = worker._sweep_terminal_job_sandboxes()

    assert sorted(SweepSandbox.swept) == ["cancelled", "done", "paused"]
    assert swept == 3


class _FakePgResult:
    """Fake SQLAlchemy result for the lock probe and the job id query."""

    def __init__(self, value: Any) -> None:
        """Remember the value the caller reads back.

        Args:
            value: The lock outcome or the job id list.
        """
        self._value = value

    def scalar(self) -> Any:
        """Return the lock probe outcome.

        Returns:
            The stored value.
        """
        return self._value

    def scalars(self) -> _FakePgResult:
        """Return self so ``.all()`` reads the id list.

        Returns:
            This result.
        """
        return self

    def all(self) -> Any:
        """Return the job id list.

        Returns:
            The stored value.
        """
        return self._value


class _FakePgEngine:
    """Fake Postgres engine whose transaction-scoped advisory locks are shared by every caller."""

    class dialect:  # noqa: N801 - mirrors ``Engine.dialect.name``
        """Dialect stub."""

        name = "postgresql"

    def __init__(self, job_ids: list[str]) -> None:
        """Hold the terminal job ids and the set of held lock keys.

        Args:
            job_ids: The ids the job query returns.
        """
        self._job_ids = job_ids
        self.held: set[int] = set()

    @contextmanager
    def begin(self) -> Iterator[Any]:
        """Open a fake transaction that releases its locks on exit.

        Yields:
            A fake connection.
        """
        mine: set[int] = set()
        engine = self

        class _Conn:
            """Fake connection bound to this transaction."""

            def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _FakePgResult:
                """Grant the lock if free, or return the job ids.

                Args:
                    statement: The SQL statement.
                    params: The lock key, or ``None`` for the job query.

                Returns:
                    The lock outcome or the job ids.
                """
                del statement
                if params is None:
                    return _FakePgResult(list(engine._job_ids))
                key = params["k"]
                if key in engine.held:
                    return _FakePgResult(False)
                engine.held.add(key)
                mine.add(key)
                return _FakePgResult(True)

        try:
            yield _Conn()
        finally:
            self.held -= mine


def test_sweep_runs_on_only_one_worker_at_a_time(store: FakeJobStore) -> None:
    """A second worker skips the sweep while a peer holds the advisory lock."""
    store.seed_job("done", status="success")
    store.engine = _FakePgEngine(["done"])  # type: ignore[attr-defined]
    first = BackgroundWorker(job_store=cast(JobStore, store), _allow_unprotected_test_execution=True)
    second = BackgroundWorker(job_store=cast(JobStore, store), _allow_unprotected_test_execution=True)
    peer_results: list[int] = []
    # The peer stops at the held lock, so it never re-enters the sync bridge
    # from the bridge's own thread.
    SweepSandbox.on_sweep = lambda: peer_results.append(second._sweep_terminal_job_sandboxes())

    assert first._sweep_terminal_job_sandboxes() == 1
    assert peer_results == [0]
    assert SweepSandbox.swept == ["done"]
    SweepSandbox.on_sweep = None
    assert second._sweep_terminal_job_sandboxes() == 1


def test_local_worker_start_removes_idle_sandbox_networks(store: FakeJobStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """A local worker clears the old idle sandbox networks before it starts."""
    started: list[str] = []

    class _Worker:
        """Record the start of a stand-in worker."""

        def __init__(self, **kwargs: object) -> None:
            """Accept the worker's settings.

            Args:
                **kwargs: The worker's settings.
            """

        def start(self) -> None:
            """Record the start."""
            started.append("start")

        def stop(self, timeout: float) -> None:
            """Accept the singleton reset.

            Args:
                timeout: The join timeout.
            """

        def threads_alive(self) -> bool:
            """Report the stand-in as alive.

            Returns:
                Always ``True``.
            """
            return True

    now = int(time.time())
    inspect = "network inspect --format {{len .Containers}} {{.Created.Unix}}"
    fake = _FakeDocker(
        {
            "network ls": _CliResult(0, b"old used\n", b""),
            f"{inspect} old": _CliResult(0, f"0 {now - 3600}".encode(), b""),
            f"{inspect} used": _CliResult(0, f"2 {now - 3600}".encode(), b""),
        }
    )
    monkeypatch.setattr(docker_provider, "_docker", fake)
    monkeypatch.setattr(engine_module, "BackgroundWorker", _Worker)
    monkeypatch.setattr(engine_module, "_worker", None)
    monkeypatch.setattr(engine_module.settings, "local_mode", True)

    engine_module.get_worker(job_store=cast(JobStore, store))

    assert [call for call in fake.calls if call[:2] == ["network", "rm"]] == [["network", "rm", "old"]]
    assert started == ["start"]
