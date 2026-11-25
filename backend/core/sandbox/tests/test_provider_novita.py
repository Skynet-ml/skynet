"""Tests for the Novita provider against a fake SDK and a fake clock, with no network and no cost."""

from __future__ import annotations

import asyncio
import itertools
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterator, Mapping
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

from core.billing.budgets import BudgetService
from core.billing.runtime import BudgetRuntime, UsagePendingError
from core.config import settings
from core.sandbox.capabilities import SandboxSpecError
from core.sandbox.environment import JOB_TAG, NetworkPolicy
from core.sandbox.errors import SandboxFatalError, SandboxInfraError, StartTimeoutError
from core.sandbox.metering import usage_adapter
from core.sandbox.providers import novita
from core.sandbox.resources import ResourceRequest, SandboxSpec
from core.storage.models import Base, BillingCustomerModel, ExecutionOperationModel


# The provider matches SDK errors by class name, so the fakes keep the SDK names.
class SandboxException(Exception):  # noqa: N818
    """Stand in for the SDK's base API error, which the provider matches by name."""


class RateLimitException(SandboxException):
    """Stand in for the SDK's 429 error."""


class NotFoundException(SandboxException):
    """Stand in for the SDK's base not-found error."""


class FileNotFoundException(NotFoundException):
    """Stand in for the SDK's missing-file error."""


class SandboxNotFoundException(NotFoundException):
    """Stand in for the SDK's missing-box error."""


class CommandExitException(SandboxException):
    """Stand in for the SDK's nonzero-exit error."""

    def __init__(self, exit_code: int) -> None:
        """Keep the exit code.

        Args:
            exit_code: The command's exit code.
        """
        super().__init__(f"exit {exit_code}")
        self.exit_code = exit_code


class FakeClock:
    """Return a time that the test sets by hand."""

    def __init__(self) -> None:
        """Start the clock at zero."""
        self.now = 0.0

    def monotonic(self) -> float:
        """Return the fake time.

        Returns:
            The current fake time in seconds.
        """
        return self.now

    async def sleep(self, seconds: float) -> None:
        """Move the fake time forward and yield once.

        Args:
            seconds: The requested delay.
        """
        self.now += seconds
        await asyncio.sleep(0)


class FakeHandle:
    """Act as an SDK background command handle."""

    def __init__(self, pid: int, outcome: Callable[[], Any]) -> None:
        """Keep the pid and the wait behavior.

        Args:
            pid: The fake process id.
            outcome: An async function that returns the result or raises.
        """
        self.pid = pid
        self._outcome = outcome
        self.disconnected = False

    async def wait(self) -> Any:
        """Return the command result.

        Returns:
            An object with ``exit_code``.
        """
        return await self._outcome()

    async def disconnect(self) -> None:
        """Record that the caller stopped the output events."""
        self.disconnected = True


class FakeCommands:
    """Run fake commands: ``hang`` never ends, ``fail`` exits 3, anything else prints ``out``."""

    def __init__(self) -> None:
        """Start with no recorded calls."""
        self.runs: list[tuple[str, dict[str, Any]]] = []
        self.killed: list[int] = []
        self._pids = itertools.count(1)

    async def run(self, command: str, **options: Any) -> FakeHandle:
        """Record a command and return its handle.

        Args:
            command: The shell command.
            **options: The SDK options.

        Returns:
            The fake handle.
        """
        self.runs.append((command, options))

        async def outcome() -> Any:
            """End the fake command.

            Returns:
                The result with exit code 0.

            Raises:
                CommandExitException: For a ``fail`` command.
            """
            if "hang" in command:
                await asyncio.Event().wait()
            if "drop" in command:
                raise TimeoutError
            options["on_stdout"]("out")
            if "fail" in command:
                options["on_stderr"]("bad")
                raise CommandExitException(3)
            return SimpleNamespace(exit_code=0)

        return FakeHandle(next(self._pids), outcome)

    async def kill(self, pid: int) -> bool:
        """Record a command kill.

        Args:
            pid: The process id.

        Returns:
            True.
        """
        self.killed.append(pid)
        return True


class FakeFiles:
    """Keep files in a dict."""

    def __init__(self) -> None:
        """Start with no files."""
        self.data: dict[str, bytes] = {}
        self.read_error: BaseException | None = None

    async def write(self, path: str, data: bytes) -> None:
        """Store one file.

        Args:
            path: The sandbox path.
            data: The content.
        """
        self.data[path] = data

    async def read(self, path: str, format: str = "text") -> Any:
        """Return one file.

        Args:
            path: The sandbox path.
            format: The SDK read format.

        Returns:
            The content.

        Raises:
            FileNotFoundException: The file does not exist.
        """
        if self.read_error is not None:
            raise self.read_error
        if path not in self.data:
            raise FileNotFoundException(f"404: {path}")
        return bytearray(self.data[path])


class FakePaginator:
    """Return one page of listed boxes."""

    def __init__(self, items: list[Any]) -> None:
        """Keep the page.

        Args:
            items: The listed boxes.
        """
        self._items = items
        self.has_next = True

    async def next_items(self) -> list[Any]:
        """Return the page once.

        Returns:
            The listed boxes.
        """
        self.has_next = False
        return self._items


class FakeQuery:
    """Stand in for ``SandboxQuery``."""

    def __init__(self, metadata: dict[str, str]) -> None:
        """Keep the metadata filter.

        Args:
            metadata: The metadata that a box must carry.
        """
        self.metadata = metadata


class FakeSandbox:
    """Act as ``AsyncSandbox``, with a class-level registry of live boxes."""

    live: ClassVar[dict[str, FakeSandbox]] = {}
    calls: ClassVar[list[dict[str, Any]]] = []
    create_error: ClassVar[BaseException | None] = None
    leak_on_error: ClassVar[bool] = False
    create_gate: ClassVar[asyncio.Event | None] = None
    create_delay: ClassVar[float] = 0.0
    info_gate: ClassVar[asyncio.Event | None] = None
    info_entered: ClassVar[asyncio.Event | None] = None
    list_error: ClassVar[BaseException | None] = None
    kill_error: ClassVar[BaseException | None] = None
    info_size: ClassVar[tuple[int, int]] = (2, 2048)
    _ids: ClassVar[Iterator[int]] = itertools.count(1)

    def __init__(self, metadata: Mapping[str, str]) -> None:
        """Open one fake box.

        Args:
            metadata: The box metadata.
        """
        self.sandbox_id = f"sbx-{next(self._ids)}"
        self.metadata = dict(metadata)
        self.commands = FakeCommands()
        self.files = FakeFiles()
        self.networks: list[dict[str, Any]] = []
        # The real SDK has a class and an instance ``kill``; the instance one shadows the classmethod here.
        self.kill = self._kill_self
        type(self).live[self.sandbox_id] = self

    @classmethod
    def reset(cls) -> None:
        """Clear every class-level setting."""
        cls.live = {}
        cls.calls = []
        cls.create_error = None
        cls.leak_on_error = False
        cls.create_gate = None
        cls.create_delay = 0.0
        cls.info_gate = None
        cls.info_entered = None
        cls.list_error = None
        cls.kill_error = None
        cls.info_size = (2, 2048)

    @classmethod
    async def create(cls, **options: Any) -> FakeSandbox:
        """Record a create and open a box, or fail as configured.

        Args:
            **options: The SDK create options.

        Returns:
            The new box.

        Raises:
            BaseException: The configured create error.
        """
        cls.calls.append(options)
        if cls.create_gate is not None:
            await cls.create_gate.wait()
        await asyncio.sleep(cls.create_delay)
        if cls.create_error is not None:
            if cls.leak_on_error:
                cls(options["metadata"])
            raise cls.create_error
        return cls(options["metadata"])

    @classmethod
    def list(cls, *, query: FakeQuery) -> FakePaginator:
        """List live boxes whose metadata matches.

        Args:
            query: The metadata filter.

        Returns:
            A paginator over the matches.

        Raises:
            BaseException: The configured list error.
        """
        if cls.list_error is not None:
            raise cls.list_error
        items = [
            SimpleNamespace(sandbox_id=box.sandbox_id)
            for box in cls.live.values()
            if query.metadata.items() <= box.metadata.items()
        ]
        return FakePaginator(items)

    @classmethod
    async def kill(cls, sandbox_id: str) -> bool:
        """Kill a box by id.

        Args:
            sandbox_id: The box id.

        Returns:
            True when the box existed.
        """
        return cls.live.pop(sandbox_id, None) is not None

    async def _kill_self(self) -> bool:
        """Kill this box.

        Returns:
            True when the box existed.

        Raises:
            BaseException: The configured kill error.
        """
        if self.kill_error is not None:
            raise self.kill_error
        return type(self).live.pop(self.sandbox_id, None) is not None

    async def get_info(self) -> SimpleNamespace:
        """Return the box info with the configured size.

        Returns:
            A stand-in for ``SandboxInfo``.
        """
        if self.info_entered is not None:
            self.info_entered.set()
        if self.info_gate is not None:
            await self.info_gate.wait()
        cpus, memory = self.info_size
        return SimpleNamespace(sandbox_id=self.sandbox_id, cpu_count=cpus, memory_mb=memory)

    async def set_network(self, **options: Any) -> None:
        """Record a network update.

        Args:
            **options: ``allow_out`` and ``deny_out``.
        """
        self.networks.append(options)


class FakeUsage:
    """Record the reservation calls of the provider."""

    def __init__(self, request: Mapping[str, Any]) -> None:
        """Keep the request.

        Args:
            request: The request that the provider funded.
        """
        self.request = dict(request)
        self.calls: list[tuple[Any, ...]] = []
        self.late_id: str | None = None

    def start(self, at: float, name: str) -> None:
        """Record the create start.

        Args:
            at: The clock time.
            name: The box tag.
        """
        self.calls.append(("start", at, name))

    def created(self, at: float, timeout_seconds: int, *, sandbox_id: str | None = None) -> None:
        """Record the create return.

        Args:
            at: The clock time.
            timeout_seconds: The Novita timeout.
            sandbox_id: The id of a box that the create returned late.
        """
        self.calls.append(("created", at, timeout_seconds))
        self.late_id = sandbox_id

    def refused(self) -> None:
        """Record a refusal."""
        self.calls.append(("refused",))

    def confirm(self, info: Any) -> None:
        """Record the created box and refuse one larger than its funding, as the real reservation does.

        Args:
            info: The box info.

        Raises:
            SandboxSpecError: The box has more CPUs than the request.
        """
        self.calls.append(("confirm", info.sandbox_id))
        if info.cpu_count > self.request["vcpus"]:
            raise SandboxSpecError("too large")

    def stopped(self, at: float, *, confirmed: bool, box_seen: bool) -> None:
        """Record the stop.

        Args:
            at: The clock time.
            confirmed: The stop was confirmed.
            box_seen: A box was seen.
        """
        self.calls.append(("stopped", at, confirmed, box_seen))

    def settle(self) -> None:
        """Record the settle."""
        self.calls.append(("settle",))


@pytest.fixture(autouse=True)
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> type[FakeSandbox]:
    """Swap the SDK classes for the fakes.

    Args:
        monkeypatch: Restores the classes after the test.

    Returns:
        The fake sandbox class.
    """
    FakeSandbox.reset()
    monkeypatch.setattr(novita.NovitaEnvironment, "sandbox_cls", FakeSandbox)
    monkeypatch.setattr(novita.NovitaEnvironment, "query_cls", FakeQuery)
    monkeypatch.setattr(settings, "usage_markup", 1.0)
    return FakeSandbox


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Engine]:
    """Create a private funded wallet and ledger.

    Args:
        tmp_path: Per-test directory for the SQLite file.

    Yields:
        The engine bound to the funded ledger.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'novita.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username="alice", stripe_customer_id="fixture", balance_cents=100, grant_remaining=0)
        )
        session.commit()
    yield engine
    engine.dispose()


def _runtime(database: Engine) -> BudgetRuntime:
    """Bind a real budget runtime to the local ledger.

    Args:
        database: Engine of the funded ledger.

    Returns:
        A runtime for a fresh 50-cent budget owned by ``alice``.
    """
    service = BudgetService(engine=database)
    budget = service.create("alice", 50, idempotency_key="draft")
    return BudgetRuntime(service, username="alice", budget_id=budget.id, generation=0, phase="setup", wait_timeout=0)


def _spec(**overrides: Any) -> SandboxSpec:
    """Build a small protected spec.

    Args:
        **overrides: Spec fields to change.

    Returns:
        The spec.
    """
    values: dict[str, Any] = {
        "lifetime_seconds": 600.5,
        "resources": ResourceRequest(cpus=2, memory_mb=2048),
        "image": "base",
        "operation_key": "box-op",
        "tags": {JOB_TAG: "job-1", novita.LONG_RUNNING_KEY: "true"},
        "env": {"A": "1"},
    }
    values.update(overrides)
    return SandboxSpec(**values)


def _protected(clock: FakeClock, **overrides: Any) -> tuple[novita.NovitaEnvironment, list[FakeUsage]]:
    """Build a protected environment with a recording usage factory.

    Args:
        clock: The fake clock.
        **overrides: Spec fields to change.

    Returns:
        The environment and the list of usages it reserved.
    """
    usages: list[FakeUsage] = []

    def factory(budget: Any, request: Mapping[str, Any], *, operation_key: str) -> FakeUsage:
        """Record one reservation.

        Args:
            budget: The budget.
            request: The funded request.
            operation_key: The operation key.

        Returns:
            The recording usage.
        """
        usage = FakeUsage(request)
        usages.append(usage)
        return usage

    env = novita.NovitaEnvironment(_spec(**overrides), clock=clock, budget=object(), usage_factory=factory)
    return env, usages


def _state(database: Engine, runtime: BudgetRuntime) -> Any:
    """Read the only operation of the runtime's budget.

    Args:
        database: Engine of the ledger.
        runtime: The runtime.

    Returns:
        The operation snapshot.
    """
    with Session(database) as session:
        (operation_id,) = session.scalars(
            select(ExecutionOperationModel.id).where(ExecutionOperationModel.budget_id == runtime.budget_id)
        ).all()
    return runtime.service.get_operation(operation_id, "alice")


def test_capabilities_are_billable_and_adapter_exists() -> None:
    """Mark the provider billable and find its usage adapter."""
    assert novita.NovitaEnvironment.capabilities.billable is True
    assert usage_adapter("novita") is not None


def test_preflight_needs_the_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse to run without ``NOVITA_API_KEY``, and pass with it.

    Args:
        monkeypatch: Sets the variable.
    """
    monkeypatch.delenv(novita.API_KEY_ENV, raising=False)
    with pytest.raises(SandboxFatalError):
        novita.NovitaEnvironment.preflight()
    monkeypatch.setenv(novita.API_KEY_ENV, "fixture")
    novita.NovitaEnvironment.preflight()


@pytest.mark.parametrize("resources", [ResourceRequest(cpus=3), ResourceRequest(cpus=2, memory_mb=1500)])
def test_spec_that_novita_cannot_size_is_refused_before_create(resources: ResourceRequest) -> None:
    """Refuse an odd CPU count or a memory size in part GiB, before any reserve or create.

    Args:
        resources: The size that Novita cannot give.
    """
    with pytest.raises(SandboxSpecError):
        novita.NovitaEnvironment(_spec(resources=resources), budget=object())
    assert FakeSandbox.calls == []


async def test_spec_with_no_memory_keeps_the_template_memory() -> None:
    """Send only the CPU count when the spec sets no memory, and fund Novita's largest memory."""
    FakeSandbox.info_size = (1, 4096)
    env, usages = _protected(FakeClock(), resources=ResourceRequest(cpus=1))
    await env.start()
    assert FakeSandbox.calls[0]["resources"] == {"cpu": 1}
    assert usages[0].request["memory_mb"] == 8192
    await env.stop()


def test_protected_spec_needs_a_bounded_lifetime_and_a_key() -> None:
    """Refuse a protected box with no operation key, or a lifetime past Novita's cap."""
    with pytest.raises(SandboxSpecError):
        novita.NovitaEnvironment(_spec(operation_key=None), budget=object())
    with pytest.raises(SandboxSpecError):
        novita.NovitaEnvironment(_spec(lifetime_seconds=24 * 3600.0), budget=object())


async def test_create_sets_the_funded_lifetime_and_kill_on_timeout() -> None:
    """Send the funded lifetime as the Novita timeout, kill at timeout, and drop ``long_running``."""
    clock = FakeClock()
    env, usages = _protected(clock)
    await env.start()
    (options,) = FakeSandbox.calls
    assert options["timeout"] == 601
    assert options["lifecycle"] == {"on_timeout": "kill", "auto_resume": False}
    assert novita.LONG_RUNNING_KEY not in options["metadata"]
    assert options["metadata"][novita.BOX_TAG] == env.name
    assert options["metadata"][JOB_TAG] == "job-1"
    assert options["template"] == "base"
    assert options["envs"] == {"A": "1"}
    assert options["resources"] == {"cpu": 2, "memory_gib": 2}
    (usage,) = usages
    assert usage.request == {
        "lifetime_ms": (601 + 390) * 1000,
        "vcpus": 2,
        "memory_mb": 2048,
        "template": "base",
    }
    await env.stop()


async def test_unprotected_timeout_is_capped_at_a_day() -> None:
    """Cut an unprotected lifetime to Novita's 24-hour cap."""
    env = novita.NovitaEnvironment(_spec(lifetime_seconds=200_000.0, operation_key=None), clock=FakeClock())
    await env.start()
    assert FakeSandbox.calls[0]["timeout"] == 24 * 3600
    await env.stop()
    assert FakeSandbox.live == {}


async def test_reserve_to_settle_charges_the_measured_runtime(database: Engine) -> None:
    """Reserve before the create, then settle ten minutes at the published rate after the kill.

    Args:
        database: The funded ledger.
    """
    runtime = _runtime(database)
    clock = FakeClock()
    env = novita.NovitaEnvironment(_spec(), clock=clock, budget=runtime)
    clock.now = 100.0
    await env.start()
    clock.now = 100.0 + 599.2
    await env.stop()
    assert FakeSandbox.live == {}
    operation = _state(database, runtime)
    assert operation.state == "settled"
    # 600 seconds * (2 * 0.0000098 + 2 * 0.0000032) USD = 1.56 cents.
    assert operation.actual_cents == Decimal("1.56")


async def test_failure_after_create_kills_and_settles() -> None:
    """Kill the box and settle when the box is larger than its funding."""
    FakeSandbox.info_size = (16, 2048)
    clock = FakeClock()
    env, usages = _protected(clock)
    with pytest.raises(SandboxSpecError):
        await env.start()
    assert FakeSandbox.live == {}
    (usage,) = usages
    assert usage.calls[-2][0] == "stopped"
    assert usage.calls[-2][2:] == (True, True)
    assert usage.calls[-1] == ("settle",)


async def test_cancel_after_create_kills_and_settles() -> None:
    """Kill the box and settle when the caller cancels the start after the create."""
    FakeSandbox.info_gate = asyncio.Event()
    FakeSandbox.info_entered = asyncio.Event()
    clock = FakeClock()
    env, usages = _protected(clock)
    task = asyncio.ensure_future(env.start())
    await FakeSandbox.info_entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert FakeSandbox.live == {}
    (usage,) = usages
    assert ("stopped", 0.0, True, True) in usage.calls
    assert usage.calls[-1] == ("settle",)


async def test_stop_during_start_settles_and_raises_infra_error() -> None:
    """End a start that ``stop()`` cancelled with an infra error, never a bare cancel."""
    FakeSandbox.info_gate = asyncio.Event()
    FakeSandbox.info_entered = asyncio.Event()
    env, usages = _protected(FakeClock())
    task = asyncio.ensure_future(env.start())
    await FakeSandbox.info_entered.wait()
    await env.stop()
    with pytest.raises(SandboxInfraError):
        await task
    assert FakeSandbox.live == {}
    assert usages[0].calls[-1] == ("settle",)


@pytest.mark.parametrize(
    "error",
    [RateLimitException("429: slow down"), SandboxException("400: bad template")],
)
async def test_refused_create_releases_the_hold(database: Engine, error: BaseException) -> None:
    """Release the hold when Novita refused the create and the sweep found no box.

    Args:
        database: The funded ledger.
        error: The refusal.
    """
    FakeSandbox.create_error = error
    runtime = _runtime(database)
    env = novita.NovitaEnvironment(_spec(), clock=FakeClock(), budget=runtime)
    with pytest.raises(SandboxInfraError):
        await env.start()
    await env.stop()
    operation = _state(database, runtime)
    assert operation.state == "released"
    assert operation.actual_cents == 0


async def test_unknown_create_outcome_stays_pending(database: Engine) -> None:
    """Keep the hold pending when the create failed with an unknown outcome and no box is listed.

    Args:
        database: The funded ledger.
    """
    FakeSandbox.create_error = SandboxException("502: bad gateway")
    runtime = _runtime(database)
    env = novita.NovitaEnvironment(_spec(), clock=FakeClock(), budget=runtime)
    with pytest.raises(SandboxInfraError):
        await env.start()
    with pytest.raises(UsagePendingError):
        await env.stop()
    assert _state(database, runtime).state == "pending"


async def test_unknown_create_that_leaked_a_box_is_killed_and_settled(database: Engine) -> None:
    """Kill a box that a failed create opened, and settle its runtime.

    Args:
        database: The funded ledger.
    """
    FakeSandbox.create_error = SandboxException("502: bad gateway")
    FakeSandbox.leak_on_error = True
    runtime = _runtime(database)
    clock = FakeClock()
    env = novita.NovitaEnvironment(_spec(), clock=clock, budget=runtime)
    with pytest.raises(SandboxInfraError):
        await env.start()
    assert FakeSandbox.live == {}
    assert _state(database, runtime).state == "settled"


async def test_create_timeout_stays_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    """Raise a start timeout and keep the outcome unconfirmed when the create hangs.

    Args:
        monkeypatch: Shortens the create timeout.
    """
    monkeypatch.setattr(novita, "CREATE_TIMEOUT", 0.01)
    monkeypatch.setattr(novita, "LATE_CREATE_WAIT", 0.01)
    FakeSandbox.create_gate = asyncio.Event()
    env, usages = _protected(FakeClock())
    with pytest.raises(StartTimeoutError):
        await env.start()
    (usage,) = usages
    assert ("stopped", 0.0, False, False) in usage.calls


async def test_failed_sweep_leaves_the_stop_unconfirmed() -> None:
    """Treat a failed sweep as an unconfirmed stop."""
    env, usages = _protected(FakeClock())
    await env.start()
    FakeSandbox.list_error = SandboxException("500: down")
    await env.stop()
    stopped = [call for call in usages[0].calls if call[0] == "stopped"]
    assert stopped == [("stopped", 0.0, False, True)]


async def test_exec_runs_in_the_cwd_and_reports_the_exit_code() -> None:
    """Quote the working directory, run sudo as root and return the exit code of a failed command."""
    env = novita.NovitaEnvironment(_spec(operation_key=None), clock=FakeClock())
    await env.start()
    seen: list[tuple[str, str]] = []
    result = await env.exec("echo hi", cwd="/my dir", sudo=True, on_output=lambda s, t: seen.append((s, t)))
    assert (result.exit_code, result.stdout) == (0, "out")
    assert seen == [("stdout", "out")]
    box = next(iter(FakeSandbox.live.values()))
    command, options = box.commands.runs[-1]
    assert command == "cd '/my dir' && eval 'echo hi'"
    assert options["user"] == "root"
    assert options["background"] is True
    failed = await env.exec("fail")
    assert (failed.exit_code, failed.stderr) == (3, "bad")
    await env.stop()


async def test_exec_timeout_kills_the_command() -> None:
    """Kill a command that passes its limit and report exit 124."""
    env = novita.NovitaEnvironment(_spec(operation_key=None), clock=FakeClock())
    await env.start()
    result = await env.exec("hang", timeout_sec=0.01)
    assert result.timed_out is True
    assert result.exit_code == novita.TIMEOUT_EXIT_CODE
    box = next(iter(FakeSandbox.live.values()))
    assert box.commands.killed == [1]
    await env.stop()


async def test_files_round_trip_and_missing_file_is_none() -> None:
    """Write and read a text file, and return ``None`` for a missing file."""
    env = novita.NovitaEnvironment(_spec(operation_key=None), clock=FakeClock())
    await env.start()
    await env.upload_files({"/tmp/a.txt": "héllo"})
    assert await env.download_file("/tmp/a.txt") == "héllo"
    assert await env.download_file("/tmp/missing") is None
    await env.stop()


async def test_network_policy_uses_harbor_shapes() -> None:
    """Send the deny-all, allowlist and open shapes to ``set_network``, with explicit lists that clear a deny-all."""
    env = novita.NovitaEnvironment(_spec(operation_key=None), clock=FakeClock())
    await env.start()
    await env.set_network_policy(NetworkPolicy(disabled=True))
    await env.set_network_policy(NetworkPolicy(allowed_hosts=("api.example.com",)))
    await env.set_network_policy(NetworkPolicy())
    box = next(iter(FakeSandbox.live.values()))
    assert box.networks == [
        {"allow_out": [], "deny_out": [novita.ALL_TRAFFIC]},
        {"allow_out": ["api.example.com"], "deny_out": [novita.ALL_TRAFFIC]},
        {"allow_out": [], "deny_out": []},
    ]
    await env.stop()


async def test_stop_job_sandboxes_kills_tagged_boxes() -> None:
    """Kill only the boxes that carry the job tag."""
    FakeSandbox({JOB_TAG: "job-1"})
    FakeSandbox({JOB_TAG: "job-2"})
    assert await novita.NovitaEnvironment.stop_job_sandboxes("job-1") == 1
    assert [box.metadata[JOB_TAG] for box in FakeSandbox.live.values()] == ["job-2"]


async def test_create_return_time_is_recorded_with_the_timeout() -> None:
    """Record the create return and the timeout, so a late stop is not billed past the Novita kill."""
    clock = FakeClock()
    env, usages = _protected(clock)
    clock.now = 7.0
    await env.start()
    assert ("created", 7.0, 601) in usages[0].calls
    await env.stop()


async def test_late_stop_bills_only_to_the_novita_timeout(database: Engine) -> None:
    """Bill a box whose stop came after its timeout only up to the timeout.

    Args:
        database: The funded ledger.
    """
    runtime = _runtime(database)
    clock = FakeClock()
    env = novita.NovitaEnvironment(_spec(), clock=clock, budget=runtime)
    await env.start()
    clock.now = 601.0 + 115.0
    await env.stop()
    operation = _state(database, runtime)
    # 601 seconds * (2 * 0.0000098 + 2 * 0.0000032) USD = 1.5626 cents.
    assert operation.actual_cents == Decimal("1.5626")


async def test_cancelled_exec_kills_the_command() -> None:
    """Kill a running command when the caller cancels the exec, and let the cancel go on."""
    env = novita.NovitaEnvironment(_spec(operation_key=None), clock=FakeClock())
    await env.start()
    box = next(iter(FakeSandbox.live.values()))
    task = asyncio.ensure_future(env.exec("hang"))
    while not box.commands.runs:  # noqa: ASYNC110
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert box.commands.killed == [1]
    await env.stop()


async def test_missing_box_on_download_is_an_infra_error() -> None:
    """Raise an infra error, not ``None``, when the box is gone during a read."""
    env = novita.NovitaEnvironment(_spec(operation_key=None), clock=FakeClock())
    await env.start()
    box = next(iter(FakeSandbox.live.values()))
    box.files.read_error = SandboxNotFoundException("404: sandbox gone")
    with pytest.raises(SandboxInfraError):
        await env.download_file("/tmp/a.txt")
    await env.stop()


async def test_stop_during_reserve_releases_the_late_hold(database: Engine) -> None:
    """Release a hold whose reserve thread finished after a stop, with no create sent.

    Args:
        database: The funded ledger.
    """
    entered = threading.Event()
    gate = threading.Event()

    def slow_reserve(budget: Any, request: Mapping[str, Any], *, operation_key: str) -> Any:
        """Reserve after the test opens the gate.

        Args:
            budget: The budget.
            request: The funded request.
            operation_key: The operation key.

        Returns:
            The real reservation.
        """
        entered.set()
        gate.wait(5)
        return novita.USAGE.reserve(budget, request, operation_key=operation_key)

    runtime = _runtime(database)
    env = novita.NovitaEnvironment(_spec(), clock=FakeClock(), budget=runtime, usage_factory=slow_reserve)
    start = asyncio.ensure_future(env.start())
    await asyncio.to_thread(entered.wait, 5)
    stop = asyncio.ensure_future(env.stop())
    await asyncio.sleep(0)
    gate.set()
    await stop
    with pytest.raises(SandboxInfraError):
        await start
    assert FakeSandbox.calls == []
    assert _state(database, runtime).state == "released"


async def test_failed_kill_of_the_known_box_stays_pending(database: Engine) -> None:
    """Keep the hold pending when the kill of the created box fails.

    Args:
        database: The funded ledger.
    """
    runtime = _runtime(database)
    env = novita.NovitaEnvironment(_spec(), clock=FakeClock(), budget=runtime)
    await env.start()
    FakeSandbox.kill_error = SandboxException("500: down")
    with pytest.raises(UsagePendingError):
        await env.stop()
    assert _state(database, runtime).state == "pending"


async def test_cancel_during_create_with_an_empty_list_stays_pending(
    database: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the hold pending when the caller cancels a create and no box is listed.

    Args:
        database: The funded ledger.
        monkeypatch: Shortens the wait for the create call.
    """
    monkeypatch.setattr(novita, "CREATE_TIMEOUT", 0.01)
    monkeypatch.setattr(novita, "LATE_CREATE_WAIT", 0.01)
    FakeSandbox.create_gate = asyncio.Event()
    runtime = _runtime(database)
    env = novita.NovitaEnvironment(_spec(), clock=FakeClock(), budget=runtime)
    task = asyncio.ensure_future(env.start())
    while not FakeSandbox.calls:  # noqa: ASYNC110
        await asyncio.sleep(0.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(UsagePendingError):
        await env.stop()
    assert _state(database, runtime).state == "pending"


async def test_legacy_domain_is_refused_before_the_reserve(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse a legacy domain before any hold or create, because the SDK cannot size a box there.

    Args:
        monkeypatch: Makes the SDK report a legacy domain.
    """
    monkeypatch.setattr(novita, "should_use_legacy", lambda opts: True)
    monkeypatch.setenv(novita.API_KEY_ENV, "fixture")
    with pytest.raises(SandboxFatalError):
        novita.NovitaEnvironment.preflight()
    env, usages = _protected(FakeClock())
    with pytest.raises(SandboxSpecError):
        await env.start()
    assert usages == []
    assert FakeSandbox.calls == []
    await env.stop()


def test_create_waits_for_the_sdk_resource_timeout() -> None:
    """Give the create the SDK's six-minute resource limit, and fund the hold and the lifetime cap from it."""
    assert novita.CREATE_REQUEST_TIMEOUT == 360.0
    assert novita.CREATE_TIMEOUT > novita.CREATE_REQUEST_TIMEOUT
    assert novita.MAX_PROTECTED_LIFETIME == novita.NOVITA_MAX_LIFETIME - novita.CREATE_TIMEOUT
    env = novita.NovitaEnvironment(_spec(), clock=FakeClock())
    assert env._create_options()["request_timeout"] == novita.CREATE_REQUEST_TIMEOUT
    assert env._usage_request()["lifetime_ms"] == (env._timeout() + novita.CREATE_TIMEOUT) * 1000


async def test_box_created_after_the_deadline_is_killed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wait for a create that ends after the deadline, then kill the box that it returns.

    Args:
        monkeypatch: Shortens the create deadline.
    """
    monkeypatch.setattr(novita, "CREATE_TIMEOUT", 0.01)
    gate = FakeSandbox.create_gate = asyncio.Event()
    env, usages = _protected(FakeClock())
    task = asyncio.ensure_future(env.start())
    while env._starting is None or not env._starting.done():  # noqa: ASYNC110
        await asyncio.sleep(0.001)
    # The deadline has passed and the teardown waits for the create.
    gate.set()
    with pytest.raises(StartTimeoutError):
        await task
    assert FakeSandbox.live == {}
    (usage,) = usages
    assert usage.late_id is not None
    assert usage.late_id.startswith("sbx-")
    assert ("created", 0.0, 601) in usage.calls
    assert ("stopped", 0.0, True, True) in usage.calls


async def test_foreign_timeout_in_create_is_an_infra_error(database: Engine) -> None:
    """Wrap a TimeoutError that is not the create deadline, and keep the unknown outcome pending.

    Args:
        database: The funded ledger.
    """
    FakeSandbox.create_error = TimeoutError()
    runtime = _runtime(database)
    env = novita.NovitaEnvironment(_spec(), clock=FakeClock(), budget=runtime)
    with pytest.raises(SandboxInfraError):
        await env.start()
    with pytest.raises(UsagePendingError):
        await env.stop()
    assert _state(database, runtime).state == "pending"


async def test_exec_wraps_a_foreign_timeout_and_disconnects_after_a_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wrap a TimeoutError that is not the exec limit, and disconnect a command after its timeout kill.

    Args:
        monkeypatch: Records the handles that the fake box returns.
    """
    env = novita.NovitaEnvironment(_spec(operation_key=None), clock=FakeClock())
    await env.start()
    box = next(iter(FakeSandbox.live.values()))
    handles: list[FakeHandle] = []
    run = box.commands.run

    async def recording_run(command: str, **options: Any) -> FakeHandle:
        """Run the fake command and keep its handle.

        Args:
            command: The shell command.
            **options: The SDK options.

        Returns:
            The fake handle.
        """
        handle = await run(command, **options)
        handles.append(handle)
        return handle

    monkeypatch.setattr(box.commands, "run", recording_run)
    with pytest.raises(SandboxInfraError):
        await env.exec("drop")
    assert handles[0].disconnected is False
    result = await env.exec("hang", timeout_sec=0.01)
    assert result.timed_out is True
    assert handles[1].disconnected is True
    await env.stop()


async def test_refusal_after_a_cancelled_create_releases_the_hold(database: Engine) -> None:
    """Release the hold when a create that a stop cancelled answers later with a refusal.

    Args:
        database: The funded ledger.
    """
    gate = FakeSandbox.create_gate = asyncio.Event()
    FakeSandbox.create_error = SandboxException("404: unknown template")
    runtime = _runtime(database)
    env = novita.NovitaEnvironment(_spec(), clock=FakeClock(), budget=runtime)
    starting = asyncio.ensure_future(env.start())
    while not FakeSandbox.calls:  # noqa: ASYNC110
        await asyncio.sleep(0.001)
    stopping = asyncio.ensure_future(env.stop())
    while not env._starting.done():  # noqa: ASYNC110
        await asyncio.sleep(0.001)
    gate.set()
    await stopping
    with pytest.raises(SandboxInfraError):
        await starting
    assert _state(database, runtime).state == "released"


async def test_failed_info_read_is_an_infra_error(database: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Wrap a failed read of the new box, then kill it and settle its runtime.

    Args:
        database: The funded ledger.
        monkeypatch: Makes the info read fail.
    """

    async def broken_info(self: FakeSandbox) -> Any:
        """Fail like a dropped connection.

        Args:
            self: The fake box.

        Raises:
            TimeoutError: Always.
        """
        raise TimeoutError

    monkeypatch.setattr(FakeSandbox, "get_info", broken_info)
    runtime = _runtime(database)
    env = novita.NovitaEnvironment(_spec(), clock=FakeClock(), budget=runtime)
    with pytest.raises(SandboxInfraError):
        await env.start()
    assert FakeSandbox.live == {}
    assert _state(database, runtime).state == "settled"


async def test_network_policy_errors_are_infra_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Map a failed ``set_network`` call to an infra error.

    Args:
        monkeypatch: Makes the network call fail.
    """
    env = novita.NovitaEnvironment(_spec(operation_key=None), clock=FakeClock())
    await env.start()
    box = next(iter(FakeSandbox.live.values()))

    async def broken(**options: Any) -> None:
        """Fail like an API error.

        Args:
            **options: The network options.

        Raises:
            SandboxException: Always.
        """
        raise SandboxException("500: down")

    monkeypatch.setattr(box, "set_network", broken)
    with pytest.raises(SandboxInfraError):
        await env.set_network_policy(NetworkPolicy(disabled=True))
    await env.stop()


@pytest.mark.parametrize(
    ("command", "built", "stdout"),
    [
        ("", "eval ''", ""),
        ("echo a\\", "eval 'echo a\\'", "a\\\n"),
        ("echo a; pwd", "eval 'echo a; pwd'", "a\n{dir}\n"),
    ],
)
async def test_exec_cwd_wraps_any_command(command: str, built: str, stdout: str, tmp_path: Path) -> None:
    """Build a cd that guards every part of the command, and check that bash runs the built string.

    Args:
        command: The caller's command.
        built: The expected text after the cd.
        stdout: The expected bash output; ``{dir}`` is the working directory.
        tmp_path: The working directory.
    """
    env = novita.NovitaEnvironment(_spec(operation_key=None), clock=FakeClock())
    await env.start()
    folder = str(tmp_path)
    await env.exec(command, cwd=folder)
    box = next(iter(FakeSandbox.live.values()))
    sent, _ = box.commands.runs[-1]
    await env.stop()
    assert sent == f"cd {folder} && {built}"
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not installed")
    result = await asyncio.to_thread(subprocess.run, [bash, "-c", sent], capture_output=True, text=True, check=False)
    assert (result.returncode, result.stdout) == (0, stdout.format(dir=folder))
    missing = sent.replace(folder, folder + "/missing", 1)
    failed = await asyncio.to_thread(subprocess.run, [bash, "-c", missing], capture_output=True, text=True, check=False)
    assert failed.returncode != 0
    assert failed.stdout == ""
