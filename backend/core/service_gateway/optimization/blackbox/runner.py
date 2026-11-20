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
gets ``ASCENT_SERVICE_<NAME>_URL``, a loopback address in the box whose
requests go through the same relay, so the service's key stays with the
parent. Its optional ``mcp`` list names the http servers of the role's own
MCP file: each gets ``ASCENT_MCP_<NAME>_URL`` on the same loopback port, and
``ASCENT_MCP_CONFIG`` names the role's own ``.mcp.json`` in the checkout. The
parent replaced that file before the box opened: each entry is a loopback url
on ``RELAY_PORT``, never the real url, a header or a secret. In a stage box,
``python3 app_runner.py --stage <call_dir> <path/file.py:function>`` runs
one stage call and writes ``{"output": ...}`` or ``{"error": ...}``. When the
stage's own MCP file holds a server, the parent also writes ``relay.json``
(the same relay config, with the call's own prefix and answer folder) into
the call's folder: the call then gets its own loopback relay on a free port,
checked against the stage's own role. A command stage runs as
``--stage-command <call_dir> <package> <timeout> <command> [<arg> ...]``,
behind that relay when it has one, and writes the same ``{"output": ...}``
or ``{"error": ...}`` as a Python stage, with the failed command's
``stderr`` and ``stdout`` tails next to the error (SKY-757). Parallel
calls share the stage box, so the tree's MCP file, which names
``RELAY_PORT``, reaches no call; each call gets ``mcp.json`` in its folder
with its own port, and ``ASCENT_MCP_CONFIG`` names that copy (SKY-624).

A repository with a ``[run]`` entry runs the candidate first, in its own
candidate box (SKY-398). ``python3 app_runner.py --candidate <call_dir>``
reads ``{"entry", "input", "timeout", "stages"}``, starts the entry in
its own session with only the case's input (E42), and writes
``{"output", "error", "elapsed_s"}``. The parent then ends the box, and
every process the entry left ends with it; the metric runs in another box
that never holds the candidate's code.
A function entry (``path/file.py:function``) runs as ``--run <dir>
<entry>`` and its output is its JSON return value. A command entry
(``["sh", "run.sh"]``) runs as ``--run-command <dir> <package> <timeout>
<command> [<arg> ...]``: OpenAscent's ``call_run`` writes ``{"input": ...}``
to the file that ``ASCENT_INPUT`` names, starts the command with an empty
stdin, and takes the ``output`` value of the JSON object that the command
writes to the file that ``ASCENT_OUTPUT`` names (SKY-757). The command's
printed text goes to the run logs only. The metric call then gets that output as
``score(repo_path, case, output)``, and may start no process of its own (E40).
Each relay frame names the box's role (``run`` or ``metric``), and the
parent refuses a frame whose role is not the box's own.
A metric that also names an ``elapsed_s`` parameter after ``output`` gets
the entry's run time in seconds as ``elapsed_s=...`` (SKY-642). The
supervisor measures it in its own process, from just before the entry
starts until the entry exits and its process group is killed, so the
candidate's code cannot patch the clock it reads.

A command ``[metric] entry`` cannot import ``call``, ``llm`` or ``log``, so
the runner serves them to it on a loopback port of its own, in the
OpenAscent wire form (SKY-767): ``ASCENT_STAGE_<NAME>_URL`` for each stage,
through the same relay as ``call()``; ``ASCENT_MODEL_URL`` with
``ASCENT_MODEL`` and an empty ``ASCENT_MODEL_KEY`` only with a judge model,
through the same client as ``llm()``; and ``ASCENT_LOG_URL``, which writes
the same event as ``log()``. The command's environment holds no model key.
A Python metric gets none of these helper URLs; it reads the box's own
``ASCENT_MODEL_KEY`` through ``llm()``.
"""

from __future__ import annotations

import base64
import ctypes
import difflib
import http.server
import importlib.util
import inspect
import io
import json
import math
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
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

INPUT_FILE = "input.json"
CODE_FILE = "app_scorer_code.txt"
OUTPUT_FILE = "output.json"
# A command metric's model key: the scorer writes it, and the runner reads and
# deletes it before the command starts (SKY-767).
MODEL_KEY_FILE = "model_key"
SCORER_FILENAME = "<scorer_code>"
# Names the scorer namespace binds the model helper and the image wrapper to.
LLM_HELPER_NAME = "llm"
# The gateway key rides in the environment so it never touches the box
# filesystem, except for a command metric (``MODEL_KEY_FILE``).
ENV_API_KEY = "ASCENT_MODEL_KEY"
ENV_BUDGET_RELAY_URL = "APP_BUDGET_RELAY_URL"
# The image's box PATH, with /app/.venv/bin first after the sudo guard folder.
# sudo gives a root supervisor a secure PATH without the venv, so the entry
# gets this one after the user switch.
ENV_ENTRY_PATH = "APP_ENTRY_PATH"
# This file runs alone in the box, without ``core.config``; boxes started by an
# older control plane still carry only the pre-rename spellings.
_LEGACY_ENV_PREFIX = "SKYNET_"
# A command metric reaches the model and the stages only through its helper
# URLs (SKY-767), which meter each call and check each stage. These names hold
# the model key, the protected model mailbox and the tool relay token that the
# box (or a contained session, ``sandbox_runtime._contained_environment``)
# sets, so they stay out of the command's environment, the ``APP_*`` names in
# both spellings. The box's own ``ASCENT_MODEL*`` values go too: the command
# gets these names only from its helpers, and only with a judge model.
_COMMAND_HIDDEN_ENV = (
    *(
        name
        for base in (ENV_BUDGET_RELAY_URL, "APP_TOOL_RELAY_TOKEN")
        for name in (base, _LEGACY_ENV_PREFIX + base.removeprefix("APP_"))
    ),
    "ASCENT_MODEL_URL",
    "ASCENT_MODEL",
    ENV_API_KEY,
    "OPENAI_BASE_URL",
    "ANTHROPIC_BASE_URL",
)
IMAGE_HELPER_NAME = "Image"
LOG_HELPER_NAME = "log"
# ``from openascent import call, StageError`` reaches the run's ``[target.<name>]`` stages.
STAGE_HELPER_NAME = "call"
STAGE_ERROR_NAME = "StageError"
# ``app_runner.py --stage <call_dir> <path/file.py:function>`` runs one stage call.
STAGE_FLAG = "--stage"
# ``app_runner.py --stage-command <call_dir> <package> <timeout> <command> [<arg> ...]``
# runs one command stage call, behind the stage's relay when the call has one (SKY-611).
STAGE_COMMAND_FLAG = "--stage-command"
# ``app_runner.py --run-command <dir> <package> <timeout> <command> [<arg> ...]`` is
# the candidate process of a command entry. Both command modes run the command
# through OpenAscent's ``call_run``, so this file holds no second copy of the
# two-file rule (SKY-757).
RUN_COMMAND_FLAG = "--run-command"
# A stage call's relay config, next to its input; only a stage whose own MCP file holds a server gets one.
STAGE_RELAY_FILE = "relay.json"
# The stage call's own MCP file, next to its input, with the call's own relay port (SKY-624).
_STAGE_MCP_FILE = "mcp.json"
# The metric reaches each listed service at this variable's loopback address (E45).
SERVICE_ENV_PREFIX = "ASCENT_SERVICE_"
# Each server of the role's own MCP file is at this variable's loopback address (E49).
MCP_ENV_PREFIX = "ASCENT_MCP_"
# Names the role's own ``.mcp.json``, for MCP clients that read a config file.
MCP_CONFIG_ENV = "ASCENT_MCP_CONFIG"
# A command metric reaches its helpers at these loopback addresses (SKY-767),
# in the form the OpenAscent helpers use: ``ASCENT_STAGE_<NAME>_URL`` for each
# stage, the judge model's chat endpoint, and the log.
STAGE_ENV_PREFIX = "ASCENT_STAGE_"
MODEL_URL_ENV = "ASCENT_MODEL_URL"
MODEL_ENV = "ASCENT_MODEL"
# Always empty: the runner adds the gateway's key itself, so the command never holds it.
MODEL_KEY_ENV = "ASCENT_MODEL_KEY"
LOG_URL_ENV = "ASCENT_LOG_URL"
# The largest helper request body; a stage input or a chat request is far smaller.
_HELPER_BODY_BYTES = 16 * 1024 * 1024
# The chat request fields the model helper passes on. Each other field is refused,
# so the command cannot reach a field that changes the route or its cost bound.
_MODEL_REQUEST_KEYS = frozenset({"model", "messages", "max_tokens", "temperature", "reasoning_effort", "stream"})
# The box's loopback relay port. It is fixed because the parent writes each
# role's .mcp.json with loopback urls before the box opens (E49, E53).
RELAY_PORT = 47_311
# ``app_runner.py --candidate <call_dir>`` runs the ``[run]`` entry and stops what it left (E38).
CANDIDATE_FLAG = "--candidate"
# ``app_runner.py --run <dir> <path/file.py:function>`` is the candidate process of a function entry.
RUN_FLAG = "--run"
# The Vercel provider's root exec wrapper writes the command's stdout, stderr
# and exit code to these files in a folder that root makes first
# (``ROOT_OUTPUT_PREFIX`` and ``_wrapper`` in core/sandbox/providers/vercel.py).
# This file runs in the box on the standard library alone, so it cannot import
# them; a test keeps the two equal.
WRAPPER_FOLDER_PREFIX = "/tmp/.app-root-output-"
WRAPPER_OUTPUT_NAME = "output.out"
WRAPPER_EXIT_NAME = "exit"
# The largest output file that a command entry or a command stage may write (SKY-757).
MAX_RUN_STDOUT_BYTES = 1024 * 1024
# A command entry's own process gets this much time after the command's time
# limit, so that ``call_run`` stops the command and reports the limit itself.
_COMMAND_STOP_SECONDS = 10.0
# The largest ``output.json`` the supervisor reads from a command entry's
# process. That process writes the checked output again with ``json.dumps``
# and ``ensure_ascii=False``. No string grows then: strict JSON already escapes
# each character that the dump escapes. Only a number grows, at most 3.8
# times: ``1e15,`` (5 bytes) becomes ``1000000000000000.0,`` (19 bytes). The
# cap stops a process that left the entry's session from making the supervisor
# read a huge file in its place (SKY-757).
_COMMAND_ANSWER_BYTES = 4 * MAX_RUN_STDOUT_BYTES + 64 * 1024
# The characters of a failed command's stderr and of its stdout that its error
# shows (SKY-757).
COMMAND_LOG_CHARS = 800
# The characters of a failed command's cause that its error shows. The cause
# and the two tails fit in the [run] feedback, which keeps only its last 1,988
# characters, so the feedback always names the cause (SKY-757).
COMMAND_CAUSE_CHARS = 300
# The characters of each log that the box sends to the parent. The bound keeps
# the answer file small. The box knows no secrets, so this cut can split one:
# the box marks each log it cut, and the parent drops the cut edge before it
# masks the log (SKY-757).
_COMMAND_LOG_BOUND = 4_000
# OpenAscent's invalid-JSON cause can quote the output file: the first 40
# characters of a number, or a key as a repr. The mask can miss both, so a
# failed command or command metric gets this fixed cause instead (SKY-757).
INVALID_JSON_CAUSE = (
    "The output file is not valid JSON. Write one strict JSON value to ASCENT_OUTPUT: "
    "UTF-8, no NaN or Infinity, no duplicate keys, no lone surrogate escape."
)
# The metric parameter, the input key and the supervisor output key for the entry's run time (SKY-642).
ELAPSED_PARAM = "elapsed_s"
# The metric input key that names the folder the metric setup step installed
# the metric's packages into. ``-I`` reads no user or PYTHONPATH folder, and
# the image's venv has no system site, so the runner adds this one (SKY-398).
METRIC_SITE_PARAM = "metric_site"
# A command [metric] entry (SKY-738): the input keys that carry the command, its
# time limit and the folder the parent copied the OpenAscent package into. The
# package's call_metric runs the command under the two-file rule, so this file
# holds no second copy of that rule. A command [run] entry's input also names
# that folder with ASCENT_PACKAGE_PARAM (SKY-757).
METRIC_ENTRY_PARAM = "metric_entry"
METRIC_TIMEOUT_PARAM = "metric_timeout"
ASCENT_PACKAGE_PARAM = "ascent_package"
# The function a "path.py:function" [metric] entry names; absent keeps the default lookup.
METRIC_FUNCTION_PARAM = "function"
_PR_SET_DUMPABLE = 4
_PR_SET_NO_NEW_PRIVS = 38
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
# The module scorer code imports those helpers from: ``from openascent import llm, Image``.
# A neutral name, so a contract holds no product name (SKY-768). No other name works.
HELPER_MODULE_NAME = "openascent"
# Where Vercel's egress proxy leaves its CA when a network policy rewrites headers.
PROXY_CA_PATH = "/usr/local/share/ca-certificates/vercel-proxy-ca.crt"
_ENTRYPOINT_NAMES = ("score", "metric")
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


class MissingScorerError(ScorerError):
    """The scorer code loads but defines none of the functions the default entry looks for."""


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
        ``ASCENT_SERVICE_<NAME>_URL``, with each character other than a letter or digit as ``_``.
    """
    return SERVICE_ENV_PREFIX + _env_part(name) + "_URL"


def mcp_env_name(name: str) -> str:
    """Return the variable that holds one MCP server's loopback address.

    Args:
        name: The server's name in the role's MCP file.

    Returns:
        ``ASCENT_MCP_<NAME>_URL``, with each character other than a letter or digit as ``_``.
    """
    return MCP_ENV_PREFIX + _env_part(name) + "_URL"


def stage_env_name(name: str) -> str:
    """Return the variable that holds one stage's loopback address for a command metric.

    The OpenAscent ``call`` helper reads the same name, so the rule is its
    rule: each ASCII letter in upper case, each ASCII digit unchanged, and
    each other character as ``_``. ``str.upper()`` alone can turn one
    character into ASCII letters (``ß`` into ``SS``).

    Args:
        name: The ``[target.<name>]`` name.

    Returns:
        ``ASCENT_STAGE_<NAME>_URL``.
    """
    part = "".join(char.upper() if char.isascii() and char.isalnum() else "_" for char in name)
    return STAGE_ENV_PREFIX + part + "_URL"


def _env_part(name: str) -> str:
    """Return a name as a variable part: upper case, each character other than a letter or digit as ``_``.

    Args:
        name: The name to convert.

    Returns:
        The name in upper case, with ``_`` for each other character.
    """
    return "".join(char if char.isalnum() else "_" for char in name.upper())


def start_service_proxy(
    client: StageClient,
    names: Sequence[str],
    mcp: Sequence[str] = (),
    *,
    port: int = 0,
    mcp_file: str | None = None,
    own_mcp_file: Path | None = None,
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
        own_mcp_file: A file to write with this proxy's servers and name in
            ``ASCENT_MCP_CONFIG`` instead of ``mcp_file``.

    Returns:
        ``ASCENT_SERVICE_<NAME>_URL`` for each service, ``ASCENT_MCP_<NAME>_URL``
        for each server, and ``ASCENT_MCP_CONFIG`` when there is a server and a file.
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
            """Keep the access log out of the scorer's stderr, where log() events travel.

            Args:
                format: The log format string.
                args: The values for the format string.
            """

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, name="service-proxy", daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    env = {service_env_name(name): f"{base}/service/{urllib.parse.quote(name, safe='')}/" for name in names}
    servers = {name: f"{base}/mcp/{urllib.parse.quote(name, safe='')}" for name in mcp}
    env.update({mcp_env_name(name): url for name, url in servers.items()})
    if servers and own_mcp_file is not None:
        # Built from the server names, not the variables: "a-b" and "a_b" share
        # one variable name but must keep their own URLs (SKY-624).
        own = {"mcpServers": {name: {"type": "http", "url": url} for name, url in servers.items()}}
        own_mcp_file.write_text(json.dumps(own, indent=2) + "\n", encoding="utf-8")
        env[MCP_CONFIG_ENV] = str(own_mcp_file.resolve())
    elif servers and mcp_file:
        env[MCP_CONFIG_ENV] = str(Path(mcp_file).resolve())
    return env


def _relay_env(client: StageClient, stages: dict[str, Any], own_mcp_file: Path | None = None) -> dict[str, str]:
    """Start the loopback proxy when the role may reach a service or an MCP server.

    Args:
        client: The relay client of this call.
        stages: The call's relay config.
        own_mcp_file: A file for the proxy to write with its own servers, as
            in ``start_service_proxy``.

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
        own_mcp_file=own_mcp_file,
    )


def missing_stage(name: Any = None, input: Any = None) -> Any:
    """Refuse a stage call in a run that has no ``[target.<name>]`` stages.

    Args:
        name: The stage name of the call.
        input: The stage input of the call.

    Raises:
        StageError: Always.
    """
    raise StageError("This run has no target stages, so call() has nothing to call.")


@dataclass
class Image:
    """An image a scorer hands to ``llm()``.

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
    """Read a variable; an ``APP_*`` name falls back to its pre-rename ``SKYNET_*`` name.

    The ``ASCENT_*`` names of the open standard have no legacy name (SKY-776).

    Args:
        name: Variable name.

    Returns:
        The first non-empty value, or ``None`` when no name is set.
    """
    legacy = os.environ.get(_LEGACY_ENV_PREFIX + name[len("APP_") :]) if name.startswith("APP_") else None
    return os.environ.get(name) or legacy or None


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
    """JSON fallback for scorer payloads and log fields: an image becomes a data URL, anything else ``str()``.

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

    Scorer code calls it as ``from openascent import log`` then
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
    _emit_event(event, event if message is None else str(message), name, fields)


def _emit_event(event: str, message: str, level: str, fields: dict[str, Any]) -> None:
    """Write one checked event line to the real stderr, where the host reads ``log()`` events.

    A command metric's log helper writes through here too, so its events
    are the same lines, and a field named ``level`` or ``message`` stays a field.

    Args:
        event: The event name.
        message: The line shown in the log.
        level: One of the four levels.
        fields: Details shown with the event; values that are not JSON become text.
    """
    line = {
        "level": level,
        "logger": "scorer",
        "message": message,
        "event": event,
        "fields": _json_plain(json.loads(json.dumps(fields, default=side_info_json_default))) if fields else None,
    }
    # The real stderr: a scorer that redirects sys.stderr must not swallow its own events.
    stream = sys.__stderr__ or sys.stderr
    stream.write(EVENT_PREFIX + json.dumps(line, allow_nan=False) + "\n")
    stream.flush()


# The one form of a metric answer (SKY-769). Every answer error ends with it.
ANSWER_FORM = (
    'Return one object: {"score": <number>, "feedback": "<optional text>", '
    '"scores": {"<name>": {"score": <number>, "feedback": "<optional text>"}}}. '
    'Only "score" is required.'
)
_ANSWER_KEYS = ("score", "feedback", "scores")
_NAMED_SCORE_KEYS = ("score", "feedback")
# Keys of an older answer form, and the key that replaced each one.
_REPLACED_KEYS = {"cases": "scores"}


def _answer_error(problem: str) -> ScorerError:
    """Build the error for a wrong metric answer: the wrong part, then the valid form.

    Args:
        problem: What is wrong, and where.

    Returns:
        The error to raise.
    """
    return ScorerError(f"{problem} {ANSWER_FORM}")


def _kind(value: Any) -> str:
    """Name the kind of a value for an answer error, without its content.

    Args:
        value: A part of the metric answer.

    Returns:
        A short name such as ``a bare number`` or ``a pair``.
    """
    if isinstance(value, (bool, int, float)):
        return "a bare number"
    if isinstance(value, (tuple, list)):
        return "a pair" if len(value) == 2 else type(value).__name__
    return type(value).__name__


def _finite(value: Any, where: str) -> float:
    """Return a score as a float when it is a finite number.

    Args:
        value: The score the metric gave.
        where: Names the score, for the error.

    Returns:
        The score as a float.

    Raises:
        ScorerError: When the score is not a number, is a boolean, or is not finite.
    """
    # A bool is an int in Python, but True is not a measurement.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _answer_error(f"{where} must be a finite number, not {type(value).__name__}.")
    if not math.isfinite(value):
        raise _answer_error(f"{where} must be a finite number, not {value!r}.")
    return float(value)


def _check_keys(entry: dict, allowed: Sequence[str], where: str) -> None:
    """Refuse each key of an answer object that the answer form does not have.

    Args:
        entry: The answer object or one named score.
        allowed: The keys the object may hold.
        where: Names the object, for the error.

    Raises:
        ScorerError: At the first unknown key, with the nearest valid key when one is close.
    """
    for key in entry:
        if key in allowed:
            continue
        name = str(key)
        replaced = _REPLACED_KEYS.get(name)
        if replaced in allowed:
            raise _answer_error(f"{where} has the unknown key `{name}`. Use `{replaced}`.")
        close = difflib.get_close_matches(name, allowed, n=1)
        hint = f" Did you mean `{close[0]}`?" if close else ""
        raise _answer_error(f"{where} has the unknown key `{name}`.{hint}")


def feedback(value: Any, where: str) -> str:
    """Return the feedback text, or an empty string when there is none.

    Feedback is optional: absent feedback, ``None`` and a blank string all
    mean no feedback, stored as ``""``.

    Args:
        value: The feedback the metric gave.
        where: Names the score the feedback belongs to, for the error.

    Returns:
        The feedback text, or ``""``.

    Raises:
        ScorerError: When the feedback is given but is not text.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _answer_error(f"The feedback of {where} must be text, not {type(value).__name__}.")
    return value if value.strip() else ""


def named_scores(raw: Any) -> dict[str, dict[str, Any]]:
    """Validate the optional named scores into ``{name: {"score", "feedback"}}``.

    Args:
        raw: The metric's ``scores`` value.

    Returns:
        Each named score with its float score and its feedback (``""`` when none).

    Raises:
        ScorerError: When ``scores`` is not an object, or a named score is not
            an object with a finite ``score``, optional text ``feedback`` and no other key.
    """
    if not isinstance(raw, dict):
        raise _answer_error(f'"scores" must be an object that maps each name to a score object, not {_kind(raw)}.')
    named: dict[str, dict[str, Any]] = {}
    for name, entry in raw.items():
        where = f"the named score {name!r}"
        if not isinstance(entry, dict):
            raise _answer_error(f'A named score is an object such as {{"score": 1.0}}; {where} is {_kind(entry)}.')
        _check_keys(entry, _NAMED_SCORE_KEYS, f"The named score {name!r}")
        if "score" not in entry:
            raise _answer_error(f'The named score {name!r} has no "score".')
        named[str(name)] = {
            "score": _finite(entry["score"], f'The "score" of {where}'),
            "feedback": feedback(entry.get("feedback"), where),
        }
    return named


def normalize_score(raw: Any, *, own_keys: bool = False) -> tuple[float, SideInfo]:
    """Check a metric answer and split it into ``(score, side_info)``.

    The answer is one object: ``score`` (a finite number) is required,
    ``feedback`` (text) and ``scores`` (named score objects) are optional,
    and every other key is an error. A bare number, a pair and an object
    with ``score`` attributes are errors too.

    Args:
        raw: The metric's answer.
        own_keys: Keep the keys Skynet adds after the check (``status``,
            ``error`` and others). Set it only for a document Skynet built
            from an answer it already checked.

    Returns:
        The float score and side info with ``feedback`` (``""`` when none)
        and, when given, ``scores`` as ``{name: {"score": float, "feedback": str}}``.

    Raises:
        ScorerError: When the answer does not have the one valid form.
    """
    if not isinstance(raw, dict):
        raise _answer_error(f"The metric answer must be an object, not {_kind(raw)}.")
    if not own_keys:
        _check_keys(raw, _ANSWER_KEYS, "The metric answer")
    if "score" not in raw:
        raise _answer_error('The metric answer has no "score".')
    score = _finite(raw["score"], 'The "score" of the metric answer')
    side_info = {key: value for key, value in raw.items() if key != "score"}
    side_info["feedback"] = feedback(raw.get("feedback"), "the metric answer")
    if "scores" in side_info:
        side_info["scores"] = named_scores(side_info["scores"])
    return score, side_info


def helper_module(helpers: dict[str, Any]) -> types.ModuleType:
    """Build the ``openascent`` module scorer code imports its helpers from.

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


def load_scorer_from_code(
    code: str, *, helpers: dict[str, Any] | None = None, entrypoint: str | None = None
) -> Callable[..., Any]:
    """Execute scorer source and return the callable it defines.

    Looks for ``score`` then ``metric``, then falls back to the single
    function the code defines.

    Args:
        code: User-authored python source.
        helpers: Names the scorer may import from the ``openascent`` module. They
            are also bound in its namespace, for scorers written before the
            import existed.
        entrypoint: The function that ``[metric] entry`` names. ``None`` and
            ``score``, the default entry's function, keep the lookup above.

    Returns:
        The scorer callable.

    Raises:
        ScorerError: When the code fails to compile or load, or defines no
            unambiguous scorer function. ``MissingScorerError`` marks the
            default lookup that finds no function.
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
    if entrypoint not in (None, "score"):
        named = namespace.get(entrypoint)
        if not callable(named):
            raise ScorerError(f"scorer code defines no function '{entrypoint}', which [metric] entry names.")
        return named
    for name in _ENTRYPOINT_NAMES:
        candidate = namespace.get(name)
        if callable(candidate):
            return candidate
    defined = [
        obj for obj in namespace.values() if inspect.isfunction(obj) and obj.__code__.co_filename == SCORER_FILENAME
    ]
    if len(defined) == 1:
        return defined[0]
    raise MissingScorerError("scorer code must define a function named 'score(candidate, case=None)'.")


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
        """Remove transport prefixes without changing the provider model id.

        Args:
            value: A model name.

        Returns:
            The model name without its transport prefixes.
        """
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
        return _completion_text(
            self.complete(messages, temperature=temperature, max_tokens=max_tokens, reasoning_effort=reasoning_effort)
        )

    def serves(self, model: str) -> bool:
        """Return whether a model name names the model bound to this client.

        Args:
            model: The model a request names.

        Returns:
            Whether it matches the bound or the declared model.
        """
        return model_matches_bound(model, self._model, self._declared_model)

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> Any:
        """Send one chat completion with the bound model and record its usage.

        ``llm()`` and a command metric's model helper both send through here,
        so both get the same model, limits, metering and budget stop.

        Args:
            messages: The chat messages, in OpenAI form.
            temperature: Per-call sampling override.
            max_tokens: Per-call output limit.
            reasoning_effort: Per-call reasoning level.

        Returns:
            The gateway's decoded answer.

        Raises:
            ScorerError: When the gateway refuses the call.
            GatewayControl: When the run's budget stopped this or an earlier call.
        """
        if self.control is not None:
            raise GatewayControl(**self.control)
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
        return payload

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


def _gateway_client(gateway: Any) -> GatewayClient | None:
    """Build the model client from a call's ``gateway`` section.

    Args:
        gateway: The ``gateway`` value of the call's input.

    Returns:
        The client, or ``None`` when the run has no model route.
    """
    if not isinstance(gateway, dict):
        return None
    return GatewayClient(
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
    llm = _gateway_client(gateway)
    helpers = {
        LLM_HELPER_NAME: llm if llm is not None else missing_llm,
        IMAGE_HELPER_NAME: Image,
        LOG_HELPER_NAME: log,
        STAGE_HELPER_NAME: relay if relay is not None and stages.get("names") else missing_stage,
        STAGE_ERROR_NAME: StageError,
    }
    result: dict[str, Any] = {"score": None, "side_info": {}, "error": None, "usage": []}
    try:
        fn = load_scorer_from_code(
            str(payload.get("code") or ""), helpers=helpers, entrypoint=payload.get(METRIC_FUNCTION_PARAM)
        )
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


def _load_openascent(folder: Any) -> types.ModuleType:
    """Import the OpenAscent package from the folder the parent copied it into.

    The runner starts with ``-I`` and the image may not install the package,
    so it loads the folder by path; no ``sys.path`` entry lets a planted
    module shadow a standard one.

    Args:
        folder: The package folder from the metric input.

    Returns:
        The ``openascent`` package.

    Raises:
        ScorerError: When the input names no package folder.
    """
    init = Path(str(folder or "")) / "__init__.py"
    if not folder or not init.is_file():
        raise ScorerError("The box has no OpenAscent package, so the command cannot run.")
    spec = importlib.util.spec_from_file_location("openascent", init, submodule_search_locations=[str(init.parent)])
    if spec is None or spec.loader is None:
        raise ScorerError("The box has no OpenAscent package, so the command cannot run.")
    module = importlib.util.module_from_spec(spec)
    sys.modules["openascent"] = module
    spec.loader.exec_module(module)
    return module


class _HelperError(Exception):
    """A helper request that gets an error answer with this status."""

    def __init__(self, status: int, message: str) -> None:
        """Bind the answer's status and error text.

        Args:
            status: The HTTP status, not 2xx.
            message: The ``error`` text of the answer.
        """
        super().__init__(message)
        self.status = status


def _refuse_constant(name: str) -> Any:
    """Refuse ``NaN``, ``Infinity`` and ``-Infinity``, which strict JSON does not allow.

    Args:
        name: The constant that ``json.loads`` found.

    Raises:
        ValueError: Always.
    """
    raise ValueError(f"{name} is not JSON.")


class _QuietHTTPServer(http.server.ThreadingHTTPServer):
    """A loopback HTTP server that prints nothing when a request fails."""

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Drop the error report of a failed request.

        The default prints a traceback to stderr, where log() events travel. A
        metric command that leaves early (``curl --max-time``) breaks the
        answer write, and ``do_POST`` already answers every other failure.

        Args:
            request: The failed request's socket.
            client_address: The client's address.
        """


class MetricHelpers:
    """Serve a command metric's stages, judge model and log on one loopback port (SKY-767).

    A command metric cannot import ``call``, ``llm`` or ``log``, so the runner
    serves them in the OpenAscent wire form. The server is a thread of this
    process and starts no process, so the one-process rule of the metric box
    stays as it is. Each stage request goes through the same relay as a
    Python metric's ``call()``, and each model request through the same
    client as ``llm()``, which adds the key, meters the call and keeps a
    budget stop.
    """

    def __init__(
        self, relay: StageClient | None, names: Sequence[str], gateway: GatewayClient | None, model: str | None
    ) -> None:
        """Start the server on a free loopback port.

        Args:
            relay: The call's stage relay, or ``None`` without stages.
            names: The stages the metric may call.
            gateway: The model client, or ``None`` when the run has no judge model.
            model: The model name that ``ASCENT_MODEL`` gives the metric.
        """
        self._relay = relay
        self._names = tuple(names)
        self._gateway = gateway
        self._model = model
        self.stops: list[dict[str, str]] = []
        helpers = self

        class Handler(http.server.BaseHTTPRequestHandler):
            """Answer each helper request with a JSON body."""

            def do_POST(self) -> None:
                """Read one JSON request, run its helper and write the answer."""
                try:
                    status, document = 200, helpers.answer(self.path, self.read_json())
                except _HelperError as exc:
                    status, document = exc.status, {"error": str(exc)}
                except Exception as exc:
                    # socketserver would print a traceback to stderr, where
                    # log() events travel, and the client would get no answer.
                    status, document = 500, {"error": f"The helper failed: {type(exc).__name__}."}
                data = json.dumps(document).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def read_json(self) -> Any:
                """Read the request body as JSON, without ``NaN`` or ``Infinity``.

                Returns:
                    The decoded body.

                Raises:
                    _HelperError: When the body is too large, not JSON, too
                        deeply nested or holds a non-finite number.
                """
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    raise _HelperError(400, "The request has no valid Content-Length.") from None
                if length > _HELPER_BODY_BYTES:
                    # The unread body would be taken as the next request.
                    self.close_connection = True
                    raise _HelperError(413, f"The request body is larger than {_HELPER_BODY_BYTES} bytes.")
                try:
                    return json.loads(self.rfile.read(length) if length > 0 else b"", parse_constant=_refuse_constant)
                except (ValueError, RecursionError):
                    raise _HelperError(400, "The request body is not JSON.") from None

            def log_message(self, format: str, *args: Any) -> None:
                """Keep the access log out of the scorer's stderr, where log() events travel.

                Args:
                    format: The log format string.
                    args: The values for the format string.
                """

        self._server = _QuietHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, name="metric-helpers", daemon=True).start()
        base = f"http://127.0.0.1:{self._server.server_address[1]}"
        variables = [stage_env_name(name) for name in names]
        # Two names with one variable (``a-b`` and ``a_b``) get none, so a call
        # fails as an absent stage and never reaches the other stage.
        self.env: dict[str, str] = {}
        for name in names:
            variable = stage_env_name(name)
            if variables.count(variable) == 1:
                self.env[variable] = f"{base}/stage/{urllib.parse.quote(name, safe='')}"
        self.env[LOG_URL_ENV] = f"{base}/log"
        if gateway is not None and model:
            self.env.update({MODEL_URL_ENV: f"{base}/model", MODEL_ENV: model, MODEL_KEY_ENV: ""})

    def close(self) -> None:
        """Stop the server and free its port."""
        self._server.shutdown()
        self._server.server_close()

    def answer(self, path: str, body: Any) -> dict[str, Any]:
        """Run the helper that a request path names.

        Args:
            path: The request path.
            body: The decoded request body.

        Returns:
            The answer body of a success.

        Raises:
            _HelperError: When the path names no helper, or the helper refused or failed.
        """
        route = urllib.parse.urlsplit(path).path
        if route.startswith("/stage/"):
            return self._stage(urllib.parse.unquote(route[len("/stage/") :]), body)
        if route == "/model/chat/completions" and self._gateway is not None:
            return self._chat(self._gateway, body)
        if route == "/log":
            return self._log(body)
        raise _HelperError(404, f"No helper is at {route}.")

    def _stage(self, name: str, body: Any) -> dict[str, Any]:
        """Call one stage through the relay.

        Args:
            name: The stage name from the path.
            body: ``{"input": <any JSON>}``.

        Returns:
            ``{"output": <the stage's output>}``.

        Raises:
            _HelperError: When the stage is not in the run, the body is not
                the stage form, the stage failed, or the budget stopped the call.
        """
        if self._relay is None or name not in self._names:
            known = ", ".join(self._names) or "none"
            raise _HelperError(404, f"Unknown stage {name!r}; the stages of this run are: {known}.")
        if not isinstance(body, dict) or set(body) != {"input"}:
            raise _HelperError(400, 'A stage request is one object with only the key "input".')
        try:
            return {"output": self._relay(name, body["input"])}
        except StageError as exc:
            raise _HelperError(502, str(exc)) from None
        except GatewayControl as exc:
            self.stops.append({"code": exc.code, "message": str(exc)})
            raise _HelperError(503, str(exc)) from None

    def _chat(self, gateway: GatewayClient, body: Any) -> dict[str, Any]:
        """Send one chat completion through the run's model client.

        Args:
            gateway: The run's model client.
            body: An OpenAI chat request for the ``ASCENT_MODEL`` model.

        Returns:
            The gateway's answer.

        Raises:
            _HelperError: When the request names another model, has a field
                the helper does not pass on, asks for a stream, the gateway
                refused it, or the budget stopped the call.
        """
        if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
            raise _HelperError(400, 'A model request is one object with a "messages" list.')
        extra = sorted(set(body) - _MODEL_REQUEST_KEYS)
        if extra:
            allowed = ", ".join(sorted(_MODEL_REQUEST_KEYS))
            raise _HelperError(400, f"The model helper does not take {', '.join(extra)}; it takes {allowed}.")
        if body.get("stream"):
            raise _HelperError(400, "The model helper does not stream; send the request without stream.")
        requested = body.get("model")
        if requested is not None and not (isinstance(requested, str) and gateway.serves(requested)):
            raise _HelperError(
                400, f"The model {requested!r} is not the judge model; send the model that {MODEL_ENV} names."
            )
        try:
            answer = gateway.complete(
                body["messages"],
                temperature=body.get("temperature"),
                max_tokens=body.get("max_tokens"),
                reasoning_effort=body.get("reasoning_effort"),
            )
        except GatewayControl as exc:
            self.stops.append({"code": exc.code, "message": str(exc)})
            raise _HelperError(503, str(exc)) from None
        except ScorerError as exc:
            raise _HelperError(502, str(exc)) from None
        if not isinstance(answer, dict):
            raise _HelperError(502, "The model gave an answer that is not a JSON object.")
        return answer

    def _log(self, body: Any) -> dict[str, Any]:
        """Write one event to the run log, the same line as ``log(event, **fields)``.

        Args:
            body: ``{"event": <name>, "fields": {...}}``.

        Returns:
            An empty object.

        Raises:
            _HelperError: When the body is not the log form.
        """
        if not isinstance(body, dict) or not set(body) <= {"event", "fields"}:
            raise _HelperError(400, 'A log request is one object with the keys "event" and "fields".')
        event, fields = body.get("event"), body.get("fields", {})
        if not isinstance(event, str) or not event.strip():
            raise _HelperError(400, 'A log request needs a non-empty "event" name.')
        if not isinstance(fields, dict):
            raise _HelperError(400, 'The "fields" of a log request must be an object.')
        _emit_event(event, event, "INFO", fields)
        return {}


def run_metric_command(payload: dict[str, Any], call_dir: Path | None = None) -> dict[str, Any]:
    """Call a command ``[metric] entry`` once, through OpenAscent's ``call_metric`` (SKY-738).

    The command gets ``ASCENT_INPUT`` and ``ASCENT_OUTPUT`` and runs in this
    process's box, folder and environment, so it has the same network,
    services, vCPUs and time limit as a Python metric. It also gets the
    :class:`MetricHelpers` addresses (SKY-767), and its environment has no
    model key.

    Args:
        payload: The decoded ``input.json``, with ``metric_entry``.
        call_dir: The call's folder, which can hold ``MODEL_KEY_FILE``.

    Returns:
        The ``output.json`` contents: score, side info and error, and
        ``cause``, the package's short name of a failed call's cause.
    """
    result: dict[str, Any] = {"score": None, "side_info": {}, "error": None, "usage": []}
    stages = payload.get("stages")
    # The command runs as this process's user, and this process holds the
    # model key in memory and the relay values in its environment. So this
    # process's /proc/<pid>/mem and environ must not be readable. The flag
    # does not reach the command (execve resets it), and it does not cover a
    # parent process that a contained session keeps alive.
    _block_tracing()
    key = _take_model_key(call_dir)
    relay = StageClient(stages) if isinstance(stages, dict) else None
    if relay is not None:
        os.environ.update(_relay_env(relay, stages))
    gateway = payload.get("gateway")
    if key and isinstance(gateway, dict):
        gateway = {**gateway, "api_key": key}
    llm = _gateway_client(gateway)
    names = [str(name) for name in stages.get("names") or ()] if isinstance(stages, dict) else []
    model = (gateway.get("declared_model") or gateway.get("model")) if isinstance(gateway, dict) else None
    helpers = MetricHelpers(relay, names, llm, str(model) if model else None)
    env = {name: value for name, value in os.environ.items() if name not in _COMMAND_HIDDEN_ENV}
    env.update(helpers.env)
    case = payload.get("case")
    case = case if isinstance(case, dict) else {"input": case}
    metric_input: dict[str, Any] = {"input": case.get("input"), "output": payload.get("output")}
    if "expected" in case:
        metric_input["expected"] = case["expected"]
    if ELAPSED_PARAM in payload:
        metric_input[ELAPSED_PARAM] = payload[ELAPSED_PARAM]
    ascent: types.ModuleType | None = None
    try:
        ascent = _load_openascent(payload.get(ASCENT_PACKAGE_PARAM))
        call = ascent.call_metric(
            tuple(payload[METRIC_ENTRY_PARAM]),
            metric_input,
            timeout_s=float(payload[METRIC_TIMEOUT_PARAM]),
            cwd=str(Path.cwd()),
            env=env,
        )
        score, side_info = normalize_score(call.result.to_dict())
        result.update(score=score, side_info=side_info)
    except BaseException as exc:  # the metric is arbitrary — any failure is reported, not raised
        invalid = ascent is not None and isinstance(exc, ascent.InvalidJsonError)
        result["error"] = f"{type(exc).__name__}: {INVALID_JSON_CAUSE if invalid else exc}"
        if ascent is not None and isinstance(exc, ascent.AscentError):
            result["cause"] = exc.code
            if getattr(exc, "exit_code", None) is not None:
                result["exit_code"] = exc.exit_code
            if exc.stderr:
                result["error"] += f"\nstderr: {exc.stderr[-2_000:]}"
    finally:
        helpers.close()
    if llm is not None:
        result["usage"] = llm.usage
    # A budget stop wins over the command's answer, as it does for a Python metric.
    control = (llm.control if llm is not None else None) or (helpers.stops[0] if helpers.stops else None)
    if control is not None:
        result = {"score": None, "side_info": {}, "error": None, "usage": result["usage"], "control": control}
    return result


def _take_model_key(call_dir: Path | None) -> str | None:
    """Read a command metric's model key file and delete it.

    The command runs as this process's user, so the delete, not the file
    mode, keeps the key from the command. The scorer starts no other process
    in the metric box between its write and this delete.

    Args:
        call_dir: The call's folder, or ``None``.

    Returns:
        The key, or ``None`` when the call has no key file.

    Raises:
        OSError: When the file cannot be deleted, so the command never starts
            while the key is on disk.
    """
    if call_dir is None:
        return None
    path = call_dir / MODEL_KEY_FILE
    try:
        key = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    path.unlink()
    return key or None


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
    if len(args) >= 5 and args[0] in (STAGE_COMMAND_FLAG, RUN_COMMAND_FLAG):
        return run_stage_command(Path(args[1]), args[2], float(args[3]), args[4:])
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
    command = payload.get(METRIC_ENTRY_PARAM)
    allowed = [str(part) for part in command] if isinstance(command, list) else None
    refused = refuse_processes(allow=allowed) if "output" in payload else None
    result = run_metric_command(payload, call_dir) if allowed is not None else run_call(payload)
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


def refuse_processes(allow: Sequence[str] | None = None) -> list[bool]:
    """Refuse every process the metric starts from now on in this process (E40).

    The metric reads the candidate's report; it must not run the candidate
    again. An audit hook stays for the life of the process, so only the
    metric call's own process installs it.

    Args:
        allow: A command ``[metric] entry`` (SKY-738): the one process start
            this process may make, once, for exactly this argument list. The
            metric box holds no candidate code, so the command cannot run the
            candidate again.

    Returns:
        A list that gets an entry each time a process start was refused.
    """
    refused: list[bool] = []
    pending = [list(allow)] if allow is not None else []

    def hook(event: str, args: tuple[Any, ...]) -> None:
        """Abort a process start with the refusal message.

        Args:
            event: The audit event's name.
            args: The event's arguments.

        Raises:
            ScorerError: When the event starts a process.
        """
        if event in _PROCESS_EVENTS:
            if event == "subprocess.Popen" and pending and list(args[1]) == pending[0]:
                pending.clear()
                return
            refused.append(True)
            raise ScorerError(RERUN_REFUSED)

    sys.addaudithook(hook)
    return refused


class EntryUserError(Exception):
    """The root supervisor cannot run the entry safely as a user other than root."""


def unsafe_output() -> str | None:
    """Say why the supervisor's stdout could carry a forged result, or ``None`` when it is safe.

    The parent reads the result frame from stdout alone. A pipe, socket or
    device has its other end with the runtime, so the entry cannot replace
    it. A file must be the Vercel root exec wrapper's
    ``/tmp/.app-root-output-<token>/output.out``. A process that reads the
    token from root's command line can plant a file, FIFO or link at a
    ``/tmp`` name, but the wrapper's ``mkdir`` fails on any planted name and
    the supervisor then never starts. So the folder must be a real folder
    that root owns with no group or other write bit: in sticky ``/tmp`` the
    entry, which runs as another user, cannot plant into it, rename it or
    remove it (TU-1). The file must belong to root, have no group or other
    write bit and one link, and its own name must reach it: a link that the
    redirect followed has another name. The supervisor also creates the
    wrapper's exit code file as root before the entry starts, so the entry
    cannot create it first and write a forged exit code (SKY-398).

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
    folder, _, name = path.rpartition("/")
    token = folder[len(WRAPPER_FOLDER_PREFIX) :]
    if (
        not folder.startswith(WRAPPER_FOLDER_PREFIX)
        or name != WRAPPER_OUTPUT_NAME
        or len(token) != 32
        or token.strip("0123456789abcdef")
    ):
        return f"The supervisor's stdout {path} is not a file of the exec wrapper."
    try:
        parent = os.lstat(folder)
    except OSError:
        return "The supervisor cannot check the exec wrapper's folder."
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 or parent.st_mode & 0o022:
        return f"The exec wrapper's folder {folder} is not a root-only folder."
    if os.geteuid() != 0 or info.st_uid != 0 or info.st_mode & 0o022 or info.st_nlink != 1:
        return f"The supervisor's stdout {path} is not a root-only file."
    if not stat.S_ISREG(named.st_mode) or (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino):
        return f"The supervisor's stdout {path} is a link or was replaced."
    # The parent reads the wrapper's files as the box user.
    os.fchmod(1, 0o644)
    with _quiet():
        os.fchmod(2, 0o644)
    try:
        sentinel = os.open(f"{folder}/{WRAPPER_EXIT_NAME}", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    except OSError:
        return "The exec wrapper's exit code file exists before the entry started."
    os.fchmod(sentinel, 0o644)
    os.close(sentinel)
    return None


def _has_capabilities() -> bool:
    """Tell whether this process holds any effective Linux capability.

    Returns:
        ``False`` only when ``/proc/self/status`` shows no effective capability.
        A host without that file counts as holding them.
    """
    try:
        status = Path("/proc/self/status").read_text(encoding="utf-8")
    except OSError:
        return True
    for line in status.splitlines():
        if line.startswith("CapEff:"):
            return int(line.split()[1], 16) != 0
    return True


def _entry_user(env: dict[str, str]) -> dict[str, Any]:
    """Choose the user the entry runs as, and set its environment to match.

    A root supervisor starts the entry as the owner of the candidate tree,
    its working directory. The entry then cannot signal, trace or read the
    supervisor, or change the exec wrapper's root files (SKY-398). A local
    Docker box drops every root capability, so its root cannot switch users.
    That root holds no capability, and a stdout pipe has no file that the
    entry could replace. So only a root with no capabilities and a pipe
    stdout runs the entry as itself.

    Args:
        env: The entry's environment; on a user switch it gets ``HOME``,
            ``USER``, ``LOGNAME``, and ``PATH`` from ``ENV_ENTRY_PATH``.

    Returns:
        The ``subprocess.Popen`` arguments that switch the user, or none.

    Raises:
        EntryUserError: When root owns the candidate tree, or the
            supervisor cannot set ``no_new_privs``.
    """
    if os.geteuid() != 0 or (not stat.S_ISREG(os.fstat(1).st_mode) and not _has_capabilities()):
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
    if env.get(ENV_ENTRY_PATH):
        env["PATH"] = env[ENV_ENTRY_PATH]
    _block_privilege_gain()
    return {"user": owner.st_uid, "group": owner.st_gid, "extra_groups": []}


def _block_privilege_gain() -> None:
    """Set ``no_new_privs`` on the root supervisor before it starts the entry as another user.

    The entry and every process it starts inherit the flag, and no process
    can clear it. The kernel then ignores setuid bits, so the entry cannot
    use the box's ``sudo`` to get back to root (SKY-398). The flag does not
    take root's own capabilities, so the user switch still works. The flag
    is set here and not in ``preexec_fn``, which is unsafe once the relay
    threads run.

    Raises:
        EntryUserError: When the flag cannot be set.
    """
    try:
        failed = ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0
    except (AttributeError, OSError):
        failed = True
    if failed:
        raise EntryUserError("The supervisor cannot set no_new_privs, so it does not start the [run] entry.")


def _read_answer(path: Path, user: Any, limit: int | None = None) -> dict[str, Any]:
    """Read an entry's ``output.json`` without following a link the entry left.

    Args:
        path: The answer file.
        user: The entry's uid, or ``None`` when it runs as the supervisor's user.
        limit: The largest file in bytes, or ``None`` for no limit.

    Returns:
        The answer document, or ``{"error"}`` when the file is over ``limit``.

    Raises:
        OSError: When the file is missing, a link, not a file, or not the entry's own.
    """
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        # A root supervisor must not pass on a root file that the entry hard-linked here.
        if not stat.S_ISREG(info.st_mode) or (user is not None and info.st_uid != user):
            raise OSError(f"{path} is not the entry's own file")
        data = handle.read() if limit is None else handle.read(limit + 1)
        if limit is not None and len(data) > limit:
            return {"error": f"The command's answer file is larger than {limit} bytes."}
        answer = json.loads(data.decode("utf-8"))
    return answer if isinstance(answer, dict) else {}


def run_candidate(payload: dict[str, Any], call_dir: Path) -> dict[str, Any]:
    """Run the ``[run]`` entry on one case's input in the candidate box.

    The supervisor kills the entry's process group when the entry exits, so
    a process left in that group cannot hold the output pipe open and
    stretch the run time (SKY-642). The parent then ends the box, which is
    the only cleanup of every other process (SKY-398).

    Args:
        payload: ``{"entry", "input", "timeout", "stages"}``, and
            ``ascent_package`` for a command entry.
        call_dir: The candidate call's folder.

    Returns:
        ``{"output", "error", "elapsed_s"}``: the entry's output, why the run
        failed, and the entry's run time in seconds, measured here, outside
        its processes. A failed command also gives ``stderr`` and ``stdout``.
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
        return _run_entry(
            entry, timeout, env, switch, run_dir, json.dumps(payload.get("input")), payload.get(ASCENT_PACKAGE_PARAM)
        )
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def _run_entry(
    entry: Any,
    timeout: float,
    env: dict[str, str],
    switch: dict[str, Any],
    run_dir: Path,
    body: str,
    package: Any = None,
) -> dict[str, Any]:
    """Start the entry, wait for it, kill its process group, and read its output.

    A command entry runs in a ``--run-command`` process of this file, as the
    entry's user. That process calls OpenAscent's ``call_run``, which needs
    no root and has no user switch of its own (SKY-757).

    Args:
        entry: A ``path/file.py:function`` string or a command list.
        timeout: The entry's time limit in seconds.
        env: The entry's environment.
        switch: The ``subprocess.Popen`` arguments that switch the user.
        run_dir: A new folder for the entry's input and output.
        body: The case input as JSON.
        package: The OpenAscent package folder for a command entry.

    Returns:
        ``{"output", "error", "elapsed_s"}``, and ``stderr`` and ``stdout``
        when a command failed.
    """
    user = switch.get("user")
    wait = timeout
    try:
        (run_dir / INPUT_FILE).write_text(body, encoding="utf-8")
        if user is not None:
            os.chown(run_dir, user, switch["group"])
            os.chown(run_dir / INPUT_FILE, user, switch["group"])
        if isinstance(entry, str):
            # The supervisor runs on the base interpreter without site-packages;
            # the entry is candidate code and needs the box's python3 from PATH,
            # where [setup] installed its packages (SKY-642).
            python = shutil.which("python3", path=env.get("PATH")) or sys.executable
            argv = [python, str(Path(__file__).resolve()), RUN_FLAG, str(run_dir), entry]
            limit = None
        else:
            # The command's own process needs only the standard library and the
            # copied OpenAscent package, which a python3 from PATH may be too old
            # to load. The supervisor's interpreter is the image's root-owned,
            # world-executable base python3 (or the PATH one where the image has
            # none), so the entry's user can run it and cannot replace it. The
            # command still finds python3 on PATH (SKY-757).
            argv = [sys.executable, str(Path(__file__).resolve()), RUN_COMMAND_FLAG, str(run_dir)]
            argv += [str(package or ""), f"{timeout:g}", *(str(part) for part in entry)]
            wait = timeout + _COMMAND_STOP_SECONDS
            limit = _COMMAND_ANSWER_BYTES
        # The candidate runs in its own processes, so it cannot patch this clock (SKY-642).
        started = time.monotonic()
        # Its prints go to stderr: the parent reads relay frames on stdout.
        child = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=2, start_new_session=True, **switch)
    except OSError as exc:
        return {"output": None, "error": f"The command could not start: {exc}"}
    try:
        code: int | None = child.wait(timeout=wait)
    except subprocess.TimeoutExpired:
        code = None
    with _quiet():
        os.killpg(child.pid, signal.SIGKILL)
    if code is None:
        child.wait()
    elapsed = time.monotonic() - started
    result: dict[str, Any] = {"output": None, "error": None, ELAPSED_PARAM: round(elapsed, 6)}
    if code is None:
        result["error"] = f"timeout after {timeout:g} s"
    elif code < 0:
        result["error"] = f"killed by signal {_signal_name(-code)}"
    else:
        try:
            answer = _read_answer(run_dir / OUTPUT_FILE, user, limit)
        except (OSError, ValueError):
            answer = {"error": f"exited with code {code} before it returned"}
        if "output" in answer:
            result["output"] = answer["output"]
        else:
            result["error"] = str(answer.get("error") or "returned nothing")
            result.update((stream, str(answer[stream])) for stream in ("stderr", "stdout") if answer.get(stream))
            result.update((key, True) for key in ("stderr_cut", "stdout_cut") if answer.get(key))
    return result


def command_error(document: dict[str, Any], mask: Callable[[str], str], secrets: Iterable[str]) -> str:
    """Join a failed call's cause and the tails of its stderr and its stdout.

    stderr comes first: a stage error keeps only its head, and a command's
    stdout is often progress output (SKY-757).

    Args:
        document: ``{"error"}``, and ``stderr`` and ``stdout`` from a failed
            command, with ``stderr_cut`` or ``stdout_cut`` when the box cut that log.
        mask: Removes the secrets from a text. It runs before each cut here,
            so a secret across one of these cuts leaves no part of itself.
        secrets: The values that ``mask`` removes, to find the edge of a log the box cut.

    Returns:
        The first ``COMMAND_CAUSE_CHARS`` of the masked cause, then the last
        ``COMMAND_LOG_CHARS`` of each masked log.
    """
    error = mask(str(document["error"]))
    if len(error) > COMMAND_CAUSE_CHARS:
        error = error[: COMMAND_CAUSE_CHARS - 1] + "…"
    for stream in ("stderr", "stdout"):
        if document.get(stream):
            text = str(document[stream])
            if document.get(f"{stream}_cut"):
                text = _drop_cut_edge(text, secrets)
            error += f"\n{stream}: {mask(text)[-COMMAND_LOG_CHARS:]}"
    return error


def _drop_cut_edge(text: str, secrets: Iterable[str]) -> str:
    """Drop the start of a log the box cut, so no part of a split secret stays.

    The cut can leave the end of a secret at the start, which no mask finds.
    That end is shorter than the longest secret. The drop also covers each
    whole secret that it would split, so the mask still finds the rest.

    Args:
        text: The log after the box's cut.
        secrets: The values to hide.

    Returns:
        The log without its first characters.
    """
    values = [value for value in set(secrets) if value]
    edge = max((len(value) for value in values), default=1) - 1
    moved = True
    while moved:
        moved = False
        for value in values:
            start = text.find(value, max(0, edge - len(value) + 1))
            while 0 <= start < edge:
                if start + len(value) > edge:
                    edge, moved = start + len(value), True
                start = text.find(value, start + 1)
    return text[edge:]


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


def run_stage_command(call_dir: Path, package: str, timeout: float, command: Sequence[str]) -> int:
    """Run one command call through OpenAscent's ``call_run``: ``input.json`` in, ``output.json`` out.

    A command stage and a command ``[run]`` entry both run here (SKY-757).
    ``call_run`` writes ``{"input": ...}`` to a new folder, starts the
    command with ``ASCENT_INPUT`` and ``ASCENT_OUTPUT`` and an empty stdin,
    and checks the ``{"output": ...}`` file the command writes. The text the
    command prints goes to this process's stderr, the run logs, only.

    Args:
        call_dir: The call's folder: the stage call's folder in the stage
            box, or the candidate's new run folder.
        package: The folder the parent copied the OpenAscent package into.
        timeout: The command's time limit in seconds.
        command: The command and its arguments.

    Returns:
        The process exit code: 0 whenever the output was written.
    """
    ascent: types.ModuleType | None = None
    try:
        env = {**os.environ, **_stage_relay_env(call_dir)}
        value = json.loads((call_dir / INPUT_FILE).read_text(encoding="utf-8"))
        ascent = _load_openascent(package)
        call = ascent.call_run(
            tuple(command),
            value,
            timeout_s=timeout,
            cwd=str(Path.cwd()),
            env=env,
            max_output_bytes=MAX_RUN_STDOUT_BYTES,
        )
        sys.stderr.write(_join_logs(_whole_lines(ascent, call.stdout), _whole_lines(ascent, call.stderr)))
        # Compact, so the supervisor's answer cap holds for any output under the package limit.
        text = json.dumps({"output": call.output}, allow_nan=False, ensure_ascii=False, separators=(",", ":"))
    except BaseException as exc:  # the command is arbitrary: any failure is reported, not raised
        if ascent is not None and isinstance(exc, ascent.AscentError):
            # The same logs as a passed call: stdout, then stderr (SKY-757).
            sys.stderr.write(_join_logs(_whole_lines(ascent, exc.stdout), _whole_lines(ascent, exc.stderr)))
            # The parent masks the secrets and only then cuts each log to
            # COMMAND_LOG_CHARS. It drops the edge of a log cut here (SKY-757).
            cause = INVALID_JSON_CAUSE if isinstance(exc, ascent.InvalidJsonError) else str(exc)
            document: dict[str, Any] = {"error": cause}
            for stream, log in (("stderr", exc.stderr), ("stdout", exc.stdout)):
                if log:
                    document[stream] = log[-_COMMAND_LOG_BOUND:]
                if len(log) > _COMMAND_LOG_BOUND:
                    document[f"{stream}_cut"] = True
        else:
            document = {"error": f"{type(exc).__name__}: {exc}"}
        text = json.dumps(document)
    _write_output(call_dir, text)
    return 0


def _whole_lines(ascent: types.ModuleType, log: str) -> str:
    """Drop the first line of a log that OpenAscent may have cut.

    OpenAscent keeps only the last ``LOG_TAIL_BYTES`` of each log, cut at any
    byte and with no mark. The run-log scrubber masks only whole secrets, so a
    cut through a secret would log its end. The first line holds that cut
    (SKY-757). A replaced bad byte encodes longer, so a log at the limit may
    also be one that OpenAscent did not cut: it loses its first line too.

    Args:
        ascent: The loaded OpenAscent package.
        log: One log of the call, as OpenAscent returned it.

    Returns:
        The log, without its first line when it may be cut: empty when it has no line break.
    """
    if len(log.encode("utf-8")) < ascent.component.LOG_TAIL_BYTES:
        return log
    _, newline, rest = log.partition("\n")
    return rest if newline else ""


def _join_logs(stdout: str, stderr: str) -> str:
    """Join a command's stdout and stderr for the logs, with a line break between them.

    Args:
        stdout: The command's stdout.
        stderr: The command's stderr.

    Returns:
        stdout, then a line break when stdout does not end with one, then stderr.
    """
    if stdout and not stdout.endswith("\n"):
        stdout += "\n"
    return stdout + stderr


def _stage_relay_env(call_dir: Path) -> dict[str, str]:
    """Start this call's own loopback relay when the parent wrote a relay config for it.

    Parallel calls share the stage box, so the proxy takes the config's port,
    a free one, and the call gets its own copy of the stage's MCP file with
    that port in its folder (SKY-624).

    Args:
        call_dir: The call's folder in the stage box.

    Returns:
        The proxy's variables, or nothing when the call has no relay config.
        ``ASCENT_MCP_CONFIG`` names the call's own MCP file.
    """
    path = call_dir / STAGE_RELAY_FILE
    if not path.is_file():
        return {}
    config = json.loads(path.read_text(encoding="utf-8"))
    # The stage's code may change its directory; the answer folder must not move with it.
    config["response_dir"] = str(Path(str(config["response_dir"])).resolve())
    return _relay_env(StageClient(config), config, own_mcp_file=call_dir / _STAGE_MCP_FILE)


if __name__ == "__main__":
    sys.exit(main())
