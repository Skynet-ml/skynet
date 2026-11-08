"""Async Docker sandbox provider for local development, with an egress proxy for allowlists.

Ported from Skynet's sync ``blackbox/docker_sandbox.py``. Changes: every
Docker CLI call runs through ``asyncio.create_subprocess_exec`` instead of
``subprocess`` and pump threads, the box implements ``SandboxEnvironment``,
the network policy can change after start (deny-all and allowlist), and the
proxy call record is left out.

Harbor runs a compose project with an egress sidecar. This provider keeps
Skynet's per-box proxy container instead: it is the same sidecar pattern
without compose, it runs on the box's own image (Docker pulls no other
image), and ``docker_egress_proxy.py`` is already hardened and tested.

Open egress is refused: Docker has no edge policy, and its default bridge
reaches services that listen only on the host's loopback. A box is offline,
or it reaches only its allowlisted hosts on port 443 through the proxy.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import io
import json
import logging
import math
import posixpath
import re
import tarfile
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..capabilities import EnvironmentCapabilities, NetworkMode, SandboxSpecError
from ..environment import JOB_TAG, ExecResult, NetworkPolicy, OutputSink, SandboxEnvironment
from ..errors import SandboxInfraError
from ..resources import ResourceMode, SandboxSpec
from ..transfer import unpack

logger = logging.getLogger(__name__)

DOCKER_CAPABILITIES = EnvironmentCapabilities(
    name="docker",
    network_modes=frozenset({NetworkMode.DENY_ALL, NetworkMode.ALLOWLIST}),
    dynamic_network_policy=True,
    max_vcpus=32,
    even_vcpus=True,
    memory_mb_per_vcpu=2048,
)

DEFAULT_DOCKER_BIN = "docker"
WORKSPACE_DIR = "/workspace"
DOCKER_JOB_LABEL = "skynet.job"
# The legacy runtime uses the same label, so one idle-network sweep and one
# job sweep cover the boxes of both runtimes while callers move over.
SANDBOX_LABELS: Mapping[str, str] = {"skynet": "blackbox"}
ROLE_LABEL = "skynet.role"
EGRESS_ROLE = "egress"

READY_MARKER = "skynet-egress-proxy-ready"
_EGRESS_PROXY_SOURCE = (
    Path(__file__).resolve().parents[2] / "service_gateway/optimization/blackbox/docker_egress_proxy.py"
).read_text(encoding="utf-8")

_TMPFS_MOUNTS = (
    f"{WORKSPACE_DIR}:rw,exec,nosuid,mode=1777,size=4g",
    "/tmp:rw,exec,nosuid,mode=1777,size=1g",
    "/etc/codex:rw,noexec,nosuid,mode=0755,size=1m",
)
_PIDS_LIMIT = 1024
_CLI_TIMEOUT_SECONDS = 60.0
_KILL_GRACE_SECONDS = 15
_TIMEOUT_EXIT_CODES = frozenset({124, 137})
_READ_CHUNK_BYTES = 65536
_SHELL = ("bash", "--noprofile", "--norc", "-c")
# Reads NUL-terminated NAME=value pairs from stdin, so secrets never appear
# on a command line that ``ps`` or ``docker inspect`` shows.
_ENV_LOADER = 'pairs=(); while IFS= read -r -d "" pair; do pairs+=("$pair"); done; exec env -- "${pairs[@]}" "$@"'
_MISSING_FILE_EXIT = 3
_PACK_FILE_SCRIPT = (
    f'test -f "$1" || exit {_MISSING_FILE_EXIT}; '
    'cd -- "$(dirname -- "$1")" && exec tar -c -h -f - -- "$(basename -- "$1")"'
)
_UPLINK_NETWORK = "bridge"
_EGRESS_PORT = 3128
_EGRESS_READY_SECONDS = 30.0
_EGRESS_POLL_SECONDS = 0.2
_EGRESS_USER = "65534:65534"
_IDLE_NETWORK_MIN_AGE_SECONDS = 300
_NAME_UNSAFE = re.compile(r"[^a-z0-9-]+")
_NAME_STEM_CHARS = 40


@dataclass(frozen=True)
class _CliResult:
    """Hold the result of one Docker CLI call."""

    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def error_line(self) -> str:
        """Return the last non-empty stderr line, cut to 500 characters."""
        lines = [line for line in self.stderr.decode("utf-8", errors="replace").splitlines() if line.strip()]
        return lines[-1][:500] if lines else f"exit code {self.returncode}"


@dataclass(frozen=True)
class _Egress:
    """Name the internal network and the proxy container behind one box."""

    network: str
    proxy: str

    @property
    def env(self) -> dict[str, str]:
        """Return the proxy variables that pip, uv, npm, cargo, go and git read.

        Returns:
            The proxy URL under every common name, and a loopback bypass.
        """
        url = f"http://{self.proxy}:{_EGRESS_PORT}"
        names = ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "npm_config_https_proxy", "npm_config_proxy")
        # The variables stay after the proxy goes away, and loopback must
        # never go through a removed proxy.
        loopback = dict.fromkeys(("NO_PROXY", "no_proxy"), "localhost,127.0.0.1,::1")
        return dict.fromkeys(names, url) | loopback


async def _kill(process: asyncio.subprocess.Process) -> None:
    """Kill a CLI process and wait for it to end.

    Args:
        process: The process to kill.
    """
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    await process.wait()


async def _docker(docker_bin: str, args: Sequence[str], *, stdin: bytes | None = None) -> _CliResult:
    """Run one Docker CLI command and capture its output.

    Args:
        docker_bin: The Docker CLI path.
        args: The CLI arguments.
        stdin: Bytes for the command's stdin, or ``None`` for no input.

    Returns:
        The exit code and the captured output.

    Raises:
        SandboxInfraError: The CLI is missing or did not finish in time.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            docker_bin,
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as error:
        raise SandboxInfraError(f"The Docker CLI {docker_bin!r} could not start: {error}") from error
    # A cancel waits for the CLI to end. Killing the CLI does not stop the
    # daemon: a killed ``docker run`` can still create its container after
    # the removal by name ran, and that container would then stay behind.
    communication = asyncio.ensure_future(process.communicate(stdin))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _CLI_TIMEOUT_SECONDS
    cancelled: asyncio.CancelledError | None = None
    while not communication.done() and loop.time() < deadline:
        try:
            await asyncio.wait({communication}, timeout=deadline - loop.time())
        except asyncio.CancelledError as error:
            cancelled = error
    if not communication.done():
        communication.cancel()
        await _kill(process)
        with contextlib.suppress(asyncio.CancelledError):
            await communication
        if cancelled is not None:
            raise cancelled
        raise SandboxInfraError(f"docker {args[0]} did not finish in {_CLI_TIMEOUT_SECONDS:.0f}s.")
    if cancelled is not None:
        raise cancelled
    stdout, stderr = communication.result()
    return _CliResult(process.returncode or 0, stdout, stderr)


async def _remove(docker_bin: str, args: Sequence[str], missing: bytes) -> None:
    """Remove one Docker object and log a failure. Never raises an Exception.

    Args:
        docker_bin: The Docker CLI path.
        args: The CLI arguments of the remove command.
        missing: Stderr text that means the object is already gone.
    """
    try:
        result = await _docker(docker_bin, args)
    except SandboxInfraError as error:
        logger.warning("Could not remove the Docker sandbox object %s: %s", args[-1], error)
        return
    if result.returncode != 0 and missing not in result.stderr:
        logger.warning("Could not remove the Docker sandbox object %s: %s", args[-1], result.error_line)


def _label_args(labels: Mapping[str, str]) -> list[str]:
    """Turn labels into Docker ``--label`` arguments.

    Args:
        labels: Maps each label name to its value.

    Returns:
        Alternating ``--label`` flags and ``name=value`` items.
    """
    return [item for key, value in labels.items() for item in ("--label", f"{key}={value}")]


def _env_stream(env: Mapping[str, str]) -> bytes:
    """Encode a command's environment for ``_ENV_LOADER``.

    Args:
        env: The command's environment.

    Returns:
        NUL-terminated ``NAME=value`` pairs.

    Raises:
        SandboxSpecError: A name or value holds a NUL, which no environment can carry.
    """
    if any("\0" in name or "\0" in value for name, value in env.items()):
        raise SandboxSpecError("Sandbox environment names and values cannot contain NUL characters.")
    return "".join(f"{name}={value}\0" for name, value in env.items()).encode("utf-8")


def _sandbox_path(path: str) -> str:
    """Resolve a sandbox path; a relative path is relative to the workspace.

    Args:
        path: An absolute sandbox path, or a path relative to ``WORKSPACE_DIR``.

    Returns:
        The normalized absolute POSIX path.

    Raises:
        SandboxSpecError: The path is empty, holds a NUL or names the root.
    """
    if not path or "\0" in path:
        raise SandboxSpecError(f"Invalid sandbox path: {path!r}")
    resolved = posixpath.normpath(posixpath.join(WORKSPACE_DIR, path))
    if resolved in ("/", "//"):
        raise SandboxSpecError(f"Invalid sandbox path: {path!r}")
    return resolved


def _unique_name(stem: str | None) -> str:
    """Return a Docker-safe container name with a random suffix.

    Args:
        stem: The readable part of the name, or ``None`` for the default.

    Returns:
        ``<stem>-<8 hex chars>`` in ``[a-z0-9-]``.
    """
    safe = _NAME_UNSAFE.sub("-", (stem or "").lower()).strip("-")[:_NAME_STEM_CHARS].rstrip("-")
    return f"{safe or 'skynet-local'}-{uuid.uuid4().hex[:8]}"


async def _pump(name: str, reader: asyncio.StreamReader, captured: list[str], on_output: OutputSink | None) -> None:
    """Read one output pipe to its end and hand each chunk to the sink.

    Args:
        name: ``stdout`` or ``stderr``.
        reader: The pipe.
        captured: Receives each decoded chunk.
        on_output: Receives ``(name, text)`` for each chunk, or ``None``.
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    while True:
        chunk = await reader.read(_READ_CHUNK_BYTES)
        text = decoder.decode(chunk, final=not chunk)
        if text:
            captured.append(text)
            if on_output is not None:
                on_output(name, text)
        if not chunk:
            return


async def _feed(writer: asyncio.StreamWriter, data: bytes) -> None:
    """Write bytes to a process's stdin, then close it.

    Args:
        writer: The stdin pipe.
        data: The bytes to write.
    """
    # A CLI that exits early (a removed container) closes the pipe; its exit
    # code and stderr then report why.
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        writer.write(data)
        await writer.drain()
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        writer.close()
        await writer.wait_closed()


class DockerEnvironment(SandboxEnvironment):
    """Run one sandbox as a hardened container on the local Docker daemon.

    The root filesystem is read-only, every capability is dropped, and only
    ``/workspace``, ``/tmp`` and ``/etc/codex`` are writable. Development
    only: the factory never allows it in production.
    """

    capabilities = DOCKER_CAPABILITIES

    def __init__(self, spec: SandboxSpec, *, image: str | None = None, docker_bin: str = DEFAULT_DOCKER_BIN) -> None:
        """Check the spec and choose the image.

        Args:
            spec: The sandbox to open.
            image: The local image to boot when the spec names none. Docker
                never pulls it; it must already be on this machine.
            docker_bin: The Docker CLI path; OrbStack works through the same CLI.

        Raises:
            SandboxSpecError: The spec breaks a provider rule or names no image.
        """
        super().__init__(spec)
        self._image = spec.image or image
        if not self._image:
            raise SandboxSpecError("The Docker sandbox needs an image in the spec or the provider.")
        self._docker_bin = docker_bin
        self._name: str | None = None
        self._egress: _Egress | None = None
        self._egress_up = False
        self._stopped = False
        self._started_at = 0.0
        owner = spec.tags.get(JOB_TAG)
        self._labels = {**SANDBOX_LABELS, **spec.tags, **({DOCKER_JOB_LABEL: owner} if owner else {})}

    @property
    def container_name(self) -> str | None:
        """Return the container name, or ``None`` before start."""
        return self._name

    async def start(self) -> None:
        """Start the box, and its proxy first when the policy allows hosts.

        Raises:
            RuntimeError: The sandbox was already started.
            SandboxInfraError: Docker cannot start the box or its proxy.
        """
        if self._name is not None:
            raise RuntimeError("The Docker sandbox was already started.")
        self._name = _unique_name(self.spec.name)
        self._egress = _Egress(network=f"{self._name}-net", proxy=f"{self._name}-egress")
        self._started_at = time.monotonic()
        if self.spec.allowed_hosts:
            await self._open_egress(self.spec.allowed_hosts)
        await self._run_box()

    async def stop(self) -> None:
        """Remove the box, its proxy and its network.

        A cancel waits until the removal ends, then reaches the caller, so a
        cancelled stop never leaves a container or a network behind.

        Raises:
            asyncio.CancelledError: The task was cancelled during the removal.
        """
        if self._name is None or self._stopped:
            return
        self._stopped = True
        removal = asyncio.ensure_future(self._remove_all())
        cancelled: asyncio.CancelledError | None = None
        while not removal.done():
            try:
                await asyncio.wait({removal})
            except asyncio.CancelledError as error:
                cancelled = error
        if cancelled is not None:
            raise cancelled
        removal.result()

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
        """Run one shell command in the box and stream its output.

        Args:
            command: The shell command, run by ``bash --noprofile --norc -c``.
            cwd: The working directory; the default is ``/workspace``.
            env: Extra environment variables for this command.
            timeout_sec: Kill the command after this long; ``None`` sets no limit.
            on_output: Receives ``(stream, text)`` chunks while the command runs.
            sudo: Run the command as the container's root user.

        Returns:
            The exit code, the captured output and whether the timeout fired.

        Raises:
            SandboxInfraError: The command ran past its kill deadline or Docker is missing.
        """
        name = self._require_running()
        inner = [*_SHELL, command]
        deadline: float | None = None
        if timeout_sec is not None:
            seconds = max(1, math.ceil(timeout_sec))
            inner = ["timeout", "--signal=KILL", f"{seconds}s", *inner]
            deadline = seconds + _KILL_GRACE_SECONDS
        stream = _env_stream({**self.spec.env, **(env or {})})
        user = ["-u", "root"] if sudo else []
        workdir = _sandbox_path(cwd) if cwd else WORKSPACE_DIR
        argv = ["exec", "-i", *user, "-w", workdir, name, *_SHELL, _ENV_LOADER, "skynet-env", *inner]
        try:
            process = await asyncio.create_subprocess_exec(
                self._docker_bin,
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            raise SandboxInfraError(f"The Docker CLI {self._docker_bin!r} could not start: {error}") from error
        captured: dict[str, list[str]] = {"stdout": [], "stderr": []}
        try:
            async with asyncio.timeout(deadline):
                await asyncio.gather(
                    _feed(process.stdin, stream),
                    _pump("stdout", process.stdout, captured["stdout"], on_output),
                    _pump("stderr", process.stderr, captured["stderr"], on_output),
                )
                exit_code = await process.wait()
        except TimeoutError as error:
            await _kill(process)
            raise SandboxInfraError(
                f"The command still ran {_KILL_GRACE_SECONDS}s past its {timeout_sec}s timeout: {command[:200]}"
            ) from error
        except BaseException:
            await _kill(process)
            raise
        return ExecResult(
            exit_code=exit_code,
            stdout="".join(captured["stdout"]),
            stderr="".join(captured["stderr"]),
            timed_out=timeout_sec is not None and exit_code in _TIMEOUT_EXIT_CODES,
        )

    async def upload_files(self, files: Mapping[str, str]) -> None:
        """Write text files into the box in one tar stream, creating parents.

        Args:
            files: Maps each sandbox path to its text content. A relative
                path is relative to ``/workspace``.

        Raises:
            SandboxInfraError: The box refused the files.
        """
        name = self._require_running()
        if not files:
            return
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for path, text in files.items():
                data = text.encode("utf-8")
                info = tarfile.TarInfo(_sandbox_path(path).lstrip("/"))
                info.size = len(data)
                info.mode = 0o644
                info.mtime = int(time.time())
                archive.addfile(info, io.BytesIO(data))
        result = await _docker(
            self._docker_bin,
            ["exec", "-i", name, "tar", "-x", "-f", "-", "--no-same-owner", "-C", "/"],
            stdin=buffer.getvalue(),
        )
        if result.returncode != 0:
            raise SandboxInfraError(f"Could not write files into the Docker sandbox: {result.error_line}")

    async def download_file(self, path: str) -> str | None:
        """Read one text file from the box through a checked tar stream.

        Args:
            path: The sandbox path. A relative path is relative to ``/workspace``.

        Returns:
            The file content, or ``None`` when the file does not exist.

        Raises:
            SandboxInfraError: The box could not be read.
            UnsafeArchiveError: The box sent an archive with an unsafe member.
        """
        name = self._require_running()
        target = _sandbox_path(path)
        result = await _docker(self._docker_bin, ["exec", name, "sh", "-c", _PACK_FILE_SCRIPT, "sh", target])
        if result.returncode == _MISSING_FILE_EXIT:
            return None
        if result.returncode != 0:
            raise SandboxInfraError(f"Could not read {path!r} from the Docker sandbox: {result.error_line}")
        with tempfile.TemporaryDirectory(prefix="skynet-docker-") as scratch:
            # The archive comes from the box, so the unpack checks every
            # member before it writes anything.
            unpack(result.stdout, scratch)
            data = (Path(scratch) / posixpath.basename(target)).read_bytes()
        return data.decode("utf-8", errors="replace")

    @classmethod
    async def stop_job_sandboxes(cls, job_id: str) -> int:
        """Remove every container of a job, then the job's networks.

        A failure on one container or network is logged and skipped.

        Args:
            job_id: The value of the ``JOB_TAG`` tag.

        Returns:
            The number of sandbox boxes removed; egress proxies do not count.
        """
        selector = f"label={DOCKER_JOB_LABEL}={job_id}"
        try:
            listed = await _docker(
                DEFAULT_DOCKER_BIN,
                ["ps", "-a", "--filter", selector, "--format", f'{{{{.ID}}}} {{{{.Label "{ROLE_LABEL}"}}}}'],
            )
        except SandboxInfraError as error:
            logger.warning("Could not list the Docker sandboxes of job %s: %s", job_id, error)
            return 0
        if listed.returncode != 0:
            logger.warning("Could not list the Docker sandboxes of job %s: %s", job_id, listed.error_line)
            return 0
        stopped = 0
        for line in listed.stdout.decode().splitlines():
            container, _, role = line.strip().partition(" ")
            if not container:
                continue
            try:
                removed = await _docker(DEFAULT_DOCKER_BIN, ["rm", "-f", container])
            except SandboxInfraError as error:
                logger.warning("Could not remove the Docker sandbox %s of job %s: %s", container, job_id, error)
                continue
            if removed.returncode != 0:
                logger.warning(
                    "Could not remove the Docker sandbox %s of job %s: %s", container, job_id, removed.error_line
                )
                continue
            if role != EGRESS_ROLE:
                stopped += 1
        # A network can go only when no container is attached, so it goes last.
        try:
            networks = await _docker(DEFAULT_DOCKER_BIN, ["network", "ls", "-q", "--filter", selector])
        except SandboxInfraError as error:
            logger.warning("Could not list the Docker sandbox networks of job %s: %s", job_id, error)
            return stopped
        for network in networks.stdout.decode().split():
            await _remove(DEFAULT_DOCKER_BIN, ["network", "rm", network], b"not found")
        return stopped

    async def _apply_network_policy(self, policy: NetworkPolicy) -> None:
        """Switch the box to deny-all, or to a new allowlist behind the proxy.

        Args:
            policy: The checked policy; the base class refuses open egress.

        Raises:
            SandboxInfraError: Docker did not confirm the new networks.
        """
        name = self._require_running()
        egress = self._require_egress()
        if policy.disabled:
            for network in await self._networks():
                result = await _docker(self._docker_bin, ["network", "disconnect", "--force", network, name])
                if result.returncode != 0:
                    raise SandboxInfraError(f"Could not switch the Docker sandbox network off: {result.error_line}")
            if await self._networks():
                raise SandboxInfraError("Docker did not confirm that the sandbox network is off.")
            await self._drop_egress()
            return
        if self._egress_up:
            # The new proxy keeps the old name on the same network, so the
            # proxy variables in the box stay valid.
            await _remove(self._docker_bin, ["rm", "-f", egress.proxy], b"No such container")
            self._egress_up = False
            await self._start_proxy(policy.allowed_hosts)
            self._egress_up = True
        else:
            await self._open_egress(policy.allowed_hosts)
            # A box started with --network none must leave "none" before it
            # can join another network; a box that left every network has no
            # "none" to leave, so this result does not matter.
            await _docker(self._docker_bin, ["network", "disconnect", "none", name])
            result = await _docker(self._docker_bin, ["network", "connect", egress.network, name])
            if result.returncode != 0:
                raise SandboxInfraError(f"Could not connect the Docker sandbox to its proxy: {result.error_line}")
        if await self._networks() != [egress.network]:
            raise SandboxInfraError("Docker did not confirm that the sandbox reaches only its proxy.")

    def _require_running(self) -> str:
        """Return the container name of the running box.

        Returns:
            The container name.

        Raises:
            RuntimeError: The sandbox is not started or is stopped.
        """
        if self._name is None or self._stopped:
            raise RuntimeError("The Docker sandbox is not running.")
        return self._name

    def _require_egress(self) -> _Egress:
        """Return the names of the box's network and proxy.

        Returns:
            The egress names, which ``start`` sets.

        Raises:
            RuntimeError: The sandbox is not started.
        """
        if self._egress is None:
            raise RuntimeError("The Docker sandbox is not running.")
        return self._egress

    def _remaining_lifetime(self) -> int:
        """Return the whole seconds left in the box's lifetime, at least 1.

        Returns:
            The seconds left.
        """
        return max(1, math.ceil(self.spec.lifetime_seconds - (time.monotonic() - self._started_at)))

    async def _run_box(self) -> None:
        """Start the box container.

        Raises:
            SandboxInfraError: Docker cannot start the container.
        """
        name = self._require_running()
        egress = self._require_egress()
        resources = self.spec.resources
        limits: list[str] = []
        if resources.mode is not ResourceMode.IGNORE:
            memory_mb = resources.memory_mb or resources.cpus * 2048
            limits = ["--cpus", str(resources.cpus), "--memory", f"{memory_mb}m"]
        # spec.env can hold secrets, so each command gets it over stdin instead.
        fixed_env = {"HOME": WORKSPACE_DIR, "TMPDIR": "/tmp", **egress.env}
        argv = [
            "run",
            "-d",
            "--rm",
            "--init",
            "--name",
            name,
            "--network",
            egress.network if self._egress_up else "none",
            *limits,
            "--pids-limit",
            str(_PIDS_LIMIT),
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            *[item for mount in _TMPFS_MOUNTS for item in ("--tmpfs", mount)],
            "-w",
            WORKSPACE_DIR,
            *[item for key, value in fixed_env.items() for item in ("-e", f"{key}={value}")],
            *_label_args(self._labels),
            "--entrypoint",
            "sleep",
            str(self._image),
            # The box's only process is this timer, so the container and every
            # command in it end when the lifetime does.
            str(math.ceil(self.spec.lifetime_seconds)),
        ]
        result = await _docker(self._docker_bin, argv)
        if result.returncode != 0 or not result.stdout.strip():
            raise SandboxInfraError(f"Could not start the Docker sandbox: {result.error_line}")

    async def _open_egress(self, hosts: Sequence[str]) -> None:
        """Create the box's internal network and start its proxy on it.

        Args:
            hosts: The hosts that the proxy allows.

        Raises:
            SandboxInfraError: Docker cannot create the network or start the proxy.
        """
        egress = self._require_egress()
        created = await _docker(
            self._docker_bin, ["network", "create", "--internal", *_label_args(self._labels), egress.network]
        )
        if created.returncode != 0:
            raise SandboxInfraError(f"Could not create the Docker sandbox network: {created.error_line}")
        try:
            await self._start_proxy(hosts)
        except BaseException:
            await self._drop_egress()
            raise
        self._egress_up = True

    async def _start_proxy(self, hosts: Sequence[str]) -> None:
        """Start the proxy, give it the uplink network and wait until it listens.

        The proxy runs on the box's own image, which is already on this
        machine, so Docker pulls no other image. It lives no longer than the box.

        Args:
            hosts: The hosts that the proxy allows.

        Raises:
            SandboxInfraError: The proxy did not start, connect or report ready in time.
        """
        egress = self._require_egress()
        argv = [
            "run",
            "-d",
            "--rm",
            "--init",
            "--name",
            egress.proxy,
            "--network",
            egress.network,
            "--cpus",
            "1",
            "--memory",
            "256m",
            "--pids-limit",
            "256",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            _EGRESS_USER,
            *_label_args({**self._labels, ROLE_LABEL: EGRESS_ROLE}),
            "--entrypoint",
            "python3",
            str(self._image),
            "-I",
            "-c",
            _EGRESS_PROXY_SOURCE,
            str(_EGRESS_PORT),
            str(self._remaining_lifetime()),
            *sorted(set(hosts)),
        ]
        started = await _docker(self._docker_bin, argv)
        if started.returncode != 0:
            raise SandboxInfraError(f"Could not start the Docker sandbox egress proxy: {started.error_line}")
        connected = await _docker(self._docker_bin, ["network", "connect", _UPLINK_NETWORK, egress.proxy])
        if connected.returncode != 0:
            raise SandboxInfraError(f"Could not connect the Docker sandbox egress proxy: {connected.error_line}")
        deadline = time.monotonic() + _EGRESS_READY_SECONDS
        marker = READY_MARKER.encode()
        while True:
            logs = await _docker(self._docker_bin, ["logs", egress.proxy])
            if marker in logs.stdout or marker in logs.stderr:
                return
            if time.monotonic() >= deadline:
                raise SandboxInfraError(f"The Docker sandbox egress proxy did not start: {logs.error_line}")
            await asyncio.sleep(_EGRESS_POLL_SECONDS)

    async def _networks(self) -> list[str]:
        """List the networks of the box, other than ``none``.

        Returns:
            The network names.

        Raises:
            SandboxInfraError: Docker cannot inspect the box.
        """
        result = await _docker(
            self._docker_bin, ["inspect", "--format", "{{json .NetworkSettings.Networks}}", self._require_running()]
        )
        if result.returncode != 0:
            raise SandboxInfraError(f"Could not inspect the Docker sandbox: {result.error_line}")
        networks = json.loads(result.stdout.decode() or "null") or {}
        return [network for network in networks if network != "none"]

    async def _drop_egress(self) -> None:
        """Remove the proxy, then the network. Never raises an Exception."""
        egress = self._require_egress()
        await _remove(self._docker_bin, ["rm", "-f", egress.proxy], b"No such container")
        await _remove(self._docker_bin, ["network", "rm", egress.network], b"not found")
        self._egress_up = False

    async def _remove_all(self) -> None:
        """Remove the box, then its proxy and network, by name. Never raises an Exception."""
        # Names, not ids: a cancelled start can leave a container whose id
        # never reached us.
        if self._name is not None:
            await _remove(self._docker_bin, ["rm", "-f", self._name], b"No such container")
        if self._egress is not None:
            await self._drop_egress()


async def remove_idle_sandbox_networks(docker_bin: str = DEFAULT_DOCKER_BIN) -> int:
    """Remove every sandbox network that no container uses.

    A killed backend leaves a box's network behind after ``--rm`` removes the
    box and its proxy, and each network holds an address range from Docker's
    small pool. A network younger than ``_IDLE_NETWORK_MIN_AGE_SECONDS`` is
    skipped, because a box that is still starting has a network with no
    container yet. It never raises an Exception: a failure is logged and skipped.

    Args:
        docker_bin: The Docker CLI path.

    Returns:
        The number of networks removed.
    """
    filters = [arg for key, value in SANDBOX_LABELS.items() for arg in ("--filter", f"label={key}={value}")]
    try:
        listed = await _docker(docker_bin, ["network", "ls", "-q", *filters])
    except SandboxInfraError as error:
        logger.warning("Could not list idle Docker sandbox networks: %s", error)
        return 0
    if listed.returncode != 0:
        logger.warning("Could not list idle Docker sandbox networks: %s", listed.error_line)
        return 0
    removed = 0
    for network in listed.stdout.decode().split():
        try:
            inspected = await _docker(
                docker_bin, ["network", "inspect", "--format", "{{len .Containers}} {{.Created.Unix}}", network]
            )
            if inspected.returncode != 0:
                continue
            attached, _, created = inspected.stdout.decode().strip().partition(" ")
            if attached != "0" or not created.isdigit():
                continue
            if time.time() - int(created) < _IDLE_NETWORK_MIN_AGE_SECONDS:
                continue
            result = await _docker(docker_bin, ["network", "rm", network])
        except SandboxInfraError as error:
            logger.warning("Could not remove the idle Docker sandbox network %s: %s", network, error)
            continue
        if result.returncode != 0:
            logger.warning("Could not remove the idle Docker sandbox network %s: %s", network, result.error_line)
            continue
        removed += 1
    return removed
