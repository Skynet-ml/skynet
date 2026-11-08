"""The async Sail sandbox provider, for development only.

Adapted from Harbor 0.25.0 (Apache-2.0), sail: the Sailbox create, the size
fit, the egress policy map, the bash command form and the file calls come
from ``harbor/environments/sail.py``. Changes:

- The provider implements the async ``SandboxEnvironment`` contract. The
  factory refuses it in production (``production_allowed=False``).
- The provider adds no retry of its own (SKY-406 D3, D10). An SDK error
  maps to ``SandboxInfraError``, because the SDK error classes are not known.
- The start and run phase timeouts bound the create and each command. A
  create that a timeout or a stop cancels gets a short grace to return its
  handle, as in Harbor, so the box can still be terminated.
- Every Sailbox gets ``max_lifetime_seconds`` from the spec lifetime, so Sail
  ends a box that a lost process never stops.
- The spec sizes map to ``Sailbox.size_for``. The spec image comes from a
  registry; ``DEFAULT_IMAGE`` is used when the spec names none.
- ``stop_job_sandboxes`` terminates the Sailboxes that this process started
  for the job. Sail has no list call in the Harbor code, so boxes of other
  processes end only at their lifetime limit.
- Harbor's Dockerfile and compose builds, the DinD strategy, directory
  transfer, attach and sleep-on-stop are left out.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import math
import shlex
import uuid
from collections.abc import Awaitable, Iterator, Mapping
from pathlib import PurePosixPath
from typing import Any

from ..capabilities import EnvironmentCapabilities, NetworkMode, network_mode
from ..environment import JOB_TAG, ExecResult, NetworkPolicy, OutputSink, SandboxEnvironment
from ..errors import RunTimeoutError, SandboxError, SandboxFatalError, SandboxInfraError, StartTimeoutError
from ..resources import ResourceMode, SandboxSpec
from ..telemetry import current_output_sink
from ..timeouts import Phase, PhaseTimeouts

try:
    from sail import App, Config, EgressPolicy, Image, Sailbox
except ImportError:
    App = None
    Config = None
    EgressPolicy = None
    Image = None
    Sailbox = None

logger = logging.getLogger(__name__)

APP_NAME = "skynet"
DEFAULT_IMAGE = "python:3.12-slim"
STOP_TIMEOUT = 60.0
# Harbor waits this long for a cancelled create, so a box made during the
# request still has a handle to terminate.
CREATE_GRACE = 30.0
# The SDK timeout should end a command first; this margin only catches a
# hung SDK call.
EXEC_GRACE = 30.0
OUTPUT_BUFFER_BYTES = 64 * 1024 * 1024

SAIL_CAPABILITIES = EnvironmentCapabilities(
    name="sail",
    network_modes=frozenset(NetworkMode),
    dynamic_network_policy=True,
)

# Process-local: the sweep sees only the boxes that this process started.
_JOB_BOXES: dict[str, set[SailEnvironment]] = {}


@contextlib.contextmanager
def _mapped(action: str) -> Iterator[None]:
    """Turn an SDK error into a sandbox error.

    Args:
        action: What the provider was doing, for the error message.

    Yields:
        Nothing.

    Raises:
        SandboxInfraError: The SDK call raised an error that is not a sandbox error.
    """
    try:
        yield
    except SandboxError:
        raise
    except Exception as error:
        raise SandboxInfraError(f"Sail could not {action}: {type(error).__name__}.") from error


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
        logger.warning("Sail could not %s: %s", action, type(error).__name__)
        return False
    return True


def _egress_policy(*, disabled: bool, allowed_hosts: tuple[str, ...]) -> Any:
    """Map a network policy to a Sail egress policy.

    Args:
        disabled: True when the box has no egress.
        allowed_hosts: The hosts that egress may reach; empty means any host.

    Returns:
        The Sail egress policy document.
    """
    mode = network_mode(disabled=disabled, allowed_hosts=allowed_hosts)
    if mode is NetworkMode.DENY_ALL:
        return EgressPolicy.no_network()
    if mode is NetworkMode.ALLOWLIST:
        return EgressPolicy.allow_only(*dict.fromkeys(allowed_hosts))
    return EgressPolicy.allow_all()


def _size_parameters(spec: SandboxSpec) -> dict[str, Any]:
    """Map the spec sizes to the Sail size arguments of the create call.

    Args:
        spec: The sandbox to open.

    Returns:
        The ``size``, ``memory_limit_gib`` and ``disk_limit_gib`` arguments, or
        nothing when the spec ignores sizes.
    """
    request = spec.resources
    if request.mode is ResourceMode.IGNORE:
        return {}
    fit = Sailbox.size_for(
        cpus=request.cpus,
        memory_gib=request.memory_mb / 1024 if request.memory_mb else None,
        disk_gib=request.disk_mb / 1024 if request.disk_mb else None,
    )
    return {"size": fit.size, "memory_limit_gib": fit.memory_limit_gib, "disk_limit_gib": fit.disk_limit_gib}


def _bash_argv(command: str, cwd: str | None) -> list[str]:
    """Build the argv that runs a shell command in a working directory.

    Args:
        command: The shell command.
        cwd: The working directory, or ``None``.

    Returns:
        The ``bash -c`` argv.
    """
    if cwd is not None:
        command = f"cd {shlex.quote(cwd)} && exec bash -c {shlex.quote(command)}"
    return ["bash", "-c", command]


class SailEnvironment(SandboxEnvironment):
    """Run commands in one Sailbox."""

    capabilities = SAIL_CAPABILITIES

    def __init__(self, spec: SandboxSpec, *, timeouts: PhaseTimeouts | None = None) -> None:
        """Check the spec and prepare the create call.

        Args:
            spec: The sandbox to open.
            timeouts: The phase timeouts; ``None`` sets no start or run limit.

        Raises:
            SandboxSpecError: The spec breaks a provider rule.
        """
        super().__init__(spec)
        self._start_limit = timeouts.seconds(Phase.START) if timeouts else None
        self._default_timeout = timeouts.seconds(Phase.RUN) if timeouts else None
        self._name = spec.name or f"skynet-{uuid.uuid4().hex[:12]}"
        self._job_id = spec.tags.get(JOB_TAG)
        self._sailbox: Any = None
        self._create_task: asyncio.Future[Any] | None = None
        self._starting: asyncio.Future[None] | None = None
        self._started = False
        self._terminated = False
        self._stop_task: asyncio.Future[None] | None = None

    @classmethod
    def preflight(cls) -> None:
        """Check that the SDK and the Sail credentials exist.

        Raises:
            SandboxFatalError: The SDK is not installed, or Sail finds no credentials.
        """
        if Sailbox is None:
            raise SandboxFatalError("The sail package is not installed: install the `sail` extra.")
        try:
            Config.from_env()
        except ValueError as error:
            raise SandboxFatalError("The Sail sandbox found no credentials in the environment.") from error

    async def start(self) -> None:
        """Create the Sailbox and wait until it runs.

        Any failure or cancel stops the box before the error goes on.

        Raises:
            StartTimeoutError: The Sailbox did not run within the start timeout.
            SandboxInfraError: A stop ended the start, the sandbox already
                started once, or the create call failed.
            SandboxFatalError: The SDK or the credentials are missing.
        """
        if self._started:
            raise SandboxInfraError("The Sail sandbox already started once; it cannot start again.")
        self._started = True
        # A stop before the start leaves a finished stop task; drop it so the
        # stop after this start terminates the box.
        self._stop_task = None
        self.preflight()
        self._starting = asyncio.ensure_future(self._create())
        try:
            await self._starting
        except BaseException as error:
            await self.stop()
            task = asyncio.current_task()
            if isinstance(error, asyncio.CancelledError) and task is not None and not task.cancelling():
                raise SandboxInfraError("The Sail sandbox stopped during its start.") from None
            raise
        finally:
            self._starting = None

    async def _create(self) -> None:
        """Find the Sail app, then create the Sailbox and keep its handle.

        Raises:
            StartTimeoutError: The Sailbox did not run within the start timeout.
            SandboxInfraError: An SDK call failed.
        """
        spec = self.spec
        limit = self._start_limit
        try:
            async with asyncio.timeout(limit) as deadline:
                with _mapped("find the Sail app"):
                    app = await App.find.aio(APP_NAME, mint_if_missing=True)
                with _mapped("prepare the Sailbox"):
                    params: dict[str, Any] = {
                        "app": app,
                        "image": Image.from_registry(spec.image or DEFAULT_IMAGE),
                        "name": self._name,
                        "max_lifetime_seconds": math.ceil(spec.lifetime_seconds),
                        "egress_policy": _egress_policy(
                            disabled=spec.network_disabled, allowed_hosts=tuple(spec.allowed_hosts)
                        ),
                        **_size_parameters(spec),
                    }
                    if limit is not None:
                        params["image_build_timeout"] = math.ceil(limit)
                self._create_task = asyncio.ensure_future(Sailbox.create.aio(**params))
                try:
                    with _mapped("create the Sailbox"):
                        # The shield keeps the create running through a cancel,
                        # so the grace below can still get the box handle.
                        self._keep(await asyncio.shield(self._create_task))
                except asyncio.CancelledError:
                    await self._settle_create()
                    raise
        except TimeoutError:
            if not deadline.expired():
                raise
            raise StartTimeoutError(limit) from None

    async def _settle_create(self) -> None:
        """Give a cancelled create ``CREATE_GRACE`` seconds to return the box."""
        task = self._create_task
        if task is None:
            return
        try:
            self._keep(await asyncio.wait_for(task, CREATE_GRACE))
        except (asyncio.CancelledError, Exception) as error:
            task.cancel()
            logger.warning("Sail did not return a cancelled Sailbox create: %s", type(error).__name__)

    def _keep(self, sailbox: Any) -> None:
        """Keep the Sailbox handle and list the box under its job.

        Args:
            sailbox: The SDK Sailbox handle.
        """
        self._sailbox = sailbox
        if self._job_id is not None:
            _JOB_BOXES.setdefault(self._job_id, set()).add(self)

    def _require_sailbox(self) -> Any:
        """Return the Sailbox handle.

        Returns:
            The SDK Sailbox handle.

        Raises:
            SandboxInfraError: The Sailbox is not running.
        """
        if self._sailbox is None:
            raise SandboxInfraError("The Sail sandbox is not running.")
        return self._sailbox

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
        """Run one shell command in the Sailbox and wait for it.

        Args:
            command: The shell command.
            cwd: The working directory; the Sailbox default by default.
            env: Extra environment variables for this command.
            timeout_sec: The time limit; ``None`` uses the run phase timeout.
            on_output: Receives ``(stream, text)`` pairs. Sail does not
                stream, so each stream arrives once, after the command ends.
            sudo: Run the command as root.

        Returns:
            The exit code and the captured output.

        Raises:
            SandboxInfraError: The Sailbox is not running, or the SDK call failed.
            RunTimeoutError: The command ran past the time limit.
        """
        sailbox = self._require_sailbox()
        limit = timeout_sec if timeout_sec is not None else self._default_timeout
        try:
            async with asyncio.timeout(limit + EXEC_GRACE if limit is not None else None):
                with _mapped("run the command"):
                    result = await sailbox.run.aio(
                        _bash_argv(command, cwd),
                        env={**self.spec.env, **(env or {})},
                        timeout=math.ceil(limit) if limit is not None else None,
                        user="root" if sudo else None,
                        output_buffer_bytes=OUTPUT_BUFFER_BYTES,
                    )
        except TimeoutError:
            raise RunTimeoutError(limit) from None
        if result.timed_out:
            raise RunTimeoutError(limit)
        if result.stdout_truncated or result.stderr_truncated:
            logger.warning("Sail kept only the last %d MiB of a command output stream.", OUTPUT_BUFFER_BYTES >> 20)
        sink = on_output or current_output_sink()
        if sink is not None:
            for stream, text in (("stdout", result.stdout), ("stderr", result.stderr)):
                if text:
                    sink(stream, text)
        return ExecResult(exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr)

    async def upload_files(self, files: Mapping[str, str]) -> None:
        """Write UTF-8 text files into the Sailbox, making parent directories.

        Args:
            files: The text of each file, by absolute path.

        Raises:
            SandboxInfraError: The Sailbox is not running, the parent
                directories could not be made, or a write failed.
        """
        sailbox = self._require_sailbox()
        parents = sorted({str(PurePosixPath(path).parent) for path in files} - {".", "/"})
        if parents:
            made = await self.exec("mkdir -p " + " ".join(shlex.quote(parent) for parent in parents))
            if not made.ok:
                raise SandboxInfraError("Sail could not make the upload directories.")
        for path, text in files.items():
            with _mapped("write a file"):
                await sailbox.fs.write.aio(path, io.BytesIO(text.encode("utf-8")))

    async def download_file(self, path: str) -> str | None:
        """Read one UTF-8 text file from the Sailbox.

        Args:
            path: The absolute path of the file.

        Returns:
            The file text, or ``None`` when the file does not exist.

        Raises:
            SandboxInfraError: The Sailbox is not running, or the read failed
                for a file that exists.
        """
        sailbox = self._require_sailbox()
        chunks: list[bytes] = []
        try:
            with _mapped("read a file"):
                async with sailbox.fs.read_stream(path) as stream:
                    async for chunk in stream:
                        chunks.append(chunk)  # noqa: PERF401
        except SandboxInfraError as error:
            if isinstance(error.__cause__, FileNotFoundError):
                return None
            # The SDK error for a missing file is not known, so ask the box.
            probe = await self.exec(f"test -e {shlex.quote(path)}")
            if probe.exit_code == 1:
                return None
            raise
        return b"".join(chunks).decode("utf-8")

    async def _apply_network_policy(self, policy: NetworkPolicy) -> None:
        """Send a new egress policy to the running Sailbox.

        Args:
            policy: The checked policy.

        Raises:
            SandboxInfraError: The Sailbox is not running, or Sail refused the policy.
        """
        sailbox = self._require_sailbox()
        with _mapped("change the egress policy"):
            await sailbox.set_egress_policy.aio(
                _egress_policy(disabled=policy.disabled, allowed_hosts=policy.allowed_hosts)
            )

    async def stop(self) -> None:
        """Terminate the Sailbox once. Safe to call again and before start."""
        if self._stop_task is None:
            self._stop_task = asyncio.ensure_future(self._teardown())
        await asyncio.shield(self._stop_task)

    async def _teardown(self) -> None:
        """End a running start, then terminate the Sailbox."""
        if self._starting is not None and not self._starting.done():
            # A stop during the start: end the create first, so its box handle
            # is known before this teardown looks for it.
            self._starting.cancel()
            with contextlib.suppress(BaseException):
                await self._starting
        sailbox, self._sailbox = self._sailbox, None
        if sailbox is None:
            return
        boxes = _JOB_BOXES.get(self._job_id) if self._job_id is not None else None
        if boxes is not None:
            boxes.discard(self)
            if not boxes:
                del _JOB_BOXES[self._job_id]
        self._terminated = await _bounded(sailbox.terminate.aio(), "terminate the Sailbox")

    @classmethod
    async def stop_job_sandboxes(cls, job_id: str) -> int:
        """Terminate every Sailbox that this process started for the job.

        Args:
            job_id: The job tag value.

        Returns:
            The number of Sailboxes that were terminated. A box that fails to
            terminate is logged and the sweep goes on.
        """
        stopped = 0
        for env in list(_JOB_BOXES.get(job_id, ())):
            await env.stop()
            if env._terminated:
                stopped += 1
        return stopped
