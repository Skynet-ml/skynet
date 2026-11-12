"""Tests for the E2B provider against a fake SDK, with no network and no cost."""

from __future__ import annotations

import asyncio
import itertools
import shlex
from collections.abc import Callable, Mapping
from typing import Any, ClassVar

import pytest

from core.sandbox import factory
from core.sandbox.environment import JOB_TAG, NetworkPolicy
from core.sandbox.errors import SandboxFatalError, SandboxInfraError, StartTimeoutError
from core.sandbox.pacing import ProviderAtCapacityError
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

    async def set_timeout(self, timeout: int) -> None:  # noqa: ASYNC109 - the SDK method has this name
        """Record a timeout extension, or raise the next scripted error.

        Args:
            timeout: The new timeout in seconds.

        Raises:
            BaseException: The next scripted error.
        """
        if self.timeout_errors:
            raise self.timeout_errors.pop(0)
        self.timeouts.append(timeout)

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
    """Declare every network mode and dynamic policy, with no billing."""
    caps = e2b.E2BEnvironment.capabilities
    assert caps.name == "e2b"
    assert caps.dynamic_network_policy
    assert not caps.billable
    assert not caps.protected


def test_factory_entry_is_not_allowed_in_production() -> None:
    """Point the factory at this class and keep it out of production."""
    entry = factory.PROVIDERS["e2b"]
    assert entry.module == "core.sandbox.providers.e2b"
    assert entry.cls == "E2BEnvironment"
    assert entry.extra == "e2b"
    assert entry.production_allowed is False
