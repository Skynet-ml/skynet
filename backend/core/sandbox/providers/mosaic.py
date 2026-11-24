"""The async Mosaic sandbox provider, for development only.

Adapted from Harbor 0.25.0 (Apache-2.0), mosaic: the sandbox create, the
process poll loop, the file calls and the lost-create cleanup come from
``harbor/environments/mosaic.py``. Changes:

- The provider implements the async ``SandboxEnvironment`` contract. The
  factory refuses it in production (``production_allowed=False``).
- The provider adds no retry of its own (SKY-406 D3, D10). The SDK replays
  only idempotent calls, and the create carries the box tag as its
  idempotency key, so a replayed create gets the same box back. An SDK
  error maps to one sandbox error group.
- The start and run phase timeouts bound the create and each command. A
  command that runs past its limit or whose ``exec`` is cancelled is killed.
- Each sandbox carries a box tag in its metadata. A stop during the create,
  or after a create that timed out, destroys the create's box, or finds the
  box by that tag, so a lost create does not leak a box. The stop waits a
  bounded time; a create thread that runs longer gets its box destroyed in
  the background when it returns.
- ``stop_job_sandboxes`` finds the sandboxes of a job by their metadata and
  reads every page of the list.
- With a budget the box is protected (SKY-549). It reserves its whole
  funded lifetime before the create, sends an explicit vCPU and memory
  shape, and checks the shape after the create. Mosaic's ``ttl_seconds`` is
  an inactivity limit, not a lifetime cap, so the provider destroys the box
  itself when its funded lifetime ends. The stop settles the hold whenever
  the box id is known, also for a lost create's box found by its tag. It
  releases the hold only when Mosaic refused the create and then lists no
  box with the tag, and keeps it pending in every other case.
- A ``sudo`` command runs as the ``root`` user.
- Harbor's snapshot builds, volumes, secrets, SSH, pause, the ``/workspace``
  transfer staging and directory transfer are left out.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import logging
import math
import re
import shlex
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from pathlib import PurePosixPath
from typing import Any

from core.billing.budgets import BudgetError
from core.billing.mosaic_usage import MosaicUsageReservation
from core.billing.runtime import UsagePendingError

from ..capabilities import EnvironmentCapabilities, NetworkMode, SandboxSpecError, network_mode
from ..environment import JOB_TAG, ExecResult, OutputSink, SandboxEnvironment
from ..errors import (
    SandboxError,
    SandboxFatalError,
    SandboxInfraError,
    SandboxRejectedError,
    SandboxTransientError,
    StartTimeoutError,
)
from ..resources import ResourceMode, SandboxSpec
from ..timeouts import Phase, PhaseTimeouts

try:
    from mosaic_sandbox import (
        AsyncSandbox,
        AuthenticationError,
        MosaicError,
        NotFoundError,
        RateLimitError,
        Sandbox,
        UnknownFieldError,
        UnsupportedShapeError,
        UnsupportedTemplateError,
    )
    from mosaic_sandbox import PermissionError as MosaicPermissionError
    from mosaic_sandbox import TimeoutError as MosaicTimeoutError
    from mosaic_sandbox.credentials import resolve_token
except ImportError:
    AsyncSandbox = None
    AuthenticationError = None
    MosaicError = None
    NotFoundError = None
    RateLimitError = None
    Sandbox = None
    UnknownFieldError = None
    UnsupportedShapeError = None
    UnsupportedTemplateError = None
    MosaicPermissionError = None
    MosaicTimeoutError = None
    resolve_token = None

logger = logging.getLogger(__name__)

API_TOKEN_ENV = "MOSAIC_API_TOKEN"
BOX_TAG = "skynet_box"
STOP_TIMEOUT = 60.0
KILL_TIMEOUT = 30.0
POLL_INTERVAL = 0.1
LIST_PAGE_SIZE = 500
_TRANSIENT_CODES = frozenset({"transport", "client_timeout"})
_ENDED_STATES = frozenset({"destroyed", "destroying", "deleted"})
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# The provider contract reports a timeout as exit code 124, like GNU timeout.
_TIMEOUT_EXIT_CODE = 124

MOSAIC_CAPABILITIES = EnvironmentCapabilities(name="mosaic", billable=True, network_modes=frozenset(NetworkMode))
# The shape a protected box sends when the spec names none: Mosaic's default
# template shape, and 2 GiB for each vCPU as in its published shapes.
_DEFAULT_VCPU = 2
_MEMORY_MB_PER_VCPU = 2048

# The event loop keeps only weak references to tasks, so a background
# cleanup that nobody awaits must stay here until it ends.
_BACKGROUND_CLEANUPS: set[asyncio.Future[None]] = set()


def _classes(*classes: type[BaseException] | None) -> tuple[type[BaseException], ...]:
    """Return the SDK error classes that exist.

    Args:
        *classes: SDK error classes, ``None`` when the SDK is not installed.

    Returns:
        The classes that are not ``None``, for use in an ``except`` clause.
    """
    return tuple(cls for cls in classes if cls is not None)


def _error_group(error: BaseException) -> type[SandboxError]:
    """Choose the sandbox error group of one SDK error.

    Args:
        error: A ``MosaicError``.

    Returns:
        The sandbox error class to raise.
    """
    status = getattr(error, "status", None)
    if isinstance(error, _classes(RateLimitError, MosaicTimeoutError)) or getattr(error, "code", None) in (
        _TRANSIENT_CODES
    ):
        return SandboxTransientError
    if isinstance(error, _classes(AuthenticationError, MosaicPermissionError)):
        return SandboxFatalError
    if isinstance(error, _classes(UnknownFieldError, UnsupportedShapeError, UnsupportedTemplateError)) or status in (
        400,
        422,
    ):
        return SandboxRejectedError
    if isinstance(status, int) and status >= 500:
        return SandboxTransientError
    return SandboxInfraError


@contextlib.contextmanager
def _mapped(action: str) -> Iterator[None]:
    """Turn an SDK error into the sandbox error of its group.

    Args:
        action: What the provider was doing, for the error message.

    Yields:
        Nothing.

    Raises:
        SandboxTransientError: A transport error, a rate limit, a timeout or a 5xx answer.
        SandboxFatalError: The API token is wrong or has no permission.
        SandboxRejectedError: Mosaic rejected the request as invalid.
        SandboxInfraError: Any other SDK error.
    """
    try:
        yield
    except _classes(MosaicError) as error:
        group = _error_group(error)
        raise group(f"Mosaic could not {action}: {type(error).__name__}.") from error


def _refusal(error: BaseException | None) -> str | None:
    """Name the refusal of a create, when Mosaic answered it with a refusal.

    Args:
        error: The create's SDK error, or ``None``.

    Returns:
        The sandbox error group of a 4xx answer, a refused token or a rate
        limit, or ``None`` for any other outcome. A refusal alone does not
        prove that no box exists; only an empty list after it does.
    """
    if error is None or not isinstance(error, _classes(MosaicError)):
        return None
    group = _error_group(error)
    if isinstance(error, _classes(RateLimitError)) or group in (SandboxRejectedError, SandboxFatalError):
        return group.__name__
    return None


async def _bounded(awaitable: Awaitable[Any], action: str) -> bool:
    """Wait for a cleanup call for at most ``STOP_TIMEOUT`` seconds.

    Args:
        awaitable: The cleanup call.
        action: What the call does, for the log.

    Returns:
        True when the call finished without an error.
    """
    try:
        await asyncio.wait_for(awaitable, STOP_TIMEOUT)
    except Exception as error:
        logger.warning("Mosaic could not %s: %s", action, type(error).__name__)
        return False
    return True


def _cleanup_done(action: str, task: asyncio.Future[None]) -> None:
    """Forget a background cleanup and log its failure.

    Args:
        action: What the cleanup does, for the log.
        task: The finished cleanup task.
    """
    _BACKGROUND_CLEANUPS.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.warning("Mosaic could not %s: %s", action, type(task.exception()).__name__)


async def _list_all(metadata: Mapping[str, str]) -> list[Any]:
    """List every sandbox with this metadata, page by page.

    Args:
        metadata: The metadata filter.

    Returns:
        The sandbox summaries of all pages.

    Raises:
        SandboxError: A list call failed; see ``_mapped``.
    """
    found: list[Any] = []
    cursor: str | None = None
    while True:
        with _mapped("list the sandboxes"):
            page, cursor = await asyncio.to_thread(
                Sandbox.list_page, metadata=dict(metadata), limit=LIST_PAGE_SIZE, cursor=cursor
            )
        found.extend(page)
        if cursor is None:
            return found


def _create_params(spec: SandboxSpec, box_tag: str) -> dict[str, Any]:
    """Map the spec to the arguments of ``Sandbox.create``.

    Args:
        spec: The sandbox to open. ``validate_spec`` already checked it.
        box_tag: The tag that names this one box.

    Returns:
        The keyword arguments of the create call.
    """
    params: dict[str, Any] = {
        "name": spec.name or f"skynet-{box_tag[:12]}",
        "metadata": {**spec.tags, BOX_TAG: box_tag},
        "ttl_seconds": math.ceil(spec.lifetime_seconds),
        "idempotency_key": box_tag,
    }
    request = spec.resources
    if request.mode is not ResourceMode.IGNORE:
        params["vcpu"] = request.cpus
        if request.memory_mb is not None:
            params["memory_mb"] = request.memory_mb
    mode = network_mode(disabled=spec.network_disabled, allowed_hosts=tuple(spec.allowed_hosts))
    if mode is NetworkMode.DENY_ALL:
        params["network_enabled"] = False
    elif mode is NetworkMode.ALLOWLIST:
        params["network_enabled"] = True
        params["network_allow"] = list(dict.fromkeys(spec.allowed_hosts))
    return params


def _settle_failure(error: Exception) -> Exception:
    """Return the usage error that a failed settle raises from ``stop()``.

    Args:
        error: The settle failure.

    Returns:
        A usage error unchanged. Any other failure becomes a
        ``UsagePendingError``, because the hold stays pending and the error
        text can hold database details.
    """
    if isinstance(error, UsagePendingError | BudgetError):
        return error
    pending = UsagePendingError("Sandbox usage is not yet settled; the reservation remains active.")
    pending.__cause__ = error
    return pending


def _destroy_by_id(sandbox_id: str) -> None:
    """Destroy one sandbox by its id. Blocking; run it in a thread.

    Args:
        sandbox_id: The Mosaic sandbox id.
    """
    Sandbox.connect(sandbox_id).destroy()


class MosaicEnvironment(SandboxEnvironment):
    """Run commands in one Mosaic sandbox."""

    capabilities = MOSAIC_CAPABILITIES

    def __init__(
        self,
        spec: SandboxSpec,
        *,
        timeouts: PhaseTimeouts | None = None,
        budget: Any | None = None,
        usage_factory: Callable[..., Any] = MosaicUsageReservation,
    ) -> None:
        """Check the spec and prepare the create call.

        Args:
            spec: The sandbox to open.
            timeouts: The phase timeouts; ``None`` sets no start or run limit.
            budget: The ``BudgetRuntime`` that funds the box, or ``None``.
            usage_factory: Makes the usage reservation of a protected box.

        Raises:
            SandboxSpecError: Mosaic cannot open this spec, or a protected box
                has no operation key.
        """
        if budget is not None:
            if spec.operation_key is None:
                raise SandboxSpecError("A protected Mosaic sandbox needs an operation key.")
            self.capabilities = dataclasses.replace(MOSAIC_CAPABILITIES, protected=True)
        super().__init__(spec)
        self._start_limit = timeouts.seconds(Phase.START) if timeouts else None
        self._default_timeout = timeouts.seconds(Phase.RUN) if timeouts else None
        self._box_tag = uuid.uuid4().hex
        self._create_params = _create_params(spec, self._box_tag)
        if budget is not None:
            vcpu = spec.resources.cpus if spec.resources.mode is not ResourceMode.IGNORE else _DEFAULT_VCPU
            self._create_params["vcpu"] = vcpu
            self._create_params.setdefault("memory_mb", vcpu * _MEMORY_MB_PER_VCPU)
        self._budget = budget
        self._usage_factory = usage_factory
        self._usage: Any | None = None
        self._billing_call: asyncio.Future[Any] | None = None
        self._expiry: asyncio.Future[None] | None = None
        self._start_error: BaseException | None = None
        self._settle_error: Exception | None = None
        self._box: Any = None
        self._create_call: asyncio.Future[Any] | None = None
        self._starting: asyncio.Future[None] | None = None
        self._create_dispatched = False
        self._started = False
        self._stop_task: asyncio.Future[None] | None = None

    @property
    def usage(self) -> Any | None:
        """Return the usage reservation of a protected box, once it exists."""
        return self._usage

    def _usage_request(self) -> dict[str, Any]:
        """Build the request that the reservation prices.

        Returns:
            The box shape, and its lifetime plus two stop limits, because the
            box runs until its destroy ends.
        """
        # The expiry destroy waits STOP_TIMEOUT, but the SDK destroy thread
        # retries for up to 61 s and Mosaic bills until it ends, so one stop
        # limit is not enough margin.
        return {
            "vcpu": self._create_params["vcpu"],
            "memory_mb": self._create_params["memory_mb"],
            "lifetime_ms": math.ceil((self.spec.lifetime_seconds + 2 * STOP_TIMEOUT) * 1000),
        }

    @classmethod
    def preflight(cls) -> None:
        """Check that the SDK and an API token exist.

        Raises:
            SandboxFatalError: The SDK is not installed, or Mosaic finds no
                API token (``MOSAIC_API_TOKEN`` or the saved config).
        """
        if Sandbox is None or resolve_token is None:
            raise SandboxFatalError("The mosaic-sandbox package is not installed: install the `mosaic` extra.")
        token, _source = resolve_token()
        if not token:
            raise SandboxFatalError(f"The Mosaic sandbox needs the {API_TOKEN_ENV} variable.")

    async def start(self) -> None:
        """Create the sandbox and keep it.

        Any failure or cancel stops the box before the error goes on.

        Raises:
            StartTimeoutError: The create did not finish within the start timeout.
            SandboxInfraError: A stop ended the start, or the sandbox already
                started once.
            SandboxError: The create call failed; see ``_mapped``.
        """
        if self._started:
            # The box tag is the create idempotency key, so a second create
            # would get the first box back.
            raise SandboxInfraError("The Mosaic sandbox already started once; it cannot start again.")
        self._started = True
        # A stop before the start leaves a finished stop task; drop it so the
        # stop after this start destroys the box.
        self._stop_task = None
        self.preflight()
        self._starting = asyncio.ensure_future(self._create())
        try:
            await self._starting
        except BaseException as error:
            self._start_error = error
            try:
                await self.stop()
            except Exception as settle_error:
                # Keep the start error for the caller; the owner's next
                # stop() reports the settle failure.
                self._settle_error = settle_error
            task = asyncio.current_task()
            if isinstance(error, asyncio.CancelledError) and task is not None and not task.cancelling():
                raise SandboxInfraError("The Mosaic sandbox stopped during its start.") from None
            raise
        finally:
            self._starting = None

    async def _reserve(self) -> Any | None:
        """Reserve the funded lifetime of a protected box.

        Returns:
            The usage reservation, or ``None`` for an unprotected box.
        """
        if self._budget is None:
            return None
        reserving = asyncio.ensure_future(
            asyncio.to_thread(
                self._usage_factory, self._budget, self._usage_request(), operation_key=self.spec.operation_key
            )
        )
        try:
            self._usage = await asyncio.shield(reserving)
        except asyncio.CancelledError:
            # The thread cannot be cancelled and may still reserve; keep its
            # result so the teardown releases it instead of leaking it.
            while not reserving.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(reserving)
            if not reserving.cancelled() and reserving.exception() is None:
                self._usage = reserving.result()
            raise
        return self._usage

    async def _billing(self, call: Callable[..., Any], *args: Any) -> Any:
        """Run one reservation call in a thread that a cancel cannot cut short.

        The teardown awaits the same call before it settles.

        Args:
            call: The reservation method.
            *args: Its arguments.

        Returns:
            The result of the call.
        """
        self._billing_call = asyncio.ensure_future(asyncio.to_thread(call, *args))
        return await asyncio.shield(self._billing_call)

    async def _expire(self, at: float) -> None:
        """Stop the box when its funded lifetime ends.

        Args:
            at: The event loop time at which the funded lifetime ends.
        """
        await asyncio.sleep(max(0.0, at - asyncio.get_running_loop().time()))
        logger.info("Mosaic sandbox reached its funded lifetime; destroying it.")
        if self._stop_task is None:
            self._stop_task = asyncio.ensure_future(self._teardown())
        # The stop() of the box's owner raises a settle error, not this task.
        with contextlib.suppress(BaseException):
            await asyncio.shield(self._stop_task)

    async def _create(self) -> None:
        """Reserve a protected box's funding, then create the sandbox in a thread and keep it.

        Raises:
            StartTimeoutError: The create did not finish within the start timeout.
        """
        limit = self._start_limit
        usage = await self._reserve()
        try:
            async with asyncio.timeout(limit) as deadline:
                if usage is not None:
                    usage.dispatched()
                funded_until = asyncio.get_running_loop().time() + self.spec.lifetime_seconds
                self._create_dispatched = True
                # The SDK create blocks, and a cancel cannot end its thread.
                # The teardown waits for this future to find the box it made.
                self._create_call = asyncio.ensure_future(asyncio.to_thread(Sandbox.create, **self._create_params))
                with _mapped("create the sandbox"):
                    sync_box = await asyncio.shield(self._create_call)
                # The box is kept here, not in start(), so a stop that cancels
                # the start right after the create still sees it.
                self._box = AsyncSandbox(sync_box)
                if usage is not None:
                    with _mapped("confirm the sandbox"):
                        await self._billing(usage.confirm_created, sync_box)
                    self._expiry = asyncio.ensure_future(self._expire(funded_until))
        except TimeoutError:
            if not deadline.expired():
                raise
            raise StartTimeoutError(limit) from None

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
        """Run one shell command in the sandbox and wait for it.

        Args:
            command: The shell command.
            cwd: The working directory; the sandbox default by default.
            env: Extra environment variables for this command.
            timeout_sec: The time limit; ``None`` uses the run phase timeout.
            on_output: Receives ``(stream, text)`` pairs while the command runs.
            sudo: Run the command as the ``root`` user instead of the
                sandbox default user.

        Returns:
            The exit code and the captured output. When the command runs past
            the time limit, it is killed first, and the result has exit code
            124, ``timed_out`` set and the output so far.

        Raises:
            SandboxInfraError: The sandbox is not running, or Mosaic gave no
                exit code.
            SandboxRejectedError: An environment variable name is not valid.
        """
        box = self._box
        if box is None:
            raise SandboxInfraError("The Mosaic sandbox is not running.")
        wrapped = self._wrap(command, cwd, {**self.spec.env, **(env or {})})
        with _mapped("start the command"):
            handle = await box.process.start(wrapped, **({"user": "root"} if sudo else {}))
        limit = timeout_sec if timeout_sec is not None else self._default_timeout
        stdout: list[str] = []
        stderr: list[str] = []
        finished = False
        timed_out = False
        try:
            async with asyncio.timeout(limit) as deadline:
                with _mapped("wait for the command"):
                    exit_code = await self._poll(handle, stdout, stderr, on_output)
            finished = True
        except TimeoutError:
            if not deadline.expired():
                raise
            timed_out = True
        finally:
            if not finished:
                # A timeout, a cancel or a lost wait must not leave the command running.
                await asyncio.shield(_kill(handle))
        if timed_out:
            return ExecResult(
                exit_code=_TIMEOUT_EXIT_CODE, stdout="".join(stdout), stderr="".join(stderr), timed_out=True
            )
        if exit_code is None:
            raise SandboxInfraError("Mosaic gave no exit code for the command.")
        return ExecResult(exit_code=exit_code, stdout="".join(stdout), stderr="".join(stderr))

    @staticmethod
    async def _poll(handle: Any, stdout: list[str], stderr: list[str], on_output: OutputSink | None) -> int | None:
        """Read the command output until the process finishes.

        Args:
            handle: The SDK process handle.
            stdout: Collects the standard output chunks.
            stderr: Collects the standard error chunks.
            on_output: Receives ``(stream, text)`` pairs, or ``None``.

        Returns:
            The exit code, or ``None`` when Mosaic gave none.
        """
        while True:
            payload = await handle.logs()
            for stream, chunks in (("stdout", stdout), ("stderr", stderr)):
                text = payload.get(stream)
                if text:
                    chunks.append(text)
                    if on_output is not None:
                        on_output(stream, text)
            if payload.get("state") == "finished":
                exit_code = payload.get("exit_code")
                if exit_code is None:
                    exit_code = (await handle.info()).exit_code
                return exit_code
            await asyncio.sleep(POLL_INTERVAL)

    @staticmethod
    def _wrap(command: str, cwd: str | None, env: Mapping[str, str] | None) -> str:
        """Build the shell line that runs the command with its cwd and env.

        Args:
            command: The shell command.
            cwd: The working directory, or ``None``.
            env: Extra environment variables, or ``None``.

        Returns:
            The quoted shell line.

        Raises:
            SandboxRejectedError: An environment variable name is not valid.
        """
        parts: list[str] = []
        for key, value in (env or {}).items():
            if not _ENV_KEY.fullmatch(key):
                raise SandboxRejectedError(f"{key!r} is not a valid environment variable name.")
            parts.append(f"{key}={shlex.quote(value)}")
        line = " ".join([*parts, "bash", "-c", shlex.quote(command)])
        if cwd is not None:
            line = f"cd {shlex.quote(cwd)} && {line}"
        return line

    async def upload_files(self, files: Mapping[str, str]) -> None:
        """Write UTF-8 text files into the sandbox, making parent directories.

        Args:
            files: The text of each file, by absolute path.

        Raises:
            SandboxInfraError: The sandbox is not running, or the parent
                directories could not be made.
        """
        box = self._box
        if box is None:
            raise SandboxInfraError("The Mosaic sandbox is not running.")
        parents = sorted({str(PurePosixPath(path).parent) for path in files} - {".", "/"})
        if parents:
            made = await self.exec("mkdir -p " + " ".join(shlex.quote(parent) for parent in parents))
            if not made.ok:
                raise SandboxInfraError("Mosaic could not make the upload directories.")
        for path, text in files.items():
            with _mapped("write a file"):
                await box.files.write(path, text, create_parents=True)

    async def download_file(self, path: str) -> str | None:
        """Read one UTF-8 text file from the sandbox.

        Args:
            path: The absolute path of the file.

        Returns:
            The file text, or ``None`` when the file does not exist.

        Raises:
            SandboxInfraError: The sandbox is not running.
        """
        box = self._box
        if box is None:
            raise SandboxInfraError("The Mosaic sandbox is not running.")
        try:
            with _mapped("read a file"):
                data = await box.files.read_bytes(path)
        except SandboxError as error:
            if NotFoundError is not None and isinstance(error.__cause__, NotFoundError):
                return None
            raise
        return data.decode("utf-8")

    async def stop(self) -> None:
        """Destroy the sandbox once and settle its hold. Safe to call again.

        Raises:
            UsagePendingError: The settle failed, so the hold stays pending.
            BudgetError: The settle write failed.
        """
        if self._stop_task is None:
            self._stop_task = asyncio.ensure_future(self._teardown())
        await asyncio.shield(self._stop_task)
        # Only one stop caller gets a settle failure, so a second stop
        # returns quietly.
        settle_error, self._settle_error = self._settle_error, None
        if settle_error is not None:
            raise settle_error

    async def _teardown(self) -> None:
        """End a running start, destroy the sandbox or the lost create's box, then settle the hold."""
        if self._starting is not None and not self._starting.done():
            # A stop during the start: end the wait for the create first.
            self._starting.cancel()
            with contextlib.suppress(BaseException):
                await self._starting
        if self._expiry is not None:
            self._expiry.cancel()
        if self._billing_call is not None:
            with contextlib.suppress(BaseException):
                await asyncio.shield(self._billing_call)
        box, call = self._box, self._create_call
        self._box = self._create_call = None
        usage, self._usage = self._usage, None
        if box is not None:
            destroyed = await _bounded(box.destroy(), "destroy the sandbox")
            await self._close_usage(usage, [(box.id, None, destroyed)])
        elif self._create_dispatched:
            action = "destroy the lost sandbox"
            cleanup = asyncio.ensure_future(self._close_lost_create(call, usage))
            _BACKGROUND_CLEANUPS.add(cleanup)
            cleanup.add_done_callback(functools.partial(_cleanup_done, action))
            # The create thread cannot be cancelled and can outlive the stop
            # limit, so the cleanup, and the settle after it, go on in the
            # background after the wait.
            done, _pending = await asyncio.wait({cleanup}, timeout=STOP_TIMEOUT)
            if not done:
                logger.warning("Mosaic did not %s in time; it goes on in the background.", action)
        elif usage is not None:
            error = self._start_error
            await self._close_usage(usage, [], type(error).__name__ if error is not None else "no_box_created")

    async def _close_lost_create(self, call: asyncio.Future[Any] | None, usage: Any | None) -> None:
        """Destroy the box of a lost create, then settle, release or keep its hold.

        Args:
            call: The create thread future, or ``None``.
            usage: The usage reservation of a protected box, or ``None``.
        """
        try:
            create_error, boxes = await self._destroy_orphan(call)
        except BaseException:
            # The SDK retries a create under its idempotency key and reports
            # only the last attempt, so no create error proves that no box
            # exists without a list.
            await self._close_usage(usage, None)
            raise
        refusal = _refusal(create_error)
        if not boxes and refusal is None:
            # A lost create may still make its box after the list: Mosaic's
            # list shows only running, paused, archived and destroyed boxes
            # (the ``state`` enum of GET /v1/sandboxes in
            # https://sandbox.mosaicos.com/openapi.json, read 2026-11-24),
            # and nothing there says a box that is still being created is listed.
            await self._close_usage(usage, None)
            return
        await self._close_usage(usage, boxes, refusal or "no_box_created")

    async def _close_usage(
        self, usage: Any | None, boxes: list[tuple[str, Any, bool]] | None, reason: str = "no_box_created"
    ) -> None:
        """Settle, release or keep the hold of a protected box after its teardown.

        Args:
            usage: The usage reservation, or ``None`` for an unprotected box.
            boxes: The boxes of this create as ``(id, SDK box or None, destroy
                confirmed)``; empty when no box exists, ``None`` when unknown.
            reason: Why no box exists, for the release audit record.
        """
        if usage is None:
            return
        try:
            if boxes == []:
                try:
                    await asyncio.to_thread(usage.release, reason)
                except BudgetError:
                    await asyncio.to_thread(usage.pending)
            elif boxes is not None and len(boxes) == 1:
                ((sandbox_id, handle, destroyed),) = boxes
                if usage.sandbox_id is None:
                    await asyncio.to_thread(usage.bind, sandbox_id, handle)
                if destroyed:
                    usage.stopped()
                await asyncio.to_thread(usage.settle)
            else:
                await asyncio.to_thread(usage.pending)
        except Exception as billing_error:
            logger.warning("Mosaic sandbox usage stays pending: %s", type(billing_error).__name__)
            self._settle_error = _settle_failure(billing_error)

    async def _destroy_orphan(
        self, call: asyncio.Future[Any] | None
    ) -> tuple[BaseException | None, list[tuple[str, Any, bool]]]:
        """Destroy the box of a create that ended before the provider kept it.

        Args:
            call: The create thread future, or ``None``.

        Returns:
            The create's SDK error, or ``None``, and every box of this create
            as ``(id, SDK box or None, destroy confirmed)``. An empty list
            means Mosaic lists no box with this provider's box tag.

        Raises:
            SandboxError: The list call failed; see ``_mapped``.
        """
        sync_box = create_error = None
        if call is not None:
            try:
                sync_box = await asyncio.shield(call)
            except Exception as error:
                create_error = error
        if sync_box is not None:
            return None, [(sync_box.id, sync_box, await self._destroy_lost(sync_box.destroy))]
        # The create failed or still runs; the server may still have made the
        # box, so look for it by its tag.
        boxes = []
        for summary in await _list_all({BOX_TAG: self._box_tag}):
            if summary.state in _ENDED_STATES:
                boxes.append((summary.id, None, True))
            else:
                boxes.append((summary.id, None, await self._destroy_lost(_destroy_by_id, summary.id)))
        return create_error, boxes

    @staticmethod
    async def _destroy_lost(destroy: Callable[..., Any], *args: Any) -> bool:
        """Destroy one lost box in a thread and log, not raise, a failure.

        Args:
            destroy: The blocking destroy call.
            *args: Its arguments.

        Returns:
            True when Mosaic confirmed the destroy.
        """
        try:
            with _mapped("destroy the lost sandbox"):
                await asyncio.to_thread(destroy, *args)
        except Exception as error:
            logger.warning("Mosaic could not destroy the lost sandbox: %s", type(error).__name__)
            return False
        return True

    @classmethod
    async def stop_job_sandboxes(cls, job_id: str) -> int:
        """Destroy every running sandbox that carries this job tag.

        Args:
            job_id: The job tag value.

        Returns:
            The number of sandboxes that were destroyed. A list that runs past
            ``STOP_TIMEOUT`` is logged, and no sandbox is destroyed.

        Raises:
            SandboxError: The list call failed; see ``_mapped``.
        """
        cls.preflight()
        found: list[Any] = []
        try:
            async with asyncio.timeout(STOP_TIMEOUT):
                found = await _list_all({JOB_TAG: job_id})
        except TimeoutError:
            logger.warning("Mosaic did not list the sandboxes of job %s in time.", job_id)
        stopped = 0
        for summary in found:
            if summary.state in _ENDED_STATES or (summary.metadata or {}).get(JOB_TAG) != job_id:
                continue
            if await _bounded(asyncio.to_thread(_destroy_by_id, summary.id), "destroy a job sandbox"):
                stopped += 1
        return stopped


async def _kill(handle: Any) -> None:
    """Kill a running command and log, not raise, a failure.

    Args:
        handle: The SDK process handle.
    """
    try:
        await asyncio.wait_for(handle.kill(), KILL_TIMEOUT)
    except Exception as error:
        logger.warning("Mosaic could not kill the command: %s", type(error).__name__)
