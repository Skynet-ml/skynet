"""Serve model calls from the user's own coding-CLI subscriptions in local mode.

A :class:`CliDispatcher` stands where an ``OpenRouterDispatcher`` stands in the
model gateway, so guests keep the OpenAI chat protocol (and native harnesses
the Anthropic messages protocol) while the answer comes from ``claude`` or
``codex`` on the user's machine. The wallet
records every call at zero cents; ``usage.cost`` carries the model's public
list price so cost scores stay comparable with hosted runs.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import random
import re
import secrets
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from .. import config
from ..config import Settings
from ..local.cli_catalog import CliProvider, cli_model_arg
from ..local.cli_probe import read_cli_limits, write_cli_limit
from .cli_runners import CLI_EFFORTS, RUNNERS, CliCompletion, CliRunError, SubscriptionRateLimitedError
from .model_dispatch import ModelHTTPResult
from .openrouter_quotes import fetch_endpoint_prices
from .operation_pricing import ChargePolicy, UnpricedOperationError, operation_quote
from .runtime import BudgetRuntime, PaidResult

__all__ = [
    "NEXT_PROMPT_ROLE",
    "THINKING_LABEL_ROLE",
    "CliDispatcher",
    "SubscriptionRateLimitedError",
    "local_subscription_route",
]

_CHAT_PATH = "/chat/completions"
_MESSAGES_PATH = "/messages"
_UNSUPPORTED_PATHS = frozenset({"/responses", "/embeddings"})
_RETRY_DELAYS_SECONDS = (2.0, 4.0, 8.0)
_MAX_REQUEST_BYTES = 32 * 1024 * 1024
_PRICE_TIMEOUT_SECONDS = 10
# The request field that carries the turn's effort to the CLI. A plain
# ``reasoning_effort`` cannot do it: dspy adds ``"low"`` to a call whose LM has
# no effort, so it would override the CLI's default on turns with no pick.
CLI_EFFORT_KEY = "skynet_cli_effort"
# A thinking label is a side call the turn never waits for. It runs in its
# own one-call pool and never waits there, so labels cannot hold the slots of
# the turn's own CLI calls; its client gives up after 8 s, so a longer run is
# stopped soon after, as nobody will read its answer.
THINKING_LABEL_ROLE = "thinking_label"
_LABEL_CLI_TIMEOUT_SECONDS = 10.0
# A next-message suggestion is a side call too, in its own one-call pool so it
# never blocks a label or a turn. Its prompt is larger than a label's and a CLI
# start alone takes 3-7 s, so it gets a longer limit than a label.
NEXT_PROMPT_ROLE = "next_prompt"
_NEXT_PROMPT_CLI_TIMEOUT_SECONDS = 25.0
# A newer suggestion makes the running one stale, because the user has a new
# reply to answer. The newer call marks the older one replaced, and the older
# run stops at its next streamed piece. The pool holds the current call plus
# one replaced call that has not streamed yet, so a new turn does not wait for
# a stale run's CLI start.
_NEXT_PROMPT_POOL_SIZE = 2
_NEXT_PROMPT_SLOT_WAIT_SECONDS = 5.0
_latest_suggestion_lock = threading.Lock()
_latest_suggestion: dict[str, threading.Event] = {}
# Seams for tests: the back-off sleep and the price lookup.
_sleep: Callable[[float], None] = time.sleep
_prices_lock = threading.Lock()
_last_prices: dict[str, tuple[Decimal, Decimal]] = {}
_semaphores_lock = threading.Lock()
_semaphores: dict[tuple[str, int], threading.BoundedSemaphore] = {}
_TOOL_KEY = '"tool_calls"'
_TOOL_OBJECT = re.compile(r'\{\s*"tool_calls"')
_FENCE_OPEN = re.compile(r"```[\w+-]*")
_FENCE_BEFORE = re.compile(r"```[\w+-]*\s*$")


def _semaphore(cli: str, size: int) -> threading.BoundedSemaphore:
    """Return the process-wide slot pool for one CLI.

    Args:
        cli: The CLI.
        size: Concurrent calls allowed.

    Returns:
        The shared semaphore for ``(cli, size)``.
    """
    with _semaphores_lock:
        return _semaphores.setdefault((cli, size), threading.BoundedSemaphore(size))


def _replace_suggestion(pool: str) -> threading.Event:
    """Mark the running suggestion in ``pool`` replaced and register a new one.

    Args:
        pool: The CLI's suggestion pool key.

    Returns:
        The new call's event, which a later call sets to replace it.
    """
    replaced = threading.Event()
    with _latest_suggestion_lock:
        older = _latest_suggestion.get(pool)
        if older is not None:
            older.set()
        _latest_suggestion[pool] = replaced
    return replaced


def _stop_when_replaced(replaced: threading.Event, provider: str) -> Callable[[str, str], None]:
    """Return a stream callback that stops a suggestion run once a newer one replaces it.

    Args:
        replaced: The run's event (see :func:`_replace_suggestion`).
        provider: The CLI.

    Returns:
        An ``on_delta`` callback. It raises, and the runner then kills the CLI
        process group, because the runners stop on any error in the callback.
    """

    def on_delta(_kind: str, _text: str) -> None:
        """Raise when a newer suggestion replaced this run.

        Args:
            _kind: The streamed piece's kind.
            _text: The streamed piece.

        Raises:
            CliRunError: When the run is replaced.
        """
        if replaced.is_set():
            raise CliRunError(provider, "a newer suggestion replaced this one", transient=False)

    return on_delta


def _list_prices(model: str) -> tuple[Decimal, Decimal] | None:
    """Return the model's public prompt and completion price per token.

    Args:
        model: OpenRouter catalog slug.

    Returns:
        ``(prompt_usd, completion_usd)`` per token, the last value read when the
        catalog is unreachable now, or ``None`` when it was never read.
    """
    try:
        with httpx.Client(timeout=_PRICE_TIMEOUT_SECONDS, trust_env=False) as client:
            data = fetch_endpoint_prices(model, client=client)
        pricing = data["endpoints"][0]["pricing"]
        prices = (Decimal(str(pricing["prompt"])), Decimal(str(pricing["completion"])))
    except Exception:
        with _prices_lock:
            return _last_prices.get(model)
    with _prices_lock:
        _last_prices[model] = prices
    return prices


_step_cwd_lock = threading.Lock()
_step_cwd_path: Path | None = None


def _step_cwd() -> Path:
    """Return one private, empty directory that every agent step call of this process runs in.

    Claude Code and codex put the working directory into the request ahead of
    the prompt, so a new directory per call made each step's request differ
    from the start and the prompt cache never matched.

    Returns:
        The directory, removed when the process exits. A new one replaces it
        when it was deleted, because a missing working directory makes the CLI
        start fail as "not installed" until a restart.
    """
    global _step_cwd_path
    with _step_cwd_lock:
        if _step_cwd_path is None or not _step_cwd_path.is_dir():
            path = tempfile.mkdtemp(prefix="skynet-cli-")
            atexit.register(shutil.rmtree, path, ignore_errors=True)
            _step_cwd_path = Path(path)
        return _step_cwd_path


def _text(content: Any) -> str:
    """Flatten one OpenAI message content into plain text.

    Args:
        content: A string or a list of content parts.

    Returns:
        The text parts joined by newlines.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or "") if isinstance(part, dict) else str(part)
            for part in content
            if not isinstance(part, dict) or part.get("type", "text") == "text"
        )
    return "" if content is None else json.dumps(content)


def _render_turn(message: Mapping[str, Any]) -> str:
    """Render one chat message as a labeled turn.

    Args:
        message: One OpenAI chat message.

    Returns:
        The role heading, then the text and any tool calls as JSON.
    """
    role = str(message.get("role") or "user")
    body = _text(message.get("content"))
    if message.get("tool_calls"):
        body = f"{body}\n{json.dumps(message['tool_calls'])}".strip()
    return f"### {role.capitalize()}\n{body}"


def cache_prefix(request: Mapping[str, Any]) -> str:
    """Return the start of :func:`render_prompt`'s prompt that agent steps repeat byte for byte.

    An agent's system message holds its instructions and tool list, and stays
    the same from step to step; the turns after it change.

    Args:
        request: The chat completion body.

    Returns:
        The rendered leading system turn, or ``""`` when the chat has none.
    """
    messages = [m for m in request.get("messages") or [] if isinstance(m, dict)]
    if len(messages) < 2 or messages[0].get("role") != "system":
        return ""
    return _render_turn(messages[0])


def render_prompt(request: Mapping[str, Any]) -> str:
    """Turn an OpenAI chat request into one prompt for a CLI.

    Args:
        request: The chat completion body.

    Returns:
        The prompt. One user message is sent as is; a longer chat is sent as
        labeled turns, with any tools listed as JSON.
    """
    messages = [m for m in request.get("messages") or [] if isinstance(m, dict)]
    tools = request.get("tools")
    if len(messages) == 1 and messages[0].get("role") == "user" and not tools:
        return _text(messages[0].get("content"))
    parts = [_render_turn(message) for message in messages]
    if tools:
        parts.append(
            "### Tools\nThese tools are described for reference; reply in text and "
            f"describe any tool call as JSON.\n{json.dumps(tools)}"
        )
    return "\n\n".join(parts)


def _anthropic_text(content: Any) -> str:
    """Flatten one Anthropic message content into plain text.

    Tool calls and tool results become labeled JSON, since a CLI answers in text.

    Args:
        content: A string or a list of content blocks.

    Returns:
        The blocks joined by newlines.
    """
    if not isinstance(content, list):
        return _text(content)
    parts = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
        elif block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        elif block.get("type") == "tool_use":
            call = {"name": block.get("name"), "input": block.get("input")}
            parts.append(f"Tool call: {json.dumps(call)}")
        elif block.get("type") == "tool_result":
            parts.append(f"Tool result: {_anthropic_text(block.get('content'))}")
    return "\n".join(parts)


def chat_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """Turn an Anthropic messages request into the chat request a CLI answers.

    Args:
        request: The ``/messages`` body.

    Returns:
        A chat completion body with the same model, turns and tools.
    """
    messages: list[dict[str, Any]] = []
    system = request.get("system")
    if system:
        messages.append({"role": "system", "content": _anthropic_text(system)})
    messages.extend(
        {"role": message.get("role") or "user", "content": _anthropic_text(message.get("content"))}
        for message in request.get("messages") or []
        if isinstance(message, dict)
    )
    chat: dict[str, Any] = {"model": request.get("model"), "messages": messages}
    if request.get("tools"):
        chat["tools"] = request["tools"]
    return chat


def _schema(request: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the JSON schema a ``json_schema`` response format asks for.

    Args:
        request: The chat completion body.

    Returns:
        The schema, or ``None`` when the request asks for free text.
    """
    response_format = request.get("response_format")
    if not isinstance(response_format, dict) or response_format.get("type") != "json_schema":
        return None
    wrapper = response_format.get("json_schema")
    schema = wrapper.get("schema") if isinstance(wrapper, dict) else None
    return schema if isinstance(schema, dict) else None


def _tool_call(item: Any, names: set[str]) -> dict[str, Any] | None:
    """Turn one tool call a CLI wrote as JSON into an OpenAI tool call.

    Args:
        item: One entry of the written ``tool_calls`` list, either
            ``{"name", "args"}`` (DSPy's text form) or the OpenAI
            ``{"function": {"name", "arguments"}}`` form.
        names: The tools the request declares.

    Returns:
        The OpenAI tool call, or ``None`` when the entry names no declared
        tool or its arguments are not a JSON object.
    """
    if not isinstance(item, dict):
        return None
    call = item.get("function") if isinstance(item.get("function"), dict) else item
    name = call.get("name")
    arguments = call.get("arguments", call.get("args", {}))
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except ValueError:
            return None
    if name not in names or not isinstance(arguments, dict):
        return None
    return {
        "id": f"call_{uuid4().hex}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _text_tool_calls(request: Mapping[str, Any], text: str) -> tuple[list[dict[str, Any]], int] | None:
    """Return the tool calls a CLI wrote as JSON text, as native OpenAI tool calls.

    A CLI cannot call the guest's tools, so the prompt asks it to write any
    call as JSON. DSPy's native function calling reads only the message's
    ``tool_calls``, so a call left in the text fails to parse; after three
    such failures ReAct forces a ``submit`` call. The JSON may sit in a code
    fence or among other text, such as DSPy's ``[[ ## completed ## ]]``.
    Only an object whose first key is ``"tool_calls"`` counts, so the live
    stream can tell from a short prefix which text it must hold back.

    Args:
        request: The chat completion body; only its ``tools`` matter.
        text: The CLI's answer.

    Returns:
        The tool calls and the index where their block (its code fence, if
        any) starts, or ``None`` when the request declares no tools or the
        text holds no ``{"tool_calls": [...]}`` object whose every call names
        a declared tool.
    """
    names = {
        str(tool["function"].get("name"))
        for tool in request.get("tools") or []
        if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
    }
    if not names:
        return None
    decoder = json.JSONDecoder()
    for match in _TOOL_OBJECT.finditer(text):
        try:
            value, _ = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get("tool_calls"), list) and value["tool_calls"]:
            calls = [_tool_call(item, names) for item in value["tool_calls"]]
            if all(calls):
                fence = _FENCE_BEFORE.search(text, 0, match.start())
                return calls, fence.start() if fence else match.start()  # type: ignore[return-value]
    return None


def _held_from(text: str, start: int) -> int:
    """Return where the text that may still grow into a written tool call begins.

    The live stream sends answer text as it comes, but never the text of a
    tool call, which goes out as ``tool_calls`` at the end. So it holds back
    a code fence or ``{`` that :func:`_text_tool_calls` could still accept,
    and sends it once the next characters rule that out.

    Args:
        text: The answer text so far.
        start: How much of it the client already has.

    Returns:
        The index from which text must wait, or ``len(text)`` to send all of it.
    """
    for index in range(start, len(text)):
        if text[index] not in "`{":
            continue
        rest = text[index:]
        if rest[0] == "`":
            if "```".startswith(rest):
                return index
            fence = _FENCE_OPEN.match(rest)
            if fence is None:
                continue
            rest = rest[fence.end() :].lstrip()
            if not rest:
                return index
        if rest[0] == "{":
            key = rest[1:].lstrip()
            if key.startswith(_TOOL_KEY) or _TOOL_KEY.startswith(key):
                return index
    return len(text)


def _error(status: int, code: str, message: str, **fields: Any) -> ModelHTTPResult:
    """Build a JSON error answer in the gateway's error shape.

    Args:
        status: HTTP status.
        code: Stable error code.
        message: Human-readable reason.
        **fields: More fields for the error object.

    Returns:
        The answer.
    """
    error = {"type": code, "code": code, "message": message, **fields}
    return ModelHTTPResult(status, "application/json", json.dumps({"error": error}).encode())


def _future_reset(settings: Settings, cli: str) -> datetime | None:
    """Return when a recorded cap on ``cli`` ends, if it is still in force.

    Args:
        settings: Application settings (the limits file location).
        cli: The CLI.

    Returns:
        The reset time, or ``None`` when no cap is in force.
    """
    try:
        stamp = (read_cli_limits(settings).get(cli) or {}).get("reset_at")
        reset = datetime.fromisoformat(str(stamp)) if stamp else None
    except (OSError, ValueError):
        return None
    if reset is None:
        return None
    reset = reset if reset.tzinfo else reset.replace(tzinfo=UTC)
    return reset if reset > datetime.now(UTC) else None


class _ChatStream:
    """Write a streamed chat completion's chunks to the client as the CLI produces them.

    Nothing reaches the client before the first piece of thinking or answer
    text, so a call that fails before then still answers with its own status.
    Answer text that may be a written tool call waits; see :func:`_held_from`.
    """

    def __init__(self, handler: BaseHTTPRequestHandler, model: str, *, holds_tool_calls: bool = True) -> None:
        """Bind the stream to one HTTP request.

        Args:
            handler: The request's handler, whose ``wfile`` gets the chunks.
            model: The model name the chunks carry.
            holds_tool_calls: Hold back text that may be a written tool call.
                Only a request that declares tools gets that text as native
                ``tool_calls``; for any other request the text stays answer
                text, and holding it would only delay it.
        """
        self.handler = handler
        self.holds_tool_calls = holds_tool_calls
        self.base = {
            "id": f"cli-{uuid4()}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
        }
        self.started = False
        self.content = ""
        self.sent = ""

    def __call__(self, kind: str, text: str) -> None:
        """Send one piece of thinking (``reasoning``) or answer (``content``) text.

        Args:
            kind: ``"reasoning"`` or ``"content"``.
            text: The new text.
        """
        if kind == "content":
            self.content += text
            # DSPy's text tool protocol (subscription routes, SKY-245) declares
            # no tools, so its streamed tool call and submit reply pass live.
            held = _held_from(self.content, len(self.sent)) if self.holds_tool_calls else len(self.content)
            ready = self.content[:held]
            # Blank text before a tool call would leave a blank content field.
            if not ready.strip():
                return
            text = ready[len(self.sent) :]
            self.sent = ready
            if not text:
                return
        field_name = "content" if kind == "content" else "reasoning_content"
        chunk = {**self.base, "choices": [{"index": 0, "delta": {field_name: text}, "finish_reason": None}]}
        self.write(f"data: {json.dumps(chunk)}\n\n".encode())

    def write(self, data: bytes) -> None:
        """Send bytes, sending the 200 event-stream headers first if they are not sent yet.

        Args:
            data: Raw SSE bytes.
        """
        if not self.started:
            self.started = True
            self.handler.send_response(200)
            self.handler.send_header("Content-Type", "text/event-stream")
            self.handler.send_header("Cache-Control", "no-cache")
            self.handler.end_headers()
        self.handler.wfile.write(data)
        self.handler.wfile.flush()


class CliDispatcher:
    """Answer one model role's chat calls from a local CLI subscription."""

    embeddings = False

    def __init__(
        self, runtime: BudgetRuntime | None, cli: CliProvider, model: str, role: str, settings: Settings
    ) -> None:
        """Bind one model role to a CLI.

        Args:
            runtime: The spending authority that records each call at zero
                cents, or ``None`` for API-side agents, which meter their own usage.
            cli: The CLI that serves this role first.
            model: OpenRouter catalog slug the guest names.
            role: Task, judge, optimization or agent attribution.
            settings: Application settings.

        Raises:
            UnpricedOperationError: Outside local mode, or when ``cli`` cannot run ``model``.
        """
        if not settings.local_mode:
            raise UnpricedOperationError("Subscription models run only in local mode.")
        try:
            cli_model_arg(cli, model)
        except ValueError as error:
            raise UnpricedOperationError(str(error)) from error
        self.runtime = runtime
        self.cli = cli
        self.model = model
        self.role = role
        self.settings = settings
        self.policy = ChargePolicy("subscription_model")

    def dispatch(
        self,
        path: str,
        request: Mapping[str, Any],
        operation_key: str | None = None,
        attempt: int = 0,
        protocol_headers: Mapping[str, str] | None = None,
        stream: _ChatStream | None = None,
    ) -> ModelHTTPResult:
        """Answer one chat completion or Anthropic message and record it at zero cents.

        Args:
            path: The protocol path: ``/chat/completions`` or ``/messages``.
            request: The request body.
            operation_key: Stable identity of this physical attempt, if any.
            attempt: Retry number within the logical operation.
            protocol_headers: Unused; kept for the dispatcher interface.
            stream: For a streamed chat completion, the live stream that gets
                the CLI's thinking and answer text as they arrive, or ``None``.

        Returns:
            The answer in the request's protocol, a 429 for a capped
            subscription, a 502 for a failed CLI, or a 422 for a path a CLI
            cannot serve.

        Raises:
            UnpricedOperationError: When the request names another model or path.
        """
        del protocol_headers
        if path in _UNSUPPORTED_PATHS:
            return _error(
                422, "subscription.unsupported_path", "Subscription models serve only chat completions and messages."
            )
        if path not in (_CHAT_PATH, _MESSAGES_PATH) or request.get("model") != self.model:
            raise UnpricedOperationError("This scoped route cannot dispatch a different model or API operation.")
        if self.runtime is None:
            return self._answer(path, request, stream)[0]
        quote = operation_quote(
            request, Decimal(0), self.policy, {"provider": "subscription", "cli": self.cli, "model": self.model}
        )

        def send() -> PaidResult[ModelHTTPResult]:
            """Run the CLI and report a zero charge with its usage as evidence."""
            result, evidence = self._answer(path, request, stream)
            return PaidResult(value=result, provider_usd=Decimal(0), evidence=evidence)

        return self.runtime.execute(
            quote,
            self.policy,
            send,
            operation_key=operation_key or str(uuid4()),
            cost_kind="model",
            role=self.role,
            attempt=attempt,
        )

    def stop_batch(self) -> None:
        """Do nothing: subscription calls never wait on a batch."""

    def reconcile(self, generation_id: str | None = None) -> None:
        """Do nothing: a subscription call never leaves a bill to read later.

        Args:
            generation_id: Unused; kept for the dispatcher interface.
        """
        del generation_id

    def _providers(self) -> list[str]:
        """Return this role's CLI, then the configured fallbacks, without repeats."""
        fallback = [name.strip() for name in self.settings.cli_fallback_order.split(",") if name.strip()]
        order = [self.cli]
        order += [name for name in fallback if name in RUNNERS and name not in order]
        return order

    def _answer(
        self, path: str, request: Mapping[str, Any], stream: _ChatStream | None = None
    ) -> tuple[ModelHTTPResult, dict[str, Any]]:
        """Run the request on the first CLI that is not capped.

        Args:
            path: ``/chat/completions`` or ``/messages``.
            request: The request body.
            stream: The live chat stream, or ``None``.

        Returns:
            The answer and the evidence to record with it.
        """
        chat = chat_request(request) if path == _MESSAGES_PATH else request
        prompt = render_prompt(chat)
        prefix = cache_prefix(chat)
        schema = _schema(chat)
        effort = request.get(CLI_EFFORT_KEY)
        capped: SubscriptionRateLimitedError | None = None
        evidence: dict[str, Any] = {"provider": "subscription", "model": self.model, "cli": self.cli}
        for provider in self._providers():
            if stream is not None and stream.started:
                # The client already has one CLI's text; another CLI's would mix into it.
                break
            try:
                model_arg = cli_model_arg(provider, self.model)  # type: ignore[arg-type]
            except ValueError:
                continue
            reset = _future_reset(self.settings, provider)
            if reset is not None:
                capped = SubscriptionRateLimitedError(
                    provider, reset, f"{provider} is capped until {reset.isoformat()}"
                )
                continue
            started = time.monotonic()
            try:
                completion = self._run(provider, model_arg, prompt, schema, stream, prefix, effort)
            except SubscriptionRateLimitedError as error:
                capped = error
                if error.reset_at is not None:
                    # Without a state directory the cap still answers 429; only the UI hint is lost.
                    with contextlib.suppress(OSError, ValueError):
                        write_cli_limit(self.settings, provider, error.reset_at, error.message)  # type: ignore[arg-type]
                continue
            except CliRunError as error:
                evidence.update(cli=provider, status=502, failure=error.message)
                return _error(502, "subscription.cli_failed", error.message, provider=provider), evidence
            usage = self._usage(provider, completion)
            evidence.update(
                cli=provider,
                status=200,
                usage=usage,
                latency_ms=completion.model_ms
                if completion.model_ms is not None
                else round((time.monotonic() - started) * 1000),
            )
            if path == _MESSAGES_PATH:
                return self._message(request, completion.text, usage), evidence
            return self._success(request, completion.text, usage, stream), evidence
        assert capped is not None or not self._providers() or (stream is not None and stream.started)
        provider = capped.provider if capped else self.cli
        reset_at = capped.reset_at.isoformat() if capped and capped.reset_at else None
        message = capped.message if capped else "No subscription CLI can serve this model."
        evidence.update(cli=provider, status=429, failure=message)
        return (
            _error(429, "subscription.rate_limited", message, provider=provider, reset_at=reset_at),
            evidence,
        )

    def _run(
        self,
        provider: str,
        model_arg: str,
        prompt: str,
        schema: dict[str, Any] | None,
        stream: _ChatStream | None = None,
        prefix: str = "",
        effort: Any = None,
    ) -> CliCompletion:
        """Run one CLI call, retrying transient failures with jittered back-off.

        Every retry is free under a subscription, so retrying inside one
        recorded operation cannot double-charge the wallet. A timeout is not
        transient, so one hung call holds a slot for one timeout only. A call
        that already streamed text to the client is not retried, as the
        retry's text would repeat it.

        Args:
            provider: The CLI.
            model_arg: Its ``--model`` value.
            prompt: The full prompt.
            schema: A JSON schema for the answer, or ``None``.
            stream: The live chat stream, or ``None``.
            prefix: The start of ``prompt`` that later calls repeat, or ``""``.
            effort: The turn's effort level from the request, or ``None``. A
                side call ignores it, and a level the CLI cannot take is not sent.

        Returns:
            The completion.

        Raises:
            SubscriptionRateLimitedError: When the subscription is capped (never retried).
            CliRunError: When the call still fails after the last retry.
        """
        runner = RUNNERS[provider]
        if self.role in (THINKING_LABEL_ROLE, NEXT_PROMPT_ROLE):
            return self._run_label(provider, model_arg, prompt, schema)
        slots = _semaphore(provider, self.settings.cli_max_concurrency)
        # Only claude takes a cache mark from the caller.
        runner_kwargs: dict[str, Any] = {"cache_prefix": prefix} if provider == "claude" and prefix else {}
        if isinstance(effort, str) and effort in CLI_EFFORTS:
            runner_kwargs["effort"] = effort
        for retry in range(len(_RETRY_DELAYS_SECONDS) + 1):
            if stream is not None and not stream.started:
                # Held text of a failed attempt must not prefix the next one's.
                stream.content = stream.sent = ""
            try:
                with slots:
                    return runner(model_arg, prompt, schema, self.settings, _step_cwd(), stream, **runner_kwargs)
            except CliRunError as error:
                streamed = stream is not None and stream.started
                if not error.transient or streamed or retry == len(_RETRY_DELAYS_SECONDS):
                    raise
                _sleep(_RETRY_DELAYS_SECONDS[retry] * random.uniform(0.75, 1.25))
        raise AssertionError("unreachable")

    def _run_label(self, provider: str, model_arg: str, prompt: str, schema: dict[str, Any] | None) -> CliCompletion:
        """Run one side call (label or suggestion) in its role's pool, or fail at once when it is busy.

        Args:
            provider: The CLI.
            model_arg: Its ``--model`` value.
            prompt: The full prompt.
            schema: A JSON schema for the answer, or ``None``.

        Returns:
            The completion.

        Raises:
            SubscriptionRateLimitedError: When the subscription is capped.
            CliRunError: When another call of this role runs on this CLI, or the call fails or times out.
        """
        pool = f"{provider}:{self.role}"
        if self.role != NEXT_PROMPT_ROLE:
            slot = _semaphore(pool, 1)
            if not slot.acquire(blocking=False):
                raise CliRunError(provider, f"{provider} is already running a {self.role} call", transient=False)
            try:
                return self._run_side_call(provider, model_arg, prompt, schema, _LABEL_CLI_TIMEOUT_SECONDS, {})
            finally:
                slot.release()
        replaced = _replace_suggestion(pool)
        slot = _semaphore(pool, _NEXT_PROMPT_POOL_SIZE)
        try:
            if not slot.acquire(timeout=_NEXT_PROMPT_SLOT_WAIT_SECONDS):
                raise CliRunError(provider, f"{provider} is still running older suggestions", transient=False)
            try:
                if replaced.is_set():
                    raise CliRunError(provider, "a newer suggestion replaced this one", transient=False)
                stop = {"on_delta": _stop_when_replaced(replaced, provider)}
                return self._run_side_call(provider, model_arg, prompt, schema, _NEXT_PROMPT_CLI_TIMEOUT_SECONDS, stop)
            finally:
                slot.release()
        finally:
            with _latest_suggestion_lock:
                if _latest_suggestion.get(pool) is replaced:
                    del _latest_suggestion[pool]

    def _run_side_call(
        self,
        provider: str,
        model_arg: str,
        prompt: str,
        schema: dict[str, Any] | None,
        limit: float,
        runner_kwargs: dict[str, Any],
    ) -> CliCompletion:
        """Run one side call with thinking off and a short time limit.

        Args:
            provider: The CLI.
            model_arg: Its ``--model`` value.
            prompt: The full prompt.
            schema: A JSON schema for the answer, or ``None``.
            limit: The most seconds the run may take.
            runner_kwargs: Extra keyword arguments for the runner.

        Returns:
            The completion.
        """
        timeout = min(self.settings.cli_call_timeout_seconds, limit)
        label_settings = self.settings.model_copy(update={"cli_call_timeout_seconds": timeout})
        # A side call is a line or two; thinking would outlast its limit.
        no_thinking = {"think": False} if provider == "claude" else {}
        with tempfile.TemporaryDirectory(prefix="skynet-cli-") as cwd:
            return RUNNERS[provider](
                model_arg, prompt, schema, label_settings, Path(cwd), **no_thinking, **runner_kwargs
            )

    def _usage(self, provider: str, completion: CliCompletion) -> dict[str, Any]:
        """Build the usage block, priced at the model's public list price.

        Args:
            provider: The CLI that served the call.
            completion: Its answer.

        Returns:
            The OpenAI-shaped usage block with the subscription fields.
        """
        prices = _list_prices(self.model)
        cost = (
            float(prices[0] * completion.prompt_tokens + prices[1] * completion.completion_tokens)
            if prices is not None
            else None
        )
        return {
            "prompt_tokens": completion.prompt_tokens,
            "completion_tokens": completion.completion_tokens,
            "total_tokens": completion.prompt_tokens + completion.completion_tokens,
            "cost": cost,
            "is_byok": False,
            "cost_details": {"upstream_inference_cost": 0},
            "model_ms": completion.model_ms,
            "token_source": "subscription",
            "cli_provider": provider,
        }

    def _success(
        self, request: Mapping[str, Any], text: str, usage: dict[str, Any], stream: _ChatStream | None = None
    ) -> ModelHTTPResult:
        """Build the chat completion answer, as JSON or as SSE.

        Args:
            request: The chat completion body.
            text: The CLI's answer.
            usage: The usage block.
            stream: The live chat stream; when it already sent text, the SSE
                carries only the rest of the answer, in the same stream identity.

        Returns:
            The answer. Tool calls the CLI wrote as JSON text come back as
            native ``tool_calls`` with ``finish_reason`` ``"tool_calls"``; the
            text before their block stays the content, as the live stream
            already sent it, and the block and any text after it are dropped.
        """
        identity = f"cli-{uuid4()}"
        written = _text_tool_calls(request, text)
        tool_calls = written[0] if written else None
        content = text[: written[1]] if written else text
        if tool_calls and not content.strip():
            content = ""
        finish_reason = "tool_calls" if tool_calls else "stop"
        if not request.get("stream"):
            message: dict[str, Any] = {"role": "assistant", "content": content}
            if tool_calls:
                message = {"role": "assistant", "content": content or None, "tool_calls": tool_calls}
            body = {
                "id": identity,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": self.model,
                "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
                "usage": usage,
            }
            return ModelHTTPResult(200, "application/json", json.dumps(body).encode())
        base = {"id": identity, "object": "chat.completion.chunk", "created": int(time.time()), "model": self.model}
        rest = content
        if stream is not None and stream.started:
            base = stream.base
            # The streamed deltas are the answer's start; a final text that
            # differs from them cannot be fixed in a stream, so it is dropped.
            rest = content[len(stream.sent) :] if content.startswith(stream.sent) else ""
        delta: dict[str, Any] = {"role": "assistant", "content": rest}
        if tool_calls:
            delta = {"role": "assistant", **({"content": rest} if rest else {})}
            delta["tool_calls"] = [{"index": i, **call} for i, call in enumerate(tool_calls)]
        chunks = [
            {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}], "usage": usage},
        ]
        stream = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return ModelHTTPResult(200, "text/event-stream", stream.encode())

    def _message(self, request: Mapping[str, Any], text: str, usage: dict[str, Any]) -> ModelHTTPResult:
        """Build the Anthropic message answer, as JSON or as SSE events.

        Args:
            request: The ``/messages`` body.
            text: The CLI's answer.
            usage: The chat usage block.

        Returns:
            The answer.
        """
        extra = {key: usage[key] for key in ("cost", "model_ms", "token_source", "cli_provider")}
        tokens = {"input_tokens": usage["prompt_tokens"], "output_tokens": usage["completion_tokens"]}
        message = {
            "id": f"msg_cli_{uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "model": self.model,
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {**tokens, **extra},
        }
        if not request.get("stream"):
            return ModelHTTPResult(200, "application/json", json.dumps(message).encode())
        start = {
            **message,
            "content": [],
            "stop_reason": None,
            "usage": {"input_tokens": tokens["input_tokens"], "output_tokens": 0},
        }
        events = [
            ("message_start", {"type": "message_start", "message": start}),
            (
                "content_block_start",
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            ),
            (
                "content_block_delta",
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {**tokens, **extra},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
        stream = "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)
        return ModelHTTPResult(200, "text/event-stream", stream.encode())


class _LocalSubscriptionGateway:
    """A loopback endpoint that serves API-side agents from CLI subscriptions."""

    def __init__(self, settings: Settings) -> None:
        """Start the endpoint on a free loopback port.

        Args:
            settings: Application settings.
        """
        self.settings = settings
        self.routes: dict[str, CliDispatcher] = {}
        self.tokens: dict[tuple[str, str, str], str] = {}
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            """Serve chat completions for registered tokens only."""

            def log_message(self, format: str, *args: Any) -> None:
                """Keep prompts and tokens out of the logs."""

            def _reply(self, response: ModelHTTPResult) -> None:
                """Write one answer."""
                self.send_response(response.status)
                self.send_header("Content-Type", response.content_type)
                self.send_header("Content-Length", str(len(response.body)))
                self.end_headers()
                self.wfile.write(response.body)

            def do_POST(self) -> None:
                """Answer one chat completion for a known token."""
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                route = gateway.routes.get(token)
                if route is None:
                    self._reply(_error(401, "unauthorized", "Unknown scoped model route."))
                    return
                stream: _ChatStream | None = None
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= _MAX_REQUEST_BYTES:
                        self._reply(_error(413, "request_too_large", "The model request exceeds the supported size."))
                        return
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        raise TypeError("A JSON object is required.")
                    path = self.path.removeprefix("/v1")
                    stream = (
                        _ChatStream(self, route.model, holds_tool_calls=bool(body.get("tools")))
                        if path == _CHAT_PATH and body.get("stream")
                        else None
                    )
                    response = route.dispatch(path, body, stream=stream)
                except (UnpricedOperationError, ValueError, TypeError) as error:
                    response = _error(422, "unpriced_operation", str(error))
                if stream is None or not stream.started:
                    self._reply(response)
                elif response.status == 200:
                    stream.write(response.body)
                else:
                    # The 200 status is already sent; an error chunk makes the client raise.
                    stream.write(b"data: " + response.body + b"\n\n")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True, name="local-subscription-gateway").start()

    @property
    def url(self) -> str:
        """Return the loopback base URL."""
        return f"http://127.0.0.1:{self._server.server_port}/v1"

    def route(self, model: str, cli: CliProvider, role: str) -> dict[str, str]:
        """Return the route for one model role, registering it on first use.

        Args:
            model: OpenRouter catalog slug.
            cli: The CLI that serves it first.
            role: Usage attribution.

        Returns:
            ``{url, token, model, role}``.
        """
        key = (model, cli, role)
        with _gateway_lock:
            token = self.tokens.get(key)
            if token is None:
                dispatcher = CliDispatcher(None, cli, model, role, self.settings)
                token = secrets.token_urlsafe(32)
                self.routes[token] = dispatcher
                self.tokens[key] = token
        return {"url": self.url, "token": token, "model": model, "role": role}


_gateway_lock = threading.RLock()
_gateway: _LocalSubscriptionGateway | None = None


def local_subscription_route(model: str, cli: CliProvider, role: str) -> dict[str, str]:
    """Return a loopback route that answers ``model`` from a CLI subscription.

    API-side agents have no run gateway; they share this process-wide one.

    Args:
        model: OpenRouter catalog slug.
        cli: The CLI that serves it first.
        role: Usage attribution.

    Returns:
        ``{url, token, model, role}`` for an OpenAI-compatible client.

    Raises:
        UnpricedOperationError: Outside local mode, or when ``cli`` cannot run ``model``.
    """
    global _gateway
    current = config.settings
    if not current.local_mode:
        raise UnpricedOperationError("Subscription models run only in local mode.")
    with _gateway_lock:
        if _gateway is None:
            _gateway = _LocalSubscriptionGateway(current)
        gateway = _gateway
    return gateway.route(model, cli, role)
