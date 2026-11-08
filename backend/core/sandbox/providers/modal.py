"""The async Modal sandbox provider, for dev and evaluation only.

Adapted from Harbor 0.25.0 (Apache-2.0), harbor/environments/modal.py: the
``Sandbox.create`` call, the CPU and memory request and limit tuples, the GPU
string, the egress allowlist split into domains and CIDRs, the dynamic
network policy, the exit-137 out-of-memory check and the terminate flow come
from it. Changes: the provider implements the SBX-6 async contract, the SDK
calls never retry (SKY-406 D3, D10), the factory refuses the provider in
production (SKY-406 D1), and the job sweep lists sandboxes by the job tag.
Harbor's Docker-in-Docker compose mode, SSH streaming, volumes, Dockerfile
and registry builds, directory transfer and region pinning are left out.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import math
import os
import tempfile
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any, ClassVar

from ..capabilities import EnvironmentCapabilities, NetworkMode, SandboxSpecError
from ..environment import JOB_TAG, ExecResult, NetworkPolicy, OutputSink, SandboxEnvironment
from ..errors import RunTimeoutError, SandboxFatalError, SandboxInfraError
from ..resources import ResourceMode, SandboxSpec
from ..telemetry import current_output_sink
from ..timeouts import Phase, PhaseTimeouts

try:
    from modal import App, Image, Sandbox, Secret
except ImportError:
    App = None
    Image = None
    Sandbox = None
    Secret = None

try:
    from modal.exception import ExecTimeoutError, SandboxFilesystemNotFoundError
except ImportError:
    ExecTimeoutError = None
    SandboxFilesystemNotFoundError = None

logger = logging.getLogger(__name__)

# Modal filters egress by domain and CIDR and can replace the policy of a
# running sandbox, so all three modes work. Modal sets no hard vCPU limit here.
MODAL_CAPABILITIES = EnvironmentCapabilities(
    name="modal",
    network_modes=frozenset(NetworkMode),
    dynamic_network_policy=True,
    gpus=True,
)

DEFAULT_IMAGE = "python:3.11-slim"
DEFAULT_APP_NAME = "skynet-sandbox"
# Modal ends every sandbox after 24 hours.
_MAX_LIFETIME_SEC = 24 * 60 * 60
_DEFAULT_CPU_REQUEST_CORES = 0.125
_DEFAULT_MEMORY_REQUEST_MB = 128
_OOM_EXIT_CODE = 137
# Modal's own exec timeout ends the command first; this bound only stops a
# lost connection from blocking exec past the limit.
_TIMEOUT_GRACE_SEC = 30.0
# Modal's wait() returns -1 instead of raising when its exec deadline passes.
_MODAL_TIMEOUT_EXIT_CODE = -1

_NOT_FOUND_ERRORS: tuple[type[BaseException], ...] = tuple(
    error for error in (SandboxFilesystemNotFoundError, FileNotFoundError) if error is not None
)
_EXEC_TIMEOUT_ERRORS: tuple[type[BaseException], ...] = tuple(
    error for error in (ExecTimeoutError,) if error is not None
)


class ModalEnvironment(SandboxEnvironment):
    """Run a sandbox as one Modal sandbox that sleeps until commands arrive."""

    capabilities: ClassVar[EnvironmentCapabilities] = MODAL_CAPABILITIES

    def __init__(
        self,
        spec: SandboxSpec,
        *,
        app_name: str = DEFAULT_APP_NAME,
        idle_timeout_sec: int | None = None,
        timeouts: PhaseTimeouts | None = None,
    ) -> None:
        """Create a stopped Modal sandbox.

        Args:
            spec: The sandbox to open. ``spec.image`` is a registry image;
                ``None`` uses ``DEFAULT_IMAGE``.
            app_name: The Modal app that owns the sandboxes.
            idle_timeout_sec: Modal ends the sandbox after this many idle
                seconds; ``None`` keeps the Modal default.
            timeouts: The phase timeouts; the run phase is the default
                ``exec`` time limit. ``None`` means no default limit.

        Raises:
            SandboxSpecError: The spec asks for a lifetime or a disk size that
                Modal cannot apply, or for another unsupported feature.
        """
        super().__init__(spec)
        if spec.lifetime_seconds > _MAX_LIFETIME_SEC:
            raise SandboxSpecError(f"The modal sandbox lives at most {_MAX_LIFETIME_SEC} seconds.")
        if spec.resources.disk_mb is not None and spec.resources.mode is not ResourceMode.IGNORE:
            raise SandboxSpecError("The modal sandbox cannot set a disk size.")
        self._app_name = app_name
        self._idle_timeout_sec = idle_timeout_sec
        self._default_timeout = timeouts.seconds(Phase.RUN) if timeouts is not None else None
        self._sandbox: Any = None
        # Modal reports an explicit terminate as exit 137 too, so a failure
        # after our own terminate must not read as an out-of-memory kill.
        self._termination_requested = False

    @classmethod
    def preflight(cls) -> None:
        """Check that the Modal SDK and Modal credentials are present.

        Raises:
            SandboxFatalError: The ``modal`` package is missing, or no token
                is in the environment or in ``~/.modal.toml``.
        """
        _require_sdk()
        has_env_token = bool(os.environ.get("MODAL_TOKEN_ID")) and bool(os.environ.get("MODAL_TOKEN_SECRET"))
        if not has_env_token and not (Path.home() / ".modal.toml").is_file():
            raise SandboxFatalError(
                "The modal sandbox needs MODAL_TOKEN_ID and MODAL_TOKEN_SECRET, or a ~/.modal.toml file."
            )

    async def start(self) -> None:
        """Create the Modal sandbox. Do nothing when the sandbox already runs.

        A cancel during the create waits for the create to finish, also
        through repeat cancels, terminates the new sandbox, then passes the
        cancel on, so no sandbox leaks.
        """
        if self._sandbox is not None:
            return
        _require_sdk()
        create = asyncio.ensure_future(self._create())
        try:
            sandbox = await asyncio.shield(create)
        except asyncio.CancelledError:
            # Each await is shielded, so a repeat cancel cannot cancel a create
            # that Modal may already have done on its side.
            while not create.done():
                try:
                    await asyncio.shield(create)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if create.cancelled() or create.exception() is not None:
                raise
            sandbox = create.result()
            try:
                await asyncio.shield(_terminate(sandbox))
            except Exception:
                # The cancel must still reach the caller; the sweep can stop a leaked box.
                logger.exception("Could not stop a modal sandbox after a cancelled start.")
            raise
        self._sandbox = sandbox
        self._termination_requested = False

    async def stop(self) -> None:
        """Terminate the Modal sandbox and wait for it to end.

        A cancel during the terminate waits for the terminate to finish, then
        passes the cancel on.
        """
        sandbox = self._sandbox
        if sandbox is None:
            return
        self._sandbox = None
        self._termination_requested = True
        terminate = asyncio.ensure_future(_terminate(sandbox))
        try:
            await asyncio.shield(terminate)
        except asyncio.CancelledError:
            await terminate
            raise

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
        """Run one bash command in the Modal sandbox.

        Args:
            command: The shell command.
            cwd: The working directory, or the image default.
            env: Extra environment variables, sent as a Modal secret.
            timeout_sec: The time limit; ``None`` uses the run phase timeout.
            on_output: Receives ``(stream, text)`` chunks while the command
                runs; ``None`` uses the sink of the current task.
            sudo: Ignored: Modal runs commands as root.

        Returns:
            The exit code and the captured output.

        Raises:
            RuntimeError: The sandbox is not running.
            RunTimeoutError: The command ran past the time limit.
            SandboxInfraError: The sandbox died with exit 137, likely out of memory.
        """
        sandbox = self._require_running()
        limit = timeout_sec if timeout_sec is not None else self._default_timeout
        sink = on_output or current_output_sink()
        try:
            async with asyncio.timeout(None if limit is None else limit + _TIMEOUT_GRACE_SEC) as deadline:
                exit_code, stdout, stderr = await self._run(sandbox, command, cwd, env, limit, sink)
        except Exception as exc:
            # Modal's ExecTimeoutError derives from modal.exception.Error, not
            # from the builtin TimeoutError, so it needs its own check.
            timed_out = isinstance(exc, _EXEC_TIMEOUT_ERRORS) or (isinstance(exc, TimeoutError) and deadline.expired())
            if limit is not None and timed_out:
                raise RunTimeoutError(limit) from None
            await self._raise_if_out_of_memory(exc)
            raise
        if limit is not None and exit_code == _MODAL_TIMEOUT_EXIT_CODE:
            raise RunTimeoutError(limit)
        return ExecResult(exit_code=exit_code, stdout=stdout, stderr=stderr)

    async def upload_files(self, files: Mapping[str, str]) -> None:
        """Write UTF-8 text files into the sandbox. Modal creates the parent directories.

        Args:
            files: Maps each absolute sandbox path to its text content.

        Raises:
            RuntimeError: The sandbox is not running.
        """
        sandbox = self._require_running()
        with tempfile.TemporaryDirectory(prefix="skynet-modal-") as tmp:
            for index, (path, text) in enumerate(files.items()):
                local = Path(tmp) / str(index)
                local.write_bytes(text.encode("utf-8"))
                await sandbox.filesystem.copy_from_local.aio(local, path)

    async def download_file(self, path: str) -> str | None:
        """Read one UTF-8 text file from the sandbox.

        Args:
            path: The absolute sandbox path.

        Returns:
            The exact file content, or ``None`` when the file does not exist.

        Raises:
            RuntimeError: The sandbox is not running.
        """
        sandbox = self._require_running()
        with tempfile.TemporaryDirectory(prefix="skynet-modal-") as tmp:
            local = Path(tmp) / "file"
            try:
                await sandbox.filesystem.copy_to_local.aio(path, local)
            except _NOT_FOUND_ERRORS:
                return None
            # Bytes, not read_text: universal newlines would turn "\r\n" into "\n".
            return local.read_bytes().decode("utf-8")

    @classmethod
    async def stop_job_sandboxes(cls, job_id: str, *, app_name: str = DEFAULT_APP_NAME) -> int:
        """Terminate every Modal sandbox of the app that carries the job tag.

        Args:
            job_id: The value of the ``JOB_TAG`` tag.
            app_name: The Modal app that owns the sandboxes.

        Returns:
            The number of sandboxes that the sweep stopped.
        """
        _require_sdk()
        app = await App.lookup.aio(app_name, create_if_missing=True)
        stopped = 0
        async for sandbox in Sandbox.list.aio(app_id=app.app_id, tags={JOB_TAG: job_id}):
            try:
                await _terminate(sandbox)
            except Exception:
                logger.exception("Could not stop a modal sandbox of job %s.", job_id)
                continue
            stopped += 1
        return stopped

    async def _apply_network_policy(self, policy: NetworkPolicy) -> None:
        """Replace the egress policy of the running sandbox.

        Args:
            policy: The checked policy.

        Raises:
            RuntimeError: The sandbox is not running.
        """
        sandbox = self._require_running()
        await sandbox._experimental_set_outbound_network_policy.aio(**egress_kwargs(policy))

    async def _create(self) -> Any:
        """Look up the app and create one sandbox that sleeps until it is stopped.

        Returns:
            The Modal sandbox.
        """
        spec = self.spec
        app = await App.lookup.aio(self._app_name, create_if_missing=True)
        kwargs: dict[str, Any] = {
            **resource_kwargs(spec),
            **egress_kwargs(self._network_policy or NetworkPolicy(disabled=True)),
        }
        if spec.tags:
            kwargs["tags"] = dict(spec.tags)
        if spec.env:
            kwargs["secrets"] = [Secret.from_dict(dict(spec.env))]
        if spec.name:
            kwargs["name"] = spec.name
        if self._idle_timeout_sec is not None:
            kwargs["idle_timeout"] = self._idle_timeout_sec
        return await Sandbox.create.aio(
            "sleep",
            "infinity",
            app=app,
            image=Image.from_registry(spec.image or DEFAULT_IMAGE),
            timeout=math.ceil(spec.lifetime_seconds),
            **kwargs,
        )

    async def _run(
        self,
        sandbox: Any,
        command: str,
        cwd: str | None,
        env: Mapping[str, str] | None,
        limit: float | None,
        sink: OutputSink | None,
    ) -> tuple[int, str, str]:
        """Start one command, collect both streams and wait for the exit code.

        Args:
            sandbox: The running Modal sandbox.
            command: The shell command.
            cwd: The working directory, or ``None``.
            env: Extra environment variables, or ``None``.
            limit: The time limit in seconds, or ``None``.
            sink: Receives output chunks, or ``None``.

        Returns:
            The exit code, the stdout text and the stderr text.
        """
        process = await sandbox.exec.aio(
            "bash",
            "-c",
            command,
            workdir=cwd,
            secrets=[Secret.from_dict(dict(env))] if env else [],
            timeout=None if limit is None else max(1, math.ceil(limit)),
        )
        stdout, stderr = await asyncio.gather(
            _collect(process.stdout, "stdout", sink), _collect(process.stderr, "stderr", sink)
        )
        exit_code = await process.wait.aio()
        return exit_code, stdout, stderr

    async def _raise_if_out_of_memory(self, cause: BaseException) -> None:
        """Raise an infra error when a failed SDK call comes from a sandbox that died with 137.

        Args:
            cause: The error of the failed SDK call.

        Raises:
            SandboxInfraError: The sandbox ended with exit 137 and nobody asked
                for its termination.
        """
        sandbox = self._sandbox
        if sandbox is None or self._termination_requested:
            return
        try:
            exit_code = await sandbox.poll.aio()
        except Exception:
            # The original error is the better signal when Modal cannot report a status.
            logger.debug("Could not read the modal sandbox status after a failed call.", exc_info=True)
            return
        if exit_code == _OOM_EXIT_CODE:
            memory = self.spec.resources.memory_mb
            raise SandboxInfraError(
                f"The modal sandbox likely ran out of memory (exit code 137, memory {memory or 'default'} MB)."
            ) from cause

    def _require_running(self) -> Any:
        """Return the Modal sandbox, or raise when the sandbox is stopped.

        Returns:
            The Modal sandbox.

        Raises:
            RuntimeError: The sandbox is not running.
        """
        if self._sandbox is None:
            raise RuntimeError("The modal sandbox is not running.")
        return self._sandbox


def resource_kwargs(spec: SandboxSpec) -> dict[str, Any]:
    """Map the requested sizes to the Modal ``cpu``, ``memory`` and ``gpu`` arguments.

    Args:
        spec: The sandbox spec.

    Returns:
        The keyword arguments for ``Sandbox.create``.
    """
    request = spec.resources
    kwargs: dict[str, Any] = {}
    if request.gpu:
        kwargs["gpu"] = f"any:{request.gpu}"
    if request.mode is ResourceMode.IGNORE:
        return kwargs
    cpus = request.cpus
    if request.mode is ResourceMode.REQUEST:
        kwargs["cpu"] = cpus
    elif request.mode is ResourceMode.LIMIT:
        # A small scheduling request with a hard ceiling at the requested size.
        kwargs["cpu"] = (min(_DEFAULT_CPU_REQUEST_CORES, cpus), cpus)
    else:
        kwargs["cpu"] = (cpus, cpus)
    memory = request.memory_mb
    if memory is not None:
        if request.mode in (ResourceMode.AUTO, ResourceMode.REQUEST):
            kwargs["memory"] = memory
        elif request.mode is ResourceMode.LIMIT:
            kwargs["memory"] = (min(_DEFAULT_MEMORY_REQUEST_MB, memory), memory)
        else:
            kwargs["memory"] = (memory, memory)
    return kwargs


def egress_kwargs(policy: NetworkPolicy) -> dict[str, list[str]]:
    """Map an egress policy to the Modal domain and CIDR allowlists.

    Every mode sends both lists, so the sandbox stays in Modal's allowlist
    mode and a later policy change can always replace it.

    Args:
        policy: The egress policy.

    Returns:
        The ``outbound_domain_allowlist`` and ``outbound_cidr_allowlist``
        arguments.
    """
    if policy.disabled:
        return {"outbound_domain_allowlist": [], "outbound_cidr_allowlist": []}
    if not policy.allowed_hosts:
        return {"outbound_domain_allowlist": ["*"], "outbound_cidr_allowlist": ["0.0.0.0/0"]}
    domains: list[str] = []
    cidrs: list[str] = []
    for host in policy.allowed_hosts:
        try:
            cidrs.append(str(ipaddress.ip_network(host, strict=False)))
        except ValueError:
            domains.append(host)
    return {"outbound_domain_allowlist": domains, "outbound_cidr_allowlist": cidrs}


def _require_sdk() -> None:
    """Raise when the Modal SDK is not installed.

    Raises:
        SandboxFatalError: The ``modal`` package is missing.
    """
    if Sandbox is None:
        raise SandboxFatalError("The modal sandbox needs the modal package: install the `modal` extra.")


async def _terminate(sandbox: Any) -> None:
    """Terminate one Modal sandbox and wait until it ends.

    Args:
        sandbox: The Modal sandbox.
    """
    await sandbox.terminate.aio()
    await sandbox.wait.aio(raise_on_termination=False)


async def _collect(stream: AsyncIterator[str], name: str, sink: OutputSink | None) -> str:
    """Read one output stream to its end and pass each chunk to the sink.

    Args:
        stream: The Modal stream reader.
        name: ``stdout`` or ``stderr``.
        sink: Receives ``(name, chunk)``, or ``None``.

    Returns:
        The whole stream text.
    """
    chunks: list[str] = []
    async for chunk in stream:
        chunks.append(chunk)
        if sink is not None:
            sink(name, chunk)
    return "".join(chunks)
