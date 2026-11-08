"""HTTP command server that runs inside a Singularity / Apptainer sandbox.

Adapted from Harbor 0.25.0 (Apache-2.0), singularity/server.py. Changes:

- Standard library only (``http.server``), so the image needs only
  ``python3``. Harbor's server needs FastAPI and uvicorn in a venv.
- Every request must carry the token from ``SKYNET_SERVER_TOKEN``. The
  server listens on the host loopback, which other local users can reach.
  The server removes the token from its environment, so commands never see it.
- ``/exec`` keeps stdout and stderr apart, puts each command in its own
  process group, and kills the whole group on a timeout or a ``/kill``.
- ``/upload`` and ``/download`` move text files, so no ``tar`` or bind mount
  is needed for file transfer.
- The server shuts itself down after ``--lifetime-seconds``, so a worker that
  crashes does not leave the sandbox running for ever.

The provider copies this file into the sandbox. It must run on the
``python3`` of any common image, so it uses no third-party package.
"""

from __future__ import annotations

import argparse
import contextlib
import hmac
import json
import os
import shlex
import signal
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

TOKEN_ENV = "SKYNET_SERVER_TOKEN"
TOKEN_HEADER = "X-Skynet-Token"
TIMEOUT_EXIT_CODE = 124
_SHELL = ("bash", "--noprofile", "--norc", "-c")


class CommandServer(ThreadingHTTPServer):
    """Hold the server state that every request handler shares."""

    daemon_threads = True

    def __init__(self, port: int, workdir: Path, token: str) -> None:
        """Bind the loopback port and set up the command table.

        Args:
            port: The TCP port on 127.0.0.1.
            workdir: The directory that relative paths and commands start in.
            token: The token that every request must send.
        """
        super().__init__(("127.0.0.1", port), Handler)
        self.workdir = workdir
        self.token = token
        self.lock = threading.Lock()
        self.processes: dict[str, subprocess.Popen[bytes]] = {}
        self.cancelled: set[str] = set()

    def kill(self, request_id: str) -> None:
        """Kill the process group of one command, now or when it starts.

        Args:
            request_id: The id that the ``/exec`` request sent.
        """
        with self.lock:
            process = self.processes.get(request_id)
            if process is None:
                # The kill can arrive before the command starts.
                self.cancelled.add(request_id)
                return
        kill_group(process)

    def kill_all(self) -> None:
        """Kill the process group of every running command."""
        with self.lock:
            running = list(self.processes.values())
        for process in running:
            kill_group(process)

    def stop_soon(self) -> None:
        """Kill every command and stop serving, from any thread."""
        self.kill_all()
        threading.Thread(target=self.shutdown, daemon=True).start()


def kill_group(process: subprocess.Popen[bytes]) -> None:
    """Send SIGKILL to the process group of a command.

    Args:
        process: The command, which leads its own process group.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)


def resolve(workdir: Path, path: str) -> Path:
    """Return a sandbox path, with a relative path under the working directory.

    Args:
        workdir: The working directory.
        path: The absolute or relative path.

    Returns:
        The absolute path.
    """
    candidate = Path(path)
    return candidate if candidate.is_absolute() else workdir / candidate


def run_command(server: CommandServer, body: dict[str, Any]) -> dict[str, Any]:
    """Run one shell command and wait for it, or for its timeout.

    Args:
        server: The server state.
        body: The ``/exec`` request: ``id``, ``command``, ``cwd``, ``env`` and
            ``timeout`` (seconds or null).

    Returns:
        The exit code, stdout, stderr and whether the timeout fired.
    """
    request_id = str(body["id"])
    script = str(body["command"])
    if body.get("cwd"):
        script = f"cd -- {shlex.quote(str(body['cwd']))} && {script}"
    env = dict(os.environ)
    env.update({str(key): str(value) for key, value in (body.get("env") or {}).items()})
    timeout = body.get("timeout")
    process = subprocess.Popen(
        [*_SHELL, script],
        cwd=str(server.workdir),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    with server.lock:
        server.processes[request_id] = process
        cancelled = request_id in server.cancelled
        server.cancelled.discard(request_id)
    if cancelled:
        kill_group(process)
    timed_out = False
    try:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_group(process)
            stdout, stderr = process.communicate()
            timed_out = True
    finally:
        with server.lock:
            server.processes.pop(request_id, None)
    exit_code = TIMEOUT_EXIT_CODE if timed_out else process.returncode
    return {
        "exit_code": exit_code,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
        "timed_out": timed_out,
    }


def write_file(server: CommandServer, body: dict[str, Any]) -> dict[str, Any]:
    """Write one text file, creating its parent directories.

    Args:
        server: The server state.
        body: The ``/upload`` request: ``path`` and ``content``.

    Returns:
        An empty answer.
    """
    target = resolve(server.workdir, str(body["path"]))
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        handle.write(str(body["content"]))
    return {}


def read_file(server: CommandServer, body: dict[str, Any]) -> dict[str, Any]:
    """Read one text file.

    Args:
        server: The server state.
        body: The ``/download`` request: ``path``.

    Returns:
        ``found`` and, when the file exists, its ``content``.
    """
    target = resolve(server.workdir, str(body["path"]))
    if not target.is_file():
        return {"found": False}
    with target.open(encoding="utf-8", errors="replace", newline="") as handle:
        return {"found": True, "content": handle.read()}


class Handler(BaseHTTPRequestHandler):
    """Answer one request to the command server."""

    server: CommandServer

    def log_message(self, format: str, *args: Any) -> None:
        """Write no access log; command lines must not reach the server log.

        Args:
            format: The log format string.
            *args: The format arguments.
        """

    def _authorized(self) -> bool:
        """Check the request token, and answer 403 when it is wrong.

        Returns:
            True when the token matches.
        """
        sent = self.headers.get(TOKEN_HEADER, "")
        if hmac.compare_digest(sent.encode("utf-8"), self.server.token.encode("utf-8")):
            return True
        self._answer(403, {"error": "bad token"})
        return False

    def _answer(self, status: int, payload: dict[str, Any]) -> None:
        """Send a JSON answer.

        Args:
            status: The HTTP status code.
            payload: The JSON body.
        """
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        """Answer ``/health``."""
        if not self._authorized():
            return
        if self.path == "/health":
            self._answer(200, {"ok": True})
        else:
            self._answer(404, {"error": "not found"})

    def do_POST(self) -> None:
        """Answer ``/exec``, ``/kill``, ``/upload``, ``/download`` and ``/shutdown``."""
        if not self._authorized():
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._answer(400, {"error": "the body is not JSON"})
            return
        try:
            if self.path == "/exec":
                self._answer(200, run_command(self.server, body))
            elif self.path == "/kill":
                self.server.kill(str(body["id"]))
                self._answer(200, {})
            elif self.path == "/upload":
                self._answer(200, write_file(self.server, body))
            elif self.path == "/download":
                self._answer(200, read_file(self.server, body))
            elif self.path == "/shutdown":
                self._answer(200, {})
                self.server.stop_soon()
            else:
                self._answer(404, {"error": "not found"})
        except (KeyError, OSError, TypeError, ValueError) as error:
            self._answer(500, {"error": f"{type(error).__name__}: {error}"})


def main() -> None:
    """Parse the arguments and serve until ``/shutdown`` or the lifetime ends."""
    parser = argparse.ArgumentParser(description="Skynet sandbox command server")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--lifetime-seconds", type=float, default=None)
    args = parser.parse_args()
    token = os.environ.pop(TOKEN_ENV, "")
    if not token:
        raise SystemExit(f"{TOKEN_ENV} is not set.")
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    server = CommandServer(args.port, workdir, token)
    # The default SIGTERM action skips the ``finally`` below, which would
    # leave running commands behind in their own sessions.
    signal.signal(signal.SIGTERM, lambda signum, frame: server.stop_soon())
    if args.lifetime_seconds:
        timer = threading.Timer(args.lifetime_seconds, server.stop_soon)
        timer.daemon = True
        timer.start()
    try:
        server.serve_forever()
    finally:
        server.kill_all()
        server.server_close()


if __name__ == "__main__":
    main()
