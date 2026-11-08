"""The async E2B sandbox provider, for development only.

Adapted from Harbor 0.25.0 (Apache-2.0), harbor/environments/e2b.py: the
create, background exec, file and network-update flow comes from there.
Changes:

- The provider implements the async ``SandboxEnvironment`` contract. The
  factory refuses it in production (``production_allowed=False``).
- The create call goes through ``create_with_capacity``. A 429, 502 or 503
  answer is a capacity signal, and the capacity wait has no attempt limit.
  The SDK's own create retries are off (``retries=0``), so the capacity wait
  is the only retry and other create errors are not retried.
- Each box carries a box tag. A stop kills the known box and every other
  box with the same tag, so a create that timed out or that answered 502
  after the box appeared does not leak a box.
- Calls other than the create wait on the read bucket of the provider
  pacing.
- ``exec`` reattaches to the running command by its pid after a cut
  connection, up to ``EXEC_REATTACH_ATTEMPTS`` times. Harbor 0.25.0 does not
  reattach; it lets the error go on.
- A box whose lifetime is longer than one lifetime slice gets a renew loop
  that extends its E2B timeout one slice at a time, up to the spec lifetime.
  Harbor 0.25.0 has no renew loop.
- ``stop_job_sandboxes`` finds the boxes of a job by their metadata.
- Harbor's template builds, directory transfer, user handling and log
  download are left out. The template sets the CPU and memory of the box.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any, ClassVar

from ..capabilities import EnvironmentCapabilities, NetworkMode
from ..environment import JOB_TAG, ExecResult, NetworkPolicy, OutputSink, SandboxEnvironment
from ..errors import SandboxFatalError, SandboxInfraError, StartTimeoutError
from ..pacing import SYSTEM_CLOCK, Clock, ProviderAtCapacityError, ProviderPacing, create_with_capacity
from ..resources import SandboxSpec
from ..telemetry import BoxTimings, current_output_sink

try:
    import e2b as e2b_sdk
    import httpx
    from e2b import (
        ALL_TRAFFIC,
        AsyncSandbox,
        CommandExitException,
        NotFoundException,
        RateLimitException,
        SandboxException,
        SandboxNotRunningException,
        SandboxQuery,
        TimeoutException,
    )
except ImportError:
    e2b_sdk = None
    httpx = None
    ALL_TRAFFIC = None
    AsyncSandbox = None
    CommandExitException = None
    NotFoundException = None
    RateLimitException = None
    SandboxException = None
    SandboxNotRunningException = None
    SandboxQuery = None
    TimeoutException = None

try:
    from connectrpc.errors import ConnectError as RpcConnectError
except ImportError:
    RpcConnectError = None

logger = logging.getLogger(__name__)

# Older SDK releases do not define this class; the provider must still load.
ServiceBusyException = getattr(e2b_sdk, "ServiceBusyException", None)

API_KEY_ENV = "E2B_API_KEY"
BOX_TAG = "skynet_box"
CREATE_TIMEOUT = 120.0
STOP_TIMEOUT = 60.0
DISPATCH_ATTEMPTS = 3
EXEC_REATTACH_ATTEMPTS = 5
REATTACH_DELAY = 1.0
# The E2B Hobby plan allows at most one hour for each timeout value, so a
# longer box extends its timeout one slice at a time.
LIFETIME_SLICE = 3600
RENEW_LEAD = 300.0
RENEW_RETRY = 30.0
TIMEOUT_EXIT_CODE = 124
CAPACITY_STATUSES = frozenset({429, 502, 503})

E2B_CAPABILITIES = EnvironmentCapabilities(
    name="e2b",
    network_modes=frozenset(NetworkMode),
    dynamic_network_policy=True,
)


def _caused_by(error: BaseException, types: tuple[type[BaseException], ...]) -> bool:
    """Tell whether an error or one of its causes has one of the types.

    Args:
        error: The error to check.
        types: The error types to look for.

    Returns:
        True when the error or an error in its ``__cause__`` chain matches.
    """
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        if isinstance(current, types):
            return True
        seen.add(id(current))
        current = current.__cause__
    return False


def _is_request_timeout(error: BaseException) -> bool:
    """Tell whether an SDK call ended on its own request timeout.

    Args:
        error: The error of the SDK call.

    Returns:
        True for an httpx timeout or an E2B ``TimeoutException``.
    """
    if httpx is not None and isinstance(error, httpx.TimeoutException):
        return True
    return TimeoutException is not None and isinstance(error, TimeoutException)


def _is_capacity_error(error: BaseException) -> bool:
    """Tell whether a create error means that E2B has no capacity now.

    Args:
        error: The error of the create call.

    Returns:
        True for a rate limit, a busy service or a 429, 502 or 503 answer.
    """
    if RateLimitException is not None and isinstance(error, RateLimitException):
        return True
    if ServiceBusyException is not None and isinstance(error, ServiceBusyException):
        return True
    status = getattr(error, "status_code", None)
    return SandboxException is not None and isinstance(error, SandboxException) and status in CAPACITY_STATUSES


def _is_dispatch_retryable(error: BaseException) -> bool:
    """Tell whether a failed command dispatch can be sent again.

    Only errors where the request never reached the box are safe to resend:
    a resent run after a read error could start the command twice.

    Args:
        error: The error of the ``commands.run`` call.

    Returns:
        True for a refused connection or a rate limit answer.
    """
    if RateLimitException is not None and isinstance(error, RateLimitException):
        return True
    return _caused_by(error, (ConnectionError,))


def _is_reattachable(error: BaseException) -> bool:
    """Tell whether a failed wait on a running command can reattach.

    Args:
        error: The error of the ``wait()`` call.

    Returns:
        True for a cut or timed-out connection to a box that may still run.
    """
    if SandboxNotRunningException is not None and isinstance(error, SandboxNotRunningException):
        return False
    if TimeoutException is not None and isinstance(error, TimeoutException):
        return True
    if RpcConnectError is not None and isinstance(error, RpcConnectError):
        return True
    return _caused_by(error, (OSError,))


def network_update(policy: NetworkPolicy) -> dict[str, Any]:
    """Build the ``update_network`` body of a policy, in Harbor's shapes.

    Omitted fields are cleared on the server, so an empty body opens egress.

    Args:
        policy: The checked policy.

    Returns:
        The SDK network update.
    """
    if policy.disabled:
        return {"allow_internet_access": False}
    if policy.allowed_hosts:
        return {"allow_out": list(policy.allowed_hosts), "deny_out": [ALL_TRAFFIC]}
    return {}


class E2BEnvironment(SandboxEnvironment):
    """Run commands in one E2B sandbox through the async SDK.

    The provider is for development: the factory refuses it in production,
    and Skynet does not meter or fund its usage.
    """

    capabilities: ClassVar[EnvironmentCapabilities] = E2B_CAPABILITIES
    # The SDK class; tests replace it with a fake.
    sandbox_cls: ClassVar[Any] = AsyncSandbox

    def __init__(
        self,
        spec: SandboxSpec,
        *,
        pacing: ProviderPacing | None = None,
        at_capacity: Callable[[], Awaitable[bool]] | None = None,
        clock: Clock | None = None,
    ) -> None:
        """Check the spec and prepare the box without a network call.

        Args:
            spec: The sandbox to open.
            pacing: The read and create buckets of the provider.
            at_capacity: Returns True while the provider has no capacity.
            clock: The clock for waits, renewals and timings.
        """
        super().__init__(spec)
        self._pacing = pacing
        self._at_capacity = at_capacity
        self._clock = clock or SYSTEM_CLOCK
        self.name = spec.name or f"skynet-{uuid.uuid4().hex[:12]}"
        self.timings = BoxTimings(self.name, clock=self._clock.monotonic)
        self._box: Any | None = None
        self._create_dispatched = False
        self._renewer: asyncio.Task[None] | None = None
        self._starting: asyncio.Future[None] | None = None
        self._stop_task: asyncio.Future[None] | None = None

    @classmethod
    def preflight(cls) -> None:
        """Check that the SDK and the API key exist.

        Raises:
            SandboxFatalError: The SDK or the ``E2B_API_KEY`` variable is missing.
        """
        if cls.sandbox_cls is None:
            raise SandboxFatalError("The e2b package is not installed: install the `e2b` extra.")
        if not os.environ.get(API_KEY_ENV):
            raise SandboxFatalError(f"The E2B sandbox needs the {API_KEY_ENV} variable.")

    async def start(self) -> None:
        """Wait for capacity, then create the box.

        Any failure or cancel stops the box before the error goes on. The work
        runs in its own task, so a ``stop()`` during the start can cancel it
        and then stop the box.

        Raises:
            SandboxFatalError: The SDK is not installed.
            StartTimeoutError: The create did not finish in time.
            SandboxInfraError: A ``stop()`` ended the start.
        """
        if self.sandbox_cls is None:
            raise SandboxFatalError("The e2b package is not installed: install the `e2b` extra.")
        self._starting = asyncio.ensure_future(self._start())
        try:
            await self._starting
        except BaseException as error:
            if self._stop_task is None:
                self._stop_task = asyncio.ensure_future(self._teardown())
            await asyncio.shield(self._stop_task)
            current = asyncio.current_task()
            if isinstance(error, asyncio.CancelledError) and current is not None and not current.cancelling():
                # Only stop() cancelled the start task; the caller was not
                # cancelled, so it must not see a CancelledError.
                raise SandboxInfraError("The E2B sandbox stopped during start.") from None
            raise

    async def _start(self) -> None:
        """Wait for capacity and create the box, then start the renew loop."""
        with self.timings.phase("create"):
            await create_with_capacity(
                self._create,
                _no_reservation,
                at_capacity=self._at_capacity,
                bucket=self._pacing.creates if self._pacing else None,
                clock=self._clock,
            )
        first_slice = self._first_slice()
        if first_slice < self.spec.lifetime_seconds:
            deadline = self._clock.monotonic() + self.spec.lifetime_seconds
            self._renewer = asyncio.create_task(self._renew_loop(self._clock.monotonic() + first_slice, deadline))

    def _first_slice(self) -> int:
        """Return the first E2B timeout of the box, in whole seconds.

        Returns:
            The spec lifetime, cut to one lifetime slice.
        """
        return max(1, min(LIFETIME_SLICE, math.ceil(self.spec.lifetime_seconds)))

    def _create_options(self) -> dict[str, Any]:
        """Build the keyword arguments of the create call.

        Returns:
            The template, timeouts, metadata, env and network options.
        """
        spec = self.spec
        options: dict[str, Any] = {
            "timeout": self._first_slice(),
            "metadata": {**spec.tags, BOX_TAG: self.name},
            "envs": dict(spec.env),
            "allow_internet_access": not spec.network_disabled,
            # The SDK default of 60 s would end a slow create before the
            # CREATE_TIMEOUT deadline.
            "request_timeout": CREATE_TIMEOUT,
            # The capacity wait owns the retries; an SDK retry would resend a
            # create that may have reached E2B.
            "retries": 0,
        }
        if spec.image:
            options["template"] = spec.image
        if not spec.network_disabled and spec.allowed_hosts:
            options["network"] = {"allow_out": list(spec.allowed_hosts), "deny_out": [ALL_TRAFFIC]}
        return options

    async def _create(self, _reservation: None) -> None:
        """Send the create call and keep the box.

        Args:
            _reservation: Unused; the provider holds no budget reservation.

        Raises:
            ProviderAtCapacityError: E2B answered 429, 502 or 503.
            StartTimeoutError: The create did not finish in time.
        """
        self._create_dispatched = True
        try:
            async with asyncio.timeout(CREATE_TIMEOUT) as deadline:
                self._box = await self.sandbox_cls.create(**self._create_options())
        except TimeoutError:
            if not deadline.expired():
                raise
            raise StartTimeoutError(CREATE_TIMEOUT) from None
        except Exception as error:
            if _is_request_timeout(error):
                raise StartTimeoutError(CREATE_TIMEOUT) from error
            if _is_capacity_error(error):
                raise ProviderAtCapacityError(f"E2B has no capacity: {type(error).__name__}") from error
            raise

    async def _renew_loop(self, expires_at: float, deadline: float) -> None:
        """Extend the E2B timeout one slice at a time until the spec lifetime ends.

        A failed extension is tried again until the current slice is nearly
        over. The box then ends at its last timeout, which is safe.

        Args:
            expires_at: When the current E2B timeout ends, on the clock.
            deadline: When the spec lifetime ends, on the clock.
        """
        while expires_at < deadline:
            await self._clock.sleep(max(0.0, expires_at - RENEW_LEAD - self._clock.monotonic()))
            box = self._box
            if box is None:
                return
            seconds = max(1, min(LIFETIME_SLICE, math.ceil(deadline - self._clock.monotonic())))
            await self._pace_read()
            try:
                await box.set_timeout(seconds)
            except Exception as error:
                logger.warning("E2B sandbox %s did not extend: %s", self.name, type(error).__name__)
                if self._clock.monotonic() + RENEW_RETRY >= expires_at:
                    return
                await self._clock.sleep(RENEW_RETRY)
                continue
            expires_at = self._clock.monotonic() + seconds

    async def stop(self) -> None:
        """Stop the box once. Safe to call again."""
        if self._stop_task is None:
            self._stop_task = asyncio.ensure_future(self._timed_teardown())
        await asyncio.shield(self._stop_task)

    async def _timed_teardown(self) -> None:
        """Run the teardown inside the stop phase."""
        with self.timings.phase("stop"):
            await self._teardown()

    async def _teardown(self) -> None:
        """End the start and the renew loop, then kill every box of this environment."""
        if self._starting is not None and not self._starting.done():
            # A stop during the start: end the start first, so no box appears
            # after this teardown has looked for it.
            self._starting.cancel()
            with contextlib.suppress(BaseException):
                await self._starting
        if self._renewer is not None:
            self._renewer.cancel()
            with contextlib.suppress(BaseException):
                await self._renewer
        box, self._box = self._box, None
        known = None
        if box is not None:
            known = getattr(box, "sandbox_id", None)
            try:
                await asyncio.wait_for(box.kill(), STOP_TIMEOUT)
            except Exception as error:
                logger.warning("E2B sandbox %s did not stop: %s", self.name, type(error).__name__)
        if self._create_dispatched:
            # A create that timed out, or that answered 502 after the box
            # appeared, can leave a box that this environment never saw.
            try:
                await asyncio.wait_for(self._kill_tagged({BOX_TAG: self.name}, skip=known), STOP_TIMEOUT)
            except Exception as error:
                logger.warning("E2B sandbox %s sweep failed: %s", self.name, type(error).__name__)

    @classmethod
    async def _kill_tagged(cls, metadata: Mapping[str, str], *, skip: str | None = None) -> int:
        """Kill every listed box whose metadata matches.

        A failure on one box does not stop the sweep.

        Args:
            metadata: The metadata that a box must carry.
            skip: A box id that is already stopped.

        Returns:
            The number of boxes that E2B confirmed as killed.
        """
        paginator = cls.sandbox_cls.list(query=SandboxQuery(metadata=dict(metadata)))
        killed = 0
        while paginator.has_next:
            for item in await paginator.next_items():
                if item.sandbox_id == skip:
                    continue
                try:
                    if await cls.sandbox_cls.kill(item.sandbox_id):
                        killed += 1
                except Exception as error:
                    logger.warning("E2B sandbox %s did not stop: %s", item.sandbox_id, type(error).__name__)
        return killed

    @classmethod
    async def stop_job_sandboxes(cls, job_id: str) -> int:
        """Kill every E2B box that carries the job tag.

        Args:
            job_id: The value of the ``JOB_TAG`` tag.

        Returns:
            The number of boxes that the sweep stopped.
        """
        if cls.sandbox_cls is None:
            return 0
        return await cls._kill_tagged({JOB_TAG: job_id})

    def _require_box(self) -> Any:
        """Return the running box.

        Returns:
            The SDK sandbox.

        Raises:
            SandboxInfraError: The box is not running.
        """
        if self._box is None or self._stop_task is not None:
            raise SandboxInfraError("The E2B sandbox is not running.")
        return self._box

    async def _pace_read(self) -> None:
        """Wait for the read bucket of the provider, when there is one."""
        if self._pacing is not None:
            await self._pacing.reads.acquire()

    async def exec(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_sec: float | None = None,
        on_output: OutputSink | None = None,
        sudo: bool = False,
    ) -> ExecResult:
        """Run one shell command in the box.

        The command runs in the background, so a cut connection can reattach
        to it by its pid. A timeout kills the command.

        Args:
            command: The shell command.
            cwd: The working directory, or ``None`` for the box default.
            env: Extra environment variables for this command.
            timeout_sec: The time limit, or ``None`` for no limit.
            on_output: Receives the stdout and stderr text as it arrives.
            sudo: Run the command as root.

        Returns:
            The exit code and the output. A timeout sets ``timed_out``.

        Raises:
            SandboxInfraError: The box is not running, or the command did not
                start or its connection could not reattach.
        """
        box = self._require_box()
        sink = on_output or current_output_sink()
        stdout: list[str] = []
        stderr: list[str] = []

        def on_stdout(text: str) -> None:
            """Keep and forward stdout text.

            Args:
                text: The new output.
            """
            stdout.append(text)
            if sink is not None:
                sink("stdout", text)

        def on_stderr(text: str) -> None:
            """Keep and forward stderr text.

            Args:
                text: The new output.
            """
            stderr.append(text)
            if sink is not None:
                sink("stderr", text)

        with self.timings.phase("run"):
            handle = await self._dispatch(
                box,
                command,
                cwd=cwd,
                envs=dict(env or {}),
                user="root" if sudo else None,
                on_stdout=on_stdout,
                on_stderr=on_stderr,
            )
            try:
                async with asyncio.timeout(timeout_sec) as deadline:
                    exit_code = await self._wait(box, handle, on_stdout, on_stderr)
            except TimeoutError:
                if not deadline.expired():
                    raise
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(box.commands.kill(handle.pid), STOP_TIMEOUT)
                return ExecResult(TIMEOUT_EXIT_CODE, "".join(stdout), "".join(stderr), timed_out=True)
        return ExecResult(exit_code, "".join(stdout), "".join(stderr))

    async def _dispatch(self, box: Any, command: str, **options: Any) -> Any:
        """Start the command in the background, resending only a refused dispatch.

        Args:
            box: The SDK sandbox.
            command: The shell command.
            **options: The ``commands.run`` options.

        Returns:
            The SDK command handle.

        Raises:
            SandboxInfraError: The command did not start.
        """
        for attempt in range(1, DISPATCH_ATTEMPTS + 1):
            await self._pace_read()
            try:
                # timeout=0 keeps the connection open with no limit; exec
                # applies its own limit and kills the command.
                return await box.commands.run(command, background=True, timeout=0, **options)
            except Exception as error:
                if attempt == DISPATCH_ATTEMPTS or not _is_dispatch_retryable(error):
                    raise SandboxInfraError(f"The E2B command did not start: {type(error).__name__}") from error
                logger.info("E2B sandbox %s dispatch retry %d: %s", self.name, attempt, type(error).__name__)
        raise AssertionError("unreachable")

    async def _wait(
        self,
        box: Any,
        handle: Any,
        on_stdout: Callable[[str], None],
        on_stderr: Callable[[str], None],
    ) -> int:
        """Wait for the command to end, reattaching after a cut connection.

        The wait itself is never a new dispatch, so a reattach cannot run the
        command twice. Output that the box sent during a cut can be lost.

        Args:
            box: The SDK sandbox.
            handle: The command handle of the dispatch.
            on_stdout: Receives stdout text.
            on_stderr: Receives stderr text.

        Returns:
            The exit code of the command.

        Raises:
            SandboxInfraError: The box stopped, or the reattach attempts ran out.
        """
        pid = handle.pid
        reattaches = 0
        while True:
            try:
                if handle is None:
                    await self._pace_read()
                    handle = await box.commands.connect(pid, timeout=0, on_stdout=on_stdout, on_stderr=on_stderr)
                result = await handle.wait()
                return result.exit_code
            except Exception as error:
                if CommandExitException is not None and isinstance(error, CommandExitException):
                    return error.exit_code
                if not _is_reattachable(error) or reattaches >= EXEC_REATTACH_ATTEMPTS:
                    raise SandboxInfraError(f"The E2B command connection failed: {type(error).__name__}") from error
            reattaches += 1
            handle = None
            logger.info("E2B sandbox %s reattach %d to pid %s", self.name, reattaches, pid)
            await self._clock.sleep(REATTACH_DELAY)

    async def upload_files(self, files: Mapping[str, str]) -> None:
        """Write UTF-8 text files into the box. E2B creates the parent directories.

        Args:
            files: Maps each sandbox path to its text content.

        Raises:
            SandboxInfraError: The box is not running.
        """
        box = self._require_box()
        for path, text in files.items():
            await self._pace_read()
            await box.files.write(path, text)

    async def download_file(self, path: str) -> str | None:
        """Read one UTF-8 text file from the box.

        Args:
            path: The sandbox path.

        Returns:
            The content, or ``None`` when the file does not exist.

        Raises:
            SandboxInfraError: The box is not running.
        """
        box = self._require_box()
        await self._pace_read()
        try:
            return await box.files.read(path, format="text")
        except Exception as error:
            if NotFoundException is not None and isinstance(error, NotFoundException):
                return None
            raise

    async def _apply_network_policy(self, policy: NetworkPolicy) -> None:
        """Send a new egress policy to the running box.

        Args:
            policy: The checked policy.

        Raises:
            SandboxInfraError: The box is not running.
        """
        box = self._require_box()
        await self._pace_read()
        await box.update_network(network_update(policy))


@contextlib.asynccontextmanager
async def _no_reservation() -> AsyncIterator[None]:
    """Hold no budget reservation, because Skynet does not fund E2B boxes.

    Yields:
        ``None``.
    """
    yield None
