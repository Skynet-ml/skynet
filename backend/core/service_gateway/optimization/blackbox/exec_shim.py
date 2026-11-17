"""Record the commands an agent runs with no model call (SKY-715).

Some harnesses run commands that no ``tool_use`` names: a command parsed from
the model's text, or a scripted step. The bridge puts one wrapper for each
listed command name first on ``PATH``; the wrapper runs this file, which runs
the real command, passes its output through, and appends one JSON line with
the command line, exit code and output to the session's exec log. The parent
commits each line as a step (``harness_capture``).

Stdlib only: this file ships into the sandbox next to the bridge.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys

LOG_ENV = "APP_EXEC_LOG"
SHIM_DIR_ENV = "APP_EXEC_SHIM_DIR"
# Set for the children of a recorded command, so a nested listed command is
# part of its parent's record and is not logged a second time.
ACTIVE_ENV = "APP_EXEC_SHIM_ACTIVE"
# The log keeps only the tail of each stream: a command can print without limit.
OUTPUT_LIMIT = 64 * 1024
_NOT_FOUND = 127


def _real_command(name: str) -> str | None:
    """Find the command that the shim stands in for.

    Args:
        name: The command name.

    Returns:
        The path of the first match on ``PATH`` outside the shim folder, or ``None``.
    """
    shim_dir = os.path.realpath(os.environ.get(SHIM_DIR_ENV, ""))
    path = os.pathsep.join(
        part for part in os.environ.get("PATH", "").split(os.pathsep) if part and os.path.realpath(part) != shim_dir
    )
    return shutil.which(name, path=path)


def _tail(data: bytes) -> str:
    """Decode the end of one output stream for the log.

    Args:
        data: The captured bytes.

    Returns:
        At most ``OUTPUT_LIMIT`` bytes of the end, decoded with replacement.
    """
    return data[-OUTPUT_LIMIT:].decode("utf-8", errors="replace")


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
        argv: The command name, then its arguments.

    Returns:
        The command's exit code: 127 when it is not installed, 128 plus the
        signal number when a signal ended it.
    """
    name, args = argv[0], argv[1:]
    real = _real_command(name)
    log = os.environ.get(LOG_ENV)
    if os.environ.get(ACTIVE_ENV) or not log:
        if real is None:
            print(f"{name}: command not found", file=sys.stderr)
            return _NOT_FOUND
        os.execv(real, [name, *args])
    if real is None:
        stdout, stderr, code = b"", f"{name}: command not found\n".encode(), _NOT_FOUND
    else:
        done = subprocess.run(
            [name, *args], executable=real, capture_output=True, check=False, env={**os.environ, ACTIVE_ENV: "1"}
        )
        stdout, stderr = done.stdout, done.stderr
        code = done.returncode if done.returncode >= 0 else 128 - done.returncode
    sys.stdout.buffer.write(stdout)
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(stderr)
    sys.stderr.buffer.flush()
    _append(
        log, {"command": shlex.join([name, *args]), "exit_code": code, "stdout": _tail(stdout), "stderr": _tail(stderr)}
    )
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
