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
- A ``sudo`` command runs as the ``root`` user.
- Harbor's snapshot builds, volumes, secrets, SSH, pause, the ``/workspace``
  transfer staging and directory transfer are left out.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import math
import re
import shlex
import uuid
from collections.abc import Awaitable, Iterator, Mapping
from pathlib import PurePosixPath
from typing import Any

from ..capabilities import EnvironmentCapabilities, NetworkMode, network_mode
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

MOSAIC_CAPABILITIES = EnvironmentCapabilities(name="mosaic", network_modes=frozenset(NetworkMode))

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


def _destroy_by_id(sandbox_id: str) -> None:
    """Destroy one sandbox by its id. Blocking; run it in a thread.

    Args:
        sandbox_id: The Mosaic sandbox id.
    """
    Sandbox.connect(sandbox_id).destroy()


class MosaicEnvironment(SandboxEnvironment):
    """Run commands in one Mosaic sandbox."""

    capabilities = MOSAIC_CAPABILITIES

    def __init__(self, spec: SandboxSpec, *, timeouts: PhaseTimeouts | None = None) -> None:
        """Check the spec and prepare the create call.

        Args:
            spec: The sandbox to open.
            timeouts: The phase timeouts; ``None`` sets no start or run limit.

        Raises:
            SandboxSpecError: Mosaic cannot open this spec.
        """
        super().__init__(spec)
        self._start_limit = timeouts.seconds(Phase.START) if timeouts else None
        self._default_timeout = timeouts.seconds(Phase.RUN) if timeouts else None
        self._box_tag = uuid.uuid4().hex
        self._create_params = _create_params(spec, self._box_tag)
        self._box: Any = None
        self._create_call: asyncio.Future[Any] | None = None
        self._starting: asyncio.Future[None] | None = None
        self._create_dispatched = False
        self._started = False
        self._stop_task: asyncio.Future[None] | None = None

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
            await self.stop()
            task = asyncio.current_task()
            if isinstance(error, asyncio.CancelledError) and task is not None and not task.cancelling():
                raise SandboxInfraError("The Mosaic sandbox stopped during its start.") from None
            raise
        finally:
            self._starting = None

    async def _create(self) -> None:
        """Create the sandbox in a thread and keep it.

        Raises:
            StartTimeoutError: The create did not finish within the start timeout.
        """
        limit = self._start_limit
        try:
            async with asyncio.timeout(limit) as deadline:
                self._create_dispatched = True
                # The SDK create blocks, and a cancel cannot end its thread.
                # The teardown waits for this future to find the box it made.
                self._create_call = asyncio.ensure_future(asyncio.to_thread(Sandbox.create, **self._create_params))
                with _mapped("create the sandbox"):
                    sync_box = await asyncio.shield(self._create_call)
                # The box is kept here, not in start(), so a stop that cancels
                # the start right after the create still sees it.
                self._box = AsyncSandbox(sync_box)
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
        """Destroy the sandbox once. Safe to call again."""
        if self._stop_task is None:
            self._stop_task = asyncio.ensure_future(self._teardown())
        await asyncio.shield(self._stop_task)

    async def _teardown(self) -> None:
        """End a running start, then destroy the sandbox or the lost create's box."""
        if self._starting is not None and not self._starting.done():
            # A stop during the start: end the wait for the create first.
            self._starting.cancel()
            with contextlib.suppress(BaseException):
                await self._starting
        box, call = self._box, self._create_call
        self._box = self._create_call = None
        if box is not None:
            await _bounded(box.destroy(), "destroy the sandbox")
        elif self._create_dispatched:
            action = "destroy the lost sandbox"
            cleanup = asyncio.ensure_future(self._destroy_orphan(call))
            _BACKGROUND_CLEANUPS.add(cleanup)
            cleanup.add_done_callback(functools.partial(_cleanup_done, action))
            # The create thread cannot be cancelled and can outlive the stop
            # limit, so the cleanup goes on in the background after the wait.
            done, _pending = await asyncio.wait({cleanup}, timeout=STOP_TIMEOUT)
            if not done:
                logger.warning("Mosaic did not %s in time; it goes on in the background.", action)

    async def _destroy_orphan(self, call: asyncio.Future[Any] | None) -> None:
        """Destroy the box of a create that ended before the provider kept it.

        Args:
            call: The create thread future, or ``None``.
        """
        sync_box = None
        if call is not None:
            with contextlib.suppress(Exception):
                sync_box = await asyncio.shield(call)
        if sync_box is not None:
            with _mapped("destroy the lost sandbox"):
                await asyncio.to_thread(sync_box.destroy)
            return
        # The create failed or still runs; the server may still have made the
        # box, so look for it by its tag.
        for summary in await _list_all({BOX_TAG: self._box_tag}):
            if summary.state not in _ENDED_STATES:
                with _mapped("destroy the lost sandbox"):
                    await asyncio.to_thread(_destroy_by_id, summary.id)

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
