"""Record the commands an agent runs with no model call (SKY-715).

Some harnesses run commands that no ``tool_use`` names: a command parsed from
the model's text, or a scripted step. The bridge puts one wrapper for each
listed command name first on ``PATH``; the wrapper runs this file, which runs
the real command, passes its output through as it comes, and appends one JSON
line with the command line, exit code and output to the session's exec log.
The parent commits each line as a step (``harness_capture``).

Stdlib only, so it can run in the sandbox next to the bridge. The sandbox file
maps do not ship it yet.
"""

from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading

LOG_ENV = "APP_EXEC_LOG"
# Set for the children of a recorded command, so a nested listed command is
# part of its parent's record and is not logged a second time.
ACTIVE_ENV = "APP_EXEC_SHIM_ACTIVE"
# The log keeps only the tail of each stream: a command can print without limit.
OUTPUT_LIMIT = 64 * 1024
# The log keeps only the head of a longer command line, and ends it with this
# marker. Each argv byte that is not UTF-8 is six bytes in the JSON line, so
# with no cut a big argv makes a line the reader skips.
COMMAND_LIMIT = OUTPUT_LIMIT
COMMAND_CUT = " [cut]"
_NOT_FOUND = 127
_CANNOT_RUN = 126


def _real_command(shim_dir: str, name: str) -> str | None:
    """Find the command that the shim stands in for.

    Args:
        shim_dir: The wrapper folder. The wrapper line names it, so a child
            env that drops the ``APP_*`` vars cannot make the shim run itself.
        name: The command name.

    Returns:
        The path of the first match on ``PATH`` outside the shim folder, or ``None``.
    """
    shim_dir = os.path.realpath(shim_dir)
    path = os.pathsep.join(
        part for part in os.environ.get("PATH", "").split(os.pathsep) if part and os.path.realpath(part) != shim_dir
    )
    return shutil.which(name, path=path)


def _pump(source: io.BufferedReader, sink: int, tail: bytearray) -> None:
    """Copy each chunk of one child stream to the shim's stream at once, keeping its tail.

    When the reader closes early (``cmd | head``), the shim closes its end of
    the child's pipe, so the child's next write gets SIGPIPE or EPIPE, as it
    would with no shim. Without that, a command that writes forever never ends.

    The chunk goes to the raw descriptor, not ``sys.stdout.buffer``: a failed
    flush keeps the bytes in that buffer, and Python's flush at exit then
    prints a BrokenPipeError and exits 120 instead of the command's code.

    Args:
        source: The child's pipe.
        sink: The shim's own stdout or stderr descriptor.
        tail: Holds the last ``OUTPUT_LIMIT`` bytes of the stream.
    """
    while chunk := source.read1(OUTPUT_LIMIT):
        tail += chunk
        del tail[:-OUTPUT_LIMIT]
        try:
            view = memoryview(chunk)
            while view:
                view = view[os.write(sink, view) :]
        except OSError:
            source.close()
            return


def _write_stderr(message: bytes) -> None:
    """Write a message to fd 2, and drop it when fd 2 is closed or broken.

    Args:
        message: The bytes to write.
    """
    with contextlib.suppress(OSError):
        view = memoryview(message)
        while view:
            view = view[os.write(2, view) :]


def _spawn(real: str, name: str, args: list[str]) -> subprocess.Popen[bytes]:
    """Start the real command with piped output, as the shell would start it.

    Args:
        real: The command's path.
        name: The command name, its ``argv[0]``.
        args: Its arguments.

    Returns:
        The child process.

    Raises:
        OSError: When the command cannot run.
    """
    env = {**os.environ, ACTIVE_ENV: "1"}
    # The shell passes fds 3 and up through (``<(cmd)``, ``3>&1``, a make
    # jobserver), so the child keeps them. The shim's own fds are
    # non-inheritable (PEP 446), so none of them reach the child.
    pipe = subprocess.PIPE
    try:
        return subprocess.Popen([name, *args], executable=real, stdout=pipe, stderr=pipe, env=env, close_fds=False)
    except OSError as error:
        if error.errno != errno.ENOEXEC:
            raise
    # A script with no shebang line: the shell runs it with sh, so the shim does too.
    return subprocess.Popen(["/bin/sh", real, *args], stdout=pipe, stderr=pipe, env=env, close_fds=False)


def _exec(real: str, name: str, args: list[str]) -> int:
    """Replace the shim with the real command, as the shell would run it.

    Args:
        real: The command's path.
        name: The command name, its ``argv[0]``.
        args: Its arguments.

    Returns:
        126 when the command cannot run; otherwise it does not return.
    """
    # Python ignores SIGPIPE and SIGXFSZ at startup, and an exec keeps an
    # ignore; the shell would start the command with them at their defaults.
    # This is Python's own ignore, not one the caller set, so it is not kept.
    for number in (signal.SIGPIPE, signal.SIGXFSZ):
        signal.signal(number, signal.SIG_DFL)
    try:
        try:
            os.execv(real, [name, *args])
        except OSError as error:
            if error.errno != errno.ENOEXEC:
                raise
            os.execv("/bin/sh", [name, real, *args])
    except OSError as error:
        print(f"{name}: {error.strerror}", file=sys.stderr)
    return _CANNOT_RUN


def _run(real: str, name: str, args: list[str]) -> tuple[bytes, bytes, int]:
    """Run the real command, stream its output through and forward SIGTERM to it.

    A signal the shim inherited as ignored stays ignored, so the child
    inherits that too (``cmd &`` under a non-interactive ``sh`` ignores
    SIGINT). The child is in the shim's process group, so a SIGINT from the
    terminal already reaches it: the shim only stays alive for it and does
    not send it a second time. A SIGINT sent to the shim alone does not reach
    the child, except one that comes before the child starts.

    Args:
        real: The command's path.
        name: The command name, its ``argv[0]``.
        args: Its arguments.

    Returns:
        The stdout tail, the stderr tail and the exit code (128 plus the
        signal number when a signal ended it, 126 when it cannot run).
    """
    # The handlers go in before the child starts: a signal that comes before
    # the child exists waits in ``pending``, so the shim does not die and orphan it.
    started: list[subprocess.Popen[bytes]] = []
    pending: list[int] = []

    def forward(signum: int, _frame: object) -> None:
        """Send the signal to the child, or keep it until the child starts.

        Args:
            signum: The signal number.
            _frame: The interrupted frame.
        """
        if not started:
            pending.append(signum)
        elif signum != signal.SIGINT:
            started[0].send_signal(signum)

    for number in (signal.SIGTERM, signal.SIGINT):
        if signal.getsignal(number) is not signal.SIG_IGN:
            signal.signal(number, forward)
    try:
        process = _spawn(real, name, args)
    except OSError as error:
        message = f"{name}: {error.strerror}\n".encode()
        _write_stderr(message)
        return b"", message, _CANNOT_RUN
    started.append(process)
    for number in pending:
        process.send_signal(number)
    tails = (bytearray(), bytearray())
    pumps = [
        threading.Thread(target=_pump, args=(stream, sink, tail), daemon=True)
        for stream, sink, tail in (
            # Fixed fd numbers: with fd 1 or 2 closed at start, ``sys.stdout``
            # or ``sys.stderr`` is None, and a write to the closed fd fails
            # with an OSError that ``_pump`` handles.
            (process.stdout, 1, tails[0]),
            (process.stderr, 2, tails[1]),
        )
    ]
    for pump in pumps:
        pump.start()
    code = process.wait()
    for pump in pumps:
        pump.join()
    return bytes(tails[0]), bytes(tails[1]), code if code >= 0 else 128 - code


def _tail(data: bytes) -> str:
    """Decode one captured output stream for the log.

    Args:
        data: The captured bytes; ``_pump`` already keeps only the last ``OUTPUT_LIMIT``.

    Returns:
        The bytes, decoded with replacement.
    """
    return data.decode("utf-8", errors="replace")


def _command(name: str, args: list[str]) -> str:
    """Quote the command line for the log, cut to the first ``COMMAND_LIMIT`` bytes of the quoted line.

    Args:
        name: The command name.
        args: Its arguments; a byte that is not UTF-8 is a surrogate escape.

    Returns:
        The quoted line, with ``COMMAND_CUT`` at the end when it is cut.
    """
    line = shlex.join([name, *args]).encode("utf-8", "surrogateescape")
    if len(line) <= COMMAND_LIMIT:
        return line.decode("utf-8", "surrogateescape")
    return line[:COMMAND_LIMIT].decode("utf-8", "surrogateescape") + COMMAND_CUT


def _append(log: str, record: dict[str, object]) -> None:
    """Append one record to the exec log in a single write.

    Args:
        log: The log path.
        record: The command record.
    """
    line = (json.dumps(record) + "\n").encode("utf-8")
    # One O_APPEND write, so records of commands that run at the same time do not mix.
    handle = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(handle, line)
    finally:
        os.close(handle)


def main(argv: list[str]) -> int:
    """Run one listed command, pass its output through and log it.

    Args:
        argv: The shim folder, the command name, then its arguments.

    Returns:
        The command's exit code: 127 when it is not installed, 126 when it
        cannot run, 128 plus the signal number when a signal ended it.
    """
    shim_dir, name, args = argv[0], argv[1], argv[2:]
    real = _real_command(shim_dir, name)
    log = os.environ.get(LOG_ENV)
    if os.environ.get(ACTIVE_ENV) or not log:
        if real is None:
            print(f"{name}: command not found", file=sys.stderr)
            return _NOT_FOUND
        return _exec(real, name, args)
    if real is None:
        stdout, stderr, code = b"", f"{name}: command not found\n".encode(), _NOT_FOUND
        _write_stderr(stderr)
    else:
        stdout, stderr, code = _run(real, name, args)
    record = {"command": _command(name, args), "exit_code": code, "stdout": _tail(stdout), "stderr": _tail(stderr)}
    # The command already ran. A log that cannot be written (a read-only
    # folder, a full disk, a folder at the path) loses the record, but the
    # caller still gets the real exit code, and no extra stderr text.
    with contextlib.suppress(OSError):
        _append(log, record)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
