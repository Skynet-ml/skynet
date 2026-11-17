"""Run the agent chat's shell commands on the Mac app, plus the parts both runners share.

The chat agent reads attached files with two tools shaped like Codex's
unified exec: ``exec_command`` starts a command and waits a short time for
its output, and ``write_stdin`` sends input to a command that still runs or
reads its newer output. The web app runs the commands in a Vercel Sandbox
(:mod:`.attachment_sandbox`). The Mac app runs them here, on the user's own
machine, under a ``sandbox-exec`` profile like the one of
:mod:`core.billing.host_agent`: writes only in the chat's workspace, no
network, no reads in the home folder outside the workspace and the Python
install, and a clean environment without the backend's keys.
"""

from __future__ import annotations

import codecs
import contextlib
import itertools
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

DEFAULT_YIELD_MS = 10_000
MAX_YIELD_MS = 30_000
DEFAULT_MAX_OUTPUT_TOKENS = 10_000
# Codex counts about four bytes of output as one token.
CHARS_PER_TOKEN = 4
# The most output one session holds between two reads, at each end.
KEEP_CHARS = 200_000
MAX_SESSIONS = 64
# Codex never ends the most recently used sessions to make room for a new one.
PROTECTED_SESSIONS = 8
SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
_SHELL = "/bin/bash"


def clamp_yield_ms(yield_time_ms: int | None) -> int:
    """Return the wait for a command's output, in milliseconds.

    Args:
        yield_time_ms: The wait the model asked for, or None for the default.

    Returns:
        The wait, from 0 to ``MAX_YIELD_MS``.
    """
    if yield_time_ms is None:
        return DEFAULT_YIELD_MS
    return max(0, min(int(yield_time_ms), MAX_YIELD_MS))


def cut_output(text: str, *, max_output_tokens: int | None) -> tuple[str, int | None]:
    """Cut long output to its start and its end, as Codex does.

    Args:
        text: The command output.
        max_output_tokens: The token budget; None uses the default.

    Returns:
        The kept text, and the original token count when the text was cut.
    """
    budget = max(1, max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS) * CHARS_PER_TOKEN
    if len(text) <= budget:
        return text, None
    half = budget // 2
    cut = len(text) - 2 * half
    return f"{text[:half]}\n…{cut} chars cut…\n{text[-half:]}", -(-len(text) // CHARS_PER_TOKEN)


def format_result(
    *,
    wall_seconds: float,
    exit_code: int | None,
    session_id: int | None,
    output: str,
    max_output_tokens: int | None,
) -> str:
    """Write a command result in the shape of a Codex unified exec result.

    Args:
        wall_seconds: The time the call waited.
        exit_code: The exit code, or None while the command runs.
        session_id: The session to pass to ``write_stdin`` while it runs.
        output: The output since the last read.
        max_output_tokens: The token budget of the output.

    Returns:
        The result text for the model.
    """
    text, original_tokens = cut_output(output, max_output_tokens=max_output_tokens)
    lines = [f"Wall time: {wall_seconds:.3f} seconds"]
    if exit_code is None:
        lines.append(f"Process running with session ID {session_id}")
    else:
        lines.append(f"Process exited with code {exit_code}")
    if original_tokens is not None:
        lines.append(f"Original token count: {original_tokens}")
    return "\n".join(lines) + "\nOutput:\n" + text


class CommandSession:
    """Hold the output of one command between the reads of the model."""

    def __init__(self, session_id: int, *, keep_chars: int = KEEP_CHARS) -> None:
        """Start an empty session.

        Args:
            session_id: The id the model passes to ``write_stdin``.
            keep_chars: The most unread output kept at each end.
        """
        self.id = session_id
        self.last_used = time.monotonic()
        self.exit_code: int | None = None
        self.process: subprocess.Popen[bytes] | None = None
        self._keep = keep_chars
        self._head = ""
        self._tail = ""
        self._dropped = 0
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)

    @property
    def done(self) -> bool:
        """Tell whether the command ended."""
        return self._done.is_set()

    def append(self, text: str) -> None:
        """Add new output.

        Args:
            text: The output chunk.
        """
        with self._changed:
            room = self._keep - len(self._head)
            if room > 0:
                self._head += text[:room]
                text = text[room:]
            if text:
                self._tail += text
                if len(self._tail) > self._keep:
                    self._dropped += len(self._tail) - self._keep
                    self._tail = self._tail[-self._keep :]
            self._changed.notify_all()

    def finish(self, exit_code: int) -> None:
        """Mark the command as ended.

        Args:
            exit_code: The command's exit code.
        """
        with self._changed:
            self.exit_code = exit_code
            self._done.set()
            self._changed.notify_all()

    def read(self, yield_ms: int) -> tuple[str, int | None]:
        """Wait until the command ends or the wait runs out, then take the new output.

        Args:
            yield_ms: The longest wait in milliseconds.

        Returns:
            The output since the last read, and the exit code once the command ended.
        """
        deadline = time.monotonic() + yield_ms / 1000
        with self._changed:
            while not self._done.is_set():
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._changed.wait(left)
            text = self._head
            if self._dropped:
                text += f"\n…{self._dropped} chars cut…\n"
            text += self._tail
            self._head, self._tail, self._dropped = "", "", 0
            return text, self.exit_code if self._done.is_set() else None


class SessionTable:
    """Keep a chat's command sessions and their ids, as Codex does."""

    def __init__(self) -> None:
        """Start with no sessions."""
        self._sessions: dict[int, CommandSession] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def open(self) -> CommandSession:
        """Make a new session, ending an old one when the table is full.

        Returns:
            The new session.
        """
        with self._lock:
            self._prune()
            session = CommandSession(next(self._ids))
            self._sessions[session.id] = session
            return session

    def get(self, session_id: int) -> CommandSession | None:
        """Find a session by id.

        Args:
            session_id: The id the model passed.

        Returns:
            The session, or None when it is unknown or ended and read.
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                session.last_used = time.monotonic()
            return session

    def collect(self, session: CommandSession, *, started: float, yield_ms: int, max_output_tokens: int | None) -> str:
        """Read a session's new output and write the result text.

        An ended session leaves the table once its last output is read.

        Args:
            session: The session to read.
            started: ``time.monotonic()`` when the tool call started.
            yield_ms: The longest wait in milliseconds.
            max_output_tokens: The token budget of the output.

        Returns:
            The result text for the model.
        """
        output, exit_code = session.read(yield_ms)
        if exit_code is not None:
            with self._lock:
                self._sessions.pop(session.id, None)
        return format_result(
            wall_seconds=time.monotonic() - started,
            exit_code=exit_code,
            session_id=session.id,
            output=output,
            max_output_tokens=max_output_tokens,
        )

    def all(self) -> list[CommandSession]:
        """Take every session out of the table.

        Returns:
            The sessions.
        """
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            return sessions

    def _prune(self) -> None:
        """End the least recently used session outside the most recently used ones when the table is full."""
        if len(self._sessions) < MAX_SESSIONS:
            return
        by_use = sorted(self._sessions.values(), key=lambda session: session.last_used)
        protected = {session.id for session in by_use[-PROTECTED_SESSIONS:]}
        finished = [session for session in by_use if session.done and session.id not in protected]
        victim = (finished or [session for session in by_use if session.id not in protected])[0]
        self._sessions.pop(victim.id, None)
        self._end(victim)

    def _end(self, session: CommandSession) -> None:
        """Stop a session's command.

        Args:
            session: The session to stop.
        """


def _real(path: Path | str) -> str:
    """Return the real path, as Seatbelt compares real paths only.

    Args:
        path: Any path.

    Returns:
        The absolute path with every link resolved.
    """
    return os.path.realpath(path)


def _python_root(path: str, home: str, workspace: str) -> str:
    """Return a Python folder the command may read, or the workspace when it is unsafe.

    Args:
        path: A real Python install or venv folder.
        home: The user's real home folder.
        workspace: The chat's real workspace.

    Returns:
        ``path``, or ``workspace`` when ``path`` is the home, holds it, or is a
        folder directly in it (``~/.cargo`` holds ``credentials.toml``), as
        ``host_agent`` rules for a CLI's install folder.
    """
    common = os.path.commonpath([path, home])
    if common == path or (common == home and len(Path(path).relative_to(home).parts) < 2):
        return workspace
    return path


def seatbelt_profile() -> str:
    """Return the ``sandbox-exec`` profile of a chat command.

    Later rules win, so each allow opens a hole in the denies above it. The
    command may read the system and the Python install, and read and write
    only its workspace in the hidden folders.

    Returns:
        The profile; ``WORKSPACE``, ``HOME_DIR``, ``TEMP_DIR``, ``PYTHON``
        and ``VENV`` are its parameters.
    """
    return "\n".join(
        [
            "(version 1)",
            "(allow default)",
            "(deny network*)",
            "(deny file-write*)",
            '(deny file-read-data file-read-xattr (subpath (param "HOME_DIR")) (subpath (param "TEMP_DIR")))',
            '(allow file-write* (subpath "/dev"))',
            '(deny process-exec (literal "/bin/launchctl"))',
            # `open` asks LaunchServices to start an app outside this sandbox.
            '(deny mach-lookup (global-name "com.apple.coreservices.launchservicesd")'
            ' (global-name "com.apple.coreservices.quarantine-resolver"))',
            "(deny job-creation)",
            "(deny appleevent-send)",
            '(allow file-read-data file-read-xattr (subpath (param "PYTHON")) (subpath (param "VENV")))',
            '(allow file-read-data file-read-xattr file-write* (subpath (param "WORKSPACE")))',
        ]
    )


class LocalCommandRunner(SessionTable):
    """Run one chat's commands on this Mac, inside its workspace."""

    def __init__(self, workspace: Path) -> None:
        """Make the workspace folder.

        Args:
            workspace: The chat's folder; attached files go to ``attachments/`` in it.
        """
        super().__init__()
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / ".tmp").mkdir(exist_ok=True)
        self.workspace = workspace.resolve()

    def command_env(self) -> dict[str, str]:
        """Return the clean environment of a command, without the backend's keys.

        Returns:
            The environment; the backend's Python comes first on ``PATH``.
        """
        return {
            "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin",
            "HOME": str(self.workspace),
            "TMPDIR": str(self.workspace / ".tmp"),
            "LANG": "en_US.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def _argv(self, cmd: str) -> list[str]:
        """Wrap a shell command in the chat's ``sandbox-exec`` profile.

        Args:
            cmd: The shell command.

        Returns:
            The command line to start.
        """
        workspace = _real(self.workspace)
        home = _real(Path.home())
        params = {
            "WORKSPACE": workspace,
            "HOME_DIR": home,
            "TEMP_DIR": _real(tempfile.gettempdir()),
            "PYTHON": _python_root(_real(sys.base_prefix), home, workspace),
            # The Mac app's venv is in the home folder, and Python needs its ``pyvenv.cfg``.
            "VENV": _python_root(_real(sys.prefix), home, workspace),
        }
        defines = [item for key, value in params.items() for item in ("-D", f"{key}={value}")]
        return [str(SANDBOX_EXEC), "-p", seatbelt_profile(), *defines, _SHELL, "-c", cmd]

    def _workdir(self, workdir: str | None) -> Path | None:
        """Resolve a working directory inside the workspace.

        Args:
            workdir: A path relative to the workspace, or None for the workspace.

        Returns:
            The directory, or None when it leaves the workspace.
        """
        path = (self.workspace / (workdir or ".")).resolve()
        return path if path == self.workspace or self.workspace in path.parents else None

    def exec_command(
        self,
        cmd: str,
        workdir: str | None = None,
        yield_time_ms: int | None = None,
        max_output_tokens: int | None = None,
    ) -> str:
        """Start a command and wait a short time for its output.

        Args:
            cmd: The shell command.
            workdir: The directory to run in, relative to the workspace.
            yield_time_ms: The longest wait for output.
            max_output_tokens: The token budget of the output.

        Returns:
            The result text for the model.
        """
        started = time.monotonic()
        session = self.start(cmd, workdir)
        if isinstance(session, str):
            return session
        return self.collect(
            session, started=started, yield_ms=clamp_yield_ms(yield_time_ms), max_output_tokens=max_output_tokens
        )

    def start(self, cmd: str, workdir: str | None = None) -> CommandSession | str:
        """Start a command in its own sandbox, and do not wait for it.

        Args:
            cmd: The shell command.
            workdir: The directory to run in, relative to the workspace.

        Returns:
            The command's session, or the error text when it cannot start.
        """
        cwd = self._workdir(workdir)
        if cwd is None or not cwd.is_dir():
            return "Error: the workdir must stay inside the workspace and exist."
        if sys.platform != "darwin" or not SANDBOX_EXEC.exists():
            return "Error: commands need macOS sandbox-exec, and this machine has none."
        session = self.open()
        try:
            session.process = subprocess.Popen(
                self._argv(cmd),
                cwd=cwd,
                env=self.command_env(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            session.finish(127)
            session.append(f"Could not start the command: {exc}")
        else:
            threading.Thread(target=self._pump, args=(session,), daemon=True).start()
        return session

    def write_stdin(
        self,
        session_id: int,
        chars: str | None = None,
        yield_time_ms: int | None = None,
        max_output_tokens: int | None = None,
    ) -> str:
        """Send input to a running command, or read its newer output.

        Args:
            session_id: The session id from an earlier result.
            chars: The input to send; empty only reads the output.
            yield_time_ms: The longest wait for output.
            max_output_tokens: The token budget of the output.

        Returns:
            The result text for the model.
        """
        started = time.monotonic()
        session = self.get(session_id)
        if session is None:
            return f"Error: Unknown session {session_id}. It ended, or it never existed."
        if chars and session.process is not None and session.process.stdin is not None and not session.done:
            try:
                session.process.stdin.write(chars.encode())
                session.process.stdin.flush()
            except OSError:
                pass
        return self.collect(
            session, started=started, yield_ms=clamp_yield_ms(yield_time_ms), max_output_tokens=max_output_tokens
        )

    def close(self) -> None:
        """Stop every command of the chat."""
        for session in self.all():
            self._end(session)

    def _pump(self, session: CommandSession) -> None:
        """Copy a command's output into its session until it ends.

        Args:
            session: The session of the started command.
        """
        process = session.process
        assert process is not None
        assert process.stdout is not None
        stdout = process.stdout.fileno()
        decoder = _decoder()
        for chunk in iter(lambda: os.read(stdout, 65536), b""):
            session.append(decoder.decode(chunk))
        session.append(decoder.decode(b"", final=True))
        session.finish(process.wait())

    def _end(self, session: CommandSession) -> None:
        """Stop a session's command and every process it started.

        Args:
            session: The session to stop.
        """
        process = session.process
        if process is None:
            return
        # The shell can exit before a background child, so the group still runs.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)


def _decoder() -> codecs.IncrementalDecoder:
    """Return a UTF-8 decoder that keeps a character split across two chunks.

    Returns:
        The incremental decoder.
    """
    return codecs.getincrementaldecoder("utf-8")(errors="replace")
