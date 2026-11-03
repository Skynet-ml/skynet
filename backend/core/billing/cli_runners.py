"""Run one completion on a local coding CLI under the user's own subscription.

Each runner starts the CLI once, without provider API keys in its environment,
so the CLI bills the signed-in subscription and never a key a developer left
in the shell. :func:`core.local.cli_isolation.isolated_cli` also keeps the
user's own instructions, memory and MCP servers out of each call. Each CLI runs as a plain completion: its host tools (shell, file
reads, web) are turned off, so a prompt cannot make it read the user's files.
Each CLI also gets :data:`SYSTEM_PROMPT` in place of its own agent prompt, and
no working directory, OS or sandbox details, so the model sees what a hosted
model call sends: the fixed prompt and the caller's messages.
Each CLI runs in its own process group, and a timeout stops the whole group,
including any child a wrapper script started.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import Settings
from ..local import cli_isolation, cli_probe

_LIMIT_TEXT = re.compile(
    r"usage limit|rate limit|rate-limit|hit your limit|limit reached|limit exceeded|quota|too many requests",
    re.IGNORECASE,
)
_EPOCH_RESET = re.compile(r"\|\s*(\d{10})\b")
_ISO_RESET = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?")
_CLOCK_RESET = re.compile(
    r"(?:resets?|try again|available again)\s*(?:at|after|on)?\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
    re.IGNORECASE,
)
_KILL_GRACE_SECONDS = 2.0
SYSTEM_PROMPT = (
    "You are a helpful assistant. Answer the user's message directly. "
    "Do not run code or use tools, except a tool that returns your final answer in a required format."
)
"""The system prompt each CLI gets in place of its own coding-agent prompt."""
# Codex features that give the model a host tool. With them off, codex answers
# from the prompt alone; the read-only sandbox would still let a shell read files.
_CODEX_DISABLED_FEATURES = (
    "shell_tool",
    "unified_exec",
    "code_mode_host",
    "apps",
    "plugins",
    "multi_agent",
    "memories",
    "browser_use",
    "computer_use",
    "image_generation",
    "view_image",
    "hooks",
    "skill_search",
    "goals",
    "sleep_tool",
    "tool_suggest",
)
# Codex config that keeps its own context out of the request: no sandbox,
# apps or environment (cwd, shell, date) messages, no plan or question tools,
# and no tools nested in the code-mode ``exec`` tool. The models' catalog
# entry still declares ``exec`` and ``wait``; with ``code_mode_host`` off,
# ``exec`` refuses every call.
_CODEX_CONFIG = (
    "include_permissions_instructions=false",
    "include_apps_instructions=false",
    "include_environment_context=false",
    "tools.update_plan.enabled=false",
    "tools.experimental_request_user_input.enabled=false",
    'features.code_mode.excluded_tool_namespaces=["functions"]',
)
_CLAUDE_SETTINGS = json.dumps({"showThinkingSummaries": True})
# ``codex exec`` flags from :data:`cli_isolation.CODEX_ARGS` that ``codex app-server`` rejects.
_CODEX_EXEC_ONLY_ARGS = frozenset({"--ignore-user-config", "--ignore-rules", "--ephemeral"})
_TRANSIENT_TEXT = re.compile(
    r"overloaded|timed? ?out|temporar|connection|network|econnreset|503|502|500|529", re.IGNORECASE
)


@dataclass(frozen=True)
class CliCompletion:
    """One finished CLI answer and the usage the CLI reported for it."""

    text: str
    prompt_tokens: int
    completion_tokens: int
    model_ms: int | None
    raw: dict[str, Any] = field(default_factory=dict)


class SubscriptionRateLimitedError(Exception):
    """Report that a CLI's subscription reached its usage cap."""

    def __init__(self, provider: str, reset_at: datetime | None, message: str) -> None:
        """Keep the capped provider, when the cap ends, and the CLI's own words.

        Args:
            provider: The capped CLI.
            reset_at: When the cap ends, in UTC, or ``None`` when the CLI did not say.
            message: The CLI's message, for the user.
        """
        super().__init__(message)
        self.provider = provider
        self.reset_at = reset_at
        self.message = message


class CliRunError(Exception):
    """Report a CLI call that gave no usable answer."""

    def __init__(self, provider: str, message: str, *, transient: bool) -> None:
        """Keep the provider, a short reason, and whether a retry can help.

        Args:
            provider: The CLI that failed.
            message: A short reason, without prompt text.
            transient: True when the same call can succeed on a retry.
        """
        super().__init__(message)
        self.provider = provider
        self.message = message
        self.transient = transient


def parse_reset_at(text: str, *, now: datetime | None = None) -> datetime | None:
    """Read when a usage cap ends from a CLI's limit message.

    Args:
        text: The CLI's message.
        now: The current time, for a clock-only reset such as ``resets 3pm``.

    Returns:
        The reset time in UTC, or ``None`` when the message names none.
    """
    current = now or datetime.now(UTC)
    epoch = _EPOCH_RESET.search(text)
    if epoch:
        return datetime.fromtimestamp(int(epoch.group(1)), UTC)
    stamp = _ISO_RESET.search(text)
    if stamp:
        try:
            value = datetime.fromisoformat(stamp.group(0))
        except ValueError:
            value = None
        if value is not None:
            return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(UTC)
    clock = _CLOCK_RESET.search(text)
    if clock:
        hour, minute, meridiem = int(clock.group(1)), int(clock.group(2) or 0), (clock.group(3) or "").lower()
        if meridiem == "pm" and hour < 12:
            hour += 12
        if meridiem == "am" and hour == 12:
            hour = 0
        if hour > 23 or minute > 59:
            return None
        # The CLIs print the reset in the user's own clock, which is this machine's.
        local = current.astimezone()
        reset = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if reset <= local:
            reset += timedelta(days=1)
        return reset.astimezone(UTC)
    return None


def limit_error(provider: str, text: str) -> SubscriptionRateLimitedError | None:
    """Return a cap error when a CLI's message says the subscription is capped.

    Args:
        provider: The CLI that wrote the message.
        text: The CLI's message or output.

    Returns:
        The cap error, or ``None`` when the message is not about a cap.
    """
    if not text or not _LIMIT_TEXT.search(text):
        return None
    return SubscriptionRateLimitedError(provider, parse_reset_at(text), text.strip()[:500])


def _failure(provider: str, text: str) -> Exception:
    """Classify a failed call as a cap, a transient failure, or a final failure.

    Args:
        provider: The CLI that failed.
        text: Its error text.

    Returns:
        The exception for the caller to raise.
    """
    capped = limit_error(provider, text)
    if capped is not None:
        return capped
    message = text.strip()[:500] or f"{provider} returned no answer"
    return CliRunError(provider, message, transient=bool(_TRANSIENT_TEXT.search(text)))


def _int(value: Any) -> int:
    """Return a non-negative token count, or 0 when ``value`` is not one."""
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) and value > 0 else 0


def _start(provider: str, argv: list[str], env: Mapping[str, str], cwd: Path) -> subprocess.Popen[str]:
    """Start a CLI in its own process group, with pipes for all three streams.

    Args:
        provider: The CLI, for error reports.
        argv: The full command.
        env: The CLI's environment, from :func:`cli_isolation.isolated_cli`.
        cwd: The directory the CLI runs in.

    Returns:
        The running process; its process group id is its pid.

    Raises:
        CliRunError: When the binary is missing.
    """
    try:
        return subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            env=dict(env),
            start_new_session=True,
        )
    except FileNotFoundError as error:
        raise CliRunError(provider, f"{provider} is not installed", transient=False) from error


def _kill_group(process: subprocess.Popen[str]) -> None:
    """Stop a CLI's whole process group: SIGTERM, a short grace, then SIGKILL.

    A wrapper script (such as an npm or shell shim) runs the real CLI as its
    child; killing only the wrapper leaves that child running with the pipes open.

    Args:
        process: A process started by :func:`_start`.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=_KILL_GRACE_SECONDS)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)


def _close(process: subprocess.Popen[str]) -> None:
    """Close a stopped CLI's pipes and reap it.

    Args:
        process: A process started by :func:`_start`.
    """
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            with contextlib.suppress(OSError):
                stream.close()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=_KILL_GRACE_SECONDS)


def _stream(
    provider: str,
    argv: list[str],
    prompt: str,
    env: Mapping[str, str],
    settings: Settings,
    cwd: Path,
    on_event: Callable[[dict[str, Any]], None],
) -> tuple[list[str], str, bool]:
    """Run a CLI and pass each JSON event line of its stdout to ``on_event`` as it arrives.

    Args:
        provider: The CLI, for error reports.
        argv: The full command.
        prompt: Text sent on stdin.
        env: The CLI's environment, from :func:`cli_isolation.isolated_cli`.
        settings: Application settings (timeout).
        cwd: The directory the CLI runs in.
        on_event: Called with each JSON object line, in the reading thread.

    Returns:
        ``(other_lines, stderr, timed_out)``: the stdout lines that are not
        JSON objects, all of stderr, and whether the call timeout stopped the CLI.

    Raises:
        CliRunError: When the binary is missing.
    """
    process = _start(provider, argv, env, cwd)
    timed_out = threading.Event()

    def expire() -> None:
        """Stop the whole process group at the call timeout."""
        timed_out.set()
        _kill_group(process)

    def feed() -> None:
        """Write the prompt; a CLI that exits early only closes the pipe."""
        assert process.stdin is not None
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
            process.stdin.write(prompt)
            process.stdin.close()

    timer = threading.Timer(settings.cli_call_timeout_seconds, expire)
    timer.start()
    other: list[str] = []
    stderr_lines: list[str] = []
    reader = threading.Thread(target=lambda: stderr_lines.extend(process.stderr or ()), daemon=True)
    reader.start()
    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    try:
        assert process.stdout is not None
        for line in process.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                event = None
            if isinstance(event, dict):
                on_event(event)
            elif line.strip():
                other.append(line)
        process.wait()
    except BaseException:
        _kill_group(process)
        raise
    finally:
        timer.cancel()
        writer.join(timeout=_KILL_GRACE_SECONDS)
        reader.join(timeout=_KILL_GRACE_SECONDS)
        # A child the CLI left behind must not outlive the call.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        _close(process)
    return other, "".join(stderr_lines), timed_out.is_set()


def _claude_delta(event: Mapping[str, Any]) -> tuple[str, str] | None:
    """Return the thinking or answer text of one ``stream-json`` partial-message event.

    Args:
        event: One event line from ``claude -p --output-format stream-json``.

    Returns:
        ``("reasoning", text)`` for a ``thinking_delta``, ``("content", text)``
        for a ``text_delta``, or ``None`` for any other event.
    """
    inner = event.get("event") if event.get("type") == "stream_event" else None
    if not isinstance(inner, dict) or inner.get("type") != "content_block_delta":
        return None
    delta = inner.get("delta")
    if not isinstance(delta, dict):
        return None
    kind, key = {"thinking_delta": ("reasoning", "thinking"), "text_delta": ("content", "text")}.get(
        str(delta.get("type")), ("", "")
    )
    text = delta.get(key) if kind else None
    return (kind, text) if isinstance(text, str) and text else None


def _claude_user_message(prefix: str, rest: str) -> dict[str, Any]:
    """Build the stream-json user message that sends a prompt as two text blocks.

    Args:
        prefix: The start of the prompt that later calls repeat.
        rest: The remaining text.

    Returns:
        The message, with a cache mark after ``prefix``.
    """
    # Claude Code marks its own blocks with a one-hour TTL, and the API refuses
    # a five-minute mark placed before a one-hour one.
    mark = {"type": "ephemeral", "ttl": "1h"}
    blocks = [{"type": "text", "text": prefix, "cache_control": mark}, {"type": "text", "text": rest}]
    return {"type": "user", "message": {"role": "user", "content": blocks}}


def run_claude(
    model_arg: str,
    prompt: str,
    json_schema: Mapping[str, Any] | None,
    settings: Settings,
    cwd: Path,
    on_delta: Callable[[str, str], None] | None = None,
    *,
    think: bool = True,
    cache_prefix: str = "",
) -> CliCompletion:
    """Answer one prompt with ``claude -p``, streaming its events.

    Args:
        model_arg: The CLI's ``--model`` value.
        prompt: The full prompt.
        json_schema: A JSON schema the answer must match, or ``None``.
        settings: Application settings.
        cwd: The directory the CLI runs in.
        on_delta: Called with ``(kind, text)`` for each piece of thinking
            (``"reasoning"``) or answer (``"content"``) as it arrives, or ``None``.
            A schema call streams no answer text: its answer is the structured value.
        think: ``False`` turns Claude Code's default thinking off.
        cache_prefix: The start of ``prompt`` that later calls repeat, or ``""``.

    Returns:
        The completion.

    Raises:
        SubscriptionRateLimitedError: When the subscription is capped.
        CliRunError: When the call fails, or gives no structured output for a schema.
    """
    results: list[dict[str, Any]] = []

    def take(event: dict[str, Any]) -> None:
        """Keep the result event, and pass thinking and answer text on as they arrive."""
        if event.get("type") == "result":
            results.append(event)
            return
        delta = _claude_delta(event)
        if delta is None or on_delta is None or (delta[0] == "content" and json_schema is not None):
            return
        on_delta(*delta)

    # Without the isolation flags a large personal setup overflows the context.
    with cli_isolation.isolated_cli("claude") as isolation:
        argv = [
            cli_probe.cli_binary("claude", settings),
            "-p",
            # stream-json needs --verbose; partial messages carry the thinking
            # deltas. Its last event is the same result object ``json`` prints.
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--tools",
            "",
            "--system-prompt",
            SYSTEM_PROMPT,
            *isolation.args,
            # Without it, recent models send thinking blocks with no text, and
            # the chat shows nothing while the model thinks for many seconds.
            "--settings",
            _CLAUDE_SETTINGS,
            "--model",
            model_arg,
        ]
        if json_schema is not None:
            argv += ["--json-schema", json.dumps(json_schema)]
        stdin = prompt
        if cache_prefix and prompt.startswith(cache_prefix) and prompt != cache_prefix:
            # Claude Code marks only the end of the prompt for caching, so a
            # one-block prompt that grows each step never matches the last
            # step's cache entry. The same text in two blocks, with a mark
            # after the repeated part, lets the next step read it.
            argv += ["--input-format", "stream-json"]
            stdin = json.dumps(_claude_user_message(cache_prefix, prompt[len(cache_prefix) :])) + "\n"
        env = dict(isolation.env)
        if not think:
            # Claude Code thinks by default, even on Haiku: a one-line answer
            # then took 2,700-5,500 output tokens and 25-48 s, against about
            # 1.5 s with a zero thinking budget.
            env["MAX_THINKING_TOKENS"] = "0"
        other, stderr, timed_out = _stream("claude", argv, stdin, env, settings, cwd, take)
    if not results:
        if timed_out:
            raise CliRunError("claude", "claude timed out", transient=False)
        raise _failure("claude", "\n".join(part for part in ("".join(other), stderr) if part.strip()))
    raw = results[-1]
    if raw.get("is_error"):
        raise _failure("claude", str(raw.get("result") or stderr))
    structured = raw.get("structured_output")
    if json_schema is not None and structured is None:
        # Claude Code asks once more for the StructuredOutput tool, then exits
        # with success and free text; that text does not match the schema.
        raise CliRunError("claude", "claude returned no structured output for the JSON schema", transient=True)
    text = json.dumps(structured) if structured is not None else str(raw.get("result") or "")
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    prompt_tokens = (
        _int(usage.get("input_tokens"))
        + _int(usage.get("cache_read_input_tokens"))
        + _int(usage.get("cache_creation_input_tokens"))
    )
    api_ms = raw.get("duration_api_ms")
    return CliCompletion(
        text=text,
        prompt_tokens=prompt_tokens,
        completion_tokens=_int(usage.get("output_tokens")),
        model_ms=_int(api_ms) if isinstance(api_ms, int | float) and api_ms > 0 else None,
        raw=raw,
    )


def run_codex(
    model_arg: str,
    prompt: str,
    json_schema: Mapping[str, Any] | None,
    settings: Settings,
    cwd: Path,
    on_delta: Callable[[str, str], None] | None = None,
) -> CliCompletion:
    """Answer one prompt with ``codex app-server`` in a read-only sandbox, with its host tools and agent prompt off.

    ``codex exec --json`` reports the answer only once it is complete; the app
    server sends each piece of the answer and of the reasoning summary as the
    model writes it, so the chat can show both live.

    Args:
        model_arg: The CLI's model value.
        prompt: The full prompt, sent as the turn's one text input.
        json_schema: A JSON schema the answer must match, or ``None``.
        settings: Application settings.
        cwd: The directory the CLI runs in.
        on_delta: Called with ``(kind, text)`` for each piece of reasoning
            summary (``"reasoning"``) or answer (``"content"``) as it arrives, or
            ``None``. A schema call streams no answer text: its answer is the structured value.

    Returns:
        The completion; ``model_ms`` is the time from ``turn/start`` to ``turn/completed``.

    Raises:
        SubscriptionRateLimitedError: When the subscription is capped.
        CliRunError: When the call fails.
    """
    with cli_isolation.isolated_cli("codex") as isolation:
        argv = [
            cli_probe.cli_binary("codex", settings),
            "app-server",
            # The app server has no exec-only flags. The private CODEX_HOME
            # already holds no user config or rules, and the thread is ephemeral.
            *(arg for arg in isolation.args if arg not in _CODEX_EXEC_ONLY_ARGS),
            "-c",
            'web_search="disabled"',
            *(arg for feature in _CODEX_DISABLED_FEATURES for arg in ("--disable", feature)),
            *(arg for setting in _CODEX_CONFIG for arg in ("-c", setting)),
        ]
        thread = {
            "model": model_arg,
            "baseInstructions": SYSTEM_PROMPT,
            "sandbox": "read-only",
            "approvalPolicy": "never",
            "ephemeral": True,
            "cwd": str(cwd),
        }
        # The server sends reasoning summaries only when the turn asks for them.
        turn: dict[str, Any] = {"input": [{"type": "text", "text": prompt}], "summary": "auto"}
        if json_schema is not None:
            turn["outputSchema"] = dict(json_schema)
        return _codex_turn(argv, thread, turn, isolation.env, settings, cwd, on_delta, json_schema is None)


class _CodexTurn:
    """Read the app server's notifications for one turn and keep what the completion needs."""

    def __init__(self, on_delta: Callable[[str, str], None] | None, stream_answer: bool) -> None:
        """Start with no answer.

        Args:
            on_delta: Called with ``(kind, text)`` for each streamed piece, or ``None``.
            stream_answer: Whether answer pieces go to ``on_delta``.
        """
        self.on_delta = on_delta
        self.stream_answer = stream_answer
        self.answer = ""
        self.usage: dict[str, Any] = {}
        self.errors: list[str] = []
        self.finished = False
        self.commentary: set[str] = set()
        self.reasoning_parts: set[tuple[str, str, int]] = set()

    def _send(self, kind: str, text: str) -> None:
        """Pass one piece of text on, when there is a listener and text."""
        if self.on_delta is not None and text:
            self.on_delta(kind, text)

    def _send_reasoning(self, part: tuple[str, str, int], text: str) -> None:
        """Pass one thinking piece on, with a blank line before each new part.

        Args:
            part: The source, item id and part index the piece belongs to.
            text: The text piece.
        """
        if part not in self.reasoning_parts and text:
            if self.reasoning_parts:
                text = "\n\n" + text
            self.reasoning_parts.add(part)
        self._send("reasoning", text)

    def take(self, method: str, params: Mapping[str, Any]) -> None:
        """Read one notification.

        Args:
            method: The notification method.
            params: Its parameters.
        """
        item = params.get("item") if isinstance(params.get("item"), dict) else {}
        if method == "item/started" and item.get("type") == "agentMessage" and item.get("phase") == "commentary":
            # A progress note is not the answer. It shows with the thinking, so the user sees all the CLI sends.
            self.commentary.add(str(item.get("id")))
        elif method == "item/agentMessage/delta":
            item_id = str(params.get("itemId"))
            if item_id in self.commentary:
                self._send_reasoning(("commentary", item_id, 0), str(params.get("delta") or ""))
            elif self.stream_answer:
                self._send("content", str(params.get("delta") or ""))
        elif method == "item/reasoning/summaryTextDelta":
            part = ("summary", str(params.get("itemId")), _int(params.get("summaryIndex")))
            self._send_reasoning(part, str(params.get("delta") or ""))
        elif method == "item/reasoning/textDelta":
            part = ("text", str(params.get("itemId")), _int(params.get("contentIndex")))
            self._send_reasoning(part, str(params.get("delta") or ""))
        elif method == "item/completed" and item.get("type") == "agentMessage":
            if str(item.get("id")) not in self.commentary:
                self.answer = str(item.get("text") or "")
        elif method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage")
            last = usage.get("last") if isinstance(usage, dict) else None
            self.usage = last if isinstance(last, dict) else {}
        elif method == "error" and not params.get("willRetry"):
            error = params.get("error")
            self.errors.append(str(error.get("message") if isinstance(error, dict) else error))
        elif method == "turn/completed":
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            error = turn.get("error")
            if turn.get("status") == "failed" or isinstance(error, dict):
                self.errors.append(str(error.get("message") if isinstance(error, dict) else "codex turn failed"))
            else:
                self.finished = True


def _codex_turn(
    argv: list[str],
    thread: Mapping[str, Any],
    turn: Mapping[str, Any],
    env: Mapping[str, str],
    settings: Settings,
    cwd: Path,
    on_delta: Callable[[str, str], None] | None,
    stream_answer: bool,
) -> CliCompletion:
    """Run one turn on a fresh ``codex app-server`` over stdio JSON-RPC, then stop it.

    The server stops when its stdin closes, so stdin stays open until the turn ends.

    Args:
        argv: The full command.
        thread: The ``thread/start`` parameters.
        turn: The ``turn/start`` parameters, without the thread id.
        env: The CLI's environment, from :func:`cli_isolation.isolated_cli`.
        settings: Application settings (timeout).
        cwd: The directory the CLI runs in.
        on_delta: Called with ``(kind, text)`` for each streamed piece, or ``None``.
        stream_answer: Whether answer pieces go to ``on_delta``.

    Returns:
        The completion.

    Raises:
        SubscriptionRateLimitedError: When the subscription is capped.
        CliRunError: When the call fails.
    """
    process = _start("codex", argv, env, cwd)
    timed_out = threading.Event()

    def expire() -> None:
        """Stop the whole process group at the call timeout."""
        timed_out.set()
        _kill_group(process)

    def send(message: Mapping[str, Any]) -> None:
        """Write one JSON-RPC message; a server that exited only closes the pipe."""
        assert process.stdin is not None
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError, ValueError):
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()

    state = _CodexTurn(on_delta, stream_answer)
    times: dict[str, float] = {}
    other: list[str] = []
    stderr_lines: list[str] = []
    timer = threading.Timer(settings.cli_call_timeout_seconds, expire)
    timer.start()
    reader = threading.Thread(target=lambda: stderr_lines.extend(process.stderr or ()), daemon=True)
    reader.start()
    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "skynet", "version": "1"}}})
        send({"method": "initialized"})
        send({"id": 2, "method": "thread/start", "params": dict(thread)})
        assert process.stdout is not None
        for line in process.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                message = None
            if not isinstance(message, dict):
                if line.strip():
                    other.append(line)
                continue
            method, params = message.get("method"), message.get("params")
            params = params if isinstance(params, dict) else {}
            if "id" in message and method:
                # No approval or tool request applies: the host tools are off.
                send({"id": message["id"], "error": {"code": -32601, "message": "not supported"}})
            elif "id" in message and isinstance(message.get("error"), dict):
                state.errors.append(str(message["error"].get("message") or "codex request failed"))
                break
            elif message.get("id") == 2:
                thread_id = ((message.get("result") or {}).get("thread") or {}).get("id")
                times["started"] = time.monotonic()
                send({"id": 3, "method": "turn/start", "params": {**turn, "threadId": thread_id}})
            elif isinstance(method, str):
                state.take(method, params)
                if method == "turn/completed":
                    times["finished"] = time.monotonic()
                    break
    finally:
        timer.cancel()
        _kill_group(process)
        reader.join(timeout=_KILL_GRACE_SECONDS)
        _close(process)
    if not state.finished:
        if timed_out.is_set():
            raise CliRunError("codex", "codex timed out", transient=False)
        raise _failure("codex", "\n".join(state.errors) or "".join(other) + "".join(stderr_lines))
    usage = state.usage
    return CliCompletion(
        text=state.answer,
        prompt_tokens=_int(usage.get("inputTokens")),
        completion_tokens=_int(usage.get("outputTokens")),
        model_ms=round((times["finished"] - times.get("started", times["finished"])) * 1000),
        raw={"usage": usage},
    )


RUNNERS = {"claude": run_claude, "codex": run_codex}
