"""A sandbox provider that runs commands as subprocesses on this host, for dev and tests only.

Each sandbox is a temp directory. Commands run through ``bash --noprofile
--norc -c`` as the worker's own user with a scrubbed environment, in their
own process group. The provider is not a security boundary: it does not
isolate the workload and does not enforce a network policy, so the factory
refuses it in production (SKY-406 D1). The behavior follows the legacy
``LocalSubprocessSession`` in ``core.service_gateway.optimization.blackbox``.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import logging
import os
import shutil
import signal
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar

from ..capabilities import EnvironmentCapabilities
from ..environment import JOB_TAG, ExecResult, OutputSink, SandboxEnvironment
from ..errors import RunTimeoutError, SandboxFatalError, SandboxRejectedError
from ..resources import SandboxSpec
from ..timeouts import Phase, PhaseTimeouts

logger = logging.getLogger(__name__)

# Only the open network mode: the host network is not filtered, so a spec that
# asks for deny-all or an allowlist fails validation instead of running open.
LOCAL_CAPABILITIES = EnvironmentCapabilities(name="local")

_SHELL_FLAGS = ("--noprofile", "--norc", "-c")
_ENV_PASSTHROUGH = ("SSL_CERT_FILE", "SSL_CERT_DIR")
_READ_CHUNK = 4096
# A SIGKILLed shell exits in milliseconds; the 2 s bound only stops a stuck
# reap from blocking exec.
_EXIT_POLLS = 200
_EXIT_POLL_SEC = 0.01


class LocalEnvironment(SandboxEnvironment):
    """Run a sandbox as a temp directory and host subprocesses.

    Sandbox paths resolve inside the temp directory. A path that leaves it,
    such as an absolute host path, is refused.
    """

    capabilities: ClassVar[EnvironmentCapabilities] = LOCAL_CAPABILITIES
    _running: ClassVar[set[LocalEnvironment]] = set()

    def __init__(self, spec: SandboxSpec, *, timeouts: PhaseTimeouts | None = None) -> None:
        """Create a stopped local sandbox.

        Args:
            spec: The sandbox to open. The lifetime and resource sizes are not
                applied; per-command timeouts bound local work.
            timeouts: The phase timeouts; the run phase is the default
                ``exec`` time limit. ``None`` means no default limit.

        Raises:
            SandboxSpecError: The spec asks for a network policy or another
                feature that the local provider cannot apply.
        """
        super().__init__(spec)
        self._default_timeout = timeouts.seconds(Phase.RUN) if timeouts is not None else None
        self._root: Path | None = None
        # Every process group that a command started and that can still have
        # members. A finished command keeps its group here while a background
        # child lives, so stop() can kill that child too.
        self._groups: set[int] = set()

    @property
    def path(self) -> Path | None:
        """Return the temp directory of the running sandbox, or ``None`` when it is stopped."""
        return self._root

    @classmethod
    def preflight(cls) -> None:
        """Check that bash is on PATH and warn that nothing is enforced.

        Raises:
            SandboxFatalError: bash is not on PATH.
        """
        if shutil.which("bash") is None:
            raise SandboxFatalError("The local sandbox needs bash on PATH.")
        logger.warning(
            "The local sandbox runs commands on this host. It does not enforce a network policy "
            "and does not isolate the workload. Use it for dev and tests only."
        )

    async def start(self) -> None:
        """Create the temp directory. Do nothing when the sandbox already runs."""
        if self._root is not None:
            return
        self._root = _make_box_dir()
        type(self)._running.add(self)

    async def stop(self) -> None:
        """Kill the running commands and remove the temp directory.

        A cancel during the removal, or a second cancel after it, waits for
        the removal to finish, then passes the first cancel on, so the
        directory never outlives ``stop``.
        """
        root = self._root
        if root is None:
            return
        self._root = None
        type(self)._running.discard(self)
        # PID reuse: the kernel does not reuse a pid while a group with that id
        # has members, so a group that still has members is ours. The risk is a
        # group that emptied after its last check and whose id a new, unrelated
        # session leader took before this kill. That needs a full wrap of the
        # pid space in that gap. Portable code cannot prove group ownership, so
        # we accept that small risk to never leave box processes on the host.
        for pgid in list(self._groups):
            _kill_group(pgid)
        self._groups.clear()
        cleanup = asyncio.ensure_future(asyncio.to_thread(_remove_tree, root))
        cancelled: asyncio.CancelledError | None = None
        while not cleanup.done():
            try:
                await asyncio.wait({cleanup})
            except asyncio.CancelledError as error:
                cancelled = cancelled or error
        if cancelled is not None:
            raise cancelled
        cleanup.result()

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
        """Run one shell command in the temp directory and wait for it.

        Args:
            command: The shell command.
            cwd: The working directory inside the sandbox; the temp directory
                by default.
            env: Extra environment variables for this command.
            timeout_sec: The time limit; ``None`` uses the run phase timeout.
            on_output: Receives ``(stream, text)`` chunks while the command runs.
            sudo: Not supported; a local sandbox never runs commands as root.

        Returns:
            The exit code and the captured output.

        Raises:
            RuntimeError: The sandbox is not running, or it stopped while the
                command started. The command is killed first.
            SandboxRejectedError: ``sudo`` is set.
            ValueError: ``cwd`` leaves the temp directory.
            RunTimeoutError: The command ran past the time limit. Its whole
                process group is killed first.
        """
        root = self._require_running()
        if sudo:
            raise SandboxRejectedError("A local sandbox cannot run commands as root.")
        workdir = self._resolve(cwd) if cwd is not None else root
        limit = timeout_sec if timeout_sec is not None else self._default_timeout
        spawn = asyncio.ensure_future(
            asyncio.create_subprocess_exec(
                "bash",
                *_SHELL_FLAGS,
                command,
                cwd=workdir,
                env={**self._base_env(root), **(env or {})},
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        )
        try:
            process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            # A cancelled spawn makes asyncio kill only the shell pid, so a
            # child that the shell already forked would outlive exec and stop().
            # Let the spawn finish, then kill the whole new group. A second
            # cancel must not end the wait early, or nobody kills the group.
            while not spawn.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait({spawn})
            if not spawn.cancelled() and spawn.exception() is None:
                await asyncio.shield(_end_process(spawn.result()))
            raise
        # A stop() that ran during the spawn did not see this group, so the
        # command would run on in a removed directory. A restart gives a new root.
        if self._root is not root:
            await asyncio.shield(_end_process(process))
            raise RuntimeError("The local sandbox is not running.")
        # With start_new_session the shell leads a new group whose id is its pid.
        self._groups.add(process.pid)
        stdout: list[str] = []
        stderr: list[str] = []
        finished = False
        try:
            async with asyncio.timeout(limit) as deadline:
                exit_code = await _collect(process, stdout, stderr, on_output)
            finished = True
        except TimeoutError:
            # The output sink can raise its own TimeoutError; only an expired
            # deadline is a run timeout.
            if not deadline.expired():
                raise
            raise RunTimeoutError(limit) from None
        finally:
            # A timeout, a cancel or an output-sink error must not leave the
            # command or its children running on the host.
            if not finished:
                await asyncio.shield(_end_process(process))
            if not _group_exists(process.pid):
                self._groups.discard(process.pid)
        return ExecResult(exit_code=exit_code, stdout="".join(stdout), stderr="".join(stderr))

    async def upload_files(self, files: Mapping[str, str]) -> None:
        """Write UTF-8 text files into the temp directory, creating parent directories.

        Args:
            files: Maps each sandbox path to its text content.

        Raises:
            RuntimeError: The sandbox is not running.
            ValueError: A path leaves the temp directory.
        """
        for path, text in files.items():
            target = self._resolve(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode("utf-8"))

    async def download_file(self, path: str) -> str | None:
        """Read one UTF-8 text file from the temp directory.

        Args:
            path: The sandbox path.

        Returns:
            The exact file content, or ``None`` when the file does not exist.

        Raises:
            RuntimeError: The sandbox is not running.
            ValueError: The path leaves the temp directory.
        """
        target = self._resolve(path)
        if not target.is_file():
            return None
        # Bytes, not read_text: universal newlines would turn "\r\n" into "\n".
        return target.read_bytes().decode("utf-8")

    @classmethod
    async def stop_job_sandboxes(cls, job_id: str) -> int:
        """Stop every running local sandbox in this process that carries the job tag.

        Args:
            job_id: The value of the ``JOB_TAG`` tag.

        Returns:
            The number of sandboxes that the sweep stopped.
        """
        stopped = 0
        for sandbox in list(cls._running):
            if sandbox.spec.tags.get(JOB_TAG) != job_id:
                continue
            try:
                await sandbox.stop()
            except Exception:
                logger.exception("Could not stop a local sandbox of job %s.", job_id)
                continue
            stopped += 1
        return stopped

    def _require_running(self) -> Path:
        """Return the temp directory, or raise when the sandbox is stopped.

        Returns:
            The temp directory.

        Raises:
            RuntimeError: The sandbox is not running.
        """
        if self._root is None:
            raise RuntimeError("The local sandbox is not running.")
        return self._root

    def _resolve(self, path: str) -> Path:
        """Map a sandbox path to a host path inside the temp directory.

        Args:
            path: A path relative to the temp directory, or an absolute path inside it.

        Returns:
            The resolved host path.

        Raises:
            RuntimeError: The sandbox is not running.
            ValueError: The path leaves the temp directory.
        """
        root = self._require_running()
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            raise ValueError(f"The sandbox path leaves the local sandbox directory: {path!r}")
        return target

    def _base_env(self, root: Path) -> dict[str, str]:
        """Build the scrubbed environment that every command starts from.

        Args:
            root: The temp directory.

        Returns:
            The base variables, then the spec variables.
        """
        # Not .resolve(): in a venv that follows the symlink to the base
        # interpreter, putting a python3 without the venv's packages on PATH.
        interpreter_bin = str(Path(sys.executable).parent)
        return {
            "PATH": os.pathsep.join([interpreter_bin, os.environ.get("PATH", "/usr/bin:/bin")]),
            "HOME": str(root),
            "TMPDIR": str(root / "tmp"),
            "LANG": "C.UTF-8",
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            **{name: os.environ[name] for name in _ENV_PASSTHROUGH if name in os.environ},
            **self.spec.env,
        }


async def _collect(
    process: asyncio.subprocess.Process,
    stdout: list[str],
    stderr: list[str],
    on_output: OutputSink | None,
) -> int:
    """Read both pipes to the end, then wait for the exit code.

    Args:
        process: The running command.
        stdout: Receives the stdout text chunks.
        stderr: Receives the stderr text chunks.
        on_output: Receives ``(stream, text)`` chunks as they arrive.

    Returns:
        The exit code of the command.
    """
    assert process.stdout is not None
    assert process.stderr is not None
    await asyncio.gather(
        _pump("stdout", process.stdout, stdout, on_output),
        _pump("stderr", process.stderr, stderr, on_output),
    )
    return await process.wait()


async def _pump(name: str, stream: asyncio.StreamReader, sink: list[str], on_output: OutputSink | None) -> None:
    """Decode one pipe as UTF-8 and hand each chunk on.

    Args:
        name: ``"stdout"`` or ``"stderr"``.
        stream: The pipe.
        sink: Receives the decoded chunks.
        on_output: Receives ``(name, text)`` for each decoded chunk.
    """
    # Incremental, so a multi-byte character split across two reads decodes once.
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    while True:
        data = await stream.read(_READ_CHUNK)
        text = decoder.decode(data, final=not data)
        if text:
            sink.append(text)
            if on_output is not None:
                on_output(name, text)
        if not data:
            return


async def _end_process(process: asyncio.subprocess.Process) -> None:
    """Kill the command's process group, wait a short time for the shell to exit, and close its pipes.

    Args:
        process: The command whose shell leads the group.
    """
    _kill_group(process.pid)
    # Not process.wait(): it also waits for pipe EOF, and a grandchild that
    # called setsid() left the group, so it holds the pipes open after the kill.
    for _ in range(_EXIT_POLLS):
        if process.returncode is not None:
            break
        await asyncio.sleep(_EXIT_POLL_SEC)
    # Process has no public close; closing the transport drops our pipe ends,
    # so the readers end and the transport does not leak.
    process._transport.close()  # type: ignore[attr-defined]


def _kill_group(pgid: int) -> None:
    """Kill a process group, and accept a group that already exited.

    Args:
        pgid: The group id, which is the pid of the shell that leads it.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


def _group_exists(pgid: int) -> bool:
    """Say whether a process group still has at least one member.

    Args:
        pgid: The group id.

    Returns:
        True while a process in the group exists.
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _make_box_dir() -> Path:
    """Create a private temp directory with a ``tmp`` subdirectory for one sandbox.

    Returns:
        The resolved directory path.
    """
    root = Path(tempfile.mkdtemp(prefix="skynet-local-sandbox-")).resolve()
    (root / "tmp").mkdir()
    return root


def _remove_tree(root: Path) -> None:
    """Remove a sandbox directory and log a failure instead of raising it.

    Args:
        root: The temp directory.
    """
    try:
        shutil.rmtree(root)
    except FileNotFoundError:
        return
    except OSError:
        logger.exception("Could not remove the local sandbox directory %s.", root)
