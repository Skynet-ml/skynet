"""Tests for the async Vercel provider with fake SDK objects. No network, no paid calls."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from vercel.sandbox import SandboxApiError
from vercel.sandbox._internal.recovery import execute_with_sandbox_recovery

from core.billing.model_dispatch import ModelHTTPResult
from core.billing.model_mailbox import ModelMailbox
from core.billing.signals import BudgetReached
from core.sandbox import sync_bridge
from core.sandbox.capabilities import SandboxSpecError
from core.sandbox.environment import NetworkPolicy
from core.sandbox.errors import SandboxInfraError
from core.sandbox.pacing import SYSTEM_CLOCK
from core.sandbox.providers import vercel
from core.sandbox.providers.vercel import VercelEnvironment, box_name, policy_confirmed
from core.sandbox.resources import ResourceRequest, SandboxSpec
from core.sandbox.services import ComposeService, ServicesTooLargeError
from core.sandbox.telemetry import use_output_sink

_PATHS = re.compile(r"> (\S+\.out) 2> (\S+\.err); echo \$\? > (\S+)$")


def _gone() -> SandboxApiError:
    """Build the SDK error that a stopped session answers.

    Returns:
        A 410 ``sandbox_stopped`` error.
    """
    return SandboxApiError(httpx.Response(410), "gone", data={"error": {"code": "sandbox_stopped"}})


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


class FakeFs:
    """Keep sandbox files in a dict."""

    def __init__(self) -> None:
        """Start with no files."""
        self.files: dict[str, str | bytes] = {}
        self.dirs: list[str] = []
        self.stopped = False

    def _check(self) -> None:
        """Answer 410 after the session stops.

        Raises:
            SandboxApiError: The session stopped.
        """
        if self.stopped:
            raise _gone()

    async def exists(self, path: str, *, cwd: str) -> bool:
        """Tell whether a file exists.

        Args:
            path: The file path.
            cwd: The working directory.

        Returns:
            True when the file exists.
        """
        self._check()
        return path in self.files

    async def read_text(self, path: str, *, cwd: str, errors: str = "strict") -> str:
        """Read one file; a file stored as bytes is decoded as UTF-8.

        Args:
            path: The file path.
            cwd: The working directory.
            errors: The decoding error policy.

        Returns:
            The file text.
        """
        self._check()
        value = self.files[path]
        return value.decode(errors=errors) if isinstance(value, bytes) else value

    async def write_text(self, path: str, text: str, *, cwd: str) -> None:
        """Write one file.

        Args:
            path: The file path.
            text: The file text.
            cwd: The working directory.
        """
        self._check()
        self.files[path] = text

    async def mkdir(self, path: str, *, cwd: str, recursive: bool = True) -> None:
        """Record a directory.

        Args:
            path: The directory path.
            cwd: The working directory.
            recursive: Unused.
        """
        self._check()
        self.dirs.append(path)


class FakeProcess:
    """Play a list of wait outcomes, then return the exit code."""

    def __init__(self, fs: FakeFs, script: str, outcomes: list[Any], *, block: bool = False) -> None:
        """Keep the wrapper paths and the planned outcomes.

        Args:
            fs: The box file system.
            script: The wrapper script.
            outcomes: Exceptions to raise, then an exit code.
            block: Wait until terminate or kill when True.
        """
        self.fs = fs
        self.out, self.err, self.sentinel = _PATHS.search(script).groups()
        self.outcomes = list(outcomes)
        self.block = block
        self.released = asyncio.Event()
        self.waits = 0
        self.terminated = 0
        self.killed = 0

    async def wait(self) -> int:
        """Return the next outcome, or block until the process ends.

        Returns:
            The exit code.
        """
        self.waits += 1
        if self.block:
            await self.released.wait()
            return 143
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        self.fs.files[self.out] = "hello\n"
        self.fs.files[self.sentinel] = f"{outcome}\n"
        return outcome

    async def terminate(self) -> None:
        """Count the terminate call and release a blocked wait."""
        self.terminated += 1
        self.released.set()

    async def kill(self) -> None:
        """Count the kill call and release a blocked wait."""
        self.killed += 1
        self.released.set()


class FakeSession:
    """Stand in for a Vercel runtime session; it never resumes after a stop."""

    cwd: str | None = None

    def __init__(self, readback: Any = None) -> None:
        """Keep the policy readback.

        Args:
            readback: The ``network_policy`` readback.
        """
        self.id = "session-1"
        self.network_policy = readback
        self.extensions: list[timedelta] = []
        self.policies: list[Any] = []
        self.stops = 0
        self.next_readback = readback
        self.log: list[str] | None = None
        self.box: FakeBox | None = None
        self.tail_reads = 0

    @property
    def fs(self) -> FakeFs:
        """Return the file system of the box.

        Returns:
            The fake file system.
        """
        return self.box.fs

    async def create_process(self, command: str, args: list[str], **kwargs: Any) -> FakeProcess:
        """Start a process on this session, or answer 410 after a stop.

        Args:
            command: The program.
            args: The program arguments.
            kwargs: The other SDK arguments.

        Returns:
            The fake process.

        Raises:
            SandboxApiError: The session stopped.
        """
        if self.stops:
            raise _gone()
        return await self.box.create_process(command, args, **kwargs)

    async def run_process(self, command: str, args: list[str], **kwargs: Any) -> SimpleNamespace:
        """Answer the output tail read: one base64 line for each ``start path`` pair.

        Args:
            command: The program.
            args: The shell flags, the script, ``$0``, then the pairs.
            kwargs: The other SDK arguments.

        Returns:
            The completed process.

        Raises:
            SandboxApiError: The session stopped.
        """
        if self.stops:
            raise _gone()
        self.tail_reads += 1
        pairs = args[5:]
        lines = []
        for start, path in zip(pairs[::2], pairs[1::2], strict=True):
            value = self.fs.files.get(path, "")
            data = (value if isinstance(value, bytes) else value.encode())[int(start) - 1 :]
            lines.append(base64.b64encode(data).decode())
        return SimpleNamespace(returncode=0, stdout="".join(f"{line}\n" for line in lines), stderr="")

    async def extend_execution_time_limit(self, duration: timedelta) -> FakeSession:
        """Record one extension.

        Args:
            duration: The extra lifetime.

        Returns:
            The session.
        """
        if self.log is not None:
            self.log.append(f"vercel-extend:{int(duration.total_seconds() * 1000)}")
        self.extensions.append(duration)
        return self

    async def update_network_policy(self, policy: Any) -> FakeSession:
        """Record one policy change.

        Args:
            policy: The SDK policy.

        Returns:
            The session with the planned readback.

        Raises:
            SandboxApiError: The session stopped.
        """
        if self.stops:
            raise _gone()
        self.policies.append(policy)
        self.network_policy = self.next_readback
        return self

    async def stop(self) -> None:
        """Count the stop call; later file calls answer 410."""
        self.stops += 1
        if self.box is not None:
            self.box.fs.stopped = True


class FakeBox:
    """Stand in for a Vercel sandbox and count every read."""

    def __init__(self, name: str, session: FakeSession) -> None:
        """Make a running box.

        Args:
            name: The box name.
            session: The current session.
        """
        self.name = name
        self.status = "running"
        self.cwd = "/vercel/sandbox"
        self.current_session = session
        session.box = self
        self.fs = FakeFs()
        self.stops = 0
        self.destroys = 0
        self.refreshes = 0
        self.process_reads = 0
        self.processes: list[FakeProcess] = []
        self.create_errors: list[BaseException] = []
        self.outcomes: list[Any] = [0]
        self.block = False
        self.create_calls: list[dict[str, Any]] = []

    async def refresh(self) -> None:
        """Count a status read."""
        self.refreshes += 1

    async def get_process(self, *args: Any, **kwargs: Any) -> None:
        """Count a process status read.

        Args:
            args: Unused.
            kwargs: Unused.
        """
        self.process_reads += 1

    async def create_process(self, command: str, args: list[str], **kwargs: Any) -> FakeProcess:
        """Start a fake process, or raise a planned error.

        Args:
            command: The program.
            args: The program arguments.
            kwargs: The other SDK arguments.

        Returns:
            The fake process.
        """
        self.create_calls.append({"command": command, "args": args, **kwargs})
        if self.create_errors:
            raise self.create_errors.pop(0)
        process = FakeProcess(self.fs, args[-1], self.outcomes, block=self.block)
        self.processes.append(process)
        return process

    async def stop(self) -> None:
        """Count the stop call."""
        self.stops += 1
        self.status = "stopped"

    async def destroy(self, *, delete_orphan_snapshots: bool = False) -> None:
        """Count the destroy call.

        Args:
            delete_orphan_snapshots: Unused.
        """
        self.destroys += 1


class ResumingBox(FakeBox):
    """A box whose ``create_process`` resumes a stopped session, like the SDK ``Sandbox``."""

    def __init__(self, name: str, session: FakeSession) -> None:
        """Make a running box that counts resumes.

        Args:
            name: The box name.
            session: The current session.
        """
        super().__init__(name, session)
        self.resumes = 0
        self.session_ids: list[str] = []

    def _capture_recovery_session_id(self) -> str:
        """Return the id that the SDK recovery helper pins.

        Returns:
            The current session id.
        """
        return self.current_session.id

    async def _await_shared_resume(self) -> None:
        """Resume into a new session, as the SDK does after a 410."""
        self.resumes += 1
        resumed = FakeSession()
        resumed.id = f"session-{self.resumes + 1}"
        resumed.box = self
        self.current_session = resumed

    async def create_process(self, command: str, args: list[str], **kwargs: Any) -> FakeProcess:
        """Start a process through the real SDK recovery helper.

        Args:
            command: The program.
            args: The program arguments.
            kwargs: The other SDK arguments.

        Returns:
            The fake process.
        """

        async def operation(session_id: str) -> FakeProcess:
            """Answer 410 on a stopped session, else start the process.

            Args:
                session_id: The session that the helper pinned.

            Returns:
                The fake process.
            """
            if self.current_session.stops:
                raise _gone()
            self.session_ids.append(session_id)
            process = FakeProcess(self.fs, args[-1], [0])
            self.processes.append(process)
            return process

        return await execute_with_sandbox_recovery(operation, coordinator=self)


class FakeClient:
    """Stand in for the async SDK client."""

    def __init__(self, log: list[str], readback: Any = None) -> None:
        """Prepare one box per name.

        Args:
            log: The shared call log.
            readback: The policy readback of new sessions.
        """
        self.log = log
        self.readback = readback
        self.boxes: dict[str, FakeBox] = {}
        self.create_kwargs: list[dict[str, Any]] = []
        self.create_errors: list[BaseException] = []
        self.hang = False
        self.created = asyncio.Event()
        self.closed = 0
        self.capture: Any = None

    def create_sandbox(self, **kwargs: Any) -> Any:
        """Return the single-use create operation.

        Args:
            kwargs: The SDK create arguments.

        Returns:
            An awaitable that makes the box.
        """
        self.create_kwargs.append(kwargs)
        self.log.append("create")

        async def operation() -> FakeBox:
            """Make the box, raise a planned error, or hang.

            Returns:
                The fake box.
            """
            box = self.boxes.setdefault(kwargs["name"], FakeBox(kwargs["name"], FakeSession(self.readback)))
            self.created.set()
            if self.hang:
                await asyncio.Event().wait()
            if self.create_errors:
                raise self.create_errors.pop(0)
            return box

        return operation()

    async def get_sandbox(self, *, name: str) -> FakeBox:
        """Find a box by name.

        Args:
            name: The box name.

        Returns:
            The box.
        """
        return self.boxes[name]

    async def query_sandboxes(self, *, query: Any) -> Any:
        """Yield every box.

        Args:
            query: Unused.

        Yields:
            Each box.
        """
        for box in self.boxes.values():
            yield box

    async def aclose(self) -> None:
        """Count the close call."""
        self.closed += 1


class FakeUsage:
    """Record the reservation calls in order."""

    def __init__(self, log: list[str], budget: Any, request: dict[str, Any], *, operation_key: str) -> None:
        """Record the reservation.

        Args:
            log: The shared call log.
            budget: The budget runtime.
            request: The usage request.
            operation_key: The operation key.
        """
        self.log = log
        self.request = request
        self.operation_key = operation_key
        self.extend_error: BaseException | None = None
        self.confirm_error: Exception | None = None
        self.calls: list[str] = []
        log.append("reserve")

    def _record(self, name: str) -> None:
        """Log one call.

        Args:
            name: The call name.
        """
        self.calls.append(name)
        self.log.append(name)

    def capture_response(self, response: Any) -> None:
        """Log a captured response.

        Args:
            response: Unused.
        """
        self._record("capture")

    def confirm_created(self, session: Any) -> None:
        """Log the create confirmation.

        Args:
            session: Unused.
        """
        self._record("confirm_created")

    def extend(self, lifetime_ms: int, *, deadline: float) -> None:
        """Log a funded slice, or raise a planned error.

        Args:
            lifetime_ms: The new funded lifetime.
            deadline: Unused.
        """
        self._record(f"fund:{lifetime_ms}")
        if self.extend_error is not None:
            raise self.extend_error

    def confirm_extended(self, session: Any) -> None:
        """Log the extension confirmation, or raise a planned error.

        Args:
            session: Unused.
        """
        self._record("confirm_extended")
        if self.confirm_error is not None:
            raise self.confirm_error

    def fail(self, error: BaseException) -> None:
        """Log a failed create.

        Args:
            error: Unused.
        """
        self._record("fail")

    def settle(self) -> None:
        """Log the settlement."""
        self._record("settle")


@pytest.fixture
def log() -> list[str]:
    """Return the shared call log.

    Returns:
        An empty list.
    """
    return []


@pytest.fixture
def client(log: list[str]) -> FakeClient:
    """Return a fake SDK client.

    Args:
        log: The shared call log.

    Returns:
        The fake client.
    """
    return FakeClient(log)


def make_env(
    client: FakeClient,
    log: list[str],
    *,
    lifetime: float = 300,
    protected: bool = True,
    clock: FakeClock | None = None,
    services: tuple[ComposeService, ...] = (),
    **spec_fields: Any,
) -> VercelEnvironment:
    """Make a provider wired to the fakes.

    Args:
        client: The fake client.
        log: The shared call log.
        lifetime: The spec lifetime in seconds.
        protected: Fund the box from a fake budget.
        clock: The fake clock.
        services: The compose services.
        spec_fields: Other spec fields.

    Returns:
        The provider.
    """
    spec_fields.setdefault("operation_key", "op-1")
    spec = SandboxSpec(lifetime_seconds=lifetime, image="python:3.12", **spec_fields)
    budget = SimpleNamespace(budget_id="budget-1", generation=3) if protected else None

    def usage_factory(budget: Any, request: dict[str, Any], *, operation_key: str) -> FakeUsage:
        """Make a fake reservation.

        Args:
            budget: The budget runtime.
            request: The usage request.
            operation_key: The operation key.

        Returns:
            The fake reservation.
        """
        return FakeUsage(log, budget, request, operation_key=operation_key)

    def client_factory(capture: Any) -> FakeClient:
        """Return the fake client.

        Args:
            capture: The response capture hook.

        Returns:
            The fake client.
        """
        client.capture = capture
        return client

    return VercelEnvironment(
        spec,
        budget=budget,
        client_factory=client_factory,
        usage_factory=usage_factory,
        clock=clock or FakeClock(),
        services=services,
    )


def readback(mode: str, domains: tuple[str, ...] = (), subnets: Any = None) -> SimpleNamespace:
    """Make a policy readback.

    Args:
        mode: The readback mode.
        domains: The allowed domains.
        subnets: The subnet readback.

    Returns:
        The readback.
    """
    return SimpleNamespace(mode=mode, allowed_domains=domains, subnets=subnets)


async def test_running_command_makes_no_status_reads(client: FakeClient, log: list[str]) -> None:
    """Check that exec waits on the long poll and never polls status."""
    env = make_env(client, log)
    await env.start()
    result = await env.exec("echo hello", timeout_sec=30)
    box = client.boxes[env.name]
    assert result.exit_code == 0
    assert result.stdout == "hello\n"
    assert not result.timed_out
    assert box.processes[0].waits == 1
    assert box.refreshes == 0
    assert box.process_reads == 0
    call = box.create_calls[0]
    assert call["kill_after"] == 60
    assert "timeout --signal=KILL 30s" in call["args"][-1]
    await env.stop()


async def test_wait_survives_19_transport_errors(client: FakeClient, log: list[str]) -> None:
    """Check that 19 transport errors still end with the code from wait()."""
    env = make_env(client, log)
    await env.start()
    box = client.boxes[env.name]
    box.outcomes = [httpx.ReadError("cut")] * 19 + [3]
    result = await env.exec("exit 3")
    assert result.exit_code == 3
    assert box.processes[0].waits == 20
    await env.stop()


async def test_wait_reads_sentinel_after_20_errors(client: FakeClient, log: list[str]) -> None:
    """Check that the 20th error reads the exit code from the sentinel file."""
    env = make_env(client, log)
    await env.start()
    box = client.boxes[env.name]
    box.outcomes = [httpx.RemoteProtocolError("cut")] * 20 + [0]

    original = box.create_process

    async def create_and_finish(command: str, args: list[str], **kwargs: Any) -> FakeProcess:
        """Start the process and write the sentinel as the box would.

        Args:
            command: The program.
            args: The program arguments.
            kwargs: The other SDK arguments.

        Returns:
            The fake process.
        """
        process = await original(command, args, **kwargs)
        box.fs.files[process.sentinel] = "7\n"
        return process

    box.create_process = create_and_finish
    result = await env.exec("exit 7")
    assert result.exit_code == 7
    assert box.processes[0].waits == 20
    await env.stop()


async def test_wait_without_sentinel_fails(client: FakeClient, log: list[str]) -> None:
    """Check that 20 errors with no sentinel code raise an infra error."""
    env = make_env(client, log)
    await env.start()
    client.boxes[env.name].outcomes = [httpx.ReadError("cut")] * 20
    with pytest.raises(SandboxInfraError):
        await env.exec("true")
    await env.stop()


async def test_cancel_in_create_stops_once_and_settles(client: FakeClient, log: list[str]) -> None:
    """Check that a cancel during create finds the box, stops it once and settles."""
    client.hang = True
    env = make_env(client, log)
    task = asyncio.create_task(env.start())
    await asyncio.wait_for(client.created.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await env.stop()
    box = client.boxes[env.name]
    assert box.stops == 1
    assert env.usage.calls.count("settle") == 1
    assert "fail" not in env.usage.calls


async def test_cancel_while_running_stops_once_and_settles(client: FakeClient, log: list[str]) -> None:
    """Check that a cancel while the box is idle stops it once and settles."""
    env = make_env(client, log)
    entered = asyncio.Event()

    async def body() -> None:
        """Hold the box open until the cancel."""
        async with env:
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(body())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await env.stop()
    assert client.boxes[env.name].stops == 1
    assert env.usage.calls.count("settle") == 1


async def test_cancel_in_exec_stops_process_and_box_once(client: FakeClient, log: list[str]) -> None:
    """Check that a cancel during exec ends the command, then stops the box once."""
    env = make_env(client, log)
    await env.start()
    box = client.boxes[env.name]
    box.block = True
    task = asyncio.create_task(env.exec("sleep 100"))
    for _ in range(5):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert box.processes[0].terminated == 1
    await env.stop()
    await env.stop()
    assert box.stops == 1
    assert env.usage.calls.count("settle") == 1


async def test_cancel_in_exec_with_hung_terminate_still_stops_box(
    client: FakeClient, log: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check that a terminate call that never returns does not keep the box running."""
    grace = 0.2
    monkeypatch.setattr(vercel, "CANCEL_GRACE", grace)
    env = make_env(client, log)
    await env.start()
    box = client.boxes[env.name]
    box.block = True
    hang = asyncio.Event()

    async def hung_terminate() -> None:
        """Stall like an HTTP call with a very long read timeout."""
        await hang.wait()

    task = asyncio.create_task(env.exec("sleep 100", timeout_sec=30))
    for _ in range(5):
        await asyncio.sleep(0)
    box.processes[0].terminate = hung_terminate
    task.cancel()
    try:
        done, _ = await asyncio.wait({task}, timeout=3 * grace)
        assert task in done
    finally:
        hang.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert box.processes[0].killed == 1
    await env.stop()
    assert box.stops == 1
    assert env.usage.calls.count("settle") == 1


async def test_teardown_settles_after_running_create_confirmation(
    client: FakeClient, log: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check that a cancel during the create confirmation settles only after it ends."""
    entered = threading.Event()
    gate = threading.Event()

    def slow_confirm(self: FakeUsage, session: Any) -> None:
        """Hold the create confirmation until the test releases it.

        Args:
            self: The fake reservation.
            session: Unused.
        """
        entered.set()
        gate.wait(5)
        self._record("confirm_created")

    monkeypatch.setattr(FakeUsage, "confirm_created", slow_confirm)
    env = make_env(client, log, lifetime=1200)
    task = asyncio.create_task(env.start())
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0.05)
    try:
        assert "settle" not in log
    finally:
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
    await env.stop()
    assert log.index("confirm_created") < log.index("settle")
    assert client.boxes[env.name].stops == 1


async def test_failed_create_is_not_retried_and_fails_reservation(client: FakeClient, log: list[str]) -> None:
    """Check that a failed create is sent once and releases the reservation."""
    client.create_errors = [SandboxApiError(httpx.Response(500), "boom")]
    env = make_env(client, log)
    client.boxes.clear()

    async def missing(*, name: str) -> FakeBox:
        """Report that no box exists.

        Args:
            name: The box name.

        Raises:
            SandboxApiError: Always.
        """
        raise SandboxApiError(httpx.Response(404), "missing")

    client.get_sandbox = missing
    original = client.create_sandbox

    def create_without_box(**kwargs: Any) -> Any:
        """Fail before Vercel makes a box.

        Args:
            kwargs: The SDK create arguments.

        Returns:
            A failing awaitable.
        """
        operation = original(**kwargs)
        operation.close()

        async def fail() -> None:
            """Raise the planned error."""
            raise client.create_errors.pop(0)

        return fail()

    client.create_sandbox = create_without_box
    with pytest.raises(SandboxApiError):
        await env.start()
    assert log.count("create") == 1
    assert env.usage.calls == ["fail"]
    assert client.closed == 1


async def test_order_is_capacity_then_reserve_then_create(client: FakeClient, log: list[str]) -> None:
    """Check the start order and the first funded slice."""
    clock = FakeClock()
    env = make_env(client, log, clock=clock)
    answers = [True, False]

    async def at_capacity() -> bool:
        """Report capacity once, then free capacity.

        Returns:
            The next planned answer.
        """
        log.append("capacity")
        return answers.pop(0)

    env._at_capacity = at_capacity
    await env.start()
    assert log[:5] == ["capacity", "capacity", "reserve", "create", "confirm_created"]
    assert env.usage.request["lifetime_ms"] == 300_000
    assert env.usage.operation_key == "op-1"
    await env.stop()


async def test_exec_transport_retry_does_not_reserve_again(client: FakeClient, log: list[str]) -> None:
    """Check that connect errors resend the exec up to 3 times without a new reservation."""
    env = make_env(client, log)
    await env.start()
    box = client.boxes[env.name]
    box.create_errors = [httpx.ConnectError("down"), httpx.ConnectTimeout("slow")]
    result = await env.exec("true")
    assert result.exit_code == 0
    assert len(box.create_calls) == 3
    assert log.count("reserve") == 1
    box.create_errors = [httpx.ConnectError("down")] * 3
    with pytest.raises(httpx.ConnectError):
        await env.exec("true")
    assert len(box.create_calls) == 6
    await env.stop()


async def test_read_error_on_exec_start_is_not_resent(client: FakeClient, log: list[str]) -> None:
    """Check that an error after the request may have arrived is not resent."""
    env = make_env(client, log)
    await env.start()
    box = client.boxes[env.name]
    box.create_errors = [httpx.ReadError("cut")]
    with pytest.raises(httpx.ReadError):
        await env.exec("true")
    assert len(box.create_calls) == 1
    await env.stop()


async def test_renewer_funds_each_slice_before_extension(client: FakeClient, log: list[str]) -> None:
    """Check that each slice is funded before Vercel extends the session."""
    env = make_env(client, log, lifetime=1500)
    await env.start()
    session = client.boxes[env.name].current_session
    session.log = log
    await asyncio.wait_for(env._renewer, 5)
    steps = [entry for entry in log if entry.startswith(("fund", "vercel-extend", "confirm_extended"))]
    assert steps == [
        "fund:1200000",
        "vercel-extend:600000",
        "confirm_extended",
        "fund:1500000",
        "vercel-extend:300000",
        "confirm_extended",
    ]
    assert client.create_kwargs[0]["execution_time_limit"] == timedelta(minutes=10)
    await env.stop()


async def test_renewer_stops_box_on_unconfirmed_extension(client: FakeClient, log: list[str]) -> None:
    """Check that an extension the reservation cannot confirm stops the box."""
    env = make_env(client, log, lifetime=1500)
    await env.start()
    env.usage.confirm_error = RuntimeError("pending")
    session = client.boxes[env.name].current_session
    await asyncio.wait_for(env._renewer, 5)
    assert session.stops == 1
    assert len(session.extensions) == 1
    await env.stop()


async def test_renewer_does_not_extend_unfunded_slice(client: FakeClient, log: list[str]) -> None:
    """Check that a refused slice never reaches Vercel."""
    env = make_env(client, log, lifetime=1500)
    await env.start()
    env.usage.extend_error = RuntimeError("budget reached")
    session = client.boxes[env.name].current_session
    await asyncio.wait_for(env._renewer, 5)
    assert session.extensions == []
    await env.stop()


async def test_exec_after_unconfirmed_extension_fails_without_resume(log: list[str]) -> None:
    """Check that work after the renewer stops the session fails and never resumes it."""
    client = FakeClient(log)
    env = make_env(client, log, lifetime=1500)
    client.boxes[env.name] = ResumingBox(env.name, FakeSession())
    await env.start()
    env.usage.confirm_error = RuntimeError("pending")
    await asyncio.wait_for(env._renewer, 5)
    box = client.boxes[env.name]
    assert box.current_session.stops == 1
    with pytest.raises(SandboxInfraError):
        await env.exec("echo paid-work")
    with pytest.raises(SandboxInfraError):
        await env.upload_files({"a.txt": "data"})
    assert box.resumes == 0
    assert box.session_ids == []
    await env.stop()


async def test_exec_after_unfunded_slice_fails_at_funded_end(log: list[str]) -> None:
    """Check that work runs until the funded lifetime ends, then fails closed."""
    client = FakeClient(log)
    env = make_env(client, log, lifetime=1500)
    await env.start()
    env.usage.extend_error = BudgetReached()
    await asyncio.wait_for(env._renewer, 5)
    assert (await env.exec("echo still-funded")).exit_code == 0
    env._clock.now = 600
    with pytest.raises(SandboxInfraError):
        await env.exec("echo more")
    await env.stop()


async def test_410_from_stopped_session_fails_without_resume(log: list[str]) -> None:
    """Check that a 410 from a session stopped elsewhere fails and halts the box."""
    client = FakeClient(log)
    env = make_env(client, log)
    client.boxes[env.name] = ResumingBox(env.name, FakeSession())
    await env.start()
    box = client.boxes[env.name]
    await box.current_session.stop()
    with pytest.raises(SandboxInfraError):
        await env.download_file("a.txt")
    assert env._halted is True
    with pytest.raises(SandboxInfraError):
        await env.exec("echo paid-work")
    assert box.resumes == 0
    assert box.session_ids == []
    await env.stop()


async def test_upload_after_410_halts_the_box(log: list[str]) -> None:
    """Check that an upload as the first call after a 410 raises an infra error and halts the box."""
    client = FakeClient(log)
    env = make_env(client, log)
    await env.start()
    await client.boxes[env.name].current_session.stop()
    with pytest.raises(SandboxInfraError):
        await env.upload_files({"dir/a.txt": "data"})
    assert env._halted is True
    await env.stop()


async def test_download_after_410_halts_the_box(log: list[str]) -> None:
    """Check that a download as the first call after a 410 raises an infra error and halts the box."""
    client = FakeClient(log)
    env = make_env(client, log)
    await env.start()
    await client.boxes[env.name].current_session.stop()
    with pytest.raises(SandboxInfraError):
        await env.download_file("a.txt")
    assert env._halted is True
    await env.stop()


async def test_network_policy_after_410_halts_the_box(log: list[str]) -> None:
    """Check that a policy change as the first call after a 410 raises an infra error and halts the box."""
    client = FakeClient(log)
    env = make_env(client, log)
    await env.start()
    await client.boxes[env.name].current_session.stop()
    with pytest.raises(SandboxInfraError):
        await env.set_network_policy(NetworkPolicy(allowed_hosts=("a.com",)))
    assert env._halted is True
    assert env.network_policy is None
    await env.stop()


async def test_cancel_during_reserve_fails_the_reservation(client: FakeClient, log: list[str]) -> None:
    """Check that a cancel while the reservation thread runs still releases the reservation."""
    env = make_env(client, log)
    gate = threading.Event()
    made: list[Any] = []
    original = env._usage_factory

    def slow_factory(budget: Any, request: Any, *, operation_key: str) -> Any:
        """Reserve after the test cancels the start.

        Args:
            budget: The budget runtime.
            request: The usage request.
            operation_key: The operation key.

        Returns:
            The fake reservation.
        """
        gate.wait(5)
        usage = original(budget, request, operation_key=operation_key)
        made.append(usage)
        return usage

    env._usage_factory = slow_factory
    task = asyncio.create_task(env.start())
    await asyncio.sleep(0.05)
    task.cancel()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await env.stop()
    assert len(made) == 1
    assert env.usage is made[0]
    assert made[0].calls == ["fail"]
    assert "create" not in log


async def test_stop_during_create_stops_the_new_box(client: FakeClient, log: list[str]) -> None:
    """Check that a stop while the create runs ends the start, stops the box once and settles."""
    client.hang = True
    env = make_env(client, log)
    task = asyncio.create_task(env.start())
    await asyncio.wait_for(client.created.wait(), 5)
    await asyncio.wait_for(env.stop(), 5)
    with pytest.raises(SandboxInfraError, match="stopped during start"):
        await task
    box = client.boxes[env.name]
    assert box.stops == 1
    assert box.destroys == 1
    assert env.usage.calls.count("settle") == 1
    assert "fail" not in env.usage.calls
    assert client.closed == 1


async def test_stop_during_reserve_creates_no_box(client: FakeClient, log: list[str]) -> None:
    """Check that a stop while the reservation thread runs makes no box and releases the hold."""
    env = make_env(client, log)
    gate = threading.Event()
    made: list[Any] = []
    original = env._usage_factory

    def slow_factory(budget: Any, request: Any, *, operation_key: str) -> Any:
        """Reserve after the test calls stop.

        Args:
            budget: The budget runtime.
            request: The usage request.
            operation_key: The operation key.

        Returns:
            The fake reservation.
        """
        gate.wait(5)
        usage = original(budget, request, operation_key=operation_key)
        made.append(usage)
        return usage

    env._usage_factory = slow_factory
    task = asyncio.create_task(env.start())
    await asyncio.sleep(0.05)
    stopping = asyncio.create_task(env.stop())
    await asyncio.sleep(0.05)
    gate.set()
    await asyncio.wait_for(stopping, 5)
    with pytest.raises(SandboxInfraError, match="stopped during start"):
        await task
    assert "create" not in log
    assert client.boxes == {}
    assert made[0].calls == ["fail"]


async def test_cancelled_caller_still_sees_cancel_and_box_stops(client: FakeClient, log: list[str]) -> None:
    """Check that a caller cancel during start stays a CancelledError."""
    client.hang = True
    env = make_env(client, log)
    task = asyncio.create_task(env.start())
    await asyncio.wait_for(client.created.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.boxes[env.name].stops == 1


async def test_teardown_settles_after_running_extend(client: FakeClient, log: list[str]) -> None:
    """Check that the teardown waits for a running extend before it settles."""
    env = make_env(client, log, lifetime=1500)
    await env.start()
    started = threading.Event()
    gate = threading.Event()
    usage = env.usage
    base_extend = usage.extend

    def slow_extend(lifetime_ms: int, *, deadline: float) -> None:
        """Hold the extend until the test releases it.

        Args:
            lifetime_ms: The new funded lifetime.
            deadline: The funding deadline.
        """
        started.set()
        gate.wait(5)
        base_extend(lifetime_ms, deadline=deadline)
        log.append("extend-done")

    usage.extend = slow_extend
    assert await asyncio.to_thread(started.wait, 5)
    stopping = asyncio.create_task(env.stop())
    await asyncio.sleep(0.05)
    assert "settle" not in log
    gate.set()
    await asyncio.wait_for(stopping, 5)
    assert log.index("extend-done") < log.index("settle")


async def test_wait_without_sentinel_kills_process(client: FakeClient, log: list[str]) -> None:
    """Check that the wait kills the command when it gives up without an exit code."""
    env = make_env(client, log)
    await env.start()
    box = client.boxes[env.name]
    box.outcomes = [httpx.ReadError("cut")] * 20
    with pytest.raises(SandboxInfraError):
        await env.exec("sleep 1000")
    assert box.processes[0].killed == 1
    assert env._clock.sleeps
    await env.stop()


def test_box_name_is_stable_for_operation_key() -> None:
    """Check that the same key and budget give the same name."""
    budget = SimpleNamespace(budget_id="b", generation=1)
    assert box_name("op-1", budget) == box_name("op-1", budget)
    assert box_name("op-1", budget) != box_name("op-2", budget)
    assert box_name("op-1", budget) != box_name("op-1", SimpleNamespace(budget_id="b", generation=2))
    assert box_name("op-1").startswith("skynet-")
    assert box_name(None) != box_name(None)


async def test_same_operation_key_creates_same_name(client: FakeClient, log: list[str]) -> None:
    """Check that two providers for one operation send one box name."""
    first = make_env(client, log)
    second = make_env(FakeClient([]), [])
    assert first.name == second.name
    await first.start()
    assert client.create_kwargs[0]["name"] == first.name
    await first.stop()


def test_protected_box_needs_operation_key(client: FakeClient, log: list[str]) -> None:
    """Check that a protected box without an operation key is rejected."""
    with pytest.raises(SandboxSpecError):
        make_env(client, log, operation_key=None)


async def test_create_policy_fails_closed(client: FakeClient, log: list[str]) -> None:
    """Check that an allowlist the readback does not prove stops the box."""
    client.readback = readback("allow-all")
    env = make_env(client, log, allowed_hosts=("api.example.com",))
    with pytest.raises(SandboxInfraError):
        await env.start()
    assert client.boxes[env.name].stops == 1
    assert env.usage.calls.count("settle") == 1


async def test_policy_update_fails_closed(client: FakeClient, log: list[str]) -> None:
    """Check that a missing readback on a policy change raises and clears the cache."""
    env = make_env(client, log)
    await env.start()
    session = client.boxes[env.name].current_session
    session.next_readback = None
    with pytest.raises(SandboxInfraError):
        await env.disable_network()
    assert env.network_policy is None
    session.next_readback = readback("deny-all")
    await env.disable_network()
    assert env.network_policy == NetworkPolicy(disabled=True)
    await env.stop()


def test_policy_confirmed_rules() -> None:
    """Check the readback rules for each policy mode."""
    disabled = NetworkPolicy(disabled=True)
    allow = NetworkPolicy(allowed_hosts=("a.com",))
    open_policy = NetworkPolicy()
    subnet = SimpleNamespace(allow=("10.0.0.0/8",))
    assert not policy_confirmed(disabled, None)
    assert policy_confirmed(disabled, readback("deny-all"))
    assert policy_confirmed(disabled, readback("custom"))
    assert not policy_confirmed(disabled, readback("custom", subnets=subnet))
    assert not policy_confirmed(disabled, readback("allow-all"))
    assert policy_confirmed(allow, readback("custom", ("a.com",)))
    assert not policy_confirmed(allow, readback("custom", ("a.com", "b.com")))
    assert not policy_confirmed(allow, readback("custom", ("*",)))
    assert not policy_confirmed(allow, readback("allow-all"))
    assert policy_confirmed(open_policy, readback("allow-all"))
    assert policy_confirmed(open_policy, readback("custom", ("*",)))
    assert not policy_confirmed(open_policy, readback("deny-all"))


async def test_phase_timings_recorded_per_box(client: FakeClient, log: list[str]) -> None:
    """Check that create, run and stop record timings under the box name."""
    env = make_env(client, log)
    await env.start()
    await env.exec("true")
    await env.stop()
    records = env.timings.records
    assert [record.phase for record in records] == ["create", "run", "stop"]
    assert {record.box for record in records} == {env.name}
    assert all(record.ok for record in records)


async def test_output_goes_to_context_sink(client: FakeClient, log: list[str]) -> None:
    """Check that exec forwards output to the sink of the current task."""
    env = make_env(client, log)
    await env.start()
    chunks: list[tuple[str, str]] = []
    with use_output_sink(lambda stream, text: chunks.append((stream, text))):
        await env.exec("echo hello")
    assert chunks == [("stdout", "hello\n")]
    await env.stop()


async def test_timeout_codes_mark_timed_out(client: FakeClient, log: list[str]) -> None:
    """Check that the kill code marks a command with a limit as timed out."""
    env = make_env(client, log)
    await env.start()
    client.boxes[env.name].outcomes = [137]
    result = await env.exec("sleep 100", timeout_sec=1)
    assert result.timed_out
    await env.stop()


async def test_unprotected_box_gets_full_lifetime(client: FakeClient, log: list[str]) -> None:
    """Check that a box without a budget has no reservation and no renewer."""
    env = make_env(client, log, lifetime=1500, protected=False)
    await env.start()
    assert env.usage is None
    assert env._renewer is None
    assert client.create_kwargs[0]["execution_time_limit"] == timedelta(seconds=1500)
    await env.stop()
    assert client.boxes[env.name].stops == 1


async def test_upload_and_download(client: FakeClient, log: list[str]) -> None:
    """Check text file transfer and the missing-file answer."""
    env = make_env(client, log)
    await env.start()
    await env.upload_files({"/work/a.txt": "data"})
    assert await env.download_file("/work/a.txt") == "data"
    assert await env.download_file("/work/none.txt") is None
    assert client.boxes[env.name].fs.dirs == ["/work"]
    await env.stop()


async def test_stop_job_sandboxes_counts_live_boxes(monkeypatch: pytest.MonkeyPatch, log: list[str]) -> None:
    """Check that the sweep stops live boxes and continues after one failure."""
    client = FakeClient(log)
    live = FakeBox("a", FakeSession())
    done = FakeBox("b", FakeSession())
    done.status = "stopped"
    broken = FakeBox("c", FakeSession())

    async def fail() -> None:
        """Raise a stop error.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("stop failed")

    broken.stop = fail
    client.boxes = {"a": live, "b": done, "c": broken}
    monkeypatch.setattr(VercelEnvironment, "_make_client", staticmethod(lambda capture: client))
    assert await VercelEnvironment.stop_job_sandboxes("job-1") == 1
    assert live.stops == 1
    assert done.stops == 0
    assert client.closed == 1


async def test_stop_job_sandboxes_bounds_a_hung_client_close(monkeypatch: pytest.MonkeyPatch, log: list[str]) -> None:
    """Check that a hung client close ends at the stop timeout and the sweep still returns its count."""
    client = FakeClient(log)
    live = FakeBox("a", FakeSession())
    client.boxes = {"a": live}

    async def hang() -> None:
        """Never finish the close."""
        await asyncio.Event().wait()

    client.aclose = hang
    monkeypatch.setattr(vercel, "STOP_TIMEOUT", 0.05)
    monkeypatch.setattr(VercelEnvironment, "_make_client", staticmethod(lambda capture: client))
    assert await asyncio.wait_for(VercelEnvironment.stop_job_sandboxes("job-1"), 5) == 1
    assert live.stops == 1


def test_sdk_import_is_present() -> None:
    """Check that the optional SDK loaded in the test environment."""
    assert vercel.SandboxClient is not None


DEADLINE = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
SERVICES = (
    ComposeService(name="db", command="postgres", cpus=1.0, memory_mb=1024),
    ComposeService(name="cache", command="redis-server", cpus=0.5, memory_mb=512),
)


def _service_scripts(box: FakeBox, marker: str) -> list[str]:
    """Return the service names in the box scripts that hold a marker, in order.

    Args:
        box: The fake box.
        marker: A text that the scripts must hold.

    Returns:
        The service names.
    """
    found = []
    for call in box.create_calls:
        script = call["args"][-1]
        match = re.search(r"\.svc-([\w-]+)\.pid", script)
        if marker in script and match:
            found.append(match.group(1))
    return found


async def test_services_start_in_order_after_the_create(client: FakeClient, log: list[str]) -> None:
    """Check that each service launches in start order after the create, and the phase is timed."""
    env = make_env(client, log, services=SERVICES, resources=ResourceRequest(cpus=2))
    await env.start()
    box = client.boxes[env.name]
    assert _service_scripts(box, "setsid") == ["db", "cache"]
    # Each launch is one timed run inside the services phase.
    assert [record.phase for record in env.timings.records] == ["create", "run", "run", "services"]
    await env.stop()


async def test_services_grow_the_box_and_the_reservation(client: FakeClient, log: list[str]) -> None:
    """Check that the created box size and the reserved vCPUs include each service."""
    env = make_env(client, log, services=SERVICES, resources=ResourceRequest(cpus=2))
    await env.start()
    # 2 + 1 + 0.5 vCPUs round up to an even 4.
    assert client.create_kwargs[-1]["resources"].vcpus == 4
    assert env.usage.request["vcpus"] == 4
    await env.stop()


async def test_no_services_keep_the_box_size(client: FakeClient, log: list[str]) -> None:
    """Check that a box without services keeps its size and runs no service script."""
    env = make_env(client, log, resources=ResourceRequest(cpus=2))
    await env.start()
    assert client.create_kwargs[-1]["resources"].vcpus == 2
    assert env.usage.request["vcpus"] == 2
    assert client.boxes[env.name].create_calls == []
    await env.stop()


def test_services_too_large_fail_before_any_create(client: FakeClient, log: list[str]) -> None:
    """Check that services past the largest box fail at setup with the fix, before a reserve or create."""
    big = (ComposeService(name="big", command="sleep 1", cpus=31.0),)
    with pytest.raises(ServicesTooLargeError, match="lower its 'cpus' or 'mem_limit'"):
        make_env(client, log, services=big, resources=ResourceRequest(cpus=2))
    assert log == []


async def test_services_start_again_on_a_box_from_a_snapshot(client: FakeClient, log: list[str]) -> None:
    """Check that a trial box from a snapshot starts its own services."""
    env = make_env(client, log, services=SERVICES)
    env.snapshot_id = "snap-1"
    await env.start()
    assert _service_scripts(client.boxes[env.name], "setsid") == ["db", "cache"]
    await env.stop()


async def test_snapshot_stops_the_services_first(client: FakeClient, log: list[str]) -> None:
    """Check that the services stop in reverse order before the snapshot call."""
    env = make_env(client, log, protected=False, services=SERVICES)
    await env.start()
    box = client.boxes[env.name]
    session = box.current_session
    seen: list[list[str]] = []

    async def snapshot(*, expiration: Any = None) -> Any:
        """Record the stops that ran before the snapshot.

        Args:
            expiration: Unused.

        Returns:
            The snapshot handle.
        """
        seen.append(_service_scripts(box, "kill -TERM"))
        return SimpleNamespace(id="snap-1", status="created", expires_at=1_000)

    session.snapshot = snapshot
    await env.snapshot(run_deadline=DEADLINE, now=NOW)
    assert seen == [["cache", "db"]]
    await env.stop()


async def test_cancel_in_the_service_stop_reserves_no_snapshot_hold(client: FakeClient, log: list[str]) -> None:
    """Check that the service stop runs before the hold, so a cancel there leaves no hold without pending."""
    env = make_env(client, log, services=SERVICES)
    await env.start()
    reserved: list[str] = []

    def factory(budget: Any, request: dict[str, Any], *, operation_key: str) -> FakeUsage:
        """Record the hold and make a fake one.

        Args:
            budget: The budget runtime.
            request: The usage request.
            operation_key: The operation key.

        Returns:
            The fake reservation.
        """
        reserved.append("reserved")
        return FakeUsage(log, budget, request, operation_key=operation_key)

    env._snapshot_usage_factory = factory
    real_exec = env.exec
    stopping = asyncio.Event()

    async def exec_(command: str, **kwargs: Any) -> Any:
        """Hang in the service stop.

        Args:
            command: The shell command.
            kwargs: The exec keyword arguments.

        Returns:
            The result of the real exec.
        """
        if "kill -TERM" in command:
            stopping.set()
            await asyncio.sleep(3600)
        return await real_exec(command, **kwargs)

    env.exec = exec_
    task = asyncio.create_task(env.snapshot(run_deadline=DEADLINE, now=NOW))
    waiter = asyncio.ensure_future(stopping.wait())
    await asyncio.wait({task, waiter}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
    waiter.cancel()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert stopping.is_set()
    assert reserved == []
    assert not env._halted
    env.exec = real_exec
    await asyncio.wait_for(env.stop(), 5)


async def test_start_after_stop_is_refused(client: FakeClient, log: list[str]) -> None:
    """Check that a stopped box never starts again or sends a second create."""
    env = make_env(client, log)
    await env.start()
    await env.stop()
    with pytest.raises(SandboxInfraError, match="cannot start again"):
        await env.start()
    assert log.count("create") == 1


class SnapshotQueryClient(FakeClient):
    """Add a snapshot query and a hung close to the fake SDK client."""

    def __init__(self, log: list[str]) -> None:
        """Hold one live snapshot.

        Args:
            log: The shared call log.
        """
        super().__init__(log)
        self.snapshot = SimpleNamespace(id="snap-1", status="deleted", expires_at=None)

    async def query_snapshots(self, **kwargs: Any) -> Any:
        """Yield a live snapshot.

        Args:
            kwargs: Unused.

        Yields:
            The snapshot.
        """
        yield SimpleNamespace(id="snap-1", status="created", expires_at=None)

    async def get_snapshot(self, *, snapshot_id: str) -> Any:
        """Return a snapshot that is already deleted.

        Args:
            snapshot_id: Unused.

        Returns:
            The snapshot.
        """
        return self.snapshot

    async def aclose(self) -> None:
        """Never finish the close."""
        await asyncio.Event().wait()


async def test_find_snapshot_bounds_a_hung_client_close(monkeypatch: pytest.MonkeyPatch, log: list[str]) -> None:
    """Check that a hung close ends at the stop timeout and the found snapshot still returns."""
    client = SnapshotQueryClient(log)
    monkeypatch.setattr(vercel, "STOP_TIMEOUT", 0.05)
    monkeypatch.setattr(VercelEnvironment, "_make_client", staticmethod(lambda capture: client))
    found = await asyncio.wait_for(VercelEnvironment.find_snapshot("builder"), 5)
    assert found is not None
    assert found.snapshot_id == "snap-1"


async def test_delete_snapshot_bounds_a_hung_client_close(monkeypatch: pytest.MonkeyPatch, log: list[str]) -> None:
    """Check that a hung close of an owned client ends at the stop timeout after the delete."""
    client = SnapshotQueryClient(log)
    builder = client.boxes["builder"] = FakeBox("builder", FakeSession())
    monkeypatch.setattr(vercel, "STOP_TIMEOUT", 0.05)
    monkeypatch.setattr(VercelEnvironment, "_make_client", staticmethod(lambda capture: client))
    built = vercel.BuiltSnapshot(snapshot_id="snap-1", builder_name="builder")
    await asyncio.wait_for(VercelEnvironment.delete_snapshot(built), 5)
    assert builder.destroys == 1


class GrowingProcess:
    """Write output while it runs, like a guest that waits for the parent."""

    def __init__(self, fs: FakeFs, script: str, run: Any) -> None:
        """Keep the wrapper paths and the guest body.

        Args:
            fs: The box file system.
            script: The wrapper script.
            run: An async callable ``run(fs, out_path)`` that returns the exit code.
        """
        self.fs = fs
        self.out, self.err, self.sentinel = _PATHS.search(script).groups()
        self.run = run

    async def wait(self) -> int:
        """Run the guest body and record its exit code.

        Returns:
            The exit code.
        """
        code = await self.run(self.fs, self.out)
        self.fs.files[self.sentinel] = f"{code}\n"
        return code

    async def terminate(self) -> None:
        """Accept a terminate call."""

    async def kill(self) -> None:
        """Accept a kill call."""


def _guest(box: FakeBox, run: Any) -> None:
    """Make every new process on the box run ``run``.

    Args:
        box: The fake box.
        run: The guest body, see :class:`GrowingProcess`.
    """

    async def create_process(command: str, args: list[str], **kwargs: Any) -> GrowingProcess:
        """Start the growing process.

        Args:
            command: The program.
            args: The program arguments.
            kwargs: The other SDK arguments.

        Returns:
            The fake process.
        """
        return GrowingProcess(box.fs, args[-1], run)

    box.create_process = create_process


async def test_sink_sees_output_while_the_command_runs(client: FakeClient, log: list[str]) -> None:
    """Check that output reaches the sink before exit, as the model mailbox needs (SKY-529)."""
    env = make_env(client, log)
    await env.start()
    seen = asyncio.Event()
    chunks: list[tuple[str, str]] = []

    def sink(stream: str, text: str) -> None:
        """Record a chunk and mark the early frame.

        Args:
            stream: The stream name.
            text: The new text.
        """
        chunks.append((stream, text))
        if "early" in "".join(chunk for name, chunk in chunks if name == "stdout"):
            seen.set()

    async def run(fs: FakeFs, out: str) -> int:
        """Write a frame, wait until the parent saw it, then finish.

        Args:
            fs: The box file system.
            out: The stdout path.

        Returns:
            The exit code.
        """
        fs.files[out] = "early\n"
        await seen.wait()
        fs.files[out] += "late\n"
        return 0

    _guest(client.boxes[env.name], run)
    result = await asyncio.wait_for(env.exec("guest", on_output=sink), 5)
    assert result.exit_code == 0
    assert result.stdout == "early\nlate\n"
    assert "".join(text for stream, text in chunks if stream == "stdout") == "early\nlate\n"
    assert chunks[0] == ("stdout", "early\n")
    await env.stop()


async def test_streamed_output_is_never_repeated(client: FakeClient, log: list[str]) -> None:
    """Check that a stale shorter read emits nothing and the sink total equals the file."""
    env = make_env(client, log)
    await env.start()
    session = client.boxes[env.name].current_session
    chunks: list[tuple[str, str]] = []

    async def until_read(count: int) -> None:
        """Wait until the provider made ``count`` tail reads.

        Args:
            count: The read count.
        """
        for _ in range(100_000):
            if session.tail_reads >= count:
                return
            await asyncio.sleep(0)
        raise AssertionError("The provider stopped polling the output files.")

    async def run(fs: FakeFs, out: str) -> int:
        """Grow the file, show a stale shorter copy, then grow it again.

        Args:
            fs: The box file system.
            out: The stdout path.

        Returns:
            The exit code.
        """
        err = out.removesuffix(".out") + ".err"
        fs.files[out] = "abc"
        fs.files[err] = "warn\n"
        await until_read(session.tail_reads + 2)
        fs.files[out] = "a"
        await until_read(session.tail_reads + 2)
        fs.files[out] = "abcdef"
        await until_read(session.tail_reads + 2)
        fs.files[out] = "abcdefgh"
        return 0

    _guest(client.boxes[env.name], run)
    result = await asyncio.wait_for(env.exec("guest", on_output=lambda *chunk: chunks.append(chunk)), 5)
    stdout = [text for stream, text in chunks if stream == "stdout"]
    assert "".join(stdout) == result.stdout == "abcdefgh"
    assert stdout[:2] == ["abc", "def"]
    assert "".join(text for stream, text in chunks if stream == "stderr") == result.stderr == "warn\n"
    await env.stop()


async def test_no_tail_reads_without_a_sink(client: FakeClient, log: list[str]) -> None:
    """Check that a command with no sink makes no output polls."""
    env = make_env(client, log)
    await env.start()

    async def run(fs: FakeFs, out: str) -> int:
        """Run long enough for several polls.

        Args:
            fs: The box file system.
            out: The stdout path.

        Returns:
            The exit code.
        """
        fs.files[out] = "quiet\n"
        for _ in range(50):
            await asyncio.sleep(0)
        return 0

    _guest(client.boxes[env.name], run)
    result = await env.exec("guest")
    assert result.stdout == "quiet\n"
    assert client.boxes[env.name].current_session.tail_reads == 0
    await env.stop()


async def _until_read(session: FakeSession, count: int) -> None:
    """Wait until the provider made ``count`` tail reads.

    Args:
        session: The fake session.
        count: The read count.

    Raises:
        AssertionError: The provider stopped polling.
    """
    for _ in range(100_000):
        if session.tail_reads >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError("The provider stopped polling the output files.")


async def test_a_multibyte_char_split_across_polls_reaches_the_sink_once(client: FakeClient, log: list[str]) -> None:
    """Check that a char split between polls arrives once, and a bad byte decodes the same in both reads."""
    env = make_env(client, log)
    await env.start()
    session = client.boxes[env.name].current_session
    chunks: list[tuple[str, str]] = []

    async def run(fs: FakeFs, out: str) -> int:
        """Write the first byte of ``é``, then its second byte and an invalid byte after a poll.

        Args:
            fs: The box file system.
            out: The stdout path.

        Returns:
            The exit code.
        """
        fs.files[out] = b"\xc3"
        await _until_read(session, session.tail_reads + 2)
        fs.files[out] = b"\xc3\xa9\xff"
        await _until_read(session, session.tail_reads + 2)
        return 0

    _guest(client.boxes[env.name], run)
    result = await asyncio.wait_for(env.exec("guest", on_output=lambda *chunk: chunks.append(chunk)), 5)
    assert "".join(text for stream, text in chunks if stream == "stdout") == result.stdout == "\u00e9\ufffd"
    await env.stop()


async def test_a_failing_sink_aborts_the_command(client: FakeClient, log: list[str]) -> None:
    """Check that a sink error stops the running command at once, not at its exit (SKY-529)."""
    env = make_env(client, log)
    await env.start()
    box = client.boxes[env.name]

    async def create_process(command: str, args: list[str], **kwargs: Any) -> FakeProcess:
        """Start a process that writes one line and then runs until it is stopped.

        Args:
            command: The program.
            args: The program arguments.
            kwargs: The other SDK arguments.

        Returns:
            The fake process.
        """
        process = FakeProcess(box.fs, args[-1], [], block=True)
        box.fs.files[process.out] = "bad frame\n"
        box.processes.append(process)
        return process

    def sink(stream: str, text: str) -> None:
        """Reject every chunk, like a model mailbox that reads a bad frame.

        Args:
            stream: The stream name.
            text: The new text.

        Raises:
            ValueError: Always.
        """
        raise ValueError("bad frame")

    box.create_process = create_process
    with pytest.raises(ValueError, match="bad frame"):
        await asyncio.wait_for(env.exec("guest", on_output=sink), 5)
    assert box.processes[0].terminated == 1
    await env.stop()


async def test_a_poll_that_keeps_failing_logs_one_warning(
    client: FakeClient, log: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    """Check that a tail script with a nonzero exit is a failure, and repeated failures warn once."""
    env = make_env(client, log)
    await env.start()
    session = client.boxes[env.name].current_session
    chunks: list[tuple[str, str]] = []

    async def run_process(command: str, args: list[str], **kwargs: Any) -> SimpleNamespace:
        """Answer every tail read like a box without ``base64``.

        Args:
            command: The program.
            args: The program arguments.
            kwargs: The other SDK arguments.

        Returns:
            A failed process with empty output.
        """
        session.tail_reads += 1
        return SimpleNamespace(returncode=127, stdout="\n\n", stderr="base64: command not found")

    async def run(fs: FakeFs, out: str) -> int:
        """Write output and run for several polls.

        Args:
            fs: The box file system.
            out: The stdout path.

        Returns:
            The exit code.
        """
        fs.files[out] = "done\n"
        await _until_read(session, 6)
        return 0

    session.run_process = run_process
    _guest(client.boxes[env.name], run)
    with caplog.at_level("DEBUG", logger=vercel.__name__):
        result = await asyncio.wait_for(env.exec("guest", on_output=lambda *chunk: chunks.append(chunk)), 5)
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert [record.getMessage() for record in warnings] == ["A sandbox output poll failed (3 in a row)."]
    assert chunks == [("stdout", "done\n")]
    assert result.stdout == "done\n"
    await env.stop()


async def test_a_hung_poll_times_out_and_later_output_still_streams(
    client: FakeClient, log: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check that a tail read that never returns counts as one failed poll, not a stuck poller (SKY-529)."""
    monkeypatch.setattr(vercel, "OUTPUT_POLL_TIMEOUT", 0.05)
    env = make_env(client, log)
    await env.start()
    session = client.boxes[env.name].current_session
    answer = session.run_process
    hang = asyncio.Event()
    seen = asyncio.Event()
    calls: list[dict[str, Any]] = []
    chunks: list[tuple[str, str]] = []

    async def run_process(command: str, args: list[str], **kwargs: Any) -> SimpleNamespace:
        """Stall the first tail read like a stream with no timeout; answer the rest.

        Args:
            command: The program.
            args: The program arguments.
            kwargs: The other SDK arguments.

        Returns:
            The completed process.
        """
        calls.append(kwargs)
        if len(calls) == 1:
            await hang.wait()
        return await answer(command, args, **kwargs)

    def sink(stream: str, text: str) -> None:
        """Record a chunk and mark the first stdout text.

        Args:
            stream: The stream name.
            text: The new text.
        """
        chunks.append((stream, text))
        if stream == "stdout":
            seen.set()

    async def run(fs: FakeFs, out: str) -> int:
        """Write a frame and finish only after the sink saw it.

        Args:
            fs: The box file system.
            out: The stdout path.

        Returns:
            The exit code.
        """
        fs.files[out] = "frame\n"
        await seen.wait()
        return 0

    session.run_process = run_process
    _guest(client.boxes[env.name], run)
    try:
        result = await asyncio.wait_for(env.exec("guest", on_output=sink), 5)
    finally:
        hang.set()
    assert result.stdout == "frame\n"
    assert chunks == [("stdout", "frame\n")]
    assert calls[0]["kill_after"] == vercel.OUTPUT_POLL_TIMEOUT
    await env.stop()


async def test_cancel_while_the_poller_runs_aborts_once_and_leaves_no_task(client: FakeClient, log: list[str]) -> None:
    """Check that a cancel during a streamed exec terminates the command once and ends every helper task."""
    env = make_env(client, log)
    await env.start()
    box = client.boxes[env.name]
    box.block = True
    session = box.current_session
    before = asyncio.all_tasks()
    task = asyncio.create_task(env.exec("sleep 100", on_output=lambda *chunk: None))
    await _until_read(session, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    leftover = {other for other in asyncio.all_tasks() - before if not other.done()}
    assert leftover == set()
    assert box.processes[0].terminated == 1
    assert box.processes[0].killed == 0
    await env.stop()


async def test_a_sink_error_wins_over_the_kill_deadline(
    client: FakeClient, log: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check that the caller gets the sink error when the abort it started outlives the deadline."""
    monkeypatch.setattr(vercel, "KILL_GRACE", 0.0)
    monkeypatch.setattr(vercel, "POLL_SLACK", 0.0)
    env = make_env(client, log)
    await env.start()
    box = client.boxes[env.name]

    async def create_process(command: str, args: list[str], **kwargs: Any) -> FakeProcess:
        """Start a process that writes a bad frame and ignores terminate until a kill.

        Args:
            command: The program.
            args: The program arguments.
            kwargs: The other SDK arguments.

        Returns:
            The fake process.
        """
        process = FakeProcess(box.fs, args[-1], [], block=True)
        box.fs.files[process.out] = "bad frame\n"

        async def slow_terminate() -> None:
            """Count the call and return only after the kill, like a stalled HTTP call."""
            process.terminated += 1
            await process.released.wait()

        process.terminate = slow_terminate
        box.processes.append(process)
        return process

    def sink(stream: str, text: str) -> None:
        """Reject every chunk, like a model mailbox that reads a bad frame.

        Args:
            stream: The stream name.
            text: The new text.

        Raises:
            ValueError: Always.
        """
        raise ValueError("bad frame")

    box.create_process = create_process
    with pytest.raises(ValueError, match="bad frame"):
        await asyncio.wait_for(env.exec("guest", timeout_sec=0.05, on_output=sink), 5)
    assert box.processes[0].terminated == 1
    assert box.processes[0].killed >= 1
    for _ in range(10):
        await asyncio.sleep(0)
    await env.stop()


def test_tail_script_reads_bytes_after_each_offset(tmp_path: Path) -> None:
    """Check the real tail script in a local shell: missing files and offsets past the end are empty."""
    out = tmp_path / "out"
    out.write_bytes("ab\u00e9cd".encode())
    args = [
        *vercel.SHELL_FLAGS,
        vercel.TAIL_SCRIPT,
        "tail",
        "3",
        str(out),
        "1",
        str(tmp_path / "missing"),
        "99",
        str(out),
    ]
    completed = subprocess.run(["bash", *args], capture_output=True, text=True, check=True)
    lines = completed.stdout.split("\n")
    assert [base64.b64decode(line) for line in lines[:3]] == ["\u00e9cd".encode(), b"", b""]


def test_model_mailbox_round_trip_through_the_vercel_provider(client: FakeClient, log: list[str]) -> None:
    """Check that a guest model frame gets its response while the guest still runs (SKY-529)."""
    env = make_env(client, log, protected=False, clock=SYSTEM_CLOCK)
    session = sync_bridge.open_session(env)
    calls: list[tuple[str, str, Any]] = []

    def dispatch(token: str, path: str, body: Any, headers: dict[str, str]) -> ModelHTTPResult:
        """Answer one model request.

        Args:
            token: The scoped token.
            path: The model path.
            body: The request body.
            headers: The forwarded headers.

        Returns:
            The model response.
        """
        calls.append((token, path, body))
        return ModelHTTPResult(200, "application/json", b'{"answer": 42}')

    async def run(fs: FakeFs, out: str) -> int:
        """Send one frame like the guest proxy and exit 0 only after its response arrives.

        Args:
            fs: The box file system.
            out: The stdout path.

        Returns:
            The exit code.
        """
        config_path = next(path for path in fs.files if path.endswith("/config.json"))
        config = json.loads(fs.files[config_path])
        identity = "a" * 32
        frame = {"id": identity, "path": "/v1/chat", "body": base64.b64encode(b'{"q": 1}').decode(), "headers": {}}
        fs.files[out] = "before\n" + config["prefix"] + json.dumps(frame) + "\n"
        response_path = f"{config['response_dir']}/{identity}.json"
        for _ in range(500):
            if response_path in fs.files:
                document = json.loads(fs.files[response_path])
                fs.files[out] += base64.b64decode(document["body"]).decode() + "\n"
                return 0
            await asyncio.sleep(0.01)
        return 1

    _guest(client.boxes[env.name], run)
    try:
        result = ModelMailbox(dispatch).run(session, "agent", timeout_seconds=30)
    finally:
        session.close()
    assert result.exit_code == 0
    assert result.stdout == 'before\n{"answer": 42}\n'
    assert calls == [("", "/v1/chat", {"q": 1})]
