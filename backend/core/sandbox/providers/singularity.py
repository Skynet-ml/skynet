"""Async Singularity / Apptainer sandbox provider: one container instance for each sandbox.

Adapted from Harbor 0.25.0 (Apache-2.0), singularity/singularity.py. Changes:

- The sandbox implements ``SandboxEnvironment``: the CLI runs through
  ``asyncio.create_subprocess_exec`` and the command server answers over
  ``httpx``. A cancel never leaves a container or a command behind.
- No retries. Harbor retries the image pull and the server start with
  tenacity; the Skynet error policy retries at the trial level, so a failed
  start raises once.
- The command server uses only the Python standard library
  (``singularity_server.py``), so the bootstrap installs no venv or pip
  packages. Every request carries a random token, because the server listens
  on the host loopback. The token reaches the container in its environment,
  never on a command line.
- The image cache uses a file lock-free write: each pull writes a partial
  file, and an atomic rename publishes it.
- Commands get their environment and working directory in the request body,
  never on a command line. A timeout or a cancel kills the command's process
  group in the container.
- The server stops itself after the spec lifetime, so a crashed worker does
  not leave the container running for ever.

The container shares the host network (``--containall`` makes no network
namespace), so egress is open and the sandbox can reach services on the host
loopback. Resource limits are not applied. Not for production; the factory
refuses it there.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import secrets
import shutil
import signal
import socket
import subprocess
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import httpx

from ..capabilities import EnvironmentCapabilities, NetworkMode, SandboxSpecError
from ..environment import JOB_TAG, ExecResult, OutputSink, SandboxEnvironment
from ..errors import SandboxFatalError, SandboxInfraError, StartTimeoutError
from ..resources import SandboxSpec

logger = logging.getLogger(__name__)

SINGULARITY_CAPABILITIES = EnvironmentCapabilities(
    name="singularity",
    network_modes=frozenset({NetworkMode.OPEN}),
)

TOKEN_ENV = "SKYNET_SERVER_TOKEN"
TOKEN_HEADER = "X-Skynet-Token"
_HERE = Path(__file__).resolve().parent
_SERVER_FILE = "singularity_server.py"
_BOOTSTRAP_FILE = "singularity_bootstrap.sh"
_GUEST_STAGING = "/staging"
_FALLBACK_BIN = "singularity"
_VERSION_TIMEOUT_SECONDS = 10.0
_HEALTH_TIMEOUT_SECONDS = 5.0
_FILE_TIMEOUT_SECONDS = 60.0
_KILL_TIMEOUT_SECONDS = 10.0
_SHUTDOWN_TIMEOUT_SECONDS = 5.0
_TERM_GRACE_SECONDS = 5.0
_KILL_GRACE_SECONDS = 15.0
_LOG_TAIL_CHARS = 500
# Every live sandbox of this process, for ``stop_job_sandboxes``.
_LIVE: set[SingularityEnvironment] = set()


@dataclass(frozen=True)
class _CliResult:
    """Hold the result of one CLI call."""

    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def error_line(self) -> str:
        """Return the last non-empty stderr line, cut to 500 characters."""
        lines = [line for line in self.stderr.decode("utf-8", errors="replace").splitlines() if line.strip()]
        return lines[-1][:500] if lines else f"exit code {self.returncode}"


async def _kill(process: asyncio.subprocess.Process) -> None:
    """Kill a CLI process and wait for it to end.

    Args:
        process: The process to kill.
    """
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    await process.wait()


async def _cli(argv: Sequence[str], seconds: float) -> _CliResult:
    """Run one CLI command and capture its output.

    Args:
        argv: The program and its arguments.
        seconds: Kill the CLI after this many seconds.

    Returns:
        The exit code and the captured output.

    Raises:
        SandboxInfraError: The CLI is missing or did not finish in time.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as error:
        raise SandboxInfraError(f"The CLI {argv[0]!r} could not start: {error}") from error
    # A cancel waits for the CLI to end, so no pull runs on after the caller left.
    communication = asyncio.ensure_future(process.communicate())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    cancelled: asyncio.CancelledError | None = None
    while not communication.done() and loop.time() < deadline:
        try:
            await asyncio.wait({communication}, timeout=deadline - loop.time())
        except asyncio.CancelledError as error:
            cancelled = error
            communication.cancel()
            await _kill(process)
    if not communication.done():
        communication.cancel()
        await _kill(process)
    with contextlib.suppress(asyncio.CancelledError):
        await communication
    if cancelled is not None:
        raise cancelled
    if communication.cancelled():
        raise SandboxInfraError(f"{Path(argv[0]).name} {argv[1]} did not finish in {seconds:.0f}s.")
    stdout, stderr = communication.result()
    return _CliResult(process.returncode or 0, stdout, stderr)


def _kill_group(pgid: int, sig: signal.Signals) -> None:
    """Send a signal to a process group, ignoring a group that is gone.

    Args:
        pgid: The process group id.
        sig: The signal.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


def _free_port() -> int:
    """Return a TCP port on 127.0.0.1 that is free now.

    Another process can take the port before the server binds it; the server
    then exits and the start fails once.

    Returns:
        The port number.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _find_bin(preferred: str) -> str:
    """Return the CLI path: the preferred name, then ``singularity``.

    Args:
        preferred: The preferred CLI name or path.

    Returns:
        The path found on PATH, or the preferred name when neither is there.
    """
    return shutil.which(preferred) or shutil.which(_FALLBACK_BIN) or preferred


class SingularityEnvironment(SandboxEnvironment):
    """Run commands in a Singularity / Apptainer container through a command server."""

    capabilities: ClassVar[EnvironmentCapabilities] = SINGULARITY_CAPABILITIES
    default_bin: ClassVar[str] = "apptainer"
    default_workdir: ClassVar[str] = "/app"
    image_cache_dir: ClassVar[str] = str(Path(tempfile.gettempdir()) / "skynet-singularity-images")
    ready_seconds: ClassVar[float] = 300.0
    poll_seconds: ClassVar[float] = 0.5
    pull_seconds: ClassVar[float] = 900.0

    def __init__(
        self,
        spec: SandboxSpec,
        *,
        image: str | None = None,
        workdir: str | None = None,
        singularity_bin: str | None = None,
        fakeroot: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Check the spec and keep the container options.

        Args:
            spec: The sandbox to open.
            image: A local ``.sif`` file or an image reference; the default is
                ``spec.image``. A reference without a scheme is a Docker image.
            workdir: The working directory in the container.
            singularity_bin: The CLI; the default is ``apptainer``, then
                ``singularity``.
            fakeroot: Run as root in a user namespace, so the image's root
                directories are writable.
            transport: An ``httpx`` transport for the command server, for tests.

        Raises:
            SandboxSpecError: The spec names no image or asks for what the
                provider cannot do.
        """
        super().__init__(spec)
        chosen = image or spec.image
        if not chosen:
            raise SandboxSpecError("The Singularity sandbox needs an image.")
        self._image = chosen
        self._workdir = workdir or self.default_workdir
        self._bin = singularity_bin or _find_bin(self.default_bin)
        self._fakeroot = fakeroot
        self._transport = transport
        self._staging: Path | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._client: httpx.AsyncClient | None = None
        self._stopped = False

    @classmethod
    def preflight(cls) -> None:
        """Check that ``apptainer`` or ``singularity`` is on PATH and answers.

        Raises:
            SandboxFatalError: The CLI is missing or ``version`` fails.
        """
        path = shutil.which(cls.default_bin) or shutil.which(_FALLBACK_BIN)
        if path is None:
            raise SandboxFatalError(
                f"The Singularity sandbox needs the {cls.default_bin!r} or {_FALLBACK_BIN!r} CLI on PATH."
            )
        try:
            result = subprocess.run(
                [path, "version"], capture_output=True, timeout=_VERSION_TIMEOUT_SECONDS, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SandboxFatalError(f"{path} version did not answer: {error}") from error
        if result.returncode != 0:
            detail = _CliResult(result.returncode, result.stdout, result.stderr).error_line
            raise SandboxFatalError(f"The Singularity CLI {path!r} does not work. ({detail})")

    async def start(self) -> None:
        """Pull the image when needed, start the container and wait for its server.

        A failure or a cancel stops what the start made, then reaches the caller.

        Raises:
            RuntimeError: The sandbox was already started.
            SandboxInfraError: The pull or the container failed.
            StartTimeoutError: The server did not answer in ``ready_seconds``.
        """
        if self._staging is not None:
            raise RuntimeError("The Singularity sandbox was already started.")
        self._staging = Path(tempfile.mkdtemp(prefix="skynet-singularity-"))
        _LIVE.add(self)
        try:
            await self._boot(self._staging)
        except BaseException:
            await self.stop()
            raise

    async def _boot(self, staging: Path) -> None:
        """Start the container process and wait until its server answers.

        Args:
            staging: The host directory that the container sees as ``/staging``.

        Raises:
            SandboxInfraError: The pull failed, or the container did not start
                or exited early.
            StartTimeoutError: The server did not answer in ``ready_seconds``.
        """
        shutil.copy2(_HERE / _SERVER_FILE, staging / _SERVER_FILE)
        shutil.copy2(_HERE / _BOOTSTRAP_FILE, staging / _BOOTSTRAP_FILE)
        sif = await self._resolve_image()
        port = _free_port()
        token = secrets.token_urlsafe(32)
        argv = self._argv(staging, sif, port)
        env = {**os.environ, f"{self._env_prefix()}{TOKEN_ENV}": token}
        log = await asyncio.to_thread((staging / "server.log").open, "wb")
        with log:
            spawn = asyncio.ensure_future(
                asyncio.create_subprocess_exec(
                    *argv,
                    env=env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
            )
            try:
                self._process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                # A cancelled spawn would leave the container running with
                # nobody to stop it. Wait for it, so stop() can end its group.
                # A second cancel must not end the wait early.
                while not spawn.done():
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.wait({spawn})
                if not spawn.cancelled() and spawn.exception() is None:
                    self._process = spawn.result()
                raise
            except OSError as error:
                raise SandboxInfraError(f"The Singularity CLI {self._bin!r} could not start: {error}") from error
        self._client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers={TOKEN_HEADER: token},
            transport=self._transport,
        )
        await self._wait_ready(staging)

    def _argv(self, staging: Path, sif: str, port: int) -> list[str]:
        """Return the CLI command that starts the container and its server.

        Args:
            staging: The host directory with the server files.
            sif: The image file.
            port: The server port on 127.0.0.1.

        Returns:
            The argument list.
        """
        argv = [
            self._bin,
            "exec",
            "--containall",
            "--no-mount",
            "bind-paths",
            "--writable-tmpfs",
        ]
        if self._fakeroot:
            argv.append("--fakeroot")
        argv += [
            "-B",
            f"{staging}:{_GUEST_STAGING}",
            sif,
            "bash",
            f"{_GUEST_STAGING}/{_BOOTSTRAP_FILE}",
            self._workdir,
            "--port",
            str(port),
            "--lifetime-seconds",
            str(self.spec.lifetime_seconds),
        ]
        return argv

    def _env_prefix(self) -> str:
        """Return the prefix that passes a host variable into the container.

        ``--containall`` cleans the environment; only prefixed variables pass.
        Apptainer warns about the old ``SINGULARITYENV_`` prefix.

        Returns:
            ``APPTAINERENV_`` for Apptainer, else ``SINGULARITYENV_``.
        """
        return "APPTAINERENV_" if "apptainer" in Path(self._bin).name else "SINGULARITYENV_"

    async def _resolve_image(self) -> str:
        """Return a local ``.sif`` file for the image, pulling it once into the cache.

        Returns:
            The image file path.

        Raises:
            SandboxInfraError: The pull failed.
        """
        if self._image.endswith(".sif") and await asyncio.to_thread(Path(self._image).is_file):
            return self._image
        source = self._image if "://" in self._image else f"docker://{self._image}"
        cache = Path(self.image_cache_dir)
        await asyncio.to_thread(cache.mkdir, parents=True, exist_ok=True)
        target = cache / f"{hashlib.sha256(source.encode('utf-8')).hexdigest()}.sif"
        if target.is_file():
            return str(target)
        # The CLI refuses an existing destination, so the partial file must not
        # exist yet. The rename makes a finished image appear at once.
        partial = cache / f"{target.stem}.{uuid.uuid4().hex}.partial.sif"
        try:
            result = await _cli([self._bin, "pull", str(partial), source], self.pull_seconds)
            if result.returncode != 0:
                raise SandboxInfraError(f"The Singularity pull of {source} failed: {result.error_line}")
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)
        return str(target)

    async def _wait_ready(self, staging: Path) -> None:
        """Poll the server's ``/health`` until it answers.

        Args:
            staging: The host directory with the server log.

        Raises:
            SandboxInfraError: The container exited first.
            StartTimeoutError: The server did not answer in ``ready_seconds``.
        """
        assert self._process is not None
        assert self._client is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.ready_seconds
        while True:
            if self._process.returncode is not None:
                raise SandboxInfraError(
                    f"The Singularity container exited with code {self._process.returncode} before it was ready: "
                    f"{self._log_tail(staging)}"
                )
            with contextlib.suppress(httpx.TransportError):
                response = await self._client.get("/health", timeout=_HEALTH_TIMEOUT_SECONDS)
                if response.status_code == 200:
                    return
            if loop.time() >= deadline:
                raise StartTimeoutError(self.ready_seconds)
            await asyncio.sleep(self.poll_seconds)

    @staticmethod
    def _log_tail(staging: Path) -> str:
        """Return the end of the container log.

        Args:
            staging: The host directory with the server log.

        Returns:
            The last characters of the log, or a note that it is empty.
        """
        try:
            text = (staging / "server.log").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            text = ""
        return text[-_LOG_TAIL_CHARS:] or "the log is empty"

    async def stop(self) -> None:
        """Stop the server and the container. Do nothing before start or after a stop.

        A cancel waits until the stop ends, then reaches the caller, so a
        cancelled stop never leaves the container running.

        Raises:
            asyncio.CancelledError: The task was cancelled during the stop.
        """
        if self._staging is None or self._stopped:
            return
        self._stopped = True
        teardown = asyncio.ensure_future(self._teardown(self._staging))
        cancelled: asyncio.CancelledError | None = None
        while not teardown.done():
            try:
                await asyncio.wait({teardown})
            except asyncio.CancelledError as error:
                cancelled = error
        if cancelled is not None:
            raise cancelled
        teardown.result()

    async def _teardown(self, staging: Path) -> None:
        """Shut the server down, end the container's process group and remove the staging files.

        Args:
            staging: The host directory with the server files.
        """
        try:
            process = self._process
            if process is not None and process.returncode is None:
                if self._client is not None:
                    with contextlib.suppress(httpx.HTTPError):
                        await self._client.post("/shutdown", timeout=_SHUTDOWN_TIMEOUT_SECONDS)
                _kill_group(process.pid, signal.SIGTERM)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), _TERM_GRACE_SECONDS)
                # The leader was running when the stop began, so the group id
                # is still ours while any member is left.
                _kill_group(process.pid, signal.SIGKILL)
                await process.wait()
        finally:
            if self._client is not None:
                await self._client.aclose()
            await asyncio.to_thread(shutil.rmtree, staging, True)
            _LIVE.discard(self)

    def _require_running(self) -> httpx.AsyncClient:
        """Return the server client of a started, not stopped sandbox.

        Returns:
            The client.

        Raises:
            RuntimeError: The sandbox is not running.
        """
        if self._client is None or self._stopped:
            raise RuntimeError("The Singularity sandbox is not running.")
        return self._client

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
        """Run one shell command in the container and wait for it.

        Args:
            command: The shell command, run by ``bash --noprofile --norc -c``.
            cwd: The working directory; the default is the sandbox workdir.
            env: Extra environment variables for this command.
            timeout_sec: After this long, kill the command's process group and
                return ``timed_out=True``; ``None`` sets no limit.
            on_output: Receives the ``(stream, text)`` output after the command ends.
            sudo: No effect: the command runs as the container user.

        Returns:
            The exit code, the captured output and whether the timeout fired.

        Raises:
            SandboxInfraError: The server did not answer, refused the request,
                or ran past the kill deadline. A timeout, a cancel or an error
                kills the command's process group first.
        """
        del sudo
        client = self._require_running()
        request_id = uuid.uuid4().hex
        body = {
            "id": request_id,
            "command": command,
            "cwd": cwd,
            "env": {**self.spec.env, **(env or {})},
            "timeout": timeout_sec,
        }
        limit = None if timeout_sec is None else timeout_sec + _KILL_GRACE_SECONDS
        try:
            response = await client.post("/exec", json=body, timeout=limit)
        # httpx timeouts are not builtin TimeoutError, so they cannot pass for
        # a run timeout; a server past its own kill deadline is an infra fault.
        except httpx.TimeoutException as error:
            await self._kill_command(client, request_id)
            raise SandboxInfraError(
                f"The command still ran {_KILL_GRACE_SECONDS:.0f}s past its {timeout_sec}s timeout: {command[:200]}"
            ) from error
        except httpx.TransportError as error:
            await self._kill_command(client, request_id)
            raise SandboxInfraError(f"The Singularity sandbox server did not answer: {error}") from error
        except BaseException:
            await self._kill_command(client, request_id)
            raise
        data = self._payload(response, "/exec")
        stdout = str(data.get("stdout", ""))
        stderr = str(data.get("stderr", ""))
        if on_output is not None:
            if stdout:
                on_output("stdout", stdout)
            if stderr:
                on_output("stderr", stderr)
        return ExecResult(
            exit_code=int(data["exit_code"]),
            stdout=stdout,
            stderr=stderr,
            timed_out=bool(data.get("timed_out", False)),
        )

    async def _kill_command(self, client: httpx.AsyncClient, request_id: str) -> None:
        """Ask the server to kill one command's process group. Log a failure.

        Args:
            client: The server client.
            request_id: The id that the ``/exec`` request sent.
        """
        try:
            await client.post("/kill", json={"id": request_id}, timeout=_KILL_TIMEOUT_SECONDS)
        except Exception as error:
            logger.warning("Could not kill a command in the Singularity sandbox: %s", error)

    @staticmethod
    def _payload(response: httpx.Response, route: str) -> dict[str, Any]:
        """Return the JSON body of a successful server answer.

        Args:
            response: The server answer.
            route: The route, for the error message.

        Returns:
            The JSON body.

        Raises:
            SandboxInfraError: The server answered with an error.
        """
        if response.status_code != 200:
            raise SandboxInfraError(
                f"The Singularity sandbox server refused {route} ({response.status_code}): {response.text[:500]}"
            )
        return response.json()

    async def _call(self, route: str, body: dict[str, Any]) -> dict[str, Any]:
        """Send one file request to the server.

        Args:
            route: The route.
            body: The JSON request.

        Returns:
            The JSON answer.

        Raises:
            SandboxInfraError: The server did not answer or refused the request.
        """
        client = self._require_running()
        try:
            response = await client.post(route, json=body, timeout=_FILE_TIMEOUT_SECONDS)
        except httpx.HTTPError as error:
            raise SandboxInfraError(f"The Singularity sandbox server did not answer {route}: {error}") from error
        return self._payload(response, route)

    async def upload_files(self, files: Mapping[str, str]) -> None:
        """Write text files into the container, creating parent directories.

        Args:
            files: Maps each sandbox path to its text content. A relative
                path is relative to the sandbox workdir.

        Raises:
            SandboxInfraError: The server refused a file.
        """
        for path, content in files.items():
            await self._call("/upload", {"path": path, "content": content})

    async def download_file(self, path: str) -> str | None:
        """Read one text file from the container.

        Args:
            path: The sandbox path. A relative path is relative to the sandbox workdir.

        Returns:
            The file content, or ``None`` when the file does not exist.

        Raises:
            SandboxInfraError: The server refused the request.
        """
        data = await self._call("/download", {"path": path})
        return str(data["content"]) if data.get("found") else None

    @classmethod
    async def stop_job_sandboxes(cls, job_id: str) -> int:
        """Stop every live sandbox of this process that carries the job tag.

        Args:
            job_id: The value of the ``JOB_TAG`` tag.

        Returns:
            The number of sandboxes that the sweep stopped.
        """
        stopped = 0
        for box in [box for box in _LIVE if box.spec.tags.get(JOB_TAG) == job_id]:
            try:
                await box.stop()
            except Exception as error:
                logger.warning("Could not stop a Singularity sandbox of job %s: %s", job_id, error)
                continue
            stopped += 1
        return stopped
