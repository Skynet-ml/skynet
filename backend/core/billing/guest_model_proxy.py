"""Relay sandbox-local model HTTP over supervisor-controlled stdout and files."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import stat
import subprocess
import sys
import threading
import time
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

RELAY_URL_ENV = "APP_BUDGET_RELAY_URL"
GATEWAY_URL_ENV = "ASCENT_MODEL_URL"
# The model of the run. A model request that names another model gets an error that names this variable (SKY-776).
MODEL_ENV = "ASCENT_MODEL"
# A built-in agent session gets its ASCENT_MODEL only in its own processes, so the harness bridge
# names it in this folder for the session's route key (SKY-884). A box without its own ASCENT_MODEL
# checks a request against the models named for the request's key. A scoped route key serves one model.
MODEL_BINDINGS_ENV = "APP_MODEL_BINDINGS"
# The provider prefixes that a built-in agent puts before ASCENT_MODEL: LiteLLM's and the harness provider's.
_AGENT_PREFIXES = ("openai/", "app/")
# The box user can write the bindings folder, so the proxy reads only short, one-line names from it.
_BINDING_MAX_BYTES = 256
_MODEL_NAME_MAX = 200
# The model routes whose body names its model at the top level: OpenAI chat, OpenAI Responses (Codex) and
# Anthropic messages. Only these exact paths: /v1/threads/<id>/messages is not a model call.
_MODEL_PATHS = frozenset({"/chat/completions", "/responses", "/messages"})
# The pinned sandbox image predates the APP_* names and reads only this one.
_LEGACY_RELAY_URL_ENV = "SKYNET_BUDGET_RELAY_URL"
_PRINT_LOCK = threading.Lock()
# Mirrors usage_tags.USAGE_TAGS_HEADERS: this file runs in the sandbox without the
# backend's code, and an older image still sends the legacy header.
_TAGS_HEADERS = frozenset({"x-app-usage-tags", "x-skynet-usage-tags"})
_FORWARDED_HEADERS = frozenset({"authorization", "x-api-key", "anthropic-version", "anthropic-beta"}) | _TAGS_HEADERS
# A role's .mcp.json points each server at <relay url>/mcp/<name> on this proxy.
MCP_PATH = "/v1/mcp/"
# The mailbox passes one JSON document per request, so an MCP request's HTTP method travels as this header.
METHOD_HEADER = "x-app-relay-method"
# The relay token of the role's MCP servers and services. The box holds no header for them, so the proxy adds it.
MCP_TOKEN_ENV = "APP_MCP_RELAY_TOKEN"
_MCP_HEADERS = frozenset({"accept", "content-type", "last-event-id", "mcp-protocol-version", "mcp-session-id"})
# Each [proposer] service is at <relay url>/service/<name>/ on this proxy, behind the same relay token (SKY-612).
SERVICE_PATH = "/v1/service/"
# A JSON list of the role's service names; the proxy gives the command one ASCENT_SERVICE_<NAME>_URL for each.
SERVICES_ENV = "APP_RELAY_SERVICES"
# A service takes any header but these: they belong to this connection or carry the box's own credential.
_SERVICE_DROPPED_HEADERS = frozenset(
    {
        "authorization",
        "connection",
        "content-length",
        "host",
        "keep-alive",
        "proxy-authorization",
        "te",
        "transfer-encoding",
        "upgrade",
        METHOD_HEADER,
    }
)
_RAW_PATHS = (MCP_PATH, SERVICE_PATH)


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
            """Keep scoped tokens and model input out of the command's ordinary logs.

            Args:
                format: The log format string.
                *args: The values for the format string.
            """

        def do_POST(self) -> None:
            """Relay an inference request through the trusted supervisor."""
            self._relay()

        def do_GET(self) -> None:
            """Relay a budget state check or an MCP event stream through the same supervisor."""
            self._relay()

        def do_DELETE(self) -> None:
            """Relay the end of an MCP session or a service DELETE; no other path takes DELETE."""
            if not self.path.startswith(_RAW_PATHS):
                self.send_error(405)
                return
            self._relay()

        def do_PUT(self) -> None:
            """Relay a service PUT; no other path takes PUT."""
            self._service_only()

        def do_PATCH(self) -> None:
            """Relay a service PATCH; no other path takes PATCH."""
            self._service_only()

        def _service_only(self) -> None:
            """Relay a request whose method only a service path takes."""
            if not self.path.startswith(SERVICE_PATH):
                self.send_error(405)
                return
            self._relay()

        def _relay(self) -> None:
            """Wait for the supervisor's response without contacting the internet."""
            mcp = self.path.startswith(MCP_PATH)
            service = self.path.startswith(SERVICE_PATH)
            raw = mcp or service
            length = int(self.headers.get("Content-Length", "0"))
            if (self.command == "POST" and length <= 0 and not service) or not 0 <= length <= 32 * 1024 * 1024:
                self.send_error(413)
                return
            body = self.rfile.read(length) if length else b"" if raw else b"{}"
            refusal = None if raw else _other_model_refusal(self.path, body, _request_key(self.headers), self.command)
            if refusal is not None:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(refusal)))
                self.end_headers()
                self.wfile.write(refusal)
                return
            identity = uuid4().hex
            if service:
                headers = {
                    key: value for key, value in self.headers.items() if key.lower() not in _SERVICE_DROPPED_HEADERS
                }
            else:
                forwarded = _FORWARDED_HEADERS | _MCP_HEADERS if mcp else _FORWARDED_HEADERS
                headers = {key: value for key, value in self.headers.items() if key.lower() in forwarded}
            if raw:
                headers = {key: value for key, value in headers.items() if key.lower() != METHOD_HEADER}
                headers[METHOD_HEADER] = self.command
                if os.environ.get(MCP_TOKEN_ENV):
                    headers = {key: value for key, value in headers.items() if key.lower() != "authorization"}
                    headers["Authorization"] = f"Bearer {os.environ[MCP_TOKEN_ENV]}"
            request = {
                "id": identity,
                "path": self.path,
                "body": base64.b64encode(body).decode(),
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
                # The MCP client left. Tell the supervisor, so it stops the stream and frees its place.
                stop = {"id": uuid4().hex, "stop": identity}
                with _PRINT_LOCK:
                    print(prefix + json.dumps(stop, separators=(",", ":")), flush=True)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    if not _runs_as_root():
        bindings = (response_dir.parent / "models").resolve()
        # Without the folder the box only loses the clearer error: the gateway still refuses another model.
        with contextlib.suppress(OSError):
            bindings.mkdir(exist_ok=True)
        if not bindings.is_symlink() and bindings.is_dir():
            # The handler reads the folder from this process's environment, as it reads ASCENT_MODEL.
            os.environ[MODEL_BINDINGS_ENV] = str(bindings)
    environment = {
        **os.environ,
        RELAY_URL_ENV: f"{endpoint}/v1",
        _LEGACY_RELAY_URL_ENV: f"{endpoint}/v1",
        "ANTHROPIC_BASE_URL": endpoint,
        "OPENAI_BASE_URL": f"{endpoint}/v1",
        GATEWAY_URL_ENV: f"{endpoint}/v1",
        **{
            service_env_name(name): f"{endpoint}{SERVICE_PATH}{quote(name, safe='')}/"
            for name in json.loads(os.environ.get(SERVICES_ENV) or "[]")
        },
    }
    try:
        return subprocess.run(sys.argv[2:], env=environment, check=False).returncode
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def model_matches(model: object, expected: str) -> bool:
    """Return whether a request's model name names the run's model.

    It mirrors only the transport-prefix rule of ``runner.model_matches_bound``,
    for the box's one ``ASCENT_MODEL``: this file runs in the box without the
    backend's code. The box knows only ``ASCENT_MODEL``, so the proxy refuses
    the helper's declared model name and a local subscription route's CLI name.

    Args:
        model: The model that the request names.
        expected: The run's model, from ``ASCENT_MODEL``.

    Returns:
        Whether the names match without their transport prefixes.
    """

    def normalized(value: str) -> str:
        """Remove transport prefixes without changing the provider model id.

        Args:
            value: A model name.

        Returns:
            The model name without its transport prefixes.
        """
        return value.strip().strip("/").removeprefix("litellm_proxy/").removeprefix("openrouter/")

    return isinstance(model, str) and normalized(model) == normalized(expected)


def agent_model_matches(model: object, expected: str) -> bool:
    """Return whether a built-in agent's request names its session's model.

    Args:
        model: The model that the request names.
        expected: The session's ``ASCENT_MODEL``.

    Returns:
        Whether the names match, also with a provider prefix that a built-in agent adds.
    """
    if not isinstance(model, str):
        return False
    names = [model, *(model.strip().removeprefix(prefix) for prefix in _AGENT_PREFIXES)]
    return any(model_matches(name, expected) for name in names)


def key_digest(key: str) -> str:
    """Return the folder name of a route key in the model bindings folder.

    Args:
        key: The route key.

    Returns:
        The key's SHA-256 hex digest, so the folder holds no key.
    """
    return hashlib.sha256(key.encode()).hexdigest()


def _request_key(headers: Message) -> str:
    """Return the route key that a model request sends.

    Args:
        headers: The request's headers.

    Returns:
        The bearer token or the ``x-api-key`` value, or an empty string.
    """
    bearer = headers.get("Authorization") or ""
    return bearer.removeprefix("Bearer ").strip() or (headers.get("x-api-key") or "").strip()


def _runs_as_root() -> bool:
    """Return whether this proxy runs as root.

    Returns:
        Whether the effective user is root; ``False`` on a platform without user ids.
    """
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0


def _bound_models(key: str) -> list[str]:
    """Return the models that built-in agent sessions named for a route key.

    A root proxy reads no bindings: only the scorer's supervisor runs it as
    root, the box user can rewrite its folder, and no built-in agent session
    runs there. A link or a special file in the folder could otherwise make
    root read a secret into the error, or hang a handler.

    Args:
        key: The request's route key.

    Returns:
        The valid model names, or an empty list when no session named one for the key.
    """
    folder = os.environ.get(MODEL_BINDINGS_ENV)
    if not folder or not key or _runs_as_root():
        return []
    directory = Path(folder) / key_digest(key)
    try:
        if not all(stat.S_ISDIR(os.lstat(path).st_mode) for path in (Path(folder), directory)):
            return []
        # The bridge writes each name to a dot file first, then renames it, so a dot file can be partial.
        names = sorted(entry.name for entry in directory.iterdir() if not entry.name.startswith("."))
    except OSError:
        return []
    return [model for model in (_read_binding(directory / name) for name in names) if model is not None]


def _read_binding(path: Path) -> str | None:
    """Read one model name from a regular file, never through a link.

    Args:
        path: A binding file.

    Returns:
        The model name, or ``None`` when the file is not a short, one-line, printable name.
    """
    # O_NONBLOCK keeps a FIFO from blocking the open; fstat then refuses it.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        raw = os.read(descriptor, _BINDING_MAX_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    try:
        model = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if len(raw) > _BINDING_MAX_BYTES or not 0 < len(model) <= _MODEL_NAME_MAX or not model.isprintable():
        return None
    return model


def _other_model_refusal(path: str, body: bytes, key: str = "", method: str = "POST") -> bytes | None:
    """Return the error body for a model request that names another model than ``ASCENT_MODEL``.

    The gateway refuses such a request too, but its error does not say which
    variable holds the model, so the box answers first. A box without its own
    ``ASCENT_MODEL`` checks the models that its built-in agent sessions named
    for the request's key (SKY-884).

    Args:
        path: The request path on this proxy.
        body: The raw request body.
        key: The request's route key.
        method: The request's HTTP method; only a POST calls a model.

    Returns:
        An OpenAI-shaped error body, or ``None`` when the request may go on.
    """
    box_model = os.environ.get(MODEL_ENV)
    if method != "POST" or path.split("?", 1)[0].removeprefix("/v1") not in _MODEL_PATHS:
        return None
    allowed = [box_model] if box_model else _bound_models(key)
    if not allowed:
        return None
    try:
        model = json.loads(body).get("model")
    except (ValueError, AttributeError, RecursionError):
        return None
    matches = model_matches if box_model else agent_model_matches
    if any(matches(model, name) for name in allowed):
        return None
    expected = ", ".join(allowed)
    message = f"This box serves only the model that {MODEL_ENV} names ({expected}), not {model!r}."
    error = {"message": message, "type": "invalid_request_error", "param": "model", "code": "model_not_allowed"}
    return json.dumps({"error": error}).encode()


def service_env_name(name: str) -> str:
    """Return the variable that holds one service's loopback address in the box.

    It mirrors ``runner.service_env_name``: this file runs in the box without
    the backend's code, and a role's code reads the same name in every box.

    Args:
        name: The ``[services.<name>]`` name.

    Returns:
        ``ASCENT_SERVICE_<NAME>_URL``, with each character other than a letter or digit as ``_``.
    """
    return "ASCENT_SERVICE_" + "".join(char if char.isalnum() else "_" for char in name.upper()) + "_URL"


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
