"""In-sandbox runner for python scorers: ``input.json`` → user code → ``output.json``.

This file is shipped verbatim into every scorer sandbox as ``app_runner.py``
and executed there by the box's own interpreter, so it depends on the standard
library alone and stays valid Python 3.9 (the default sandbox image's
version). The backend imports the same module for the pieces it shares with
the box — score normalization, entry-point discovery, the image wrapper — so
the two sides cannot drift apart.

Contract: ``python3 app_runner.py <call_dir>`` reads ``<call_dir>/input.json``::

    {"candidate": ..., "case": ..., "gateway": {...} | null}

The scorer source is written once per box to ``app_scorer_code.txt`` next
to the runner, never into a call's input, so the per-call files carry only
candidates and cases. A legacy input that still holds ``"code"`` wins over
that file.

and writes ``<call_dir>/output.json``::

    {"score": float | null, "side_info": {...}, "error": str | null, "usage": [...]}

A scorer that raised, returned an unusable value or failed to load is
reported through ``error``; the runner exits non-zero only when it is
itself broken (unreadable input, unwritable output). ``gateway`` names the
OpenAI-compatible endpoint the scorer's ``llm()`` helper talks to; its
``api_key`` may be absent when the sandbox injects the credential at the
network edge instead.

An input's optional ``stages`` (``{"prefix", "response_dir", "names", "services", "mcp", "role"}``)
lets the scorer reach the run's ``[target.<name>]`` stages through the
parent's relay with ``call(name, input)``. Its optional ``services`` list
names the ``[services.<name>]`` with a url that the metric may reach: each
gets ``SKYNET_SERVICE_<NAME>_URL``, a loopback address in the box whose
requests go through the same relay, so the service's key stays with the
parent. Its optional ``mcp`` list names the http servers of the role's own
MCP file: each gets ``SKYNET_MCP_<NAME>_URL`` on the same loopback port, and
``SKYNET_MCP_CONFIG`` names the role's own ``.mcp.json`` in the checkout. The
parent replaced that file before the box opened: each entry is a loopback url
on ``RELAY_PORT``, never the real url, a header or a secret. In a stage box,
``python3 app_runner.py --stage <call_dir> <path/file.py:function>`` runs
one stage call and writes ``{"output": ...}`` or ``{"error": ...}``. When the
stage's own MCP file holds a server, the parent also writes ``relay.json``
(the same relay config) into the call's folder: the stage then gets the same
loopback relay, checked against the stage's own role, and a command stage
runs as ``--stage-command <call_dir> <command> [<arg> ...]`` behind it.

A repository with a ``[run]`` entry runs the candidate first, in its own
candidate box (SKY-398). ``python3 app_runner.py --candidate <call_dir>``
reads ``{"entry", "input", "timeout", "stages"}``, starts the entry in
its own session with only the case's input (E42), and writes
``{"output", "error", "elapsed_s"}``. The parent then ends the box, and
every process the entry left ends with it; the metric runs in another box
that never holds the candidate's code.
A function entry (``path/file.py:function``) runs as ``--run <dir>
<entry>`` and its output is its JSON return value; a command entry
(``["pytest", "-q"]``) gets the input as JSON on stdin and its output is
``{"exit_code", "stdout"}`` (E41). The metric call then gets that output as
``score(repo_path, case, output)``, and may start no process of its own (E40).
Each relay frame names the box's role (``run`` or ``metric``), and the
parent refuses a frame whose role is not the box's own.
A metric that also names an ``elapsed_s`` parameter after ``output`` gets
the entry's run time in seconds as ``elapsed_s=...`` (SKY-642). The
supervisor measures it in its own process, from just before the entry
starts until the entry exits and its process group is killed, so the
candidate's code cannot patch the clock it reads.
"""

from __future__ import annotations

import base64
import ctypes
import http.server
import importlib.util
import inspect
import io
import json
import os
import pwd
import shutil
import signal
import site
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import types
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

INPUT_FILE = "input.json"
CODE_FILE = "app_scorer_code.txt"
OUTPUT_FILE = "output.json"
SCORER_FILENAME = "<scorer_code>"
# Names the scorer namespace binds the model helper and the image wrapper to.
LLM_HELPER_NAME = "llm"
# The gateway key rides in the environment so it never touches the box filesystem.
ENV_API_KEY = "APP_API_KEY"
ENV_BUDGET_RELAY_URL = "APP_BUDGET_RELAY_URL"
# This file runs alone in the box, without ``core.config``; boxes started by an
# older control plane still carry only the pre-rename spellings.
_LEGACY_ENV_PREFIX = "SKYNET_"
IMAGE_HELPER_NAME = "Image"
LOG_HELPER_NAME = "log"
# ``from skynet import call, StageError`` reaches the run's ``[target.<name>]`` stages.
STAGE_HELPER_NAME = "call"
STAGE_ERROR_NAME = "StageError"
# ``app_runner.py --stage <call_dir> <path/file.py:function>`` runs one stage call.
STAGE_FLAG = "--stage"
# ``app_runner.py --stage-command <call_dir> <command> [<arg> ...]`` runs one
# command stage call behind the stage's relay (SKY-611).
STAGE_COMMAND_FLAG = "--stage-command"
# A stage call's relay config, next to its input; only a stage whose own MCP file holds a server gets one.
STAGE_RELAY_FILE = "relay.json"
# The metric reaches each listed service at this variable's loopback address (E45).
SERVICE_ENV_PREFIX = "SKYNET_SERVICE_"
# Each server of the role's own MCP file is at this variable's loopback address (E49).
MCP_ENV_PREFIX = "SKYNET_MCP_"
# Names the role's own ``.mcp.json``, for MCP clients that read a config file.
MCP_CONFIG_ENV = "SKYNET_MCP_CONFIG"
# The box's loopback relay port. It is fixed because the parent writes each
# role's .mcp.json with loopback urls before the box opens (E49, E53).
RELAY_PORT = 47_311
# ``app_runner.py --candidate <call_dir>`` runs the ``[run]`` entry and stops what it left (E38).
CANDIDATE_FLAG = "--candidate"
# ``app_runner.py --run <dir> <path/file.py:function>`` is the candidate process of a function entry.
RUN_FLAG = "--run"
# The Vercel provider's exec wrapper writes the command's stdout, stderr and
# exit code to these files (``OUTPUT_PREFIX`` and ``EXIT_PREFIX`` in
# core/sandbox/providers/vercel.py). This file runs in the box on the standard
# library alone, so it cannot import them; a test keeps the two equal.
WRAPPER_OUTPUT_PREFIX = "/tmp/.app-output-"
WRAPPER_EXIT_PREFIX = "/tmp/.app-exit-"
MAX_RUN_STDOUT_BYTES = 1024 * 1024
# The metric parameter, the input key and the supervisor output key for the entry's run time (SKY-642).
ELAPSED_PARAM = "elapsed_s"
# The metric input key that names the folder the metric setup step installed
# the metric's packages into. ``-I`` reads no user or PYTHONPATH folder, and
# the image's venv has no system site, so the runner adds this one (SKY-398).
METRIC_SITE_PARAM = "metric_site"
_PR_SET_DUMPABLE = 4
RERUN_REFUSED = (
    "The metric started a process after the candidate ran. The metric must not run the candidate's code"
    " again: run its tests through the [run] entry, and read their report in the metric (its output"
    " argument)."
)
_PROCESS_EVENTS = frozenset(
    {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty"}
)
_STAGE_POLL_SECONDS = 0.05
# Frames one ``log()`` event on stderr; the host turns each into a run-log line.
EVENT_PREFIX = "APP_SCORER_EVENT "
# Frames the candidate supervisor's result on its own stdout. The entry never
# holds that stdout and runs as another user, so it cannot forge the result,
# unlike a file it can write (SKY-398).
RESULT_PREFIX = "APP_RUN_RESULT "
_EVENT_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
# The module scorer code imports those helpers from: ``from skynet import llm, Image``.
# Saved user scorers import it by this name, so it stays as it is.
HELPER_MODULE_NAME = "skynet"
# Where Vercel's egress proxy leaves its CA when a network policy rewrites headers.
PROXY_CA_PATH = "/usr/local/share/ca-certificates/vercel-proxy-ca.crt"
_ENTRYPOINT_NAMES = ("score", "metric")
_NUMBER_TYPES = (bool, int, float)
_RETRY_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504})
_ATTEMPTS = 3
_BACKOFF_SECONDS = 1.0
_ERROR_BODY_CHARS = 500
# The budget relay prices each call from its output cap and refuses an uncapped
# one, so llm() always sends a cap. Same floor the backend puts on its own
# chat-style LMs (language_models._DEFAULT_REASONING_MAX_TOKENS).
DEFAULT_MAX_TOKENS = 4000
_MEDIA_TYPE_BY_EXTENSION = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
}
_MEDIA_TYPE_BY_MAGIC = (
    (b"\x89PNG", "image/png"),
    (b"\xff\xd8", "image/jpeg"),
    (b"GIF8", "image/gif"),
    (b"RIFF", "image/webp"),
    (b"BM", "image/bmp"),
)
_IMAGES_HELP = (
    "llm(images=...) takes PNG/JPEG bytes, file paths, http(s) or data: URLs, "
    "base64 strings, PIL images or Image objects."
)

SideInfo = dict[str, Any]


class ScorerError(Exception):
    """A mistake in the scorer or its use of the helpers, reported to the user verbatim."""


class GatewayControl(BaseException):
    """Carry a protected admission or reconciliation signal past scorer exception handlers."""

    def __init__(self, code: str, message: str) -> None:
        """Retain the parent's stable control code and explanation.

        Args:
            code: Budget exhaustion, insufficient coverage, or pending usage.
            message: Parent-provided explanation.
        """
        super().__init__(message)
        self.code = code


class StageError(Exception):
    """A stage failed, returned no valid output, passed its timeout, or is not in this run."""


class StageClient:
    """Send ``call(name, input)`` to the parent's stage relay and wait for the stage's output.

    The box is offline: each request is one framed stdout line the parent
    reads, and the answer is a file the parent writes back into the box. The
    scorer's own timeout bounds the wait, and each stage's ``timeout`` bounds
    its call on the parent side.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        """Bind the relay's frame prefix, response folder and stage names.

        Args:
            config: ``{"prefix", "response_dir", "names", "services", "mcp", "role"}`` from the call's input.
        """
        self._prefix = str(config["prefix"])
        self._role = config.get("role")
        self._responses = Path(str(config["response_dir"]))
        self._names = tuple(str(name) for name in config.get("names") or ())
        self._lock = threading.Lock()

    def __call__(self, name: str, input: Any = None) -> Any:
        """Run one stage on ``input`` and return its JSON output.

        Args:
            name: The ``[target.<name>]`` stage to call.
            input: Any JSON value; the stage receives it as its only argument.

        Returns:
            The stage's output.

        Raises:
            StageError: When the name is not a stage of this run, the stage
                failed, returned no valid output, or passed its timeout.
            GatewayControl: When the run's budget stopped the call.
        """
        if name not in self._names:
            known = ", ".join(self._names) or "none"
            raise StageError(f"Unknown stage {name!r}; the stages of this run are: {known}.")
        response = self.exchange({"stage": name, "input": input})
        if isinstance(response.get("control"), dict):
            raise GatewayControl(str(response["control"].get("code")), str(response["control"].get("message")))
        if "output" in response:
            return response["output"]
        raise StageError(str(response.get("error") or f"Stage {name!r} returned no output."))

    def exchange(self, message: dict[str, Any]) -> dict[str, Any]:
        """Send one framed request to the parent's relay and wait for its answer.

        Args:
            message: The request, without its id.

        Returns:
            The parent's answer.
        """
        return next(self.answers(message))

    def answers(self, message: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Send one framed request, then yield its answer and each streamed part after it.

        An MCP event stream's answer has ``"more": true``; its parts follow in
        ``<id>.1.json``, ``<id>.2.json`` and on, until a part has ``"more": false``.
        Closing the generator before that part tells the parent to stop the stream.

        Args:
            message: The request, without its id.

        Yields:
            The parent's answer, then each part.
        """
        identity = self._send(message)
        response = self._wait(self._responses / f"{identity}.json")
        part = 0
        try:
            yield response
            while response.get("more"):
                part += 1
                response = self._wait(self._responses / f"{identity}.{part}.json")
                yield response
        except GeneratorExit:
            if response.get("more"):
                self._send({"stop": identity})
            raise

    def _send(self, message: dict[str, Any]) -> str:
        """Write one framed message to the parent's relay.

        Args:
            message: The message, without its id.

        Returns:
            The message's new id.
        """
        identity = os.urandom(16).hex()
        role = {"role": self._role} if self._role else {}
        frame = self._prefix + json.dumps({"id": identity, **message, **role})
        # The real stdout: a scorer that redirects sys.stdout must still reach the relay.
        stream = sys.__stdout__ or sys.stdout
        with self._lock:
            # The leading newline ends any partial line the scorer printed before this call.
            stream.write("\n" + frame + "\n")
            stream.flush()
        return identity

    def _wait(self, path: Path) -> dict[str, Any]:
        """Wait for the parent to write one answer file, then read and remove it.

        Args:
            path: The answer file.

        Returns:
            The answer, or ``{}`` when it is not an object.
        """
        while True:
            try:
                response = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, ValueError):
                # ValueError: the parent's write is still in progress.
                time.sleep(_STAGE_POLL_SECONDS)
                continue
            break
        path.unlink()
        return response if isinstance(response, dict) else {}


def service_env_name(name: str) -> str:
    """Return the variable that holds one service's loopback address.

    Args:
        name: The ``[services.<name>]`` name.

    Returns:
        ``SKYNET_SERVICE_<NAME>_URL``, with each character other than a letter or digit as ``_``.
    """
    return SERVICE_ENV_PREFIX + _env_part(name) + "_URL"


def mcp_env_name(name: str) -> str:
    """Return the variable that holds one MCP server's loopback address.

    Args:
        name: The server's name in the role's MCP file.

    Returns:
        ``SKYNET_MCP_<NAME>_URL``, with each character other than a letter or digit as ``_``.
    """
    return MCP_ENV_PREFIX + _env_part(name) + "_URL"


def _env_part(name: str) -> str:
    """Return a name as a variable part: upper case, each character other than a letter or digit as ``_``."""
    return "".join(char if char.isalnum() else "_" for char in name.upper())


def start_service_proxy(
    client: StageClient, names: Sequence[str], mcp: Sequence[str] = (), *, port: int = 0, mcp_file: str | None = None
) -> dict[str, str]:
    """Serve the run's services and MCP servers on a loopback port whose requests go through the parent's relay.

    The box is offline and never holds a key: the parent checks that the
    role may use the service or server, adds the key, calls it and sends
    its response back unchanged.

    Args:
        client: The relay client of this call.
        names: The services the role may reach.
        mcp: The servers of the role's own MCP file.
        port: The loopback port; ``0`` picks a free one.
        mcp_file: The role's ``.mcp.json``, relative to the working directory.

    Returns:
        ``SKYNET_SERVICE_<NAME>_URL`` for each service, ``SKYNET_MCP_<NAME>_URL``
        for each server, and ``SKYNET_MCP_CONFIG`` when there is a server and a file.
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        """Send each ``/service/<name>/<path>`` or ``/mcp/<name>/<path>`` request to the parent."""

        def read_body(self) -> bytes:
            """Read the request body, sent with a Content-Length or in chunks.

            Returns:
                The body bytes, decoded from the chunked coding when the client used it.

            Raises:
                ValueError: When a chunk size line is not hexadecimal.
            """
            if "chunked" not in (self.headers.get("Transfer-Encoding") or "").lower():
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length > 0 else b""
            chunks = []
            while size := int(self.rfile.readline().split(b";")[0], 16):
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                pass
            return b"".join(chunks)

        def relay(self) -> None:
            """Relay one request and write the service's or server's response."""
            kind, _, rest = self.path.lstrip("/").partition("/")
            if kind not in ("service", "mcp"):
                self.send_error(404, "Unknown relay path")
                return
            name, _, path = rest.partition("/")
            try:
                body = self.read_body()
            except ValueError:
                self.send_error(400, "Malformed request body")
                return
            answers = client.answers(
                {
                    kind: urllib.parse.unquote(name),
                    "path": path,
                    "method": self.command,
                    "headers": list(self.headers.items()),
                    "body": base64.b64encode(body).decode("ascii"),
                }
            )
            response = next(answers)
            if "error" in response:
                payload = str(response["error"]).encode("utf-8")
                headers = [("Content-Type", "text/plain; charset=utf-8")]
            else:
                payload = base64.b64decode(str(response.get("body") or ""))
                headers = [(str(key), str(value)) for key, value in response.get("headers") or ()]
            # send_response would add this proxy's own Date and Server next to the service's.
            self.send_response_only(int(response.get("status") or 502))
            for key, value in headers:
                self.send_header(key, value)
            if not response.get("more"):
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)
                return
            # An event stream has no length: its body ends when this connection closes.
            self.close_connection = True
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(payload)
                self.wfile.flush()
                for part in answers:
                    self.wfile.write(base64.b64decode(str(part.get("body") or "")))
                    self.wfile.flush()
            except OSError:
                # The client left: closing the answers tells the parent to stop the stream.
                answers.close()
                return

        # http.server dispatches on these exact names.
        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = relay  # noqa: N815

        def log_message(self, format: str, *args: Any) -> None:
            """Keep the access log out of the scorer's stderr, where log() events travel."""

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, name="service-proxy", daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    env = {service_env_name(name): f"{base}/service/{urllib.parse.quote(name, safe='')}/" for name in names}
    servers = {name: f"{base}/mcp/{urllib.parse.quote(name, safe='')}" for name in mcp}
    env.update({mcp_env_name(name): url for name, url in servers.items()})
    if servers and mcp_file:
        env[MCP_CONFIG_ENV] = str(Path(mcp_file).resolve())
    return env


def _relay_env(client: StageClient, stages: dict[str, Any]) -> dict[str, str]:
    """Start the loopback proxy when the role may reach a service or an MCP server.

    Args:
        client: The relay client of this call.
        stages: The call's relay config.

    Returns:
        The proxy's variables, or nothing when the role reaches neither.
    """
    services = [str(name) for name in stages.get("services") or ()]
    mcp = [str(name) for name in stages.get("mcp") or ()]
    if not services and not mcp:
        return {}
    mcp_file = stages.get("mcp_file")
    return start_service_proxy(
        client,
        services,
        mcp,
        port=int(stages.get("port") or 0),
        mcp_file=mcp_file if isinstance(mcp_file, str) else None,
    )


def missing_stage(name: Any = None, input: Any = None) -> Any:
    """Refuse a stage call in a run that has no ``[target.<name>]`` stages.

    Raises:
        StageError: Always.
    """
    raise StageError("This run has no target stages, so call() has nothing to call.")


@dataclass
class Image:
    """An image a scorer hands to ``llm()`` or puts in side info for the optimizer to look at.

    Mirrors ``gepa.image.Image`` so a scorer written against the optimizer's
    wrapper works unchanged inside the box.
    """

    url: str | None = None
    path: str | None = None
    base64_data: str | None = None
    media_type: str = "image/png"

    def to_openai_content_part(self) -> dict[str, Any]:
        """Render the image as an OpenAI ``image_url`` content part.

        Returns:
            The content part, with file and base64 images inlined as data URLs.

        Raises:
            ScorerError: When the image has no source.
        """
        if self.base64_data:
            url = _data_url_from_base64(self.base64_data, self.media_type)
        elif self.path:
            data = Path(self.path).read_bytes()
            media_type = _MEDIA_TYPE_BY_EXTENSION.get(Path(self.path).suffix.lower()) or self.media_type
            url = _data_url(data, media_type)
        elif self.url:
            url = self.url
        else:
            raise ScorerError("Image needs a url, a path or base64_data.")
        return {"type": "image_url", "image_url": {"url": url}}


def env_value(name: str) -> str | None:
    """Read an ``APP_*`` variable, falling back to its pre-rename ``SKYNET_*`` name.

    Args:
        name: Variable name starting with ``APP_``.

    Returns:
        The first non-empty value, or ``None`` when neither name is set.
    """
    return os.environ.get(name) or os.environ.get(_LEGACY_ENV_PREFIX + name[len("APP_") :]) or None


def _data_url(data: bytes, media_type: str) -> str:
    """Inline ``data`` as a base64 data URL.

    Args:
        data: Raw image bytes.
        media_type: Their MIME type.

    Returns:
        The ``data:`` URL.
    """
    return _data_url_from_base64(base64.b64encode(data).decode("ascii"), media_type)


def _data_url_from_base64(encoded: str, media_type: str) -> str:
    """Wrap already-encoded image data as a data URL.

    Args:
        encoded: Base64 text.
        media_type: The image's MIME type.

    Returns:
        The ``data:`` URL.
    """
    return f"data:{media_type};base64,{encoded}"


def _sniff_media_type(data: bytes) -> str:
    """Guess an image MIME type from its leading bytes, defaulting to PNG.

    Args:
        data: Raw image bytes.

    Returns:
        The MIME type.
    """
    for magic, media_type in _MEDIA_TYPE_BY_MAGIC:
        if data.startswith(magic):
            return media_type
    return "image/png"


def image_content_part(image: Any) -> dict[str, Any]:
    """Turn one scorer-supplied image into an OpenAI ``image_url`` content part.

    Args:
        image: PNG/JPEG bytes, a file path, an http(s) or ``data:`` URL, a
            base64 string, a PIL image, an ``Image`` (anything with
            ``to_openai_content_part``) or an already-built content part.

    Returns:
        The content part to place in a chat message.

    Raises:
        ScorerError: When ``image`` is none of the accepted shapes.
    """
    if isinstance(image, dict) and image.get("type"):
        return image
    to_part = getattr(image, "to_openai_content_part", None)
    if callable(to_part):
        return dict(to_part())
    if isinstance(image, (bytes, bytearray)):
        data = bytes(image)
        return {"type": "image_url", "image_url": {"url": _data_url(data, _sniff_media_type(data))}}
    # PIL images are duck-typed so Pillow stays optional inside the box.
    if hasattr(image, "save") and hasattr(image, "mode"):
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return {"type": "image_url", "image_url": {"url": _data_url(buffer.getvalue(), "image/png")}}
    if isinstance(image, str):
        if image.startswith(("data:", "http://", "https://")):
            return {"type": "image_url", "image_url": {"url": image}}
        path = Path(image)
        if path.is_file():
            data = path.read_bytes()
            media_type = _MEDIA_TYPE_BY_EXTENSION.get(path.suffix.lower()) or _sniff_media_type(data)
            return {"type": "image_url", "image_url": {"url": _data_url(data, media_type)}}
        try:
            data = base64.b64decode(image, validate=True)
        except ValueError as exc:
            raise ScorerError(_IMAGES_HELP) from exc
        return {"type": "image_url", "image_url": {"url": _data_url(data, _sniff_media_type(data))}}
    raise ScorerError(_IMAGES_HELP)


def scorer_messages(prompt: Any, input: Any = None, images: Any = None) -> list[dict[str, Any]]:
    """Build the chat messages one ``llm()`` call sends.

    Args:
        prompt: The text to run; with ``input`` it is the system message
            (the prompt under optimization), alone it is the user message.
        input: The case's input, sent as the user message when given.
        images: Images attached to the user message, if any; a single image
            or a list of them.

    Returns:
        OpenAI-style chat messages.
    """
    if images is not None and not isinstance(images, (list, tuple)):
        images = [images]
    user_text = str(prompt) if input is None else str(input)
    content: Any = user_text
    if images:
        content = [{"type": "text", "text": user_text}] + [image_content_part(image) for image in images]
    messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
    if input is not None:
        messages.insert(0, {"role": "system", "content": str(prompt)})
    return messages


def missing_llm(prompt: Any = None, input: Any = None, **kwargs: Any) -> str:
    """Stand in for ``llm`` when the scorer was given no model.

    Args:
        prompt: Ignored.
        input: Ignored.
        **kwargs: Ignored.

    Raises:
        ScorerError: Always, with guidance for selecting a model.
    """
    raise ScorerError("The metric calls llm() but has no model. Add model= to llm() or check the reflection model.")


def side_info_json_default(value: Any) -> str:
    """JSON fallback for side info: images become data URLs, anything else ``str()``.

    Args:
        value: A side-info value ``json.dumps`` cannot encode.

    Returns:
        Its string form.
    """
    to_part = getattr(value, "to_openai_content_part", None)
    if callable(to_part):
        return str(to_part()["image_url"]["url"])
    return str(value)


def _json_plain(value: Any) -> Any:
    """Make a value strict-JSON safe: non-finite numbers become their text.

    Args:
        value: Parsed JSON.

    Returns:
        The same structure with every NaN and infinity as a string.
    """
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return str(value)
    if isinstance(value, list):
        return [_json_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_plain(item) for key, item in value.items()}
    return value


def log(event: str, message: Any = None, *, level: str = "INFO", **fields: Any) -> None:
    """Send one named event to the run's log the moment it happens.

    Scorer code calls it as ``from skynet import log`` then
    ``log("judge.retry", attempt=2, reason="rate limited")``. The host shows
    it live, tagged with the candidate and case being scored.

    Args:
        event: Event name, such as ``judge.retry``.
        message: The line shown in the log; the event name when unset.
        level: ``DEBUG``, ``INFO``, ``WARNING`` or ``ERROR``.
        **fields: Details shown with the event; values that are not JSON become text.

    Raises:
        ValueError: When ``event`` is empty or ``level`` is not one of the four.
    """
    if not isinstance(event, str) or not event.strip():
        raise ValueError("log() needs a non-empty event name, such as log('judge.retry').")
    name = str(level).upper()
    if name not in _EVENT_LEVELS:
        raise ValueError(f"log() level must be one of {', '.join(_EVENT_LEVELS)}, not {level!r}.")
    line = {
        "level": name,
        "logger": "scorer",
        "message": event if message is None else str(message),
        "event": event,
        "fields": _json_plain(json.loads(json.dumps(fields, default=side_info_json_default))) if fields else None,
    }
    # The real stderr: a scorer that redirects sys.stderr must not swallow its own events.
    stream = sys.__stderr__ or sys.stderr
    stream.write(EVENT_PREFIX + json.dumps(line, allow_nan=False) + "\n")
    stream.flush()


FEEDBACK_REQUIRED = (
    'The scorer must return feedback: return {"score": <number>, "feedback": "<why it scored that>"}, '
    'optionally with "scores": {"<name>": {"score": <number>, "feedback": "<why>"}} for each thing you '
    'measure, or a (score, "feedback") pair. The optimizer learns from the feedback, so say what was wrong '
    "or missing."
)


def _feedback(value: Any, where: str) -> str:
    """Return a non-empty feedback string or explain what is missing.

    Args:
        value: The feedback the scorer gave.
        where: Names the score the feedback belongs to, for the error.

    Returns:
        The feedback text.

    Raises:
        ScorerError: When the feedback is absent, not text, or blank.
    """
    if isinstance(value, str) and value.strip():
        return value
    raise ScorerError(f"{FEEDBACK_REQUIRED} ({where} has no feedback.)")


def named_scores(raw: Any) -> dict[str, dict[str, Any]]:
    """Validate the optional named scores into ``{name: {"score", "feedback"}}``.

    Args:
        raw: The scorer's ``scores`` value.

    Returns:
        Each named score with its float score and feedback.

    Raises:
        ScorerError: When ``scores`` is not a mapping, or a named score lacks a number or feedback.
    """
    if not isinstance(raw, dict):
        raise ScorerError(f'{FEEDBACK_REQUIRED} ("scores" must map each name to a score and feedback.)')
    named: dict[str, dict[str, Any]] = {}
    for name, entry in raw.items():
        if isinstance(entry, (tuple, list)) and len(entry) == 2:
            entry = {"score": entry[0], "feedback": entry[1]}
        value = entry.get("score") if isinstance(entry, dict) else None
        if not isinstance(value, _NUMBER_TYPES):
            raise ScorerError(f"{FEEDBACK_REQUIRED} (The named score {name!r} has no numeric score.)")
        named[str(name)] = {
            "score": float(value),
            "feedback": _feedback(entry.get("feedback"), f"The named score {name!r}"),
        }
    return named


def normalize_score(raw: Any) -> tuple[float, SideInfo]:
    """Coerce a scorer's return value into ``(score, side_info)``, feedback included.

    Accepted shapes: a mapping ``{"score", "feedback", "scores"?, ...}``
    (other keys stay in the side info); a ``(score, feedback)`` pair whose
    second item is the feedback text or a mapping holding it; an object
    with numeric ``score`` and text ``feedback`` attributes. Every score
    carries feedback, the top-level one and each named score, because the
    engines' proposers learn from it.

    Args:
        raw: Whatever the scorer returned.

    Returns:
        The float score and side info with ``feedback`` and, when given,
        ``scores`` as ``{name: {"score": float, "feedback": str}}``.

    Raises:
        ScorerError: When the value has none of the accepted shapes or lacks feedback.
    """
    if isinstance(raw, (tuple, list)) and len(raw) == 2 and isinstance(raw[0], _NUMBER_TYPES):
        score, side = raw
        side_info = dict(side) if isinstance(side, dict) else {"feedback": side}
    elif isinstance(raw, dict) and isinstance(raw.get("score"), _NUMBER_TYPES):
        score = raw["score"]
        side_info = {key: value for key, value in raw.items() if key != "score"}
    elif isinstance(getattr(raw, "score", None), _NUMBER_TYPES):
        score = raw.score
        side_info = {"feedback": getattr(raw, "feedback", None)}
    elif isinstance(raw, _NUMBER_TYPES):
        raise ScorerError(f"{FEEDBACK_REQUIRED} (It returned a bare number.)")
    else:
        raise ScorerError(f"{FEEDBACK_REQUIRED} (It returned {type(raw).__name__}.)")
    side_info["feedback"] = _feedback(side_info.get("feedback"), "The score")
    if "scores" in side_info:
        side_info["scores"] = named_scores(side_info["scores"])
    return float(score), side_info


def helper_module(helpers: dict[str, Any]) -> types.ModuleType:
    """Build the ``skynet`` module scorer code imports its helpers from.

    Args:
        helpers: Helper names and the objects behind them.

    Returns:
        A module exposing every helper as an attribute.
    """
    module = types.ModuleType(
        HELPER_MODULE_NAME,
        "Helpers for scorer code: llm(prompt, input=None, images=None), Image(...), log(event, **fields)"
        " and call(stage, input) with StageError.",
    )
    module.__dict__.update(helpers)
    return module


def load_scorer_from_code(code: str, *, helpers: dict[str, Any] | None = None) -> Callable[..., Any]:
    """Execute scorer source and return the callable it defines.

    Looks for ``score`` then ``metric``, then falls back to the single
    function the code defines.

    Args:
        code: User-authored python source.
        helpers: Names the scorer may import from the ``skynet`` module. They
            are also bound in its namespace, for scorers written before the
            import existed.

    Returns:
        The scorer callable.

    Raises:
        ScorerError: When the code fails to compile or load, or defines no
            unambiguous scorer function.
    """
    namespace: dict[str, Any] = dict(helpers or {})
    sys.modules[HELPER_MODULE_NAME] = helper_module(namespace)
    try:
        # exec: user-supplied scorer code. Inside the sandbox this is the
        # whole point; on the backend only ``validate_scorer_code`` reaches
        # here, from its spawn child.
        exec(compile(code, SCORER_FILENAME, "exec", dont_inherit=True), namespace)
    except SyntaxError as exc:
        raise ScorerError(f"scorer code has a syntax error: {exc}") from exc
    except Exception as exc:
        raise ScorerError(f"scorer code failed to load: {type(exc).__name__}: {exc}") from exc
    for name in _ENTRYPOINT_NAMES:
        candidate = namespace.get(name)
        if callable(candidate):
            return candidate
    defined = [
        obj for obj in namespace.values() if inspect.isfunction(obj) and obj.__code__.co_filename == SCORER_FILENAME
    ]
    if len(defined) == 1:
        return defined[0]
    raise ScorerError("scorer code must define a function named 'score(candidate, case=None)'.")


def _takes_positional(fn: Callable[..., Any], count: int) -> bool:
    """Return True when ``fn`` takes at least ``count`` positional arguments.

    Args:
        fn: The scorer callable.
        count: How many positional arguments the call would pass.

    Returns:
        True when ``fn`` names that many positional parameters or takes ``*args``.
    """
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return True
    positional = [p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL)]
    return len(positional) >= count or any(p.kind == p.VAR_POSITIONAL for p in positional)


def accepts_case(fn: Callable[..., Any]) -> bool:
    """Return True when ``fn`` takes a second positional argument for the case.

    Args:
        fn: The scorer callable.

    Returns:
        True for ``fn(candidate, case)`` shapes, False for ``fn(candidate)``.
    """
    return _takes_positional(fn, 2)


def accepts_output(fn: Callable[..., Any], ran: bool) -> bool:
    """Return True when ``fn`` takes a third positional argument for the candidate's output.

    Args:
        fn: The scorer callable.
        ran: Whether a ``[run]`` entry ran, so there is an output to pass.

    Returns:
        True for ``fn(candidate, case, output)`` shapes; a ``*args`` scorer
        gets the output only when a candidate ran, as it did before ``[run]``.
    """
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return ran
    named = [p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    return len(named) >= 3 or (ran and any(p.kind == p.VAR_POSITIONAL for p in params))


def accepts_elapsed(fn: Callable[..., Any]) -> bool:
    """Return True when ``fn`` names an ``elapsed_s`` parameter after its first three.

    Args:
        fn: The scorer callable.

    Returns:
        True for ``fn(repo_path, case, output, elapsed_s=None)`` and
        ``fn(repo_path, case, output, *, elapsed_s=None)`` shapes. A
        ``*args`` or ``**kwargs`` scorer never gets it, so older metrics
        see the same call as before (SKY-642).
    """
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return False
    for index, param in enumerate(params):
        if param.name == ELAPSED_PARAM:
            if param.kind == param.KEYWORD_ONLY:
                return True
            return param.kind == param.POSITIONAL_OR_KEYWORD and index >= 3
    return False


def _ssl_context() -> ssl.SSLContext:
    """Build the TLS context for gateway calls, trusting the sandbox's egress proxy when present.

    Returns:
        The context.
    """
    context = ssl.create_default_context()
    if Path(PROXY_CA_PATH).is_file():
        context.load_verify_locations(PROXY_CA_PATH)
    return context


def _completion_text(payload: Any) -> str:
    """Pull the first completion's text out of a chat-completions response.

    Args:
        payload: The decoded response body.

    Returns:
        The text; empty when the response carried none.
    """
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return "" if content is None else str(content)


def model_matches_bound(model: str, bound: str, declared: str | None = None) -> bool:
    """Compare a user model name with the model bound to its gateway route.

    Args:
        model: Model named by the helper call.
        bound: Model identifier on the protected route.
        declared: Original model identifier, before route normalization.

    Returns:
        Whether the model matches the bound or declared identifier.
    """

    def normalized(value: str) -> str:
        """Remove transport prefixes without changing the provider model id."""
        return value.strip().strip("/").removeprefix("litellm_proxy/").removeprefix("openrouter/")

    return normalized(model) == normalized(bound) or bool(declared and normalized(model) == normalized(declared))


class GatewayClient:
    """What the scorer sees as ``llm``: one chat completion per call, straight to the gateway."""

    def __init__(
        self,
        url: str,
        model: str,
        *,
        api_key: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: float = 120.0,
        protected: bool = False,
        declared_model: str | None = None,
        subscription: bool = False,
        gateway_reasoning: bool = False,
    ) -> None:
        """Bind the helper to one endpoint and model.

        Args:
            url: OpenAI-compatible base URL (``/chat/completions`` is appended).
            model: Model id as the gateway knows it.
            api_key: Bearer token, or ``None`` when the sandbox injects it.
            temperature: Sampling temperature, when the Scorer step set one.
            max_tokens: Completion cap, when the Scorer step set one;
                :data:`DEFAULT_MAX_TOKENS` otherwise.
            reasoning_effort: Thinking level (``low``, ``high``, ...), when the Scorer step set one.
            timeout_seconds: Per-request timeout.
            protected: Whether the trusted parent owns dispatch and retry coverage.
            declared_model: Original model identifier resolved by the protected gateway.
            subscription: Whether the route dispatches to a local CLI.
            gateway_reasoning: Whether the route fronts OpenRouter.
        """
        relay = env_value(ENV_BUDGET_RELAY_URL)
        self._url = (relay or url).rstrip("/") + "/chat/completions"
        self._protected = protected or bool(relay)
        self.control: dict[str, str] | None = None
        self._model = model
        self._declared_model = declared_model
        self._subscription = subscription
        self._gateway_reasoning = gateway_reasoning or urllib.parse.urlsplit(url).hostname == "openrouter.ai"
        self._api_key = api_key
        self._temperature = temperature
        self._max_tokens = DEFAULT_MAX_TOKENS if max_tokens is None else max_tokens
        self._reasoning_effort = reasoning_effort
        self._timeout_seconds = timeout_seconds
        self._ssl = _ssl_context()
        self.usage: list[dict[str, Any]] = []

    def __call__(
        self,
        prompt: Any = None,
        input: Any = None,
        *,
        images: Any = None,
        messages: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        """Complete ``prompt``, optionally over ``input`` and ``images``.

        Args:
            prompt: The text to run; with ``input`` it is the system message
                (the prompt under optimization), alone it is the user message.
            input: The case's input, sent as the user message when given.
            images: Images for a vision model to look at — rendered output,
                screenshots, plots — attached to the user message.
            messages: Ready-made chat messages (OpenAI format, image parts
                included); replaces ``prompt``/``input``/``images``.
            model: The model declared by the metric, which must match the bound route.
            temperature: Per-call sampling override.
            max_tokens: Per-call output limit.
            reasoning_effort: Per-call reasoning level.

        Returns:
            The model's first completion, as text.

        Raises:
            ScorerError: Without a prompt or messages, with an image the
                helper cannot read, or when the gateway refuses the call.
        """
        if self.control is not None:
            raise GatewayControl(**self.control)
        if model is not None and not model_matches_bound(model, self._model, self._declared_model):
            raise ScorerError("llm(model=...) must match the model bound to this code.")
        if messages is None:
            if prompt is None:
                raise ScorerError("llm() needs a prompt, or messages=[...].")
            messages = scorer_messages(prompt, input, images)
        elif not isinstance(messages, list):
            raise ScorerError("llm(messages=...) takes a list of chat messages.")
        body: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens if max_tokens is not None else self._max_tokens,
        }
        chosen_temperature = temperature if temperature is not None else self._temperature
        chosen_effort = reasoning_effort if reasoning_effort is not None else self._reasoning_effort
        if chosen_temperature is not None:
            body["temperature"] = chosen_temperature
        if chosen_effort is not None:
            body["reasoning_effort"] = chosen_effort
            if self._gateway_reasoning:
                body["reasoning"] = {"effort": chosen_effort, "summary": "auto"}
        if self._subscription and reasoning_effort is not None:
            body["skynet_cli_effort"] = reasoning_effort
        payload = self._post(json.dumps(body).encode("utf-8"))
        usage = payload.get("usage") if isinstance(payload, dict) else None
        if isinstance(usage, dict):
            self.usage.append(
                {
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "total_tokens": usage.get("total_tokens"),
                }
            )
        return _completion_text(payload)

    def _post(self, data: bytes) -> Any:
        """Send one protected attempt or retry transient legacy gateway failures.

        Args:
            data: The JSON request body.

        Returns:
            The decoded response body.

        Raises:
            ScorerError: When the gateway keeps failing or answers with an error.
        """
        last_error = "llm() request failed."
        attempts = 1 if self._protected else _ATTEMPTS
        for attempt in range(attempts):
            request = urllib.request.Request(
                self._url,
                data=data,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            if self._api_key:
                request.add_header("Authorization", f"Bearer {self._api_key}")
            try:
                with urllib.request.urlopen(request, timeout=self._timeout_seconds, context=self._ssl) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", errors="replace")
                if self._protected:
                    try:
                        document = json.loads(raw)
                    except ValueError:
                        document = {}
                    error = document.get("error") if isinstance(document, dict) else None
                    if isinstance(error, dict):
                        code = error.get("code") or error.get("type")
                        if code in {
                            "budget_reached",
                            "budget_insufficient",
                            "usage_pending",
                            "unpriced_operation",
                            "provider_unavailable",
                        }:
                            control_code = "usage_pending" if code == "provider_unavailable" else code
                            self.control = {"code": control_code, "message": str(error.get("message") or code)}
                            raise GatewayControl(**self.control) from exc
                detail = raw[:_ERROR_BODY_CHARS].strip()
                last_error = "llm() request failed: HTTP {}{}".format(exc.code, ": " + detail if detail else "")
                if exc.code not in _RETRY_STATUSES:
                    raise ScorerError(last_error) from exc
            except (urllib.error.URLError, OSError, ValueError) as exc:
                if self._protected:
                    self.control = {
                        "code": "usage_pending",
                        "message": "The protected model request needs reconciliation before retrying.",
                    }
                    raise GatewayControl(**self.control) from exc
                last_error = f"llm() request failed: {exc}"
            if attempt + 1 < attempts:
                time.sleep(_BACKOFF_SECONDS * (2**attempt))
        raise ScorerError(last_error)


def run_call(payload: dict[str, Any]) -> dict[str, Any]:
    """Load the scorer and either verify readiness or score its real candidate.

    Args:
        payload: The decoded ``input.json``.

    Returns:
        The ``output.json`` contents: score, side info, error and ``llm()`` usage.
    """
    gateway = payload.get("gateway")
    stages = payload.get("stages")
    relay = StageClient(stages) if isinstance(stages, dict) else None
    if relay is not None:
        # Set before the scorer loads, so its imports and child processes see the addresses.
        os.environ.update(_relay_env(relay, stages))
    llm: GatewayClient | None = None
    if isinstance(gateway, dict):
        llm = GatewayClient(
            str(gateway["url"]),
            str(gateway["model"]),
            api_key=gateway.get("api_key") or env_value(ENV_API_KEY),
            temperature=gateway.get("temperature"),
            max_tokens=gateway.get("max_tokens"),
            reasoning_effort=gateway.get("reasoning_effort"),
            timeout_seconds=float(gateway.get("timeout_seconds") or 120.0),
            protected=bool(gateway.get("protected")),
            declared_model=gateway.get("declared_model"),
            subscription=bool(gateway.get("subscription")),
            gateway_reasoning=bool(gateway.get("gateway_reasoning")),
        )
    helpers = {
        LLM_HELPER_NAME: llm if llm is not None else missing_llm,
        IMAGE_HELPER_NAME: Image,
        LOG_HELPER_NAME: log,
        STAGE_HELPER_NAME: relay if relay is not None and stages.get("names") else missing_stage,
        STAGE_ERROR_NAME: StageError,
    }
    result: dict[str, Any] = {"score": None, "side_info": {}, "error": None, "usage": []}
    try:
        fn = load_scorer_from_code(str(payload.get("code") or ""), helpers=helpers)
        if payload.get("mode") == "readiness":
            result["readiness"] = {
                "ready": True,
                "entrypoint": fn.__name__,
                "accepts_case": accepts_case(fn),
                "model_configured": llm is not None,
            }
        else:
            candidate, case = payload.get("candidate"), payload.get("case")
            if accepts_output(fn, "output" in payload):
                timed = ELAPSED_PARAM in payload and accepts_elapsed(fn)
                extra = {ELAPSED_PARAM: payload[ELAPSED_PARAM]} if timed else {}
                raw = fn(candidate, case, payload.get("output"), **extra)
            else:
                raw = fn(candidate, case) if accepts_case(fn) else fn(candidate)
            score, side_info = normalize_score(raw)
            result["score"] = score
            result["side_info"] = json.loads(json.dumps(side_info, default=side_info_json_default))
    except GatewayControl as exc:
        result["control"] = {"code": exc.code, "message": str(exc)}
    except ScorerError as exc:
        result["error"] = str(exc)
    except BaseException as exc:  # user code is arbitrary — any failure is reported, not raised
        result["error"] = f"{type(exc).__name__}: {exc}"
    if llm is not None:
        result["usage"] = llm.usage
        if llm.control is not None:
            result.update(score=None, side_info={}, error=None, control=llm.control)
    return result


def _keep_user_site() -> None:
    """Put user site-packages back at the end of ``sys.path`` under ``python3 -I``, without its ``.pth`` files."""
    # Repo setup may pip install into user site-packages when the system folder is read-only.
    # -I hides that folder from the metric; appended last, the stdlib and system packages
    # still win, and no .pth file the version planted there runs (E38, E40).
    if not sys.flags.isolated or sys.prefix != sys.base_prefix:
        return
    folder = site.getusersitepackages()
    if Path(folder).is_dir() and folder not in sys.path:
        sys.path.append(folder)


def _add_metric_site(folder: Any) -> None:
    """Put the metric packages' folder on ``sys.path`` before the image's site-packages.

    A plain ``sys.path`` entry runs no ``.pth`` file. The metric box holds no
    candidate code, so the folder holds only the declared packages (SKY-398).

    Args:
        folder: The folder from the metric input, or ``None`` when the
            metric declares no packages.
    """
    if not isinstance(folder, str) or not Path(folder).is_dir() or folder in sys.path:
        return
    sites = [index for index, entry in enumerate(sys.path) if entry.endswith(("site-packages", "dist-packages"))]
    sys.path.insert(sites[0] if sites else len(sys.path), folder)


def main(argv: list[str] | None = None) -> int:
    """Run one scorer call from ``<call_dir>/input.json`` into ``<call_dir>/output.json``.

    The scorer source comes from the box's code file unless the input carries it.

    Args:
        argv: Command-line arguments; ``sys.argv`` when unset.

    Returns:
        The process exit code: 0 whenever the output was written.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    # The supervisor needs only the standard library (SKY-642).
    if not (len(args) == 2 and args[0] == CANDIDATE_FLAG):
        _keep_user_site()
    if len(args) == 3 and args[0] in (STAGE_FLAG, RUN_FLAG):
        return run_stage(Path(args[1]), args[2], trace=args[0] == RUN_FLAG)
    if len(args) >= 3 and args[0] == STAGE_COMMAND_FLAG:
        return run_stage_command(Path(args[1]), args[2:])
    if len(args) == 2 and args[0] == CANDIDATE_FLAG:
        refused = unsafe_output()
        if refused:
            sys.stderr.write(refused + "\n")
            return 3
        call_dir = Path(args[1])
        payload = json.loads((call_dir / INPUT_FILE).read_text(encoding="utf-8"))
        try:
            result = run_candidate(payload, call_dir)
        except EntryUserError as exc:
            sys.stderr.write(f"{exc}\n")
            return 3
        _print_result(json.dumps(result))
        return 0
    if len(args) != 1:
        sys.stderr.write("usage: app_runner.py <call_dir>\n")
        return 2
    call_dir = Path(args[0])
    payload = json.loads((call_dir / INPUT_FILE).read_text(encoding="utf-8"))
    _add_metric_site(payload.get(METRIC_SITE_PARAM))
    if "code" not in payload:
        code_path = Path(__file__).resolve().with_name(CODE_FILE)
        payload["code"] = code_path.read_text(encoding="utf-8") if code_path.is_file() else ""
    refused = refuse_processes() if "output" in payload else None
    result = run_call(payload)
    if refused:
        # The metric may have caught the refusal; its score still does not count.
        result.update(score=None, side_info={}, error=RERUN_REFUSED)
    _write_output(call_dir, json.dumps(result))
    return 0


def _write_output(call_dir: Path, text: str) -> None:
    """Write ``output.json`` in one step, so the parent never reads half of it.

    Args:
        call_dir: The call's folder.
        text: The output document.
    """
    partial = call_dir / (OUTPUT_FILE + ".part")
    # A link or folder that the version's code left here must not take the
    # write; a folder makes the unlink fail, so the call fails (SKY-642).
    partial.unlink(missing_ok=True)
    partial.write_text(text, encoding="utf-8")
    partial.replace(call_dir / OUTPUT_FILE)


def _print_result(text: str) -> None:
    """Print the candidate supervisor's result as one framed line on its own stdout.

    Args:
        text: The result document.
    """
    stream = sys.__stdout__ or sys.stdout
    # One write: a relay thread's frame cannot split the line. The leading
    # newline ends any partial line before it.
    stream.write("\n" + RESULT_PREFIX + text + "\n")
    stream.flush()


def refuse_processes() -> list[bool]:
    """Refuse every process the metric starts from now on in this process (E40).

    The metric reads the candidate's report; it must not run the candidate
    again. An audit hook stays for the life of the process, so only the
    metric call's own process installs it.

    Returns:
        A list that gets an entry each time a process start was refused.
    """
    refused: list[bool] = []

    def hook(event: str, args: tuple[Any, ...]) -> None:
        """Abort a process start with the refusal message.

        Args:
            event: The audit event's name.
            args: The event's arguments.

        Raises:
            ScorerError: When the event starts a process.
        """
        if event in _PROCESS_EVENTS:
            refused.append(True)
            raise ScorerError(RERUN_REFUSED)

    sys.addaudithook(hook)
    return refused


class EntryUserError(Exception):
    """The root supervisor found no user other than root to run the entry as."""


def unsafe_output() -> str | None:
    """Say why the supervisor's stdout could carry a forged result, or ``None`` when it is safe.

    The parent reads the result frame from stdout alone. A pipe, socket or
    device has its other end with the runtime, so the entry cannot replace
    it. A file must be the Vercel exec wrapper's
    ``/tmp/.app-output-<token>.out``. The token is a new ``uuid4``
    (``os.urandom``) for each command, so no process can know the name
    before the root wrapper creates the file. The file must belong to root,
    have no group or other write bit and one link, and its own name must
    reach it: a link that the redirect followed has another name. In sticky
    ``/tmp`` the entry, which runs as another user, then cannot rename,
    replace or write the file. The supervisor also creates the wrapper's
    exit code file as root before the entry starts, so the entry cannot
    create it first and write a forged exit code (SKY-398).

    Returns:
        The refusal, or ``None``.
    """
    try:
        info = os.fstat(1)
    except OSError:
        return "The supervisor's stdout is closed."
    if not stat.S_ISREG(info.st_mode):
        return None
    try:
        path = str(Path("/proc/self/fd/1").readlink())
        named = os.lstat(path)
    except OSError:
        return "The supervisor cannot check the file behind its stdout."
    token = path[len(WRAPPER_OUTPUT_PREFIX) : -len(".out")]
    if (
        not path.startswith(WRAPPER_OUTPUT_PREFIX)
        or not path.endswith(".out")
        or len(token) != 32
        or token.strip("0123456789abcdef")
    ):
        return f"The supervisor's stdout {path} is not a file of the exec wrapper."
    if os.geteuid() != 0 or info.st_uid != 0 or info.st_mode & 0o022 or info.st_nlink != 1:
        return f"The supervisor's stdout {path} is not a root-only file."
    if not stat.S_ISREG(named.st_mode) or (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino):
        return f"The supervisor's stdout {path} is a link or was replaced."
    # The parent reads the wrapper's files as the box user.
    os.fchmod(1, 0o644)
    with _quiet():
        os.fchmod(2, 0o644)
    try:
        sentinel = os.open(WRAPPER_EXIT_PREFIX + token, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    except OSError:
        return "The exec wrapper's exit code file exists before the entry started."
    os.fchmod(sentinel, 0o644)
    os.close(sentinel)
    return None


def _entry_user(env: dict[str, str]) -> dict[str, Any]:
    """Choose the user the entry runs as, and set its environment to match.

    A root supervisor whose stdout is the exec wrapper's root file (Vercel)
    starts the entry as the owner of the candidate tree, its working
    directory. The entry then cannot signal, trace or read the supervisor,
    or change the wrapper's root files (SKY-398). A stdout pipe has no file
    that the entry could replace, and a local Docker box drops the root
    capabilities that a user switch needs. So with a pipe, or without root,
    the entry runs as the supervisor's user.

    Args:
        env: The entry's environment; gets ``HOME``, ``USER`` and ``LOGNAME``.

    Returns:
        The ``subprocess.Popen`` arguments that switch the user, or none.

    Raises:
        EntryUserError: When root owns the candidate tree.
    """
    if os.geteuid() != 0 or not stat.S_ISREG(os.fstat(1).st_mode):
        return {}
    owner = Path().stat()
    if owner.st_uid == 0:
        raise EntryUserError("Root owns the candidate tree, so the [run] entry has no other user to run as.")
    try:
        account = pwd.getpwuid(owner.st_uid)
    except KeyError:
        pass
    else:
        env.update(HOME=account.pw_dir, USER=account.pw_name, LOGNAME=account.pw_name)
    return {"user": owner.st_uid, "group": owner.st_gid, "extra_groups": []}


def _read_answer(path: Path, user: Any) -> dict[str, Any]:
    """Read a function entry's ``output.json`` without following a link the entry left.

    Args:
        path: The answer file.
        user: The entry's uid, or ``None`` when it runs as the supervisor's user.

    Returns:
        The answer document.

    Raises:
        OSError: When the file is missing, a link, not a file, or not the entry's own.
    """
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        # A root supervisor must not pass on a root file that the entry hard-linked here.
        if not stat.S_ISREG(info.st_mode) or (user is not None and info.st_uid != user):
            raise OSError(f"{path} is not the entry's own file")
        answer = json.loads(handle.read().decode("utf-8"))
    return answer if isinstance(answer, dict) else {}


def run_candidate(payload: dict[str, Any], call_dir: Path) -> dict[str, Any]:
    """Run the ``[run]`` entry on one case's input in the candidate box.

    The supervisor kills the entry's process group when the entry exits, so
    a process left in that group cannot hold the output pipe open and
    stretch the run time (SKY-642). The parent then ends the box, which is
    the only cleanup of every other process (SKY-398).

    Args:
        payload: ``{"entry", "input", "timeout", "stages"}``.
        call_dir: The candidate call's folder.

    Returns:
        ``{"output", "error", "elapsed_s"}``: the entry's output, why the run
        failed, and the entry's run time in seconds, measured here, outside
        its processes.
    """
    entry = payload["entry"]
    timeout = float(payload["timeout"])
    _block_tracing()
    env = dict(os.environ)
    stages = payload.get("stages")
    if isinstance(stages, dict):
        env.update(_relay_env(StageClient(stages), stages))
    switch = _entry_user(env)
    # A new folder in sticky /tmp: the box user can rename anything in the
    # call's folder, so a root supervisor must not write or chown there.
    run_dir = Path(tempfile.mkdtemp(prefix=".app-run-"))
    try:
        return _run_entry(entry, timeout, env, switch, run_dir, json.dumps(payload.get("input")))
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def _run_entry(
    entry: Any, timeout: float, env: dict[str, str], switch: dict[str, Any], run_dir: Path, body: str
) -> dict[str, Any]:
    """Start the entry, wait for it, kill its process group, and read its output.

    Args:
        entry: A ``path/file.py:function`` string or a command list.
        timeout: The entry's time limit in seconds.
        env: The entry's environment.
        switch: The ``subprocess.Popen`` arguments that switch the user.
        run_dir: A new folder for a function entry's input and output.
        body: The case input as JSON.

    Returns:
        ``{"output", "error", "elapsed_s"}``.
    """
    user = switch.get("user")
    captured: list[bytes] = []
    readers: list[threading.Thread] = []
    try:
        if isinstance(entry, str):
            (run_dir / INPUT_FILE).write_text(body, encoding="utf-8")
            if user is not None:
                os.chown(run_dir, user, switch["group"])
                os.chown(run_dir / INPUT_FILE, user, switch["group"])
            # The supervisor runs on the base interpreter without site-packages;
            # the entry is candidate code and needs the box's python3 from PATH,
            # where [setup] installed its packages (SKY-642).
            python = shutil.which("python3", path=env.get("PATH")) or sys.executable
            argv = [python, str(Path(__file__).resolve()), RUN_FLAG, str(run_dir), entry]
            # The candidate runs in its own processes, so it cannot patch this clock (SKY-642).
            started = time.monotonic()
            # Its prints go to stderr: the parent reads relay frames on stdout.
            child = subprocess.Popen(
                argv, env=env, stdin=subprocess.DEVNULL, stdout=2, start_new_session=True, **switch
            )
        else:
            started = time.monotonic()
            child = subprocess.Popen(
                [str(part) for part in entry],
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=2,
                start_new_session=True,
                **switch,
            )
            readers = [
                threading.Thread(target=_feed, args=(child.stdin, body.encode("utf-8")), daemon=True),
                threading.Thread(target=_capture, args=(child.stdout, captured), daemon=True),
            ]
            for reader in readers:
                reader.start()
    except OSError as exc:
        return {"output": None, "error": f"The command could not start: {exc}"}
    try:
        code: int | None = child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        code = None
    with _quiet():
        os.killpg(child.pid, signal.SIGKILL)
    if code is None:
        child.wait()
    for reader in readers:
        reader.join(timeout=5)
    elapsed = time.monotonic() - started
    result: dict[str, Any] = {"output": None, "error": None, ELAPSED_PARAM: round(elapsed, 6)}
    if code is None:
        result["error"] = f"timeout after {timeout:g} s"
    elif code < 0:
        result["error"] = f"killed by signal {_signal_name(-code)}"
    elif isinstance(entry, str):
        try:
            answer = _read_answer(run_dir / OUTPUT_FILE, user)
        except (OSError, ValueError):
            answer = {"error": f"exited with code {code} before it returned"}
        if "output" in answer:
            result["output"] = answer["output"]
        else:
            result["error"] = str(answer.get("error") or "returned nothing")
    else:
        stdout = b"".join(captured)[:MAX_RUN_STDOUT_BYTES].decode("utf-8", errors="replace")
        result["output"] = {"exit_code": code, "stdout": stdout}
    return result


def _feed(stream: Any, data: bytes) -> None:
    """Write the case input to a command's stdin and close it, whether or not the command reads it.

    Args:
        stream: The command's stdin pipe.
        data: The input as JSON bytes.
    """
    with _quiet():
        stream.write(data)
    with _quiet():
        stream.close()


def _capture(stream: Any, chunks: list[bytes]) -> None:
    """Keep the first ``MAX_RUN_STDOUT_BYTES`` of a command's stdout and drain the rest.

    Args:
        stream: The command's stdout pipe.
        chunks: Receives the kept bytes.
    """
    kept = 0
    for block in iter(lambda: stream.read(1 << 16), b""):
        if kept < MAX_RUN_STDOUT_BYTES:
            chunks.append(block[: MAX_RUN_STDOUT_BYTES - kept])
            kept += len(chunks[-1])


class _quiet:  # noqa: N801
    """Ignore an ``OSError`` from one step: a process or pipe that is already gone."""

    def __enter__(self) -> None:
        """Enter the block."""

    def __exit__(self, kind: Any, value: Any, trace: Any) -> bool:
        """Swallow ``OSError`` and ``ValueError`` (a closed pipe).

        Args:
            kind: The exception type, if any.
            value: The exception, if any.
            trace: Its traceback.

        Returns:
            Whether the exception is swallowed.
        """
        return kind is not None and issubclass(kind, (OSError, ValueError))


def _signal_name(number: int) -> str:
    """Name a signal for the run's feedback.

    Args:
        number: The signal number.

    Returns:
        ``SIGKILL`` and the like, or the number when it has no name.
    """
    try:
        return signal.Signals(number).name
    except ValueError:
        return str(number)


def _block_tracing() -> None:
    """Stop same-user ptrace and ``/proc/<pid>/mem`` access to this process on Linux (SKY-642).

    Without Yama, any process of the same user could otherwise change the
    supervisor's clock or its output. A failure leaves the process as it was.
    """
    if not sys.platform.startswith("linux"):
        return
    with _quiet():
        ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0)


def run_stage(call_dir: Path, entry: str, *, trace: bool = False) -> int:
    """Run one Python stage call: ``<call_dir>/input.json`` → ``function(input)`` → ``output.json``.

    The working directory is the candidate's tree, so ``entry`` and the
    stage's own imports resolve against it.

    Args:
        call_dir: The call's folder in the stage box.
        entry: ``path/file.py:function``, relative to the tree.
        trace: Report a failure with its traceback, as a ``[run]`` entry
            does for the proposer's feedback.

    Returns:
        The process exit code: 0 whenever the output was written.
    """
    try:
        # Set before the entry loads, so its imports and child processes see the addresses.
        os.environ.update(_stage_relay_env(call_dir))
        payload = json.loads((call_dir / INPUT_FILE).read_text(encoding="utf-8"))
        path, _, function = entry.rpartition(":")
        source = Path(path).resolve()
        sys.path[:0] = [str(source.parent), str(Path.cwd())]
        spec = importlib.util.spec_from_file_location("app_stage_entry", source)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        text = json.dumps({"output": getattr(module, function)(payload)}, allow_nan=False)
    except BaseException as exc:  # user code is arbitrary: any failure is reported, not raised
        error = traceback.format_exc().strip() if trace else f"{type(exc).__name__}: {exc}"
        text = json.dumps({"error": error})
    _write_output(call_dir, text)
    return 0


def run_stage_command(call_dir: Path, command: Sequence[str]) -> int:
    """Run one command stage call behind the stage's relay: ``input.json`` on stdin, stdout into ``output.json``.

    Args:
        call_dir: The call's folder in the stage box.
        command: The stage's command and its arguments.

    Returns:
        The command's exit code, ``128 + n`` when signal ``n`` killed it, or
        127 when it could not start.
    """
    env = {**os.environ, **_stage_relay_env(call_dir)}
    with (call_dir / INPUT_FILE).open("rb") as stdin, (call_dir / OUTPUT_FILE).open("wb") as stdout:
        try:
            code = subprocess.run(list(command), env=env, stdin=stdin, stdout=stdout, check=False).returncode
        except OSError as exc:
            sys.stderr.write(f"The command could not start: {exc}\n")
            return 127
    return code if code >= 0 else 128 - code


def _stage_relay_env(call_dir: Path) -> dict[str, str]:
    """Start the stage's loopback relay when the parent wrote a relay config for this call.

    The parent runs one call at a time in a stage box with a relay, so this
    call's proxy can hold ``RELAY_PORT``, which the stage's own MCP file names.

    Args:
        call_dir: The call's folder in the stage box.

    Returns:
        The proxy's variables, or nothing when the call has no relay config.
    """
    path = call_dir / STAGE_RELAY_FILE
    if not path.is_file():
        return {}
    config = json.loads(path.read_text(encoding="utf-8"))
    # The stage's code may change its directory; the answer folder must not move with it.
    config["response_dir"] = str(Path(str(config["response_dir"])).resolve())
    client = StageClient(config)
    return _relay_env(client, config)


if __name__ == "__main__":
    sys.exit(main())
