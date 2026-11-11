"""Async Apple container sandbox provider: one Linux VM container for each sandbox, driven by the ``container`` CLI.

Adapted from Harbor 0.25.0 (Apache-2.0), apple_container.py. Changes:

- The container implements ``SandboxEnvironment``: every ``container`` call
  runs through ``asyncio.create_subprocess_exec``, and a cancel never leaves
  a CLI behind.
- No retries. The Skynet error policy retries at the trial level, so a failed
  start raises once.
- No image build. Harbor builds the task image with ``container build``;
  this provider boots an image that the spec or the caller names.
- No host mounts. Harbor mounts log directories into the container; files
  here move only through ``upload_files`` and ``download_file``.
- The container carries a job label for ``stop_job_sandboxes``, and its idle
  process exits when the spec lifetime ends, so a container that a crashed
  worker leaves behind stops by itself.
- Environment values reach the command on stdin, never on a command line,
  so ``ps`` on the Mac and in the container does not show them.
- Files move through ``container exec -i`` with the content on stdin, not
  ``tar``, so the image needs only ``bash`` and coreutils.
- A timeout or a cancel kills the command's process group in the container.
  Killing the ``container exec`` client alone does not stop the command.

The ``container`` CLI runs only on Apple silicon Macs. Egress is open, as in
Harbor. Not for production; the factory refuses it there.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import hashlib
import json
import logging
import math
import platform
import re
import shlex
import shutil
import subprocess
import uuid
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, TypeVar

from ..capabilities import EnvironmentCapabilities, NetworkMode, SandboxSpecError
from ..environment import JOB_TAG, ExecResult, OutputSink, SandboxEnvironment
from ..errors import SandboxFatalError, SandboxInfraError, StartTimeoutError
from ..resources import ResourceMode, SandboxSpec

logger = logging.getLogger(__name__)

APPLE_CONTAINER_CAPABILITIES = EnvironmentCapabilities(
    name="apple_container",
    network_modes=frozenset({NetworkMode.OPEN}),
)

DEFAULT_CONTAINER_BIN = "container"
JOB_LABEL = "skynet.job"
SANDBOX_LABELS: Mapping[str, str] = {"skynet": "sandbox"}

# PID 1 must answer SIGTERM, or every stop waits the full grace period. It
# exits when the lifetime ($0) ends, so a leaked container stops by itself.
_IDLE_SCRIPT = "trap 'exit 0' TERM; sleep \"$0\" & wait $!"
_CLI_TIMEOUT_SECONDS = 60.0
# A first pull of a large image can take many minutes; ``run`` then only boots.
_PULL_TIMEOUT_SECONDS = 1800.0
_LATE_POLL_SECONDS = 0.5
_STATUS_TIMEOUT_SECONDS = 10.0
_STOP_GRACE_SECONDS = 10
_KILL_GRACE_SECONDS = 15
_GROUP_KILL_SECONDS = 10.0
_TIMEOUT_EXIT_CODE = 124
_NO_GROUP_EXIT_CODE = 125
_MISSING_FILE_EXIT = 3
_READ_CHUNK_BYTES = 65536
_SHELL = ("bash", "--noprofile", "--norc", "-c")
# Reads NUL-terminated NAME=value pairs from stdin, so secrets never appear
# on a command line that ``ps`` in the container or on the Mac shows.
_ENV_LOADER = 'pairs=(); while IFS= read -r -d "" pair; do pairs+=("$pair"); done; exec env -- "${pairs[@]}" "$@"'
# The state files live under the container's own TMPDIR. The wrapper runs
# outside the env loader, so a command env that sets TMPDIR cannot move them
# away from the kill and marker scripts. Job control (``set -m``) puts the
# command in its own process group, and keeps its stdin, which carries the
# env stream. Bash prints a job notice on its own stderr when it reaps a
# killed job, so the real stderr stays on fd 3 for the command alone. The
# timer leaves a marker, so a command that exits 124 by itself is not a
# timeout. A cancel can come before the wrapper writes the group id: the kill
# leaves a cancel marker, and the wrapper checks it before and after the write.
_STATE = 'state="${TMPDIR:-/tmp}/skynet-exec-$1"'
_EXEC_WRAPPER = (
    f"{_STATE}; limit=$2; shift 2; timer=; "
    'if [ -e "$state.cancel" ]; then rm -f -- "$state.cancel"; exit 137; fi; '
    'exec 3>&2 2>/dev/null; set -m; "$@" 2>&3 3>&- & child=$!; '
    'if ! echo "$child" > "$state.pid"; then kill -KILL -- "-$child"; wait "$child"; '
    f'rm -f -- "$state.pid" "$state.cancel"; : > "$state.nogroup"; exit {_NO_GROUP_EXIT_CODE}; fi; '
    'if [ -e "$state.cancel" ]; then kill -KILL -- "-$child" 2>/dev/null; fi; '
    'if [ -n "$limit" ]; then '
    '(sleep "$limit"; : > "$state.timeout"; kill -KILL -- "-$child") >/dev/null 2>&1 3>&- & timer=$!; fi; '
    'set +m; wait "$child"; code=$?; '
    'if [ -n "$timer" ]; then kill -KILL -- "-$timer" 2>/dev/null; fi; rm -f -- "$state.pid" "$state.cancel"; '
    f'if [ -e "$state.timeout" ] && [ "$code" = 137 ]; then exit {_TIMEOUT_EXIT_CODE}; fi; '
    'rm -f -- "$state.timeout"; exit "$code"'
)
_KILL_GROUP_SCRIPT = (
    f'{_STATE}; : > "$state.cancel"; '
    'for _ in 1 2 3 4 5 6 7 8 9 10; do [ -s "$state.pid" ] && break; sleep 0.1; done; '
    'pgid=$(cat -- "$state.pid" 2>/dev/null); rm -f -- "$state.pid" "$state.timeout"; '
    'case $pgid in ""|*[!0-9]*|0|1) exit 0;; esac; kill -KILL -- "-$pgid" 2>/dev/null; exit 0'
)
_MARKER_SCRIPT = f'{_STATE}; test -e "$state.$2" && rm -f -- "$state.$2"'
_WRITE_FILE_SCRIPT = 'mkdir -p -- "$(dirname -- "$1")" && cat > "$1"'
_READ_FILE_SCRIPT = f'test -f "$1" || exit {_MISSING_FILE_EXIT}; exec cat -- "$1"'
_NAME_UNSAFE = re.compile(r"[^a-z0-9-]+")
_NAME_DASHES = re.compile(r"-{2,}")
_NAME_STEM_CHARS = 50
_LABEL_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
# The CLI reaches its API server over XPC; these errors mean the container
# system service is not running, which no retry fixes.
_SERVICE_MARKERS = ("container system start", "xpc", "apiserver", "connection invalid")
_NOT_FOUND_MARKERS = ("not found", "notfound", "no such")

_T = TypeVar("_T")


class _CliDeadlineError(SandboxInfraError):
    """Signal that a ``container`` call ran past its deadline and was killed."""


@dataclass(frozen=True)
class _CliResult:
    """Hold the result of one ``container`` call."""

    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def error_line(self) -> str:
        """Return the last non-empty stderr line, cut to 500 characters."""
        lines = [line for line in self.stderr.decode("utf-8", errors="replace").splitlines() if line.strip()]
        return lines[-1][:500] if lines else f"exit code {self.returncode}"

    def says(self, markers: Sequence[str]) -> bool:
        """Say whether stderr holds one of the markers, ignoring case.

        Args:
            markers: Lowercase text fragments.

        Returns:
            True when one marker is in stderr.
        """
        text = self.stderr.decode("utf-8", errors="replace").lower()
        return any(marker in text for marker in markers)


async def _kill(process: asyncio.subprocess.Process) -> None:
    """Kill a CLI process and wait for it to end.

    Args:
        process: The process to kill.
    """
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    await process.wait()


async def _run_to_end(awaitable: Awaitable[_T]) -> _T:
    """Run a cleanup to its end, even when the task is cancelled during it.

    Args:
        awaitable: The cleanup.

    Returns:
        The cleanup's result.

    Raises:
        asyncio.CancelledError: The task was cancelled during the cleanup.
            The cleanup ended first, and only the first cancel is raised. A
            cleanup error is logged, because the cancel replaces it.
    """
    work = asyncio.ensure_future(awaitable)
    cancelled: asyncio.CancelledError | None = None
    while not work.done():
        try:
            await asyncio.wait({work})
        except asyncio.CancelledError as error:
            cancelled = cancelled or error
    if cancelled is not None:
        if not work.cancelled() and (error := work.exception()) is not None:
            logger.warning("A container cleanup failed during a cancel: %s", type(error).__name__)
        raise cancelled
    return work.result()


async def _cli(
    container_bin: str,
    args: Sequence[str],
    *,
    stdin: bytes | None = None,
    cli_seconds: float = _CLI_TIMEOUT_SECONDS,
    kill_on_cancel: bool = False,
) -> _CliResult:
    """Run one ``container`` command and capture its output.

    Args:
        container_bin: The ``container`` CLI path.
        args: The CLI arguments.
        stdin: Bytes for the command's stdin, or ``None`` for no input.
        cli_seconds: Kill the CLI after this many seconds.
        kill_on_cancel: Kill the CLI at once on a cancel, instead of waiting
            for it to end. Only for a call that creates no container.

    Returns:
        The exit code and the captured output.

    Raises:
        SandboxInfraError: The CLI is missing.
        _CliDeadlineError: The CLI did not finish in time and was killed.
        asyncio.CancelledError: The task was cancelled. When the CLI then ran
            past its deadline, the error's ``__cause__`` is a ``_CliDeadlineError``.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            container_bin,
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as error:
        raise SandboxInfraError(f"The container CLI {container_bin!r} could not start: {error}") from error
    # A cancel waits for the CLI to end. A killed ``container run`` can still
    # create its container after a stop by name ran, and that container would
    # then stay.
    communication = asyncio.ensure_future(process.communicate(stdin))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + cli_seconds
    cancelled: asyncio.CancelledError | None = None
    while not communication.done() and loop.time() < deadline:
        try:
            await asyncio.wait({communication}, timeout=deadline - loop.time())
        except asyncio.CancelledError as error:
            cancelled = cancelled or error
            if kill_on_cancel:
                break
    if not communication.done():
        communication.cancel()
        await _run_to_end(_kill(process))
        with contextlib.suppress(asyncio.CancelledError):
            await communication
        if cancelled is not None and kill_on_cancel:
            raise cancelled
        deadline_error = _CliDeadlineError(f"container {args[0] if args else ''} did not finish in {cli_seconds:.0f}s.")
        if cancelled is not None:
            # The caller still needs to know that the CLI was killed, so it
            # can look for a container that the API server creates late.
            raise cancelled from deadline_error
        raise deadline_error
    if cancelled is not None:
        raise cancelled
    stdout, stderr = communication.result()
    return _CliResult(process.returncode or 0, stdout, stderr)


def _container_name(stem: str | None) -> str:
    """Return a container name with a random suffix.

    Args:
        stem: The readable part of the name, or ``None`` for the default.

    Returns:
        ``<stem>-<8 hex chars>`` in ``[a-z0-9-]``, at most 59 characters.
    """
    safe = _NAME_DASHES.sub("-", _NAME_UNSAFE.sub("-", (stem or "").lower()))
    safe = safe.strip("-")[:_NAME_STEM_CHARS].rstrip("-")
    return f"{safe or 'skynet'}-{uuid.uuid4().hex[:8]}"


def _job_label(job_id: str) -> str:
    """Return a label value for a job id that a CLI argument can carry.

    Args:
        job_id: The value of the ``JOB_TAG`` tag.

    Returns:
        The id itself when it is a plain token, else a stable digest, so two
        different ids never share a label.
    """
    if _LABEL_VALUE.match(job_id):
        return job_id
    return "sha256-" + hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:56]


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


def _check_path(path: str) -> str:
    """Refuse a sandbox path that no shell argument can carry.

    Args:
        path: An absolute path, or a path relative to the container's working directory.

    Returns:
        The path, unchanged.

    Raises:
        SandboxSpecError: The path is empty or holds a NUL.
    """
    if not path or "\0" in path:
        raise SandboxSpecError(f"Invalid sandbox path: {path!r}")
    return path


def _resource_args(spec: SandboxSpec) -> list[str]:
    """Map the spec's resource request onto ``container run`` options.

    The VM gets exactly the size it asks for, so every mode but ``IGNORE``
    sets it. The CLI has no disk size option, so ``disk_mb`` has no effect.

    Args:
        spec: The sandbox spec.

    Returns:
        The ``--cpus`` and ``--memory`` options; empty for ``IGNORE``.
    """
    request = spec.resources
    if request.mode is ResourceMode.IGNORE:
        return []
    args = ["--cpus", str(request.cpus)]
    if request.memory_mb is not None:
        args += ["--memory", f"{request.memory_mb}M"]
    return args


def _label_args(labels: Mapping[str, str]) -> list[str]:
    """Return one ``--label`` option for each label.

    Args:
        labels: The container labels.

    Returns:
        The CLI options.
    """
    args: list[str] = []
    for key, value in labels.items():
        args += ["--label", f"{key}={value}"]
    return args


def _service_error(action: str, result: _CliResult) -> SandboxFatalError | SandboxInfraError:
    """Return the error for a failed ``container`` call.

    Args:
        action: What the call did, for the message.
        result: The failed CLI result.

    Returns:
        A fatal error when the container system service is down, else an infra error.
    """
    if result.says(_SERVICE_MARKERS):
        return SandboxFatalError(f"{action}: {result.error_line}. Run container system start.")
    return SandboxInfraError(f"{action}: {result.error_line}")


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
    # A CLI that exits early (a stopped container) closes the pipe; its exit
    # code and stderr then report why.
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        writer.write(data)
        await writer.drain()
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        writer.close()
        await writer.wait_closed()


def _listed_containers(stdout: bytes) -> list[tuple[str, Mapping[str, Any]]]:
    """Read the names and labels from ``container list --format json``.

    Args:
        stdout: The CLI output: a JSON list of container snapshots.

    Returns:
        ``(name, labels)`` for each container.

    Raises:
        ValueError: The output is not JSON.
        TypeError: The JSON is not a list.
    """
    items = json.loads(stdout or b"[]")
    if not isinstance(items, list):
        raise TypeError("container list did not send a JSON list.")
    listed: list[tuple[str, Mapping[str, Any]]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # A snapshot keeps its id and labels under "configuration".
        raw = item.get("configuration")
        config = raw if isinstance(raw, dict) else item
        name = config.get("id")
        labels = config.get("labels")
        if isinstance(name, str) and name and isinstance(labels, dict):
            listed.append((name, labels))
    return listed


class AppleContainerEnvironment(SandboxEnvironment):
    """Run one sandbox as a container in a lightweight VM on an Apple silicon Mac.

    The ``container`` CLI must be installed and its system service started
    (``container system start``). Development only: the factory never allows
    it in production.
    """

    capabilities = APPLE_CONTAINER_CAPABILITIES
    default_container_bin: ClassVar[str] = DEFAULT_CONTAINER_BIN
    ready_seconds: ClassVar[float] = 120.0
    poll_seconds: ClassVar[float] = 0.5
    late_run_seconds: ClassVar[float] = 15.0

    def __init__(self, spec: SandboxSpec, *, image: str | None = None, container_bin: str | None = None) -> None:
        """Check the spec and choose the image.

        Args:
            spec: The sandbox to open.
            image: The image to boot when the spec names none. The CLI must
                be able to pull it; no image is built.
            container_bin: The ``container`` CLI path; the default is
                ``default_container_bin``.

        Raises:
            SandboxSpecError: The spec breaks a provider rule or names no image.
        """
        super().__init__(spec)
        self._image = spec.image or image
        if not self._image:
            raise SandboxSpecError("The Apple container sandbox needs an image in the spec or the provider.")
        self._bin = container_bin or self.default_container_bin
        self._name: str | None = None
        self._stopped = False

    @property
    def container_name(self) -> str | None:
        """Return the container name, or ``None`` before start."""
        return self._name

    @classmethod
    def preflight(cls) -> None:
        """Check for an Apple silicon Mac, the CLI on PATH and a running system service.

        Raises:
            SandboxFatalError: The host is not an Apple silicon Mac, the CLI is
                missing, or ``container system status`` fails.
        """
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            raise SandboxFatalError("The Apple container sandbox runs only on an Apple silicon Mac (macOS on arm64).")
        container_bin = shutil.which(cls.default_container_bin)
        if container_bin is None:
            raise SandboxFatalError(
                f"The Apple container sandbox needs the container CLI ({cls.default_container_bin!r}) on PATH."
            )
        try:
            result = subprocess.run(
                [container_bin, "system", "status"], capture_output=True, timeout=_STATUS_TIMEOUT_SECONDS, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SandboxFatalError(f"container system status did not answer: {error}") from error
        if result.returncode != 0:
            detail = _CliResult(result.returncode, result.stdout, result.stderr).error_line
            raise SandboxFatalError(
                f"The container system service is not running. Run container system start. ({detail})"
            )

    def _run_args(self, name: str) -> list[str]:
        """Build the ``container run`` arguments. They hold no environment value.

        Args:
            name: The container name.

        Returns:
            The CLI arguments.
        """
        owner = self.spec.tags.get(JOB_TAG)
        labels = {**SANDBOX_LABELS, **({JOB_LABEL: _job_label(owner)} if owner else {})}
        lifetime = str(max(1, math.ceil(self.spec.lifetime_seconds)))
        return [
            "run",
            "--detach",
            "--name",
            name,
            *_label_args(labels),
            *_resource_args(self.spec),
            str(self._image),
            "sh",
            "-c",
            _IDLE_SCRIPT,
            lifetime,
        ]

    async def start(self) -> None:
        """Create the container and wait until a command can run in it.

        Raises:
            RuntimeError: The sandbox was already started.
            SandboxFatalError: The container system service is not running.
            SandboxInfraError: The CLI could not pull the image or refused the container.
            StartTimeoutError: No command ran within ``ready_seconds``.
            asyncio.CancelledError: The task was cancelled. A cancel during the
                pull kills it at once. A cancel during ``run`` waits for the CLI
                to end, and for the late-container search when the CLI was killed.
        """
        if self._name is not None:
            raise RuntimeError("The Apple container sandbox was already started.")
        # A pull creates no container, so a cancel can kill it at once.
        pulled = await _cli(
            self._bin,
            ["image", "pull", "--progress", "none", str(self._image)],
            cli_seconds=_PULL_TIMEOUT_SECONDS,
            kill_on_cancel=True,
        )
        if pulled.returncode != 0:
            raise _service_error(f"The container CLI could not pull the image {self._image}", pulled)
        name = _container_name(self.spec.name)
        self._name = name
        try:
            result = await _cli(self._bin, self._run_args(name))
        except _CliDeadlineError:
            await _run_to_end(self._remove_late_container(name))
            raise
        except asyncio.CancelledError as error:
            if isinstance(error.__cause__, _CliDeadlineError):
                await _run_to_end(self._remove_late_container(name))
            raise
        if result.returncode != 0:
            raise _service_error(f"The container CLI refused the sandbox {name}", result)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.ready_seconds
        while True:
            probe = await _cli(self._bin, self._exec_args(name, "true"))
            if probe.returncode == 0:
                return
            if probe.says(_SERVICE_MARKERS):
                raise _service_error(f"The container CLI lost the sandbox {name}", probe)
            if loop.time() >= deadline:
                raise StartTimeoutError(self.ready_seconds)
            await asyncio.sleep(self.poll_seconds)

    async def _remove_late_container(self, name: str) -> None:
        """Wait a short time for a container that a killed ``run`` client can still create, and remove it.

        The API server, not the client, creates the container, so it can
        appear after the stop by name in ``stop`` already ran. A failure is
        logged; the idle process still exits when the lifetime ends.

        Args:
            name: The container name.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.late_run_seconds
        while True:
            try:
                listed = await _cli(
                    self._bin, ["list", "--all", "--format", "json"], cli_seconds=_STATUS_TIMEOUT_SECONDS
                )
                names = {item for item, _ in _listed_containers(listed.stdout)} if listed.returncode == 0 else set()
            except (SandboxInfraError, ValueError, TypeError) as error:
                logger.warning("Could not list the Apple container sandboxes after a killed run of %s: %s", name, error)
                names = set()
            if name in names:
                await _remove_container(self._bin, name)
                return
            if loop.time() >= deadline:
                return
            await asyncio.sleep(_LATE_POLL_SECONDS)

    def _exec_args(self, name: str, *argv: str, stdin: bool = False, root: bool = False) -> list[str]:
        """Build the ``container exec`` arguments for one command in the container.

        Args:
            name: The container name.
            *argv: The command and its arguments.
            stdin: Pass the client's stdin to the command.
            root: Run the command as root instead of the image's user.

        Returns:
            The CLI arguments.
        """
        options = (["--interactive"] if stdin else []) + (["--user", "root"] if root else [])
        return ["exec", *options, name, *argv]

    async def stop(self) -> None:
        """Stop and remove the container. Do nothing before start or after a stop.

        A cancel waits until the removal ends, then reaches the caller, so a
        cancelled stop never skips it. A failure is logged; the idle process
        still exits when the lifetime ends.

        Raises:
            asyncio.CancelledError: The task was cancelled during the removal.
        """
        if self._name is None or self._stopped:
            return
        self._stopped = True
        await _run_to_end(_remove_container(self._bin, self._name))

    def _require_running(self) -> str:
        """Return the container name of a started, not stopped sandbox.

        Returns:
            The container name.

        Raises:
            RuntimeError: The sandbox is not running.
        """
        if self._name is None or self._stopped:
            raise RuntimeError("The Apple container sandbox is not running.")
        return self._name

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
        """Run one shell command in the container and stream its output.

        Args:
            command: The shell command, run by ``bash --noprofile --norc -c``.
            cwd: The working directory; the default is the image's.
            env: Extra environment variables for this command.
            timeout_sec: After this long, kill the command's process group and
                return ``timed_out=True``; ``None`` sets no limit.
            on_output: Receives ``(stream, text)`` chunks while the command runs.
            sudo: Run the command as root instead of the image's user.

        Returns:
            The exit code, the captured output and whether the timeout fired.

        Raises:
            SandboxInfraError: The command ran past its kill deadline, the
                container could not record the command's process group, or the
                CLI is missing. A timeout, a cancel or an error kills the
                command's process group in the container first.
        """
        name = self._require_running()
        state = uuid.uuid4().hex
        limit = ""
        deadline: float | None = None
        if timeout_sec is not None:
            seconds = max(1, math.ceil(timeout_sec))
            limit = str(seconds)
            deadline = seconds + _KILL_GRACE_SECONDS
        stream = _env_stream({**self.spec.env, **(env or {})})
        script = f"cd -- {shlex.quote(cwd)} && {command}" if cwd else command
        argv = [*_SHELL, _EXEC_WRAPPER, "skynet-exec", state, limit, *_SHELL, _ENV_LOADER, "skynet-env"]
        args = self._exec_args(name, *argv, *_SHELL, script, stdin=True, root=sudo)
        try:
            process = await asyncio.create_subprocess_exec(
                self._bin,
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            raise SandboxInfraError(f"The container CLI {self._bin!r} could not start: {error}") from error
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
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
            await self._stop_command(process, name, state)
            raise SandboxInfraError(
                f"The command still ran {_KILL_GRACE_SECONDS}s past its {timeout_sec}s timeout: {command[:200]}"
            ) from error
        except BaseException:
            await self._stop_command(process, name, state)
            raise
        if exit_code == _NO_GROUP_EXIT_CODE and await self._take_marker(name, state, "nogroup"):
            raise SandboxInfraError(
                f"The container could not record the command's process group, so the command was killed: "
                f"{command[:200]}"
            )
        timed_out = bool(limit) and exit_code == _TIMEOUT_EXIT_CODE and await self._take_marker(name, state, "timeout")
        return ExecResult(
            exit_code=exit_code,
            stdout="".join(captured["stdout"]),
            stderr="".join(captured["stderr"]),
            timed_out=timed_out,
        )

    async def _stop_command(self, process: asyncio.subprocess.Process, name: str, state: str) -> None:
        """Kill a command's process group in the container, then its ``container exec`` client.

        The group kill runs as root, because a ``sudo`` command's group
        belongs to root. It is best effort and bounded; a failure is logged,
        so the error or cancel that stopped the command still reaches the caller.

        Args:
            process: The ``container exec`` client.
            name: The container name.
            state: The command's state id.
        """
        try:
            await _cli(
                self._bin,
                self._exec_args(name, *_SHELL, _KILL_GROUP_SCRIPT, "skynet-kill", state, root=True),
                cli_seconds=_GROUP_KILL_SECONDS,
            )
        except Exception as error:
            logger.warning("Could not kill a command in the Apple container sandbox %s: %s", name, error)
        finally:
            await _kill(process)

    async def _take_marker(self, name: str, state: str, marker: str) -> bool:
        """Read and remove one marker file that the exec wrapper left.

        Args:
            name: The container name.
            state: The command's state id.
            marker: The marker suffix: ``timeout`` or ``nogroup``.

        Returns:
            True when the marker was there.
        """
        args = self._exec_args(name, *_SHELL, _MARKER_SCRIPT, "skynet-marker", state, marker, root=True)
        result = await _cli(self._bin, args)
        return result.returncode == 0

    async def upload_files(self, files: Mapping[str, str]) -> None:
        """Write text files into the container, creating parent directories.

        Args:
            files: Maps each sandbox path to its text content. A relative
                path is relative to the container's working directory.

        Raises:
            SandboxInfraError: The container refused a file.
            SandboxSpecError: A path is empty or holds a NUL.
        """
        name = self._require_running()
        for path, text in files.items():
            args = self._exec_args(name, *_SHELL, _WRITE_FILE_SCRIPT, "skynet-write", _check_path(path), stdin=True)
            result = await _cli(self._bin, args, stdin=text.encode("utf-8"))
            if result.returncode != 0:
                raise SandboxInfraError(
                    f"Could not write {path!r} into the Apple container sandbox: {result.error_line}"
                )

    async def download_file(self, path: str) -> str | None:
        """Read one text file from the container.

        Args:
            path: The sandbox path. A relative path is relative to the
                container's working directory.

        Returns:
            The file content, or ``None`` when no regular file is there.

        Raises:
            SandboxInfraError: The container could not be read.
            SandboxSpecError: The path is empty or holds a NUL.
        """
        name = self._require_running()
        args = self._exec_args(name, *_SHELL, _READ_FILE_SCRIPT, "skynet-read", _check_path(path))
        result = await _cli(self._bin, args)
        if result.returncode == _MISSING_FILE_EXIT:
            return None
        if result.returncode != 0:
            raise SandboxInfraError(f"Could not read {path!r} from the Apple container sandbox: {result.error_line}")
        return result.stdout.decode("utf-8", errors="replace")

    @classmethod
    async def stop_job_sandboxes(cls, job_id: str, *, container_bin: str | None = None) -> int:
        """Remove every container of a job. A failure on one container is logged and skipped.

        The CLI has no label filter, so the provider lists every container
        and matches the labels itself.

        Args:
            job_id: The value of the ``JOB_TAG`` tag.
            container_bin: The ``container`` CLI path; the default is ``default_container_bin``.

        Returns:
            The number of containers removed.
        """
        container_bin = container_bin or cls.default_container_bin
        label = _job_label(job_id)
        try:
            listed = await _cli(container_bin, ["list", "--all", "--format", "json"])
        except SandboxInfraError as error:
            logger.warning("Could not list the Apple container sandboxes of job %s: %s", job_id, error)
            return 0
        if listed.returncode != 0:
            logger.warning("Could not list the Apple container sandboxes of job %s: %s", job_id, listed.error_line)
            return 0
        try:
            containers = _listed_containers(listed.stdout)
        except (ValueError, TypeError) as error:
            logger.warning("Could not read the Apple container sandboxes of job %s: %s", job_id, error)
            return 0
        stopped = 0
        for name, labels in containers:
            owned = all(labels.get(key) == value for key, value in SANDBOX_LABELS.items())
            if owned and labels.get(JOB_LABEL) == label and await _remove_container(container_bin, name):
                stopped += 1
        return stopped


async def _remove_container(container_bin: str, name: str) -> bool:
    """Stop and delete one container and log a failure. Never raises an Exception.

    Args:
        container_bin: The ``container`` CLI path.
        name: The container name.

    Returns:
        True when the CLI confirmed the delete, or the container was already gone.
    """
    try:
        stopped = await _cli(container_bin, ["stop", "--time", str(_STOP_GRACE_SECONDS), name])
        if stopped.returncode != 0 and not stopped.says(_NOT_FOUND_MARKERS):
            # The delete below still runs: a container that already exited
            # can refuse a stop.
            logger.info("container stop %s failed: %s", name, stopped.error_line)
        deleted = await _cli(container_bin, ["delete", name])
    except SandboxInfraError as error:
        logger.warning("Could not remove the Apple container sandbox %s: %s", name, error)
        return False
    if deleted.returncode != 0 and not deleted.says(_NOT_FOUND_MARKERS):
        logger.warning("Could not remove the Apple container sandbox %s: %s", name, deleted.error_line)
        return False
    return True
