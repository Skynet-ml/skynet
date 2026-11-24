"""Tests for the E2B provider against a fake SDK, with no network and no cost."""

from __future__ import annotations

import asyncio
import itertools
import shlex
import threading
from collections.abc import Callable, Mapping
from typing import Any, ClassVar

import pytest

from core.billing.runtime import UsagePendingError
from core.billing.signals import BudgetReached
from core.sandbox import factory
from core.sandbox.capabilities import SandboxSpecError
from core.sandbox.environment import JOB_TAG, NetworkPolicy
from core.sandbox.errors import SandboxFatalError, SandboxInfraError, StartTimeoutError
from core.sandbox.metering import usage_adapter
from core.sandbox.pacing import ProviderAtCapacityError, ProviderPacing
from core.sandbox.providers import e2b
from core.sandbox.resources import ResourceRequest, SandboxSpec
from core.sandbox.tests.test_environment import SandboxEnvironmentContract

pytest.importorskip("e2b", reason="The e2b extra is not installed.")


class FakeClock:
    """Advance time only when the code under test sleeps."""

    def __init__(self) -> None:
        """Start the clock at zero."""
        self.now = 0.0
        self.sleeps: list[float] = []

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
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


class FakeResult:
    """Hold the end state of a fake command."""

    def __init__(self, exit_code: int) -> None:
        """Keep the exit code.

        Args:
            exit_code: The exit code.
        """
        self.exit_code = exit_code


class FakeHandle:
    """Act as an SDK command handle."""

    def __init__(self, pid: int, outcome: Callable[[], Any]) -> None:
        """Keep the pid and the wait behavior.

        Args:
            pid: The fake process id.
            outcome: Returns the exit code, or raises, when the wait is awaited.
        """
        self.pid = pid
        self._outcome = outcome

    async def wait(self) -> FakeResult:
        """Return the result, or raise the scripted error.

        Returns:
            The result of a zero exit.

        Raises:
            CommandExitException: The command exited with a non-zero code.
        """
        outcome = self._outcome()
        if asyncio.iscoroutine(outcome):
            outcome = await outcome
        if outcome != 0:
            raise e2b.CommandExitException(stderr="", stdout="", exit_code=outcome, error=None)
        return FakeResult(0)


class FakeCommands:
    """Run the small fake command set of a fake box."""

    def __init__(self, box: FakeBox) -> None:
        """Keep the box.

        Args:
            box: The owner box.
        """
        self.box = box
        self.runs: list[dict[str, Any]] = []
        self.run_errors: list[BaseException] = []
        self.wait_errors: list[BaseException] = []
        self.connects: list[int] = []
        self.killed: list[int] = []
        self.hang = False
        self._pids = itertools.count(100)

    async def run(self, command: str, **options: Any) -> FakeHandle:
        """Start a command and stream its output.

        Args:
            command: The shell command.
            **options: The SDK options.

        Returns:
            The handle.
        """
        self.runs.append({"command": command, **options})
        if self.run_errors:
            raise self.run_errors.pop(0)
        exit_code, stdout, stderr = self.box.interpret(command, options.get("envs") or {})
        if stdout:
            options["on_stdout"](stdout)
        if stderr:
            options["on_stderr"](stderr)
        return FakeHandle(next(self._pids), lambda: self._finish(exit_code))

    def _finish(self, exit_code: int) -> Any:
        """Return the exit code, or raise the next scripted wait error.

        Args:
            exit_code: The exit code of the command.

        Returns:
            The exit code, or a coroutine that never ends for a hung command.

        Raises:
            BaseException: The next scripted wait error.
        """
        if self.wait_errors:
            raise self.wait_errors.pop(0)
        if self.hang:
            return asyncio.Event().wait()
        return exit_code

    async def connect(self, pid: int, **options: Any) -> FakeHandle:
        """Reattach to a running command.

        Args:
            pid: The process id.
            **options: The SDK options.

        Returns:
            A handle that ends with exit code 0.
        """
        self.connects.append(pid)
        return FakeHandle(pid, lambda: self._finish(0))

    async def kill(self, pid: int) -> bool:
        """Record a kill.

        Args:
            pid: The process id.

        Returns:
            True.
        """
        self.killed.append(pid)
        return True


class FakeFiles:
    """Store the files of a fake box."""

    def __init__(self, box: FakeBox) -> None:
        """Keep the box.

        Args:
            box: The owner box.
        """
        self.box = box

    async def write(self, path: str, data: str) -> None:
        """Store one file.

        Args:
            path: The path.
            data: The text.
        """
        self.box.stored[path] = data

    async def read(self, path: str, format: str = "text") -> str:
        """Return one file.

        Args:
            path: The path.
            format: The SDK read format.

        Returns:
            The text.

        Raises:
            FileNotFoundException: The file does not exist.
        """
        if path not in self.box.stored:
            raise e2b_sdk_module().FileNotFoundException(path)
        return self.box.stored[path]


def e2b_sdk_module() -> Any:
    """Return the real SDK module that the provider imported.

    Returns:
        The ``e2b`` package.
    """
    return e2b.e2b_sdk


class FakeInfo:
    """Act as an SDK list item."""

    def __init__(self, sandbox_id: str) -> None:
        """Keep the id.

        Args:
            sandbox_id: The box id.
        """
        self.sandbox_id = sandbox_id


class FakePaginator:
    """Return all items in one page."""

    def __init__(self, items: list[FakeInfo]) -> None:
        """Keep the items.

        Args:
            items: The page.
        """
        self._items = items
        self.has_next = True

    async def next_items(self) -> list[FakeInfo]:
        """Return the page once.

        Returns:
            The items.
        """
        self.has_next = False
        return self._items


class FakeBox:
    """Act as ``e2b.AsyncSandbox`` with a class-level registry of live boxes."""

    boxes: ClassVar[dict[str, FakeBox]] = {}
    create_calls: ClassVar[list[dict[str, Any]]] = []
    create_errors: ClassVar[list[BaseException]] = []
    create_delay: ClassVar[float] = 0.0
    kill_errors: ClassVar[dict[str, BaseException]] = {}
    leak_on_error: ClassVar[bool] = False
    size: ClassVar[tuple[int, int]] = (1, 1024)
    _ids: ClassVar[itertools.count] = itertools.count(1)

    def __init__(self, metadata: Mapping[str, str], envs: Mapping[str, str]) -> None:
        """Create the box state.

        Args:
            metadata: The box metadata.
            envs: The box environment.
        """
        self.sandbox_id = f"sbx-{next(self._ids)}"
        self.metadata = dict(metadata)
        self.envs = dict(envs)
        self.stored: dict[str, str] = {}
        self.files = FakeFiles(self)
        self.commands = FakeCommands(self)
        self.timeouts: list[int] = []
        self.timeout_errors: list[BaseException] = []
        self.request_timeouts: list[float | None] = []
        self.network_updates: list[dict[str, Any]] = []

    @classmethod
    def reset(cls) -> None:
        """Clear the class state between tests."""
        cls.boxes = {}
        cls.create_calls = []
        cls.create_errors = []
        cls.create_delay = 0.0
        cls.kill_errors = {}
        cls.leak_on_error = False
        cls.size = (1, 1024)

    @classmethod
    async def create(cls, **options: Any) -> FakeBox:
        """Create a box, or raise the next scripted error.

        Args:
            **options: The SDK create options.

        Returns:
            The new box.

        Raises:
            BaseException: The next scripted create error.
        """
        cls.create_calls.append(options)
        if cls.create_delay:
            await asyncio.sleep(cls.create_delay)
        if cls.create_errors:
            error = cls.create_errors.pop(0)
            if cls.leak_on_error:
                box = cls(options["metadata"], options["envs"])
                cls.boxes[box.sandbox_id] = box
            raise error
        box = cls(options["metadata"], options["envs"])
        cls.boxes[box.sandbox_id] = box
        return box

    @classmethod
    def list(cls, *, query: Any) -> FakePaginator:
        """List the live boxes whose metadata matches.

        Args:
            query: The SDK query.

        Returns:
            One page of matches.
        """
        wanted = query.metadata or {}
        return FakePaginator(
            [
                FakeInfo(box_id)
                for box_id, box in cls.boxes.items()
                if all(box.metadata.get(key) == value for key, value in wanted.items())
            ]
        )

    @classmethod
    async def _kill_id(cls, sandbox_id: str) -> bool:
        """Kill one box by id.

        Args:
            sandbox_id: The box id.

        Returns:
            True when the box existed.

        Raises:
            BaseException: The scripted kill error of the box.
        """
        if sandbox_id in cls.kill_errors:
            raise cls.kill_errors[sandbox_id]
        return cls.boxes.pop(sandbox_id, None) is not None

    def __getattribute__(self, name: str) -> Any:
        """Route ``kill`` to the instance or class variant, like the SDK.

        Args:
            name: The attribute name.

        Returns:
            The attribute.
        """
        if name == "kill":
            box_id = object.__getattribute__(self, "sandbox_id")
            return lambda: type(self)._kill_id(box_id)
        return object.__getattribute__(self, name)

    @classmethod
    async def kill(cls, sandbox_id: str) -> bool:
        """Kill one box by id, as ``AsyncSandbox.kill(sandbox_id)``.

        Args:
            sandbox_id: The box id.

        Returns:
            True when the box existed.
        """
        return await cls._kill_id(sandbox_id)

    async def set_timeout(self, timeout: int, *, request_timeout: float | None = None) -> None:  # noqa: ASYNC109 - the SDK method has this name
        """Record a timeout extension, or raise the next scripted error.

        Args:
            timeout: The new timeout in seconds.
            request_timeout: The SDK request time limit.

        Raises:
            BaseException: The next scripted error.
        """
        if self.timeout_errors:
            raise self.timeout_errors.pop(0)
        self.timeouts.append(timeout)
        self.request_timeouts.append(request_timeout)

    async def get_info(self) -> Any:
        """Return the id and the template size of the box, as ``SandboxInfo``.

        Returns:
            An object with ``sandbox_id``, ``cpu_count`` and ``memory_mb``.
        """
        cpu_count, memory_mb = self.size
        return type("Info", (), {"sandbox_id": self.sandbox_id, "cpu_count": cpu_count, "memory_mb": memory_mb})()

    async def update_network(self, update: dict[str, Any]) -> None:
        """Record a network update.

        Args:
            update: The SDK network update.
        """
        self.network_updates.append(update)

    def interpret(self, command: str, env: Mapping[str, str]) -> tuple[int, str, str]:
        """Interpret one command from the fake command set.

        Args:
            command: The shell command.
            env: The per-command environment.

        Returns:
            The exit code, stdout and stderr; 127 for an unknown command.
        """
        argv = shlex.split(command)
        if not argv:
            return 0, "", ""
        name, args = argv[0], argv[1:]
        if name == "true":
            return 0, "", ""
        if name == "false":
            return 1, "", ""
        if name == "exit":
            return (int(args[0]) if args else 0), "", ""
        if name == "echo":
            return 0, " ".join(args) + "\n", ""
        if name == "cat":
            missing = [path for path in args if path not in self.stored]
            if missing:
                return 1, "", f"cat: {missing[0]}: No such file or directory\n"
            return 0, "".join(self.stored[path] for path in args), ""
        if name == "printenv" and len(args) == 1:
            value = {**self.envs, **env}.get(args[0])
            return (1, "", "") if value is None else (0, value + "\n", "")
        return 127, "", f"{name}: command not found\n"


@pytest.fixture(autouse=True)
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> type[FakeBox]:
    """Replace the SDK class with the fake for every test.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        The fake class.
    """
    FakeBox.reset()
    monkeypatch.setattr(e2b.E2BEnvironment, "sandbox_cls", FakeBox)
    return FakeBox


def _spec(**overrides: Any) -> SandboxSpec:
    """Return a small spec.

    Args:
        **overrides: Spec fields to replace.

    Returns:
        The spec.
    """
    return SandboxSpec(**{"lifetime_seconds": 300, "resources": ResourceRequest(cpus=1), **overrides})


def _the_box() -> FakeBox:
    """Return the only live fake box.

    Returns:
        The box.
    """
    (box,) = FakeBox.boxes.values()
    return box


class TestE2BContract(SandboxEnvironmentContract):
    """Run the provider contract against the fake SDK."""

    provider_cls = e2b.E2BEnvironment


def _status_error(status: int) -> BaseException:
    """Return an SDK error with an HTTP status.

    Args:
        status: The status code.

    Returns:
        The error.
    """
    return e2b.SandboxException("busy", status_code=status)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(lambda: e2b.RateLimitException("429"), id="429"),
        pytest.param(lambda: _status_error(502), id="502"),
        pytest.param(lambda: _status_error(503), id="503"),
    ],
)
async def test_capacity_errors_wait_with_no_attempt_limit(error: Callable[[], BaseException]) -> None:
    """Wait and create again on every capacity answer, past any fixed attempt count."""
    FakeBox.create_errors = [error() for _ in range(12)]
    clock = FakeClock()
    async with e2b.E2BEnvironment(_spec(), clock=clock) as env:
        assert (await env.exec("true")).ok
    assert len(FakeBox.create_calls) == 13
    assert len(clock.sleeps) >= 12


async def test_capacity_error_maps_to_provider_at_capacity() -> None:
    """Raise the shared capacity error type for a 503."""
    env = e2b.E2BEnvironment(_spec())
    FakeBox.create_errors = [_status_error(503)]
    with pytest.raises(ProviderAtCapacityError):
        await env._create(None)


async def test_other_create_errors_are_not_retried() -> None:
    """Send the create once and raise a non-capacity error."""
    FakeBox.create_errors = [e2b.SandboxException("bad template", status_code=400)]
    with pytest.raises(e2b.SandboxException):
        async with e2b.E2BEnvironment(_spec(), clock=FakeClock()):
            pass
    assert len(FakeBox.create_calls) == 1


async def test_create_turns_off_sdk_retries_and_sends_the_spec() -> None:
    """Send retries=0, the box tag, the job tag, the env and the template."""
    spec = _spec(name="box-a", image="tmpl", env={"A": "1"}, tags={JOB_TAG: "job-1"})
    async with e2b.E2BEnvironment(spec):
        pass
    (call,) = FakeBox.create_calls
    assert call["retries"] == 0
    assert call["request_timeout"] == e2b.CREATE_TIMEOUT
    assert call["template"] == "tmpl"
    assert call["timeout"] == 300
    assert call["metadata"] == {JOB_TAG: "job-1", e2b.BOX_TAG: "box-a"}
    assert call["envs"] == {"A": "1"}
    assert call["allow_internet_access"] is True
    assert "network" not in call


async def test_create_sends_the_allowlist_and_the_disabled_network() -> None:
    """Send Harbor's allowlist shape and the internet switch."""
    async with e2b.E2BEnvironment(_spec(allowed_hosts=("pypi.org",))):
        pass
    async with e2b.E2BEnvironment(_spec(network_disabled=True)):
        pass
    allow, deny = FakeBox.create_calls
    assert allow["network"] == {"allow_out": ["pypi.org"], "deny_out": [e2b.ALL_TRAFFIC]}
    assert deny["allow_internet_access"] is False
    assert "network" not in deny


async def test_create_timeout_raises_start_timeout_and_sweeps_the_box(monkeypatch: pytest.MonkeyPatch) -> None:
    """Raise StartTimeoutError for a slow create and kill a box that appeared late."""
    monkeypatch.setattr(e2b, "CREATE_TIMEOUT", 0.01)
    FakeBox.create_delay = 0.05
    env = e2b.E2BEnvironment(_spec(name="slow"))
    with pytest.raises(StartTimeoutError):
        await env.start()
    assert FakeBox.boxes == {}


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(lambda: e2b.httpx.ReadTimeout("slow create"), id="httpx"),
        pytest.param(lambda: e2b.TimeoutException("slow create"), id="e2b"),
    ],
)
async def test_create_request_timeout_raises_start_timeout_and_sweeps_the_box(
    error: Callable[[], BaseException],
) -> None:
    """Raise StartTimeoutError for an SDK request timeout and kill the box it left."""
    FakeBox.leak_on_error = True
    FakeBox.create_errors = [error()]
    with pytest.raises(StartTimeoutError):
        await e2b.E2BEnvironment(_spec(name="slow-request"), clock=FakeClock()).start()
    assert len(FakeBox.create_calls) == 1
    assert FakeBox.boxes == {}


async def test_stop_sweeps_a_box_that_a_failed_create_left() -> None:
    """Kill the tagged box that a 500 answer left behind."""
    FakeBox.leak_on_error = True
    FakeBox.create_errors = [e2b.SandboxException("server", status_code=500)]
    other = FakeBox({e2b.BOX_TAG: "other"}, {})
    FakeBox.boxes[other.sandbox_id] = other
    with pytest.raises(e2b.SandboxException):
        await e2b.E2BEnvironment(_spec(name="leaky")).start()
    assert list(FakeBox.boxes) == [other.sandbox_id]


async def test_dispatch_retries_only_a_refused_connection() -> None:
    """Send the command again after a refused connection."""
    async with e2b.E2BEnvironment(_spec()) as env:
        box = _the_box()
        refused = e2b.SandboxException("unreachable")
        refused.__cause__ = ConnectionRefusedError()
        box.commands.run_errors = [refused]
        result = await env.exec("echo again")
    assert result.stdout == "again\n"
    assert len(box.commands.runs) == 2
    assert box.commands.runs[-1]["background"] is True
    assert box.commands.runs[-1]["timeout"] == 0


async def test_dispatch_does_not_retry_a_read_timeout() -> None:
    """Raise an infra error after a timeout that may have started the command."""
    async with e2b.E2BEnvironment(_spec()) as env:
        box = _the_box()
        box.commands.run_errors = [e2b.TimeoutException("read")]
        with pytest.raises(SandboxInfraError):
            await env.exec("true")
    assert len(box.commands.runs) == 1


async def test_dispatch_stops_after_the_attempt_limit() -> None:
    """Raise an infra error after DISPATCH_ATTEMPTS refused dispatches."""
    async with e2b.E2BEnvironment(_spec(), clock=FakeClock()) as env:
        box = _the_box()
        box.commands.run_errors = [e2b.RateLimitException("429") for _ in range(e2b.DISPATCH_ATTEMPTS)]
        with pytest.raises(SandboxInfraError):
            await env.exec("true")
    assert len(box.commands.runs) == e2b.DISPATCH_ATTEMPTS


async def test_dispatch_backs_off_after_a_rate_limit() -> None:
    """Wait a growing delay on the clock between rate-limited dispatches."""
    clock = FakeClock()
    async with e2b.E2BEnvironment(_spec(), clock=clock) as env:
        box = _the_box()
        box.commands.run_errors = [e2b.RateLimitException("429"), e2b.RateLimitException("429")]
        clock.sleeps.clear()
        result = await env.exec("echo late")
    assert result.stdout == "late\n"
    assert len(box.commands.runs) == 3
    assert clock.sleeps == [e2b.DISPATCH_BACKOFF, 2 * e2b.DISPATCH_BACKOFF]


async def test_dispatch_does_not_back_off_after_a_refused_connection() -> None:
    """Resend a refused dispatch at once."""
    clock = FakeClock()
    async with e2b.E2BEnvironment(_spec(), clock=clock) as env:
        box = _the_box()
        refused = e2b.SandboxException("unreachable")
        refused.__cause__ = ConnectionRefusedError()
        box.commands.run_errors = [refused]
        clock.sleeps.clear()
        await env.exec("true")
    assert len(box.commands.runs) == 2
    assert clock.sleeps == []


async def test_wait_reattaches_by_pid_after_a_cut_connection() -> None:
    """Reconnect to the same pid and return its exit code."""
    clock = FakeClock()
    async with e2b.E2BEnvironment(_spec(), clock=clock) as env:
        box = _the_box()
        box.commands.wait_errors = [e2b.TimeoutException("cut"), OSError("reset")]
        result = await env.exec("true")
    assert result.ok
    assert box.commands.connects == [100, 100]
    assert len(box.commands.runs) == 1
    assert clock.sleeps.count(e2b.REATTACH_DELAY) == 2


async def test_wait_stops_after_the_reattach_limit() -> None:
    """Raise an infra error after EXEC_REATTACH_ATTEMPTS reattaches."""
    assert e2b.EXEC_REATTACH_ATTEMPTS == 5
    async with e2b.E2BEnvironment(_spec(), clock=FakeClock()) as env:
        box = _the_box()
        box.commands.wait_errors = [e2b.TimeoutException("cut") for _ in range(6)]
        with pytest.raises(SandboxInfraError):
            await env.exec("true")
    assert len(box.commands.connects) == 5


async def test_wait_does_not_reattach_to_a_stopped_box() -> None:
    """Raise an infra error at once when the box is gone."""
    async with e2b.E2BEnvironment(_spec(), clock=FakeClock()) as env:
        box = _the_box()
        box.commands.wait_errors = [e2b.SandboxNotRunningException("gone")]
        with pytest.raises(SandboxInfraError):
            await env.exec("true")
    assert box.commands.connects == []


async def test_exec_timeout_kills_the_command() -> None:
    """Return timed_out with exit code 124 and kill the pid."""
    async with e2b.E2BEnvironment(_spec()) as env:
        box = _the_box()
        box.commands.hang = True
        result = await env.exec("echo slow", timeout_sec=0.01)
    assert result.timed_out
    assert result.exit_code == e2b.TIMEOUT_EXIT_CODE
    assert result.stdout == "slow\n"
    assert box.commands.killed == [100]


async def test_exec_runs_as_root_with_sudo() -> None:
    """Send the root user, the cwd and the per-command env."""
    async with e2b.E2BEnvironment(_spec()) as env:
        await env.exec("true", sudo=True, cwd="/work", env={"X": "1"})
        await env.exec("true")
        first, second = _the_box().commands.runs
    assert first["user"] == "root"
    assert first["cwd"] == "/work"
    assert first["envs"] == {"X": "1"}
    assert second["user"] is None


async def test_short_lifetime_does_not_start_the_renew_loop() -> None:
    """Keep a box of one slice or less on its create timeout."""
    async with e2b.E2BEnvironment(_spec(lifetime_seconds=600)) as env:
        assert env._renewer is None


async def test_renew_loop_extends_the_timeout_up_to_the_lifetime() -> None:
    """Extend one slice at a time and stop at the spec lifetime."""
    clock = FakeClock()
    env = e2b.E2BEnvironment(_spec(lifetime_seconds=3 * 3600 + 1200), clock=clock)
    await env.start()
    try:
        box = _the_box()
        assert FakeBox.create_calls[0]["timeout"] == e2b.LIFETIME_SLICE
        await asyncio.wait_for(env._renewer, 1)
        assert box.timeouts[:2] == [e2b.LIFETIME_SLICE, e2b.LIFETIME_SLICE]
        assert sum(box.timeouts) + e2b.LIFETIME_SLICE >= 3 * 3600 + 1200
        assert all(t <= e2b.LIFETIME_SLICE for t in box.timeouts)
    finally:
        await env.stop()


async def test_renew_loop_retries_a_failed_extension() -> None:
    """Try again after RENEW_RETRY when an extension fails."""
    clock = FakeClock()
    env = e2b.E2BEnvironment(_spec(lifetime_seconds=2 * 3600), clock=clock)
    await env.start()
    try:
        box = _the_box()
        box.timeout_errors = [e2b.SandboxException("blip")]
        await asyncio.wait_for(env._renewer, 1)
        assert box.timeouts == [e2b.LIFETIME_SLICE, 570]
        assert e2b.RENEW_RETRY in clock.sleeps
    finally:
        await env.stop()


async def test_stop_cancels_the_renew_loop() -> None:
    """End the renew loop when the box stops."""
    env = e2b.E2BEnvironment(_spec(lifetime_seconds=5 * 3600))
    await env.start()
    renewer = env._renewer
    await env.stop()
    assert renewer is not None
    assert renewer.done()
    assert FakeBox.boxes == {}


@pytest.mark.parametrize(
    ("policy", "body"),
    [
        (NetworkPolicy(disabled=True), {"allow_internet_access": False}),
        (
            NetworkPolicy(allowed_hosts=("api.example.com",)),
            {"allow_out": ["api.example.com"], "deny_out": ["0.0.0.0/0"]},
        ),
        (NetworkPolicy(), {}),
    ],
)
async def test_network_policy_uses_harbor_shapes(policy: NetworkPolicy, body: dict[str, Any]) -> None:
    """Send the Harbor update body for each policy."""
    async with e2b.E2BEnvironment(_spec(allowed_hosts=("start.example.com",))) as env:
        await env.set_network_policy(policy)
        assert _the_box().network_updates == [body]


async def test_stop_job_sandboxes_continues_after_a_failure() -> None:
    """Count the stopped boxes and keep going after one kill fails."""
    envs = [e2b.E2BEnvironment(_spec(tags={JOB_TAG: "job-x"})) for _ in range(3)]
    for env in envs:
        await env.start()
    try:
        first = next(iter(FakeBox.boxes))
        FakeBox.kill_errors = {first: e2b.SandboxException("down")}
        assert await e2b.E2BEnvironment.stop_job_sandboxes("job-x") == 2
        assert list(FakeBox.boxes) == [first]
    finally:
        FakeBox.kill_errors = {}
        for env in envs:
            await env.stop()


async def test_stop_during_start_ends_the_start() -> None:
    """Raise an infra error in start when stop ends it, and leave no box."""
    FakeBox.create_delay = 0.05
    env = e2b.E2BEnvironment(_spec())
    starting = asyncio.create_task(env.start())
    await asyncio.sleep(0.01)
    await env.stop()
    with pytest.raises(SandboxInfraError):
        await starting
    assert FakeBox.boxes == {}


async def test_stop_before_start_does_not_block_a_later_stop() -> None:
    """Kill the box and end the renew loop on stop, start, stop."""
    env = e2b.E2BEnvironment(_spec(lifetime_seconds=3 * 3600), clock=FakeClock())
    await env.stop()
    await env.start()
    renewer = env._renewer
    assert renewer is not None
    assert len(FakeBox.boxes) == 1
    await env.stop()
    assert FakeBox.boxes == {}
    assert renewer.done()


async def test_exec_after_stop_raises() -> None:
    """Refuse a command on a stopped box."""
    env = e2b.E2BEnvironment(_spec())
    async with env:
        pass
    with pytest.raises(SandboxInfraError):
        await env.exec("true")


def test_preflight_needs_the_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Raise a fatal error without E2B_API_KEY, and pass with it."""
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    with pytest.raises(SandboxFatalError, match="E2B_API_KEY"):
        e2b.E2BEnvironment.preflight()
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    e2b.E2BEnvironment.preflight()


def test_preflight_reports_a_missing_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Name the extra when the SDK is not installed."""
    monkeypatch.setattr(e2b.E2BEnvironment, "sandbox_cls", None)
    with pytest.raises(SandboxFatalError, match="`e2b` extra"):
        e2b.E2BEnvironment.preflight()


def test_capabilities_allow_every_network_mode() -> None:
    """Declare every network mode and dynamic policy, billed against the run budget."""
    caps = e2b.E2BEnvironment.capabilities
    assert caps.name == "e2b"
    assert caps.dynamic_network_policy
    assert caps.billable
    assert not caps.protected


def test_factory_entry_is_not_allowed_in_production() -> None:
    """Point the factory at this class and keep it out of production."""
    entry = factory.PROVIDERS["e2b"]
    assert entry.module == "core.sandbox.providers.e2b"
    assert entry.cls == "E2BEnvironment"
    assert entry.extra == "e2b"
    assert entry.production_allowed is False


class FakeUsage:
    """Record the calls a protected box makes to its usage reservation."""

    def __init__(self, request: Mapping[str, Any], *, operation_key: str) -> None:
        """Keep the funded request.

        Args:
            request: The request that the reservation prices.
            operation_key: The box's logical identity.
        """
        self.request = dict(request)
        self.operation_key = operation_key
        self.lifetime_ms = request["lifetime_ms"]
        self.started_at: float | None = None
        self.events: list[Any] = []
        self.extend_error: BaseException | None = None
        self.settle_error: Exception | None = None
        self.confirm_error: Exception | None = None

    def start(self, at: float) -> None:
        """Record the create call.

        Args:
            at: Monotonic time of the create call.
        """
        self.started_at = at
        self.events.append(("start", len(FakeBox.create_calls)))

    def confirm(self, info: Any) -> None:
        """Record the confirm, or raise the scripted error.

        Args:
            info: The box info.

        Raises:
            Exception: The scripted confirm error.
        """
        self.events.append(("confirm", info.sandbox_id))
        if self.confirm_error is not None:
            raise self.confirm_error

    def extend(self, lifetime_ms: int, *, deadline: float) -> None:
        """Record the extension, or raise the scripted error.

        Args:
            lifetime_ms: The new total lifetime.
            deadline: The wait deadline.

        Raises:
            BaseException: The scripted extension error.
        """
        if self.extend_error is not None:
            raise self.extend_error
        self.events.append(("extend", lifetime_ms))
        self.lifetime_ms = lifetime_ms

    def stopped(self, at: float, *, confirmed: bool) -> None:
        """Record the stop.

        Args:
            at: Monotonic time of the stop.
            confirmed: Whether every kill answered.
        """
        self.events.append(("stopped", confirmed, len(FakeBox.boxes)))

    def settle(self) -> None:
        """Record the settle, or raise the scripted error.

        Raises:
            Exception: The scripted settle error.
        """
        self.events.append(("settle",))
        if self.settle_error is not None:
            raise self.settle_error


class UsageFactory:
    """Make one ``FakeUsage`` per reservation and keep them."""

    def __init__(self) -> None:
        """Start with no reservations."""
        self.made: list[FakeUsage] = []
        self.budgets: list[Any] = []

    def __call__(self, budget: Any, request: Mapping[str, Any], *, operation_key: str) -> FakeUsage:
        """Reserve one box.

        Args:
            budget: The budget runtime.
            request: The funded request.
            operation_key: The box's logical identity.

        Returns:
            The new fake reservation.
        """
        self.budgets.append(budget)
        usage = FakeUsage(request, operation_key=operation_key)
        usage.events.append(("reserve", len(FakeBox.create_calls)))
        self.made.append(usage)
        return usage


def _protected(factory_: UsageFactory, **spec: Any) -> e2b.E2BEnvironment:
    """Return a protected environment with a fake budget and usage factory.

    Args:
        factory_: The usage factory.
        **spec: Spec fields to replace.

    Returns:
        The environment.
    """
    return e2b.E2BEnvironment(
        _spec(operation_key="op-1", **spec), budget=object(), usage_factory=factory_, clock=FakeClock()
    )


def test_e2b_has_a_usage_adapter() -> None:
    """Find the E2B usage adapter by its billing module name."""
    adapter = usage_adapter("e2b")
    assert adapter is not None
    assert adapter.name == "e2b"


def test_protected_box_needs_an_operation_key() -> None:
    """Refuse a protected box with no stable operation identity."""
    with pytest.raises(SandboxSpecError):
        e2b.E2BEnvironment(_spec(), budget=object())


async def test_protected_box_reserves_before_create_and_settles_after_kill() -> None:
    """Reserve before the create, confirm after it, settle after the kill."""
    made = UsageFactory()
    async with _protected(made, resources=ResourceRequest(cpus=2, memory_mb=2048)) as env:
        assert (await env.exec("true")).ok
        sandbox_id = _the_box().sandbox_id
    (usage,) = made.made
    assert usage.request == {"lifetime_ms": 420_000, "vcpus": 2, "memory_mb": 2048, "template": None}
    assert usage.operation_key == "op-1"
    assert usage.events == [("reserve", 0), ("start", 0), ("confirm", sandbox_id), ("stopped", True, 0), ("settle",)]
    assert FakeBox.boxes == {}


async def test_spec_without_memory_funds_the_largest_e2b_memory() -> None:
    """Fund E2B's largest memory when the spec leaves memory to the template."""
    made = UsageFactory()
    async with _protected(made):
        pass
    assert made.made[0].request["memory_mb"] == 8 * 1024


async def test_capacity_retries_reuse_one_reservation() -> None:
    """Keep one reservation across rate-limit waits and count from the last create."""
    FakeBox.create_errors = [_status_error(429), e2b.RateLimitException("429")]
    made = UsageFactory()
    async with _protected(made):
        pass
    (usage,) = made.made
    assert [event for event in usage.events if event[0] in {"reserve", "start"}] == [
        ("reserve", 0),
        ("start", 0),
        ("start", 1),
        ("start", 2),
    ]


@pytest.mark.parametrize("status", [502, 503])
async def test_protected_box_fails_the_start_on_a_gateway_error(status: int) -> None:
    """Fail the start on a 502 or 503, so the teardown sweeps a box E2B opened and settles.

    Args:
        status: The gateway status that E2B answers.
    """
    FakeBox.leak_on_error = True
    FakeBox.create_errors = [_status_error(status)]
    made = UsageFactory()
    env = _protected(made)
    with pytest.raises(e2b.SandboxException):
        await env.start()
    assert len(FakeBox.create_calls) == 1
    assert FakeBox.boxes == {}
    assert made.made[0].events[-2:] == [("stopped", True, 0), ("settle",)]


async def test_gateway_error_with_an_empty_sweep_is_not_confirmed() -> None:
    """Charge the funded lifetime when a 503 sweep finds no box: the E2B list can lag."""
    FakeBox.create_errors = [_status_error(503)]
    made = UsageFactory()
    env = _protected(made)
    with pytest.raises(e2b.SandboxException):
        await env.start()
    assert made.made[0].events[-2:] == [("stopped", False, 0), ("settle",)]


async def test_rate_limit_does_not_count_as_runtime() -> None:
    """Clear the start time after a 429, which opens no box."""
    FakeBox.create_errors = [_status_error(429) for _ in range(e2b.PROTECTED_RATE_LIMIT_ATTEMPTS)]
    made = UsageFactory()
    env = _protected(made)
    with pytest.raises(SandboxInfraError):
        await env.start()
    (usage,) = made.made
    assert len(FakeBox.create_calls) == e2b.PROTECTED_RATE_LIMIT_ATTEMPTS
    assert usage.started_at is None
    assert usage.events[-2:] == [("stopped", True, 0), ("settle",)]


async def test_failed_create_still_settles() -> None:
    """Settle the reservation when the create fails outright."""
    FakeBox.create_errors = [e2b.SandboxException("bad template", status_code=400)]
    made = UsageFactory()
    env = _protected(made)
    with pytest.raises(e2b.SandboxException):
        await env.start()
    assert made.made[0].events[-2:] == [("stopped", True, 0), ("settle",)]


async def test_oversized_template_stops_the_box_and_settles() -> None:
    """Stop a box that is larger than its funding, then settle it."""
    made = UsageFactory()
    FakeBox.size = (4, 4096)
    env = _protected(made, resources=ResourceRequest(cpus=2, memory_mb=2048))

    original = UsageFactory.__call__

    def oversized(self: UsageFactory, budget: Any, request: Mapping[str, Any], *, operation_key: str) -> FakeUsage:
        """Reserve a box whose confirm refuses the template size.

        Args:
            self: The factory.
            budget: The budget runtime.
            request: The funded request.
            operation_key: The box's logical identity.

        Returns:
            The fake reservation.
        """
        usage = original(self, budget, request, operation_key=operation_key)
        usage.confirm_error = SandboxSpecError("too large")
        return usage

    env._usage_factory = oversized.__get__(made)
    with pytest.raises(SandboxSpecError):
        await env.start()
    assert FakeBox.boxes == {}
    assert made.made[0].events[-2:] == [("stopped", True, 0), ("settle",)]


async def test_cancel_during_start_settles_the_reservation() -> None:
    """Settle the reservation when a stop cancels the start mid-create."""
    FakeBox.create_delay = 0.05
    made = UsageFactory()
    env = e2b.E2BEnvironment(_spec(operation_key="op-1"), budget=object(), usage_factory=made)
    starting = asyncio.create_task(env.start())
    await asyncio.sleep(0.01)
    await env.stop()
    with pytest.raises(SandboxInfraError):
        await starting
    assert FakeBox.boxes == {}
    assert made.made[0].events[-1] == ("settle",)


async def test_unconfirmed_kill_is_reported_to_the_settle() -> None:
    """Tell the reservation the kill failed, so it charges the funded lifetime."""
    made = UsageFactory()
    env = _protected(made)
    await env.start()
    FakeBox.kill_errors = {_the_box().sandbox_id: e2b.SandboxException("down")}
    await env.stop()
    stopped = next(event for event in made.made[0].events if event[0] == "stopped")
    assert stopped[1] is False


async def test_settle_failure_is_raised_by_stop_once() -> None:
    """Raise a failed settle from the first stop only."""
    made = UsageFactory()
    env = _protected(made)
    await env.start()
    made.made[0].settle_error = RuntimeError("database down")
    with pytest.raises(UsagePendingError):
        await env.stop()
    await env.stop()


async def test_renew_loop_funds_each_extension_before_it_asks_e2b() -> None:
    """Fund the longer lifetime first, then extend the E2B timeout."""
    made = UsageFactory()
    env = _protected(made, lifetime_seconds=2 * 3600)
    await env.start()
    try:
        box = _the_box()
        await asyncio.wait_for(env._renewer, 1)
        (usage,) = made.made
        extends = [event[1] for event in usage.events if event[0] == "extend"]
        # The first slice also funds the create time limit.
        assert usage.request["lifetime_ms"] == (e2b.LIFETIME_SLICE + e2b.CREATE_TIMEOUT) * 1000
        # Each renewal funds the time already used, the new timeout and the call time limit.
        assert box.timeouts == [e2b.LIFETIME_SLICE, 600]
        assert box.request_timeouts == [e2b.RENEW_CALL_TIMEOUT, e2b.RENEW_CALL_TIMEOUT]
        assert extends == [6_930_000, 7_230_000]
    finally:
        await env.stop()


async def test_budget_refusal_stops_the_renew_loop() -> None:
    """Leave the box on its funded timeout when the budget refuses more."""
    made = UsageFactory()
    env = _protected(made, lifetime_seconds=2 * 3600)
    original = UsageFactory.__call__

    def refusing(self: UsageFactory, budget: Any, request: Mapping[str, Any], *, operation_key: str) -> FakeUsage:
        """Reserve a box whose extensions the budget refuses.

        Args:
            self: The factory.
            budget: The budget runtime.
            request: The funded request.
            operation_key: The box's logical identity.

        Returns:
            The fake reservation.
        """
        usage = original(self, budget, request, operation_key=operation_key)
        usage.extend_error = BudgetReached("no funds")
        return usage

    env._usage_factory = refusing.__get__(made)
    await env.start()
    try:
        box = _the_box()
        await asyncio.wait_for(env._renewer, 1)
        assert box.timeouts == []
    finally:
        await env.stop()
    assert made.made[0].events[-1] == ("settle",)


async def _refused_box(made: UsageFactory) -> e2b.E2BEnvironment:
    """Start a two-hour protected box whose first extension the budget refuses.

    Args:
        made: The usage factory.

    Returns:
        The started environment, with its renew loop ended.
    """
    env = _protected(made, lifetime_seconds=2 * 3600)
    original = UsageFactory.__call__

    def refusing(self: UsageFactory, budget: Any, request: Mapping[str, Any], *, operation_key: str) -> FakeUsage:
        """Reserve a box whose extensions the budget refuses.

        Args:
            self: The factory.
            budget: The budget runtime.
            request: The funded request.
            operation_key: The box's logical identity.

        Returns:
            The fake reservation.
        """
        usage = original(self, budget, request, operation_key=operation_key)
        usage.extend_error = BudgetReached("no funds")
        return usage

    env._usage_factory = refusing.__get__(made)
    await env.start()
    await asyncio.wait_for(env._renewer, 1)
    return env


async def test_budget_refusal_raises_budget_reached_after_the_funded_timeout() -> None:
    """Raise BudgetReached, not an infra error, once the last funded timeout is over."""
    made = UsageFactory()
    env = await _refused_box(made)
    try:
        assert (await env.exec("true")).ok
        env._clock.now = e2b.LIFETIME_SLICE
        with pytest.raises(BudgetReached):
            await env.exec("true")
        with pytest.raises(BudgetReached):
            await env.download_file("/tmp/x")
    finally:
        await env.stop()


async def test_box_error_after_the_funded_timeout_raises_budget_reached() -> None:
    """Turn a box call error into BudgetReached when the box ended on a budget stop."""
    made = UsageFactory()
    env = await _refused_box(made)
    clock = env._clock

    async def expired(*args: Any, **kwargs: Any) -> Any:
        """Fail as a box that E2B ended at its timeout, after the funded time.

        Args:
            *args: The dispatch arguments.
            **kwargs: The dispatch keywords.

        Raises:
            SandboxInfraError: Always.
        """
        clock.now = e2b.LIFETIME_SLICE
        raise SandboxInfraError("sandbox not found")

    env._dispatch = expired
    try:
        with pytest.raises(BudgetReached):
            await env.exec("true")
    finally:
        await env.stop()


async def test_cancel_while_the_reservation_thread_runs_settles_it() -> None:
    """Keep and settle a reservation that a stop interrupts while its thread runs."""
    entered = threading.Event()
    release = threading.Event()
    made = UsageFactory()

    def slow(budget: Any, request: Mapping[str, Any], *, operation_key: str) -> FakeUsage:
        """Reserve only after the test releases the thread.

        Args:
            budget: The budget runtime.
            request: The funded request.
            operation_key: The box's logical identity.

        Returns:
            The fake reservation.
        """
        entered.set()
        release.wait(5)
        return made(budget, request, operation_key=operation_key)

    env = e2b.E2BEnvironment(_spec(operation_key="op-1"), budget=object(), usage_factory=slow, clock=FakeClock())
    starting = asyncio.create_task(env.start())
    await asyncio.to_thread(entered.wait, 5)
    stopping = asyncio.create_task(env.stop())
    await asyncio.sleep(0.01)
    release.set()
    await stopping
    with pytest.raises(SandboxInfraError):
        await starting
    assert FakeBox.create_calls == []
    assert made.made[0].events == [("reserve", 0), ("stopped", True, 0), ("settle",)]


async def test_failed_tagged_kill_is_not_confirmed() -> None:
    """Charge the funded lifetime when the sweep cannot kill one tagged box."""
    made = UsageFactory()
    env = _protected(made)
    await env.start()
    stray = FakeBox(_the_box().metadata, {})
    FakeBox.boxes[stray.sandbox_id] = stray
    FakeBox.kill_errors = {stray.sandbox_id: e2b.SandboxException("down")}
    await env.stop()
    stopped = next(event for event in made.made[0].events if event[0] == "stopped")
    assert stopped[1] is False


async def test_cancel_during_the_rate_limit_wait_settles_with_no_runtime() -> None:
    """Settle a reservation at zero runtime when a stop ends the wait after a 429."""
    FakeBox.create_errors = [_status_error(429)]
    blocked = asyncio.Event()
    checks = 0

    async def at_capacity() -> bool:
        """Report capacity once, then hold the start in the capacity wait.

        Returns:
            False on the first check.
        """
        nonlocal checks
        checks += 1
        if checks > 1:
            blocked.set()
            await asyncio.Event().wait()
        return False

    made = UsageFactory()
    env = e2b.E2BEnvironment(
        _spec(operation_key="op-1"), budget=object(), usage_factory=made, clock=FakeClock(), at_capacity=at_capacity
    )
    starting = asyncio.create_task(env.start())
    await asyncio.wait_for(blocked.wait(), 1)
    await env.stop()
    with pytest.raises(SandboxInfraError):
        await starting
    (usage,) = made.made
    assert usage.started_at is None
    assert usage.events[-2:] == [("stopped", True, 0), ("settle",)]


def test_protected_lifetime_must_fit_the_extension_ceiling() -> None:
    """Refuse a protected spec whose last extension would pass the 24-hour quote."""
    with pytest.raises(SandboxSpecError):
        _protected(UsageFactory(), lifetime_seconds=e2b.MAX_PROTECTED_LIFETIME + 1)
    e2b.E2BEnvironment(_spec(lifetime_seconds=e2b.E2B_MAX_LIFETIME))


async def test_longest_protected_box_stays_inside_the_quote_ceiling() -> None:
    """Fund every extension of the longest protected box within 24 hours."""
    made = UsageFactory()
    env = _protected(made, lifetime_seconds=e2b.MAX_PROTECTED_LIFETIME)
    await env.start()
    try:
        await asyncio.wait_for(env._renewer, 5)
        (usage,) = made.made
        extends = [event[1] for event in usage.events if event[0] == "extend"]
        assert extends
        assert max(extends) <= e2b.E2B_MAX_LIFETIME * 1000
    finally:
        await env.stop()


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(lambda: e2b.SandboxException("bad template", status_code=400), id="400"),
        pytest.param(lambda: e2b.SandboxException("no template", status_code=404), id="404"),
        pytest.param(lambda: e2b.AuthenticationException("bad key"), id="401"),
    ],
)
async def test_refused_create_bills_no_runtime(error: Callable[[], BaseException]) -> None:
    """Clear the start time and confirm the stop when E2B refuses the create.

    Args:
        error: Makes the refusal that E2B answers.
    """
    FakeBox.create_errors = [error()]
    made = UsageFactory()
    env = _protected(made)
    with pytest.raises(Exception):  # noqa: B017 - each refusal has its own SDK type
        await env.start()
    (usage,) = made.made
    assert usage.started_at is None
    assert usage.events[-2:] == [("stopped", True, 0), ("settle",)]


async def test_gateway_timeout_answer_keeps_the_outcome_unknown() -> None:
    """Treat a 408 as unknown: a gateway can answer it after E2B opened the box."""
    FakeBox.create_errors = [_status_error(408)]
    made = UsageFactory()
    env = _protected(made)
    with pytest.raises(e2b.SandboxException):
        await env.start()
    (usage,) = made.made
    assert usage.started_at is not None
    assert usage.events[-2:] == [("stopped", False, 0), ("settle",)]


async def test_capacity_wait_ends_when_the_hold_is_too_old() -> None:
    """End a protected capacity wait that holds the funds past the time limit."""
    FakeBox.create_errors = [_status_error(429)]
    checks = 0

    async def at_capacity() -> bool:
        """Report capacity on the first check, then no capacity forever.

        Returns:
            False on the first check, then True.
        """
        nonlocal checks
        checks += 1
        return checks > 1

    made = UsageFactory()
    clock = FakeClock()
    env = e2b.E2BEnvironment(
        _spec(operation_key="op-1"), budget=object(), usage_factory=made, clock=clock, at_capacity=at_capacity
    )
    with pytest.raises(SandboxInfraError):
        await env.start()
    assert clock.now >= e2b.PROTECTED_HOLD_WAIT
    assert len(FakeBox.create_calls) == 1
    (usage,) = made.made
    assert usage.started_at is None
    assert usage.events[-2:] == [("stopped", True, 0), ("settle",)]


def test_protected_memory_must_fit_e2b() -> None:
    """Refuse a protected spec with more memory than E2B allows, before any reservation."""
    with pytest.raises(SandboxSpecError):
        _protected(UsageFactory(), resources=ResourceRequest(cpus=1, memory_mb=e2b.MAX_MEMORY_MB + 1))


class SlowCreateBucket:
    """Act as a create bucket whose second wait moves the clock past the hold limit."""

    def __init__(self, clock: FakeClock) -> None:
        """Keep the clock.

        Args:
            clock: The fake clock of the environment.
        """
        self.clock = clock
        self.calls = 0

    async def acquire(self) -> None:
        """Wait for a create token; the second wait takes the whole hold limit."""
        self.calls += 1
        if self.calls > 1:
            self.clock.now += e2b.PROTECTED_HOLD_WAIT


class FreeBucket:
    """Act as a read bucket that never waits."""

    async def acquire(self) -> None:
        """Return at once."""


async def test_create_bucket_wait_counts_against_the_hold_limit() -> None:
    """Fail before a resent create when the create-bucket wait used up the hold limit."""
    FakeBox.create_errors = [_status_error(429)]
    made = UsageFactory()
    clock = FakeClock()
    pacing = ProviderPacing(reads=FreeBucket(), creates=SlowCreateBucket(clock))
    env = e2b.E2BEnvironment(
        _spec(operation_key="op-1"), budget=object(), usage_factory=made, clock=clock, pacing=pacing
    )
    with pytest.raises(SandboxInfraError):
        await env.start()
    assert len(FakeBox.create_calls) == 1
    (usage,) = made.made
    assert usage.started_at is None
    assert usage.events[-2:] == [("stopped", True, 0), ("settle",)]


async def test_slow_confirm_does_not_push_the_last_extension_past_the_quote() -> None:
    """Count the renew deadline from the create call, so a slow confirm cannot pass 24 hours."""
    made = UsageFactory()
    env = _protected(made, lifetime_seconds=e2b.MAX_PROTECTED_LIFETIME)
    clock = env._clock
    slow = True

    async def pace_read() -> None:
        """Make the first read wait, which runs before the size check, take 1000 seconds."""
        nonlocal slow
        if slow:
            slow = False
            clock.now += 1000

    env._pace_read = pace_read
    await env.start()
    try:
        await asyncio.wait_for(env._renewer, 5)
        (usage,) = made.made
        extends = [event[1] for event in usage.events if event[0] == "extend"]
        assert extends
        assert max(extends) <= e2b.E2B_MAX_LIFETIME * 1000
    finally:
        await env.stop()
