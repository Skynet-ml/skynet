"""Share the tool-loop parts of the agent engines that run in the worker engine loop.

Meta-Harness (SKY-722) and AutoResearch (SKY-725) run their proposer sessions as
tool loops on the engine context. Both use the budget view and the proposer
refusal here. AutoResearch also uses the bash tool call, model retry and output
cut here. Meta-Harness keeps its own copy: its tests replace those names on its
module. Both bound the prompt in UTF-8 bytes, as SKY-722 decided.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator, Mapping
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
# prompt hashes that a replay checks, bounded. The cut counts each stream's bytes in the prompt as the session
# measures it: a cut in characters lets one result of Hebrew or control characters fill ``MAX_PROMPT_BYTES``.
OUTPUT_BYTES = 30_000
# Model output is untrusted, and a committed reply replays on every resume, so a crash on its arguments could
# never recover. These bounds make an over-long or deeply nested argument an error result: deep JSON makes the
# parser, ``json.dumps`` and the next prompt hash raise RecursionError.
MAX_ARGUMENT_CHARS = 200_000
MAX_ARGUMENT_DEPTH = 32
# Every box provider runs the command as one argv string, and Linux refuses an argv string over 128 KiB
# (MAX_ARG_STRLEN) with E2BIG. A command under this bound gets to the box; a longer one gets an error result.
MAX_COMMAND_BYTES = 100_000
_REFUSED_ARGUMENTS = "{}"
# A failed model step replays as the same failure, so the tool loop retries at fixed places: a resume makes
# the same calls in the same order.
MODEL_ATTEMPTS = 3
# A rate limit (429) often clears in seconds, so a wait between attempts keeps a short burst from failing all of them.
MODEL_RETRY_WAIT_SECONDS = 10.0
# Every turn sends the whole transcript again, and each command can add up to two clipped outputs. This cap, and
# the proposer's ``max_tool_calls``, keep the prompt inside the model's context. The prompt size depends only on
# the committed replies and results, so a resume stops a session at the same place. A byte-level token holds at
# least one byte, so UTF-8 bytes bound the tokens of any output (Hebrew, base64 or binary-ish text can be about one
# token a character). A 200k-token context, less 64k tokens of answer and 8k for the tool and message framing,
# leaves 128k (SKY-722).
MAX_PROMPT_BYTES = 128_000

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
    if isinstance(value, dict) and _refused_text(json.dumps(value, ensure_ascii=False)):
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


def refused_arguments(call: Mapping[str, Any], tool: str) -> str | None:
    """Give the error result for a tool call whose arguments break the length or depth bound.

    Args:
        call: The tool call.
        tool: The tool's name, for the message.

    Returns:
        The error result as JSON text, or ``None`` when the arguments are inside the bounds.
    """
    if not _parse(call)[1]:
        return None
    error = (
        f"Send the {tool} tool arguments as JSON of at most {MAX_ARGUMENT_CHARS} characters, "
        f"nested at most {MAX_ARGUMENT_DEPTH} levels."
    )
    return json.dumps({"error": error}, ensure_ascii=False)


def _refused_text(text: str) -> bool:
    """Tell whether the JSON text of parsed arguments breaks the length bound or is not valid UTF-8.

    Args:
        text: The arguments as JSON with no escapes.

    Returns:
        ``True`` when the text is too long or holds a lone surrogate.
    """
    if len(text) > MAX_ARGUMENT_CHARS:
        return True
    # ``json.loads`` turns an escaped lone surrogate into one, which no UTF-8 encode, box or prompt can take.
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def openai_call(call: Mapping[str, Any]) -> dict[str, Any]:
    """Give one flat tool call of the engine loop in the OpenAI form that the next prompt's history holds.

    The AutoSaddler port (SKY-723) sends the same form, so every port sends one transcript shape.

    Args:
        call: The flat ``{"id", "name", "arguments"}`` call.

    Returns:
        ``{"id", "type": "function", "function": {"name", "arguments"}}``, with the arguments as JSON text.
    """
    value = call.get("arguments")
    # ``run_bash`` refuses arguments past the depth or length bound or not valid UTF-8, so the transcript holds an
    # empty object in their place.
    if isinstance(value, str):
        if _refused_text(value) or _too_deep_text(value):
            value = _REFUSED_ARGUMENTS
    else:
        # Sorted keys give the same text for a live reply and for its replay from the step log. ``_parse`` measures
        # the arguments with no escapes, so Hebrew text does not count six characters a letter.
        if _too_deep(value):
            value = _REFUSED_ARGUMENTS
        else:
            value = json.dumps(value, sort_keys=True, ensure_ascii=False)
            if _refused_text(value):
                value = _REFUSED_ARGUMENTS
    return {
        "id": call.get("id"),
        "type": "function",
        "function": {"name": call.get("name"), "arguments": value},
    }


def _too_deep_text(text: str) -> bool:
    """Tell whether JSON text nests deeper than ``MAX_ARGUMENT_DEPTH``.

    Args:
        text: The arguments as JSON text, inside the length bound.

    Returns:
        ``True`` when the parsed value is too deep or too deep for the parser; ``False`` for text that is not JSON.
    """
    try:
        return _too_deep(json.loads(text))
    # JSON nested past the parser's recursion limit is past the depth bound too.
    except RecursionError:
        return True
    except ValueError:
        return False


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


def prompt_bytes(text: str) -> int:
    """Measure a stream as the session measures it in the prompt.

    The tool result is the JSON of the output, and the session measures the JSON of the messages, so the stream
    is encoded twice: a control character takes 7 bytes, not 1.

    Args:
        text: The stream.

    Returns:
        The UTF-8 bytes of the stream, encoded twice as JSON, with its quotes.
    """
    return len(json.dumps(json.dumps(text, ensure_ascii=False), ensure_ascii=False).encode("utf-8", "surrogatepass"))


def messages_bytes(messages: list[dict[str, Any]]) -> int:
    """Measure the prompt of a session in UTF-8 bytes.

    Args:
        messages: The session's messages.

    Returns:
        The UTF-8 bytes of the messages as JSON with no escapes.
    """
    # ``surrogatepass`` keeps the measure from raising on any text that a command printed.
    return len(json.dumps(messages, ensure_ascii=False).encode("utf-8", "surrogatepass"))


def _fit(chars: Iterator[str], budget: int) -> int:
    """Count the characters, in order, whose prompt bytes fit in ``budget``.

    Args:
        chars: The characters.
        budget: The prompt bytes they may take, without quotes.

    Returns:
        How many of the first characters fit.
    """
    quotes = prompt_bytes("")
    count = 0
    for char in chars:
        # JSON escapes each code point alone, so the sizes add up, and a cut between code points always encodes.
        budget -= prompt_bytes(char) - quotes
        if budget < 0:
            break
        count += 1
    return count


def clip(text: str) -> str:
    """Cut a tool output to ``OUTPUT_BYTES`` in the prompt: keep its head and tail, with a note on what was cut.

    Args:
        text: The output.

    Returns:
        The output unchanged when it fits, else its head, a note and its tail.
    """
    # Each character takes at least one prompt byte, so the length check spares a huge output the full encode.
    if len(text) <= OUTPUT_BYTES and prompt_bytes(text) <= OUTPUT_BYTES:
        return text
    # The note for a cut of every character is the longest note, so the result always fits.
    half = (OUTPUT_BYTES - prompt_bytes(f"\n[... {len(text)} characters cut ...]\n")) // 2
    head = _fit(iter(text), half)
    tail = _fit(reversed(text), half)
    return f"{text[:head]}\n[... {len(text) - head - tail} characters cut ...]\n{text[len(text) - tail :]}"


def call_model(
    ctx: EngineContext,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    spent: Decimal,
    max_budget_usd: float | None,
) -> tuple[ModelReply | ModelCallFailedError | None, Decimal]:
    """Call the model with the tools, and try a failed call again up to ``MODEL_ATTEMPTS`` times.

    Each attempt is one ``model`` step, so a resume replays the same failures and the same retries. A live
    failure waits ``MODEL_RETRY_WAIT_SECONDS`` before the next attempt; a replayed failure made no call, so it
    does not wait. A failed attempt that the provider billed carries its committed cost, live and on a replay,
    so the proposer cap counts it the same way both times, and no retry starts once the cap is reached.

    Args:
        ctx: The engine context, with its model call.
        messages: The session's messages.
        tools: The tool definitions.
        spent: The session's committed spend before this call, in cents.
        max_budget_usd: Proposer spend still allowed, in dollars, or ``None``.

    Returns:
        The reply, the last :class:`ModelCallFailedError` when every attempt failed, or ``None`` when the billed
        failures reached the proposer cap; and the summed cost of the failed attempts, in cents.
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
        if max_budget_usd is not None and float(spent + failed_cents) / 100 >= max_budget_usd:
            return None, failed_cents
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
    # A bad tool call goes back to the model as an error result, as in the Agents SDK tool loop. The name comes
    # first, so a call to an unknown tool never gets the bash bound error.
    if call.get("name") != "bash":
        return json.dumps({"error": misuse}, ensure_ascii=False), tree
    refused = refused_arguments(call, "bash")
    if refused is not None:
        return refused, tree
    parsed, _ = _parse(call)
    command = parsed.get("command") if isinstance(parsed, dict) else None
    if not isinstance(command, str):
        return json.dumps({"error": misuse}, ensure_ascii=False), tree
    # ``_parse`` refuses a lone surrogate first; ``surrogatepass`` keeps this measure from ever raising.
    if len(command.encode("utf-8", "surrogatepass")) > MAX_COMMAND_BYTES:
        error = f"Keep the command at most {MAX_COMMAND_BYTES} bytes as UTF-8. Write long content in parts."
        return json.dumps({"error": error}, ensure_ascii=False), tree
    try:
        result, tree = ctx.run_command(tree, command, ())
    except (TreeRejectedError, BoxFailureError) as error:
        # The step committed the failure, so a resume raises it again and gets the same result here.
        return json.dumps({"error": f"The command failed and changed nothing: {error}"}, ensure_ascii=False), tree
    output = {
        "exit_code": result.exit_code,
        "stdout": clip(result.stdout),
        "stderr": clip(result.stderr),
        "timed_out": result.timed_out,
    }
    # The session measures the prompt with no escapes, so an escape here makes a Hebrew letter take 7 bytes, not 2.
    return json.dumps(output, ensure_ascii=False), tree


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
