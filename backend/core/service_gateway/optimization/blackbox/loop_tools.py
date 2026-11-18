"""Share the tool-loop parts of the agent engines that run in the worker engine loop.

Meta-Harness (SKY-722) and AutoResearch (SKY-725) run their proposer sessions as
tool loops on the engine context. Both use the budget view and the proposer
refusal here. AutoResearch also uses the bash tool call, model retry and output
cut here; Meta-Harness keeps its own, which bound the prompt in UTF-8 bytes.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from ....billing.model_gateway import MCP_RELAY_ROUTE_KEY
from ....exceptions import ServiceError
from ....models.blackbox import BLACKBOX_HARNESS_CODEX
from ....sandbox.trees import BoxFailureError, TreeRejectedError
from ....worker.engine_loop import ModelCallFailedError
from .native_runtime import NativeOptions
from .protocol import EngineContext, EvalServer, ModelReply

# Each command output goes into every later prompt of the session, so a fixed cut keeps the prompts, and the
# prompt hashes that a replay checks, bounded.
OUTPUT_CHARS = 30_000
# Model output is untrusted, and a committed reply replays on every resume, so a crash on its arguments could
# never recover. These bounds make an over-long or deeply nested argument an error result: deep JSON makes the
# parser, ``json.dumps`` and the next prompt hash raise RecursionError.
MAX_ARGUMENT_CHARS = 200_000
MAX_ARGUMENT_DEPTH = 32
_REFUSED_ARGUMENTS = "{}"
# A failed model step replays as the same failure, so the tool loop retries at fixed places: a resume makes
# the same calls in the same order.
MODEL_ATTEMPTS = 3
# A rate limit (429) often clears in seconds, so a wait between attempts keeps a short burst from failing all of them.
MODEL_RETRY_WAIT_SECONDS = 10.0
# Every turn sends the whole transcript again, and each command can add up to two clipped outputs. This cap, and
# the proposer's ``max_tool_calls``, keep the prompt inside the model's context. The prompt length depends only on
# the committed replies and results, so a resume stops a session at the same place.
MAX_PROMPT_CHARS = 400_000

# Tests replace this, so a retry test does not wait.
_sleep = time.sleep


class Budget:
    """The ``budget`` view of the gepa.oa server, read from the host eval server."""

    def __init__(self, server: EvalServer) -> None:
        """Bind the view to the host eval server.

        Args:
            server: The host eval server.
        """
        self._server = server

    @property
    def remaining(self) -> int:
        """Return the scorer calls left."""
        return self._server.remaining

    @property
    def used(self) -> int:
        """Return the scorer calls spent."""
        return self._server.used


def _parse(call: Mapping[str, Any]) -> tuple[Any, bool]:
    """Read a tool call's arguments, sent as a mapping or as JSON text, inside the length and depth bounds.

    Args:
        call: The tool call.

    Returns:
        The parsed value, or ``None`` when the text is not JSON, and whether the arguments break a bound.
    """
    value = call.get("arguments")
    if isinstance(value, str):
        if len(value) > MAX_ARGUMENT_CHARS:
            return None, True
        try:
            value = json.loads(value)
        # JSON nested past the parser's recursion limit raises RecursionError.
        except (ValueError, RecursionError):
            value = None
    if _too_deep(value):
        return None, True
    # The protocol gives valid JSON object arguments as a mapping, so the text bound above never sees them.
    if isinstance(value, dict) and len(json.dumps(value)) > MAX_ARGUMENT_CHARS:
        return None, True
    return value, False


def arguments(call: Mapping[str, Any]) -> dict[str, Any] | None:
    """Read a tool call's arguments, sent as a mapping or as JSON text.

    Args:
        call: The tool call.

    Returns:
        The arguments, or ``None`` when they are not a JSON object or break the length or depth bound.
    """
    value, refused = _parse(call)
    return value if not refused and isinstance(value, dict) else None


def openai_call(call: Mapping[str, Any]) -> dict[str, Any]:
    """Give one flat tool call of the engine loop in the OpenAI form that the next prompt's history holds.

    The AutoSaddler port (SKY-723) sends the same form, so every port sends one transcript shape.

    Args:
        call: The flat ``{"id", "name", "arguments"}`` call.

    Returns:
        ``{"id", "type": "function", "function": {"name", "arguments"}}``, with the arguments as JSON text.
    """
    value = call.get("arguments")
    if not isinstance(value, str):
        # Sorted keys give the same text for a live reply and for its replay from the step log. ``run_bash`` refuses
        # arguments past the depth or length bound, so the transcript holds an empty object in their place.
        if _too_deep(value):
            value = _REFUSED_ARGUMENTS
        else:
            value = json.dumps(value, sort_keys=True)
            if len(value) > MAX_ARGUMENT_CHARS:
                value = _REFUSED_ARGUMENTS
    return {
        "id": call.get("id"),
        "type": "function",
        "function": {"name": call.get("name"), "arguments": value},
    }


def _too_deep(value: Any) -> bool:
    """Tell whether a JSON value nests deeper than ``MAX_ARGUMENT_DEPTH``, with no recursion.

    Args:
        value: The parsed JSON value.

    Returns:
        ``True`` when a list or object sits deeper than the bound.
    """
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            item = list(item.values())
        if not isinstance(item, list):
            continue
        if depth > MAX_ARGUMENT_DEPTH:
            return True
        stack.extend((child, depth + 1) for child in item)
    return False


def clip(text: str) -> str:
    """Cut a tool output to ``OUTPUT_CHARS``: keep its head and tail, with a note on what was cut.

    Args:
        text: The output.

    Returns:
        The output unchanged when it fits, else its head, a note and its tail.
    """
    if len(text) <= OUTPUT_CHARS:
        return text
    half = OUTPUT_CHARS // 2
    return f"{text[:half]}\n[... {len(text) - 2 * half} characters cut ...]\n{text[-half:]}"


def call_model(
    ctx: EngineContext, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> tuple[ModelReply | ModelCallFailedError, Decimal]:
    """Call the model with the tools, and try a failed call again up to ``MODEL_ATTEMPTS`` times.

    Each attempt is one ``model`` step, so a resume replays the same failures and the same retries. A live
    failure waits ``MODEL_RETRY_WAIT_SECONDS`` before the next attempt; a replayed failure made no call, so it
    does not wait. A failed attempt that the provider billed carries its committed cost, live and on a replay,
    so the proposer cap counts it the same way both times.

    Args:
        ctx: The engine context, with its model call.
        messages: The session's messages.
        tools: The tool definitions.

    Returns:
        The reply, or the last :class:`ModelCallFailedError` when every attempt failed, and the summed cost of
        the failed attempts, in cents.
    """
    assert ctx.call_model is not None
    failure = ModelCallFailedError("unknown")
    failed_cents = Decimal(0)
    for attempt in range(MODEL_ATTEMPTS):
        try:
            return ctx.call_model(messages, tools), failed_cents
        except ModelCallFailedError as error:
            failure = error
            failed_cents += error.cost_cents
        # The loop raises a live failure from the model's own error, and a replayed failure with no cause.
        if failure.__cause__ is not None and attempt < MODEL_ATTEMPTS - 1:
            _sleep(MODEL_RETRY_WAIT_SECONDS)
    return failure, failed_cents


def run_bash(ctx: EngineContext, tree: str, call: Mapping[str, Any], *, misuse: str) -> tuple[str, str]:
    """Run one bash call as one command over the tree, modeled on the Sandbox SDK ``exec``.

    Args:
        ctx: The engine context, with its command call.
        tree: The workspace tree.
        call: The tool call: its ``name`` and its ``arguments``, as a mapping or JSON text.
        misuse: The error text for a call that is not a bash call with a command string.

    Returns:
        The tool result as JSON text, and the tree after the command.
    """
    assert ctx.run_command is not None
    parsed, refused = _parse(call)
    if refused:
        error = (
            f"Send the bash tool arguments as JSON of at most {MAX_ARGUMENT_CHARS} characters, "
            f"nested at most {MAX_ARGUMENT_DEPTH} levels."
        )
        return json.dumps({"error": error}), tree
    command = parsed.get("command") if isinstance(parsed, dict) else None
    if call.get("name") != "bash" or not isinstance(command, str):
        # A bad tool call goes back to the model as an error result, as in the Agents SDK tool loop.
        return json.dumps({"error": misuse}), tree
    try:
        result, tree = ctx.run_command(tree, command, ())
    except (TreeRejectedError, BoxFailureError) as error:
        # The step committed the failure, so a resume raises it again and gets the same result here.
        return json.dumps({"error": f"The command failed and changed nothing: {error}"}), tree
    output = {
        "exit_code": result.exit_code,
        "stdout": clip(result.stdout),
        "stderr": clip(result.stderr),
        "timed_out": result.timed_out,
    }
    return json.dumps(output), tree


def refuse_unsupported_proposer(options: NativeOptions, engine: str) -> None:
    """Refuse a proposer setting that the worker tool loop does not run, so the run fails and is not changed.

    The tool loop is the only proposer here: a file proposer command, a host CLI, ``[proposer] mcp`` servers and a
    reasoning effort would be silently dropped. SKY-816 brings the file proposer command back.

    Args:
        options: The run's native options.
        engine: The engine's display name, for the message.

    Raises:
        ServiceError: When the options name a proposer setting that the tool loop does not run.
    """
    proposer = options.proposer
    refused = [
        name
        for name, value in (
            ("run_command", proposer.run_command),
            ("install_command", proposer.install_command),
            ("host_cli", proposer.host_cli),
        )
        if value
    ]
    if proposer.harness != BLACKBOX_HARNESS_CODEX:
        refused.append("harness")
    if ((options.budget_route or {}).get(MCP_RELAY_ROUTE_KEY) or {}).get("servers"):
        refused.append("mcp")
    if options.reasoning_effort:
        refused.append("reasoning_effort")
    if refused:
        raise ServiceError(
            f"{engine} in the worker engine loop runs only its built-in proposer, so it cannot apply these "
            f"settings: {', '.join(refused)}. Remove them, or choose another engine."
        )
