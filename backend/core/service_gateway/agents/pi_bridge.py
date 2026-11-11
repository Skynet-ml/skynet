"""Run one Skynet agent turn on the Pi agent harness (SKY-327).

Pi (``@earendil-works/pi-coding-agent``, pinned in
``sandbox-runtime/package.json`` and on the API image's ``PATH``) runs the
agent loop: it streams the model's reply, parses its tool calls, runs the
tools and calls the model again. Skynet keeps everything that touches money,
users or data. A per-turn loopback server answers Pi on two channels:

* ``POST /v1/chat/completions``: Pi's model endpoint. The turn's handler
  calls Skynet's own metered LM, so budgets, BYOK routing and usage
  metering stay as they are, and relays the chunks to Pi.
* ``GET /tools`` and ``POST /call``: the only extension Pi may load
  (:data:`TOOL_BRIDGE_EXTENSION`) registers the turn's tools from the first
  and sends each call to the second, where the backend runs it.

Pi starts with its built-in tools, discovered extensions, skills, prompt
templates, context files and network features off, so it reaches nothing
outside this server. :class:`PiEventTranslator` turns Pi's RPC events into the
stream events the chat already reads, so the frontend does not change.

This module is agent-neutral. :mod:`.pi_react` adapts a ReAct program to it;
later agents bring their own :class:`PiTurnHandlers`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import shutil
import socket
import tempfile
import threading
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .code import _NativeSubmitArgExtractor
from .tool_pending import TOOL_PENDING_CLEAR_EVENT, TOOL_PENDING_EVENT

logger = logging.getLogger(__name__)

TOOL_BRIDGE_EXTENSION = "skynet-tool-bridge"
_EXTENSION_DIR = Path(__file__).with_name("pi_extensions")
# Pi loads only what these maps name. ``-ne`` and ``-ns`` turn off Pi's own
# discovery, so an extension or skill reaches a turn only through this list.
ALLOWED_EXTENSIONS: Mapping[str, Path] = {TOOL_BRIDGE_EXTENSION: _EXTENSION_DIR / f"{TOOL_BRIDGE_EXTENSION}.ts"}
ALLOWED_SKILLS: Mapping[str, Path] = {}

PI_PROVIDER = "skynet"
PI_MODEL = "skynet-relay"
_TOKEN_ENV = "SKYNET_BRIDGE_TOKEN"
_URL_ENV = "SKYNET_BRIDGE_URL"
# Pi keeps retries, compaction and cache warming to itself; the backend's LM
# already retries, and a hidden extra model call would go unmetered here.
_PI_SETTINGS = {
    "retry": {"enabled": False, "provider": {"timeoutMs": 600_000}},
    "compaction": {"enabled": False},
    "cacheWarming": "off",
    "enableInstallTelemetry": False,
    "quietStartup": True,
}
_STDOUT_LIMIT = 64 * 1024 * 1024
_KEEPALIVE_SECONDS = 15.0
_STOP_GRACE_SECONDS = 3.0
# The stop signals of the turn whose tool runs the current code and of the
# turns above it: a coding-agent hand-off runs a nested turn in a tool thread.
_TURN_STOP: ContextVar[tuple[threading.Event, ...]] = ContextVar("pi_turn_stop", default=())


class PiBridgeError(RuntimeError):
    """Pi could not start, rejected a command, or ended the turn with an error."""


def turn_stopped() -> bool:
    """Report whether a Pi turn that runs the current tool call has stopped.

    A tool thread cannot be cancelled. A tool that can run long checks this
    while it waits and gives up, so it does not run on after its turn.

    Returns:
        Whether the stop signal of this turn, or of a turn above it, is set.
    """
    return any(stop.is_set() for stop in _TURN_STOP.get())


@dataclass
class ToolOutcome:
    """What one bridged tool call returns to Pi."""

    text: str
    is_error: bool = False
    terminate: bool = False


class PiTurnHandlers(Protocol):
    """The agent-specific half of a bridged turn."""

    def tool_specs(self) -> list[dict[str, Any]]:
        """Return the turn's tools as ``{name, description, parameters}`` dicts."""
        ...

    async def call_tool(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        """Run one tool call Pi asked for."""
        ...

    def complete(self, body: dict[str, Any]) -> AsyncIterator[str]:
        """Answer one of Pi's model requests with OpenAI stream chunks as JSON strings."""
        ...


def resolve_allowed(names: Iterable[str], allowed: Mapping[str, Path], kind: str) -> list[Path]:
    """Map allow-listed extension or skill names to their files.

    Args:
        names: The names a turn asks for.
        allowed: The allow-list for this kind.
        kind: ``"extension"`` or ``"skill"``, for the error message.

    Returns:
        The files, in the order asked.

    Raises:
        PiBridgeError: When a name is not on the allow-list.
    """
    paths = []
    for name in names:
        if name not in allowed:
            raise PiBridgeError(f"Pi {kind} {name!r} is not on Skynet's allow-list")
        paths.append(allowed[name])
    return paths


def pi_command(
    binary: str,
    *,
    extensions: Iterable[str] = (TOOL_BRIDGE_EXTENSION,),
    skills: Iterable[str] = (),
) -> list[str]:
    """Build the argv that starts Pi in RPC mode with only allow-listed add-ons.

    Args:
        binary: The ``pi`` executable.
        extensions: Allow-listed extension names to load.
        skills: Allow-listed skill names to load.

    Returns:
        The command line.
    """
    argv = [binary, "--mode", "rpc", "--no-session", "-nbt", "-ne", "-ns", "-np", "-nc", "--no-themes", "--offline"]
    for path in resolve_allowed(extensions, ALLOWED_EXTENSIONS, "extension"):
        argv += ["-e", str(path)]
    for path in resolve_allowed(skills, ALLOWED_SKILLS, "skill"):
        argv += ["--skill", str(path)]
    return [*argv, "--provider", PI_PROVIDER, "--model", PI_MODEL]


def pi_available(binary: str) -> bool:
    """Report whether the ``pi`` executable can be found.

    Args:
        binary: Name or path of the executable.

    Returns:
        Whether it resolves on ``PATH`` or as a path.
    """
    return shutil.which(binary) is not None


def _write_agent_dir(root: Path, base_url: str) -> Path:
    """Write Pi's ``models.json`` and ``settings.json`` for one turn.

    Args:
        root: The turn's private temporary directory.
        base_url: The bridge's OpenAI-compatible base URL.

    Returns:
        The agent directory to pass as ``PI_CODING_AGENT_DIR``.
    """
    agent_dir = root / "agent"
    agent_dir.mkdir(mode=0o700)
    model = {
        "id": PI_MODEL,
        "name": PI_MODEL,
        # The bridge picks the real model and its reasoning settings; Pi
        # must not add its own thinking fields to the request.
        "reasoning": False,
        "input": ["text"],
        "contextWindow": 1_000_000,
        "maxTokens": 64_000,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    }
    models = {
        "providers": {
            PI_PROVIDER: {
                "baseUrl": base_url,
                "api": "openai-completions",
                "apiKey": f"${_TOKEN_ENV}",
                "models": [model],
            }
        }
    }
    (agent_dir / "models.json").write_text(json.dumps(models))
    (agent_dir / "settings.json").write_text(json.dumps(_PI_SETTINGS))
    return agent_dir


def _pi_env(root: Path, agent_dir: Path, base_url: str, token: str) -> dict[str, str]:
    """Build Pi's environment: no backend secrets, only what the bridge needs.

    Args:
        root: The turn's private temporary directory, used as ``HOME``.
        agent_dir: Pi's agent directory.
        base_url: The bridge's base URL.
        token: The turn's bearer token.

    Returns:
        The environment for the Pi process.
    """
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(root),
        "PI_CODING_AGENT_DIR": str(agent_dir),
        "PI_OFFLINE": "1",
        "PI_SKIP_VERSION_CHECK": "1",
        "PI_TELEMETRY": "0",
        _URL_ENV: base_url,
        _TOKEN_ENV: token,
    }


class _LoopbackServer(uvicorn.Server):
    """A uvicorn server that leaves the host process's signal handlers alone."""

    @contextlib.contextmanager
    def capture_signals(self) -> Any:
        """Skip uvicorn's signal capture; the API process owns SIGINT and SIGTERM.

        Yields:
            Nothing.
        """
        yield


class _BridgeApp:
    """The per-turn HTTP endpoints Pi calls."""

    def __init__(self, handlers: PiTurnHandlers, token: str) -> None:
        """Bind the endpoints to one turn.

        Args:
            handlers: The turn's tools and model relay.
            token: The bearer token Pi must send.
        """
        self._handlers = handlers
        self._token = token
        self.failure: BaseException | None = None
        self.failed = asyncio.Event()
        self.stop = threading.Event()
        self._stops = (*_TURN_STOP.get(), self.stop)
        self._tool_tasks: set[asyncio.Future[ToolOutcome]] = set()
        self.app = Starlette(
            routes=[
                Route("/tools", self._tools, methods=["GET"]),
                Route("/call", self._call, methods=["POST"]),
                Route("/v1/chat/completions", self._complete, methods=["POST"]),
            ]
        )

    def _authorized(self, request: Request) -> bool:
        """Check the request's bearer token in constant time.

        Args:
            request: The incoming request.

        Returns:
            Whether the token matches.
        """
        header = request.headers.get("authorization", "")
        return secrets.compare_digest(header, f"Bearer {self._token}")

    def _fail(self, error: BaseException) -> None:
        """Keep the first handler failure so the turn can raise it.

        Args:
            error: The failure.
        """
        if self.failure is None:
            self.failure = error
        self.failed.set()

    async def _tools(self, request: Request) -> Response:
        """List the turn's tools.

        Args:
            request: The incoming request.

        Returns:
            The tool specs, or 401.
        """
        if not self._authorized(request):
            return Response(status_code=401)
        return JSONResponse(self._handlers.tool_specs())

    async def _call(self, request: Request) -> Response:
        """Run one tool call, sending whitespace while it waits.

        Pi's HTTP client drops a response that stays silent for five minutes,
        and a tool can wait that long for the user's approval.

        Args:
            request: The incoming request with ``{tool, args}``.

        Returns:
            A streamed ``{ok, text, error, terminate}`` JSON body, or 401.
        """
        if not self._authorized(request):
            return Response(status_code=401)
        body = await request.json()
        name = str(body.get("tool") or "")
        args = body.get("args") if isinstance(body.get("args"), dict) else {}
        task = asyncio.ensure_future(self._run_tool(name, args))
        self._tool_tasks.add(task)
        task.add_done_callback(self._tool_tasks.discard)

        async def stream() -> AsyncIterator[bytes]:
            """Yield keepalive whitespace, then the call's JSON reply.

            Yields:
                Body bytes.
            """
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=_KEEPALIVE_SECONDS)
                if not done:
                    yield b" "
            try:
                outcome = task.result()
            except Exception as error:
                self._fail(error)
                reply = {"ok": False, "error": f"Execution error in {name}: {error}"}
            else:
                reply = (
                    {"ok": False, "error": outcome.text}
                    if outcome.is_error
                    else {"ok": True, "text": outcome.text, "terminate": outcome.terminate}
                )
            yield json.dumps(reply, ensure_ascii=False).encode()

        return StreamingResponse(stream(), media_type="application/json")

    async def _run_tool(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        """Run one tool call with the turn's stop signals in its context.

        Args:
            name: The tool name.
            args: The call's arguments.

        Returns:
            The handler's outcome.
        """
        _TURN_STOP.set(self._stops)
        return await self._handlers.call_tool(name, args)

    def _stopped(self) -> bool:
        """Report whether this turn or a turn above it has stopped.

        Returns:
            Whether a stop signal is set.
        """
        return any(stop.is_set() for stop in self._stops)

    async def close(self) -> None:
        """Set the turn's stop signal, cancel the tool calls that still run and wait for them.

        A forced server exit drops the response streams but not the tasks
        behind them. A tool thread does not stop on a cancel; it sees the
        signal through :func:`turn_stopped`, and the handler waits for it
        for a bounded time.
        """
        self.stop.set()
        tasks = list(self._tool_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _complete(self, request: Request) -> Response:
        """Relay one model call as server-sent events.

        A handler failure (a budget stop, a provider error) is kept for the
        turn to raise as itself; Pi sees a broken stream and ends its run.

        Args:
            request: Pi's chat-completions request.

        Returns:
            The SSE stream, or 401.
        """
        if not self._authorized(request):
            return Response(status_code=401)
        body = await request.json()

        async def stream() -> AsyncIterator[bytes]:
            """Yield the handler's chunks as SSE records.

            Yields:
                Body bytes.
            """
            # A nested turn whose parent stopped must not start a metered call.
            if self._stopped():
                self._fail(PiBridgeError("The agent turn stopped before this model call"))
                yield b'data: {"error": {"message": "The agent turn stopped"}}\n\n'
                return
            try:
                async for chunk in self._handlers.complete(body):
                    yield f"data: {chunk}\n\n".encode()
            except Exception as error:
                self._fail(error)
                yield b'data: {"error": {"message": "Skynet model relay failed"}}\n\n'
                return
            yield b"data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")


class PiEventTranslator:
    """Turn Pi RPC events into the chat's stream events.

    The reply reaches the user through a reply tool (``submit``) whose
    argument streams as ``message_patch``; a plain-text final answer streams
    the same way. Text written beside a tool call is taken back with
    ``message_reset``, as the old loop never showed it.
    """

    def __init__(
        self,
        emit: Callable[[dict[str, Any]], None],
        *,
        reply_tool: str,
        reply_arg: str,
        pending_skip: frozenset[str],
    ) -> None:
        """Bind the translator to a turn.

        Args:
            emit: Sends one stream event.
            reply_tool: The tool whose argument is the reply.
            reply_arg: That tool's reply argument.
            pending_skip: Tools that get no ``tool_pending`` row.
        """
        self._emit = emit
        self._reply_tool = reply_tool
        self._extractor = _NativeSubmitArgExtractor(reply_arg)
        self._skip = pending_skip
        self._streamed = ""
        self._text_reply = ""
        self._reply_index: int | None = None
        self._tool_index: dict[int, int] = {}
        self.error: str | None = None
        self.settled = False

    @property
    def text_reply(self) -> str:
        """Return the plain text of the last assistant message that ended the run."""
        return self._text_reply

    def retract(self) -> None:
        """Take back the reply streamed so far, if any."""
        if self._streamed:
            self._streamed = ""
            self._emit({"event": "message_reset", "data": {}})

    def _patch(self, delta: str | None) -> None:
        """Stream a reply delta.

        Args:
            delta: New reply text, or ``None``.
        """
        if delta:
            self._streamed += delta
            self._emit({"event": "message_patch", "data": {"chunk": delta}})

    def feed(self, event: dict[str, Any]) -> None:
        """Translate one Pi event.

        Args:
            event: One parsed RPC record.
        """
        kind = event.get("type")
        if kind == "turn_start":
            self._extractor.reset()
            self._reply_index = None
            self._tool_index = {}
            self._emit({"event": TOOL_PENDING_CLEAR_EVENT, "data": {}})
        elif kind == "message_update":
            self._update(event.get("assistantMessageEvent") or {})
        elif kind == "message_end":
            self._end(event.get("message") or {})
        elif kind == "agent_settled":
            self.settled = True
        elif kind == "extension_error":
            self.error = str(event.get("error") or "Pi extension failed")

    def _update(self, update: dict[str, Any]) -> None:
        """Translate one streamed assistant-message update.

        Args:
            update: Pi's ``assistantMessageEvent``.
        """
        kind = update.get("type")
        if kind == "thinking_delta":
            self._emit({"event": "reasoning_patch", "data": {"chunk": update.get("delta") or ""}})
        elif kind == "text_delta":
            if self._reply_index is None:
                self._patch(update.get("delta"))
        elif kind == "toolcall_start":
            content_index = update.get("contentIndex")
            tool = str(update.get("toolName") or "")
            index = len(self._tool_index)
            self._tool_index[content_index] = index
            if tool == self._reply_tool and self._reply_index is None:
                # The reply rides the tool now; text written before it was not the reply.
                self.retract()
                self._reply_index = content_index
            elif tool and tool not in self._skip:
                self._emit({"event": TOOL_PENDING_EVENT, "data": {"index": index, "tool": tool}})
        elif kind == "toolcall_delta" and update.get("contentIndex") == self._reply_index:
            self._patch(self._extractor.feed(update.get("delta") or ""))

    def _end(self, message: dict[str, Any]) -> None:
        """Settle one finished message.

        Args:
            message: Pi's final message record.
        """
        if message.get("role") != "assistant":
            return
        stop = message.get("stopReason")
        if stop == "error":
            self.error = str(message.get("errorMessage") or "The model call failed")
        if stop == "toolUse":
            if self._reply_index is None:
                self.retract()
            return
        self._text_reply = "".join(
            part.get("text", "") for part in message.get("content") or [] if part.get("type") == "text"
        )


class _PiProcess:
    """One Pi process in RPC mode."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        """Wrap a started process.

        Args:
            proc: The process with piped stdin and stdout.
        """
        self._proc = proc
        self._stderr: list[str] = []
        self._stderr_task = asyncio.ensure_future(self._drain_stderr())

    @classmethod
    async def start(cls, argv: list[str], env: dict[str, str], cwd: Path) -> _PiProcess:
        """Start Pi.

        Args:
            argv: The command line.
            env: The environment.
            cwd: The working directory.

        Returns:
            The running process.
        """
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(cwd),
            limit=_STDOUT_LIMIT,
        )
        return cls(proc)

    async def _drain_stderr(self) -> None:
        """Keep the tail of Pi's stderr for error reports."""
        assert self._proc.stderr is not None
        async for line in self._proc.stderr:
            self._stderr = [*self._stderr[-19:], line.decode(errors="replace").rstrip()]

    def stderr_tail(self) -> str:
        """Return the last lines Pi wrote to stderr."""
        return "\n".join(self._stderr)

    async def send(self, command: dict[str, Any]) -> None:
        """Write one RPC command.

        Args:
            command: The command record.
        """
        assert self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(command).encode() + b"\n")
        await self._proc.stdin.drain()

    async def records(self) -> AsyncIterator[dict[str, Any]]:
        """Yield Pi's stdout records until it closes stdout.

        Records end at LF only: JSON strings may hold other line separators.

        Yields:
            Parsed records; lines that are not JSON objects are skipped.
        """
        assert self._proc.stdout is not None
        while True:
            try:
                line = await self._proc.stdout.readuntil(b"\n")
            except asyncio.IncompleteReadError as partial:
                line = partial.partial
                if not line:
                    return
            line = line.rstrip(b"\n").rstrip(b"\r")
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                logger.debug("Pi wrote a non-JSON line: %r", line[:200])
                continue
            if isinstance(record, dict):
                yield record

    async def stop(self) -> None:
        """Ask Pi to stop, then kill it if it does not."""
        proc = self._proc
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), _STOP_GRACE_SECONDS)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
        self._stderr_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._stderr_task


@contextlib.asynccontextmanager
async def _serve(app: _BridgeApp) -> AsyncIterator[str]:
    """Serve the bridge on an ephemeral loopback port for the block's duration.

    Args:
        app: The turn's endpoints.

    Yields:
        The base URL.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(app.app, lifespan="off", log_config=None, access_log=False, log_level="warning")
    server = _LoopbackServer(config)
    task = asyncio.ensure_future(server.serve(sockets=[sock]))
    try:
        while not server.started:
            if task.done():
                task.result()
                raise PiBridgeError("The Pi bridge server stopped before it started")
            await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        server.force_exit = True
        # A second cancel can land in this wait; the tools must still stop.
        try:
            with contextlib.suppress(Exception):
                await task
        finally:
            try:
                await app.close()
            finally:
                sock.close()


async def run_pi_turn(
    *,
    binary: str,
    handlers: PiTurnHandlers,
    translator: PiEventTranslator,
    prompt: str,
    extensions: Iterable[str] = (TOOL_BRIDGE_EXTENSION,),
    skills: Iterable[str] = (),
) -> None:
    """Run one agent turn on Pi and return when Pi settles.

    Args:
        binary: The ``pi`` executable.
        handlers: The turn's tools and model relay.
        translator: Receives every Pi event.
        prompt: The user message Pi starts the run with. The handler's relay
            may render the real prompt itself.
        extensions: Allow-listed extensions to load.
        skills: Allow-listed skills to load.

    Raises:
        PiBridgeError: When the ``pi`` executable is missing, Pi fails to
            start, rejects the prompt, or ends the run with an error that no
            handler raised.
        Exception: The first exception a handler raised, as itself, so the
            caller's budget and gateway error handling still applies.
    """
    if not pi_available(binary):
        raise PiBridgeError(f"The Pi agent harness executable {binary!r} was not found; install Pi 0.87.1")
    argv = pi_command(binary, extensions=extensions, skills=skills)
    token = secrets.token_urlsafe(32)
    app = _BridgeApp(handlers, token)
    with tempfile.TemporaryDirectory(prefix="skynet-pi-") as tmp:
        root = Path(tmp)
        async with _serve(app) as base_url:
            agent_dir = _write_agent_dir(root, f"{base_url}/v1")
            pi = await _PiProcess.start(argv, _pi_env(root, agent_dir, base_url, token), root)
            try:
                await _drive(pi, app, translator, prompt)
            finally:
                await pi.stop()
    if app.failure is not None:
        raise app.failure
    if translator.error:
        raise PiBridgeError(translator.error)


async def _drive(pi: _PiProcess, app: _BridgeApp, translator: PiEventTranslator, prompt: str) -> None:
    """Send the prompt and feed Pi's events to the translator until Pi settles.

    Args:
        pi: The running process.
        app: The turn's endpoints; a handler failure ends the run early.
        translator: Receives every event.
        prompt: The starting user message.

    Raises:
        PiBridgeError: When Pi rejects the prompt or exits before it settles.
    """
    await pi.send({"type": "prompt", "message": prompt})
    records = pi.records().__aiter__()
    failed = asyncio.ensure_future(app.failed.wait())
    try:
        while True:
            next_record = asyncio.ensure_future(records.__anext__())
            done, _ = await asyncio.wait({next_record, failed}, return_when=asyncio.FIRST_COMPLETED)
            if failed in done:
                next_record.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await pi.send({"type": "abort"})
                return
            try:
                record = next_record.result()
            except StopAsyncIteration:
                raise PiBridgeError(f"Pi exited before the turn finished: {pi.stderr_tail()}") from None
            if record.get("type") == "response":
                if record.get("success") is False:
                    raise PiBridgeError(f"Pi rejected {record.get('command')}: {record.get('error')}")
                continue
            translator.feed(record)
            if translator.settled:
                return
    finally:
        failed.cancel()


__all__ = [
    "ALLOWED_EXTENSIONS",
    "ALLOWED_SKILLS",
    "TOOL_BRIDGE_EXTENSION",
    "PiBridgeError",
    "PiEventTranslator",
    "PiTurnHandlers",
    "ToolOutcome",
    "pi_available",
    "pi_command",
    "resolve_allowed",
    "run_pi_turn",
    "turn_stopped",
]
