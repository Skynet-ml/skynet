"""Relay sandbox-local model HTTP over supervisor-controlled stdout and files."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

RELAY_URL_ENV = "APP_BUDGET_RELAY_URL"
GATEWAY_URL_ENV = "APP_GATEWAY_URL"
# The pinned sandbox image predates the APP_* names and reads only these.
_LEGACY_RELAY_URL_ENV = "SKYNET_BUDGET_RELAY_URL"
_LEGACY_GATEWAY_URL_ENV = "SKYNET_GATEWAY_URL"
_PRINT_LOCK = threading.Lock()
# Mirrors usage_tags.USAGE_TAGS_HEADERS: this file runs in the sandbox without the
# backend's code, and an older image still sends the legacy header.
_TAGS_HEADERS = frozenset({"x-app-usage-tags", "x-skynet-usage-tags"})
_FORWARDED_HEADERS = frozenset({"authorization", "x-api-key", "anthropic-version", "anthropic-beta"}) | _TAGS_HEADERS
# A role's .mcp.json points each server at <relay url>/mcp/<name> on this proxy.
MCP_PATH = "/v1/mcp/"
# The mailbox passes one JSON document per request, so an MCP request's HTTP method travels as this header.
METHOD_HEADER = "x-app-relay-method"
# The relay token of the role's MCP servers. The box's MCP file holds no header, so the proxy adds it.
MCP_TOKEN_ENV = "APP_MCP_RELAY_TOKEN"
_MCP_HEADERS = frozenset({"accept", "content-type", "last-event-id", "mcp-protocol-version", "mcp-session-id"})


def main() -> int:
    """Run a command with a local model proxy and no provider credentials.

    Returns:
        The wrapped command's exit status.
    """
    config = json.loads(Path(sys.argv[1]).read_text())
    response_dir = Path(config["response_dir"])
    response_dir.mkdir(parents=True, exist_ok=True)
    prefix = config["prefix"]
    timeout = float(config["timeout_seconds"])

    class Handler(BaseHTTPRequestHandler):
        """Forward model protocol bytes through the trusted supervisor's mailbox."""

        def log_message(self, format: str, *args: object) -> None:
            """Keep scoped tokens and model input out of the command's ordinary logs."""

        def do_POST(self) -> None:
            """Relay an inference request through the trusted supervisor."""
            self._relay()

        def do_GET(self) -> None:
            """Relay a budget state check or an MCP event stream through the same supervisor."""
            self._relay()

        def do_DELETE(self) -> None:
            """Relay the end of an MCP session; no other path takes DELETE."""
            if not self.path.startswith(MCP_PATH):
                self.send_error(405)
                return
            self._relay()

        def _relay(self) -> None:
            """Wait for the supervisor's response without contacting the internet."""
            mcp = self.path.startswith(MCP_PATH)
            length = int(self.headers.get("Content-Length", "0"))
            if (self.command == "POST" and length <= 0) or not 0 <= length <= 32 * 1024 * 1024:
                self.send_error(413)
                return
            identity = uuid4().hex
            forwarded = _FORWARDED_HEADERS | _MCP_HEADERS if mcp else _FORWARDED_HEADERS
            headers = {key: value for key, value in self.headers.items() if key.lower() in forwarded}
            if mcp:
                headers = {key: value for key, value in headers.items() if key.lower() != METHOD_HEADER}
                headers[METHOD_HEADER] = self.command
                if os.environ.get(MCP_TOKEN_ENV):
                    headers = {key: value for key, value in headers.items() if key.lower() != "authorization"}
                    headers["Authorization"] = f"Bearer {os.environ[MCP_TOKEN_ENV]}"
            request = {
                "id": identity,
                "path": self.path,
                "body": base64.b64encode(self.rfile.read(length) if length else b"" if mcp else b"{}").decode(),
                "headers": headers,
            }
            with _PRINT_LOCK:
                print(prefix + json.dumps(request, separators=(",", ":")), flush=True)
            document = _wait(response_dir / f"{identity}.json", timeout)
            if document is None:
                self.send_error(504, "The trusted model response is still pending.")
                return
            content = base64.b64decode(document["body"])
            self.send_response(document["status"])
            self.send_header("Content-Type", document["content_type"])
            for key, value in document.get("headers", ()):
                self.send_header(key, value)
            if not document.get("more"):
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            # An MCP event stream: each part comes as its own file, and the connection ends with the stream.
            self.close_connection = True
            self.end_headers()
            part = 0
            try:
                self.wfile.write(content)
                self.wfile.flush()
                while document is not None and document.get("more"):
                    part += 1
                    document = _wait(response_dir / f"{identity}.{part}.json", timeout)
                    if document is not None and document.get("body"):
                        self.wfile.write(base64.b64decode(document["body"]))
                        self.wfile.flush()
            except OSError:
                pass  # The MCP client closed its event stream.

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    environment = {
        **os.environ,
        RELAY_URL_ENV: f"{endpoint}/v1",
        _LEGACY_RELAY_URL_ENV: f"{endpoint}/v1",
        "ANTHROPIC_BASE_URL": endpoint,
        "OPENAI_BASE_URL": f"{endpoint}/v1",
        GATEWAY_URL_ENV: f"{endpoint}/v1",
        _LEGACY_GATEWAY_URL_ENV: f"{endpoint}/v1",
    }
    try:
        return subprocess.run(sys.argv[2:], env=environment, check=False).returncode
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _wait(path: Path, timeout: float) -> dict | None:
    """Read and remove one supervisor response file once it is complete.

    Args:
        path: The response file.
        timeout: Seconds to wait for it.

    Returns:
        The response document, or None when it did not come in time.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            document = json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            time.sleep(0.05)
            continue
        path.unlink(missing_ok=True)
        return document
    return None


if __name__ == "__main__":
    raise SystemExit(main())
