"""Share the tool-loop parts of the agent engines that run in the worker engine loop.

Meta-Harness (SKY-722) and AutoResearch (SKY-725) run their proposer sessions as
tool loops on the engine context. Both use the budget view, the proposer
refusal, the command run, the model retry, the output cut and the prompt byte
measure here. Both bound the prompt in UTF-8 bytes, as SKY-722 decided.

In a repository run (SKY-825), a version is a tree. A session starts on the
parent version's tree with the engine's lab folder over it, and the tree it
leaves, without the lab folder, is the new version. The lab helpers are here.

When the contract file names ``[proposer] entry`` (SKY-816), :class:`FileProposer`
runs that command once per new version in place of the tool loop. The engine
keeps its own search and lab state; only the step that makes the version changes.
"""

from __future__ import annotations

import json
import shlex
import time
from collections.abc import Iterator, Mapping
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from ....billing.model_gateway import MCP_RELAY_ROUTE_KEY
from ....exceptions import ServiceError
from ....models.blackbox import BLACKBOX_HARNESS_CODEX, BLACKBOX_HARNESS_CUSTOM
from ....sandbox.grants import Grant
from ....sandbox.trees import BoxFailureError, TreeRejectedError
from ....worker.engine_loop import ModelCallFailedError
from .native_runtime import NativeOptions
from .protocol import EngineContext, EvalServer, FileTooLargeError, ModelReply
from .sandbox_contract import CommandResult

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
# The file in the lab folder that ``ASCENT_TASK`` names for a ``[proposer] entry`` command. Its name differs from
# every lab file of the engines, so it never replaces one.
TASK_FILE = "ascent-task.md"
# The file in the lab folder where the setup box lists the paths that ``[proposer] setup`` added.
_SETUP_ROOTS = "setup-roots"
# The setup box's check: stdin and the file in argv[1] are NUL-separated listings after and before the setup, so a
# name with a new line stays one path. It prints each new path whose folder existed before: a top path of the output.
_NEW_ROOTS = r"""import sys
seen = set(open(sys.argv[1], "rb").read().split(b"\0"))
for path in sorted(set(sys.stdin.buffer.read().split(b"\0")) - seen):
    if path and (path.rpartition(b"/")[0] == b"." or path.rpartition(b"/")[0] in seen):
        sys.stdout.buffer.write(path + b"\0")
"""
# The limit of each ``[proposer] setup`` and ``[proposer] entry`` command, as docs/repository-constraints.md says.
_COMMAND_TIMEOUT_SECONDS = 30 * 60
# The error of a setup that changed the seed names at most this many files.
_NAMED_FILES = 20
# The error compares at most this many common files, each read up to this many bytes, so a large repository
# cannot fill the worker's memory on that error path.
_COMPARED_FILES = 200
_COMPARED_BYTES = 1_000_000
# A failed command's message keeps the end of its output, where the error usually is.
_COMMAND_ERROR_CHARS = 2000

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
        The parsed value, or ``None`` when the text is not JSON, and whether the arguments break a bound or are not
        valid UTF-8.
    """
    value = call.get("arguments")
    if isinstance(value, str):
        if len(value) > MAX_ARGUMENT_CHARS or _too_deep_text(value):
            return None, True
        try:
            value = json.loads(value)
        except ValueError:
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
        The arguments, or ``None`` when they are not a JSON object, break the length or depth bound or are not valid
        UTF-8.
    """
    value, refused = _parse(call)
    return value if not refused and isinstance(value, dict) else None


def refused_arguments(call: Mapping[str, Any], tool: str) -> str | None:
    """Give the error result for a tool call whose arguments break the length or depth bound or are not valid UTF-8.

    Args:
        call: The tool call.
        tool: The tool's name, for the message.

    Returns:
        The error result as JSON text, or ``None`` when the arguments are inside the bounds and valid UTF-8.
    """
    if not _parse(call)[1]:
        return None
    error = (
        f"Send the {tool} tool arguments as JSON of at most {MAX_ARGUMENT_CHARS} characters, "
        f"nested at most {MAX_ARGUMENT_DEPTH} levels, with only valid UTF-8 text (no lone surrogates)."
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
    # The transcript holds an empty object for every argument that ``refused_arguments`` refuses. Compact text can
    # grow past the bound when ``_parse`` dumps it again with spaces, so a check on the text alone would keep some
    # refused text.
    if _parse(call)[1]:
        value = _REFUSED_ARGUMENTS
    elif isinstance(value, str):
        # Text that is not a JSON object passes ``_parse`` and gets the misuse error, but a lone surrogate in it would
        # still break the UTF-8 prompt, so the transcript also holds an empty object for it.
        if _refused_text(value):
            value = _REFUSED_ARGUMENTS
    else:
        # Sorted keys give the same text for a live reply and for its replay from the step log.
        value = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return {
        "id": call.get("id"),
        "type": "function",
        "function": {"name": call.get("name"), "arguments": value},
    }


def _too_deep_text(text: str) -> bool:
    """Tell whether text nests brackets deeper than ``MAX_ARGUMENT_DEPTH``, with no parser and no recursion.

    The parser's recursion limit depends on the stack at the call, so a live reply and its replay could see two
    results for the same deep text. This count depends only on the text: brackets inside JSON strings do not count.

    Args:
        text: The arguments as JSON text, valid or not.

    Returns:
        ``True`` when a ``[`` or ``{`` outside a string sits deeper than the bound.
    """
    depth = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > MAX_ARGUMENT_DEPTH:
                return True
        elif char in "]}":
            depth -= 1
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
    return _run_command(ctx, tree, command)


def _run_command(ctx: EngineContext, tree: str, command: str) -> tuple[str, str]:
    """Run one command over the tree, inside the command bound, and cut its outputs for the prompt.

    Args:
        ctx: The engine context, with its command call.
        tree: The workspace tree.
        command: The shell command.

    Returns:
        The tool result as JSON text, and the tree after the command.
    """
    assert ctx.run_command is not None
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


def refuse_unsupported_proposer(options: NativeOptions, engine: str, *, file_proposer: bool = False) -> None:
    """Refuse a proposer setting that the worker tool loop does not run, so the run fails and is not changed.

    A host CLI, ``[proposer] mcp`` servers and a reasoning effort would be silently dropped. A command is run only
    when it is the contract file's ``[proposer] entry`` in a repository run (SKY-816).

    Args:
        options: The run's native options.
        engine: The engine's display name, for the message.
        file_proposer: Whether :class:`FileProposer` runs the options' command.

    Raises:
        ServiceError: When the options name a proposer setting that the tool loop does not run.
    """
    proposer = options.proposer
    refused = [
        name
        for name, value in (
            ("run_command", not file_proposer and proposer.run_command),
            ("install_command", proposer.install_command),
            ("host_cli", proposer.host_cli),
        )
        if value
    ]
    # Repository staging sets the custom harness for the file's command.
    if proposer.harness != (BLACKBOX_HARNESS_CUSTOM if file_proposer else BLACKBOX_HARNESS_CODEX):
        refused.append("harness")
    if ((options.budget_route or {}).get(MCP_RELAY_ROUTE_KEY) or {}).get("servers"):
        refused.append("mcp")
    if options.reasoning_effort:
        refused.append("reasoning_effort")
    if refused:
        runs = (
            "runs the [proposer] entry command of the contract file"
            if file_proposer
            else "in the worker engine loop runs only its built-in proposer"
        )
        raise ServiceError(
            f"{engine} {runs}, so it cannot apply these settings: {', '.join(refused)}. "
            "Remove them, or choose another engine."
        )


def repo_seed_tree(ctx: EngineContext, lab: str, engine: str) -> str:
    """Return the visible seed tree of a repository run, when the engine's lab folder is free in it.

    The engine removes the lab folder from each session's tree to get the version, so a repository entry there would
    be lost from every version. The tree reads skip a symlink and a folder of hidden paths, so one box command checks
    for an entry of any type.

    Args:
        ctx: The engine context, with its ``repo_seed`` and its command call.
        lab: The lab folder, relative to the repository root.
        engine: The engine's display name, for the message.

    Returns:
        The seed tree id.

    Raises:
        ServiceError: When the seed tree holds an entry of any type at the lab path, or the check timed out.
    """
    assert ctx.repo_seed is not None
    assert ctx.run_command is not None
    seed = ctx.repo_seed.tree_id
    # ``test -e`` follows a link, so a link to a missing target needs ``test -L``.
    result, _ = ctx.run_command(seed, f"test ! -e {lab} && test ! -L {lab}", ())
    # A timed-out check says nothing about the repository, so it must not blame the repository's files.
    if result.timed_out:
        raise ServiceError(f"{engine} could not check the repository for a `{lab}/` path: the check timed out.")
    if result.exit_code != 0:
        raise ServiceError(f"{engine} keeps its lab in `{lab}/`, so the repository must not have that path.")
    return seed


def put_lab(ctx: EngineContext, parent: str, lab: str, files: Mapping[str, bytes]) -> str:
    """Write the engine's lab files under the lab folder over a version's tree, for one proposer session.

    Args:
        ctx: The engine context, with its tree write.
        parent: The tree of the version that the session starts from.
        lab: The lab folder, relative to the repository root.
        files: The lab file bytes, keyed by path inside the lab folder.

    Returns:
        The session's first tree.
    """
    assert ctx.put_files is not None
    return ctx.put_files({f"{lab}/{path}": data for path, data in files.items()}, parent)


def drop_lab(ctx: EngineContext, start: str, tree: str, parent: str, lab: str) -> str | None:
    """Return the version that a proposer session left: its last tree without the lab folder.

    A tree write cannot delete a file, so one ``rm`` command in a box removes the folder. A session that ran no
    command left the parent unchanged, so it needs no box.

    Args:
        ctx: The engine context, with its command call.
        start: The session's first tree, the parent with the lab.
        tree: The session's last tree.
        parent: The tree of the version that the session started from.
        lab: The lab folder, relative to the repository root.

    Returns:
        The version's tree id, or ``None`` when the box could not remove the lab, so the session left no version.
    """
    assert ctx.run_command is not None
    if tree == start:
        return parent
    try:
        result, version = ctx.run_command(tree, f"rm -rf {lab}", ())
    except (TreeRejectedError, BoxFailureError):
        return None
    return version if result.exit_code == 0 and not result.timed_out else None


def task_text(intro: str, lab: str, files: Mapping[str, bytes], names: tuple[str, ...]) -> bytes:
    """Write the task file of a ``[proposer] entry`` command: the session's prompt and the named lab files.

    Args:
        intro: The prompt that the tool loop would send.
        lab: The lab folder, relative to the repository root.
        files: The session's lab file bytes, keyed by path inside the lab folder.
        names: The lab files to copy into the task, in order; a missing one is left out.

    Returns:
        The task file bytes.
    """
    parts = [intro.encode("utf-8")]
    parts.extend(f"## `{lab}/{name}`\n\n".encode() + files[name] for name in names if name in files)
    return b"\n\n".join(parts)


class FileProposer:
    """Run the contract file's ``[proposer] entry`` once per new version, over the parent's tree (SKY-816).

    The command starts on the parent's tree with the engine's lab folder over it, and ``ASCENT_TASK`` names the
    task file there. The tree it leaves, without the lab folder and without the files that ``[proposer] setup``
    added, is the new version, so the command needs no ``eval.sh`` or ``save.sh``. Each command is one committed
    ``command`` step, so a resume replays it. A failed command gives no version. Meta-Harness then goes on to
    its next iteration; AutoResearch's native stop rule ends the run, because the round spent nothing.
    """

    def __init__(self, ctx: EngineContext, options: NativeOptions, lab: str) -> None:
        """Bind the proposer to the run's boxes and the contract file's commands.

        Args:
            ctx: The engine context, with its ``repo_seed``, its command call and its tree calls.
            options: The run's native options, with the file's commands and the proposer's hosts.
            lab: The engine's lab folder, relative to the repository root.
        """
        assert ctx.repo_seed is not None
        repo = options.repo or {}
        self._ctx = ctx
        self._lab = lab
        self._entry = options.proposer.run_command or ""
        self._setup_argv = [str(part) for part in repo.get("proposer_setup") or ()]
        self._setup_name = f"[proposer] setup command in optimize.toml ({shlex.join(self._setup_argv)})"
        # The run's gateway holds the deployment's managed key, so the box gets no grant for it, as in SKY-815.
        model_host = urlsplit(options.gateway.url or "").hostname
        self._grants = tuple(
            Grant(host) for host in sorted(set(repo.get("proposer_hosts") or ())) if host != model_host
        )
        self._exports = [f"export APP_MODEL={shlex.quote(str(options.model or ''))}"]
        self._roots: list[str] | None = None
        # Each version's tree with the setup's output, so the next session from it gets that output back.
        self._full: dict[str, str] = {}
        # The last tree of the session that left each version, with the lab still in it. Most versions never start
        # a session, so ``base`` removes the lab from it only for a version that does.
        self._last: dict[str, str] = {}

    def base(self, parent: str) -> str:
        """Return the tree that a session from a version starts on: the version with the setup's output.

        The first call runs ``[proposer] setup`` over the seed tree, once, before the first session.

        Args:
            parent: The version's tree id.

        Returns:
            The tree to put the lab over.

        Raises:
            ServiceError: When the setup fails, adds too many paths, or changes or removes a file of the seed; or
                when the box that removes the lab folder from the version's last tree fails.
        """
        if self._roots is None:
            self._roots = self._setup()
        if parent not in self._full and parent in self._last:
            full = drop_lab(self._ctx, "", self._last[parent], parent, self._lab)
            if full is None:
                # Without the setup's output every later session from this version would fail with no report. The
                # box is a committed step, so a resume raises the same error.
                raise ServiceError(f"The check of the {self._setup_name} could not run in a box. Start a new run.")
            self._full[parent] = full
        return self._full.get(parent, parent)

    def run(self, start: str) -> tuple[str, str | None]:
        """Run the command once over the session's first tree.

        Args:
            start: The parent's tree with the lab folder and the task file over it.

        Returns:
            The tree that the command left and ``None``; or ``start`` and the reason when the command failed, so
            the session leaves no version.

        Raises:
            GrantDeniedError: When the grants do not resolve. Every command would fail the same way, so the run
                stops; a resume replays the committed failure and raises it again.
        """
        assert self._ctx.run_command is not None
        task = f"{self._lab}/{TASK_FILE}"
        script = "\n".join(
            [*self._exports, f'export ASCENT_TASK="$PWD/{task}" APP_PROMPT_FILE="$PWD/{task}"', self._entry]
        )
        try:
            result, tree = self._ctx.run_command(start, script, self._grants, timeout_seconds=_COMMAND_TIMEOUT_SECONDS)
        except (TreeRejectedError, BoxFailureError) as error:
            # The step committed the failure, so a resume gets the same failed version.
            return start, f"The [proposer] entry command left no usable version: {error}"
        if result.timed_out or result.exit_code != 0:
            return start, _command_failure("[proposer] entry command", result)
        return tree, None

    def version(self, start: str, tree: str, parent: str) -> str | None:
        """Return the version that a session left: its last tree without the lab folder and the setup's output.

        Args:
            start: The session's first tree.
            tree: The session's last tree.
            parent: The tree of the version that the session started from.

        Returns:
            The version's tree id, or ``None`` when a box could not remove the paths.

        Raises:
            ServiceError: When the ``rm`` of the setup's output fails.
        """
        if tree == start:
            return parent
        if not self._roots:
            return drop_lab(self._ctx, start, tree, parent, self._lab)
        version = _remove(self._ctx, tree, [self._lab, *self._roots])
        if version is not None:
            self._last.setdefault(version, tree)
        return version

    def _setup(self) -> list[str]:
        """Run ``[proposer] setup`` once over the seed tree, and list the top paths that it added.

        One box runs the setup and lists the new paths, so the list also holds links and empty folders, which no
        tree read sees. Two more boxes check that only those paths changed: one removes the lab folder and the added
        paths, and one gives the seed's own tree to compare with. ``base`` removes only the lab folder later, for
        the first session's tree.

        Returns:
            The added paths that hold no seed path, relative to the repository root; empty with no setup.

        Raises:
            ServiceError: When the setup fails, adds too many paths, or changes or removes a file of the seed; when
                a box of the setup or of its check fails; or when the ``rm`` of its output fails.
            GrantDeniedError: When the grants do not resolve.
        """
        assert self._ctx.repo_seed is not None
        assert self._ctx.run_command is not None
        assert self._ctx.read_file is not None
        seed = self._ctx.repo_seed.tree_id
        self._full[seed] = seed
        if not self._setup_argv:
            return []
        name = self._setup_name
        listing = "find . -mindepth 1 -print0"
        lab = shlex.quote(self._lab)
        script = "\n".join(
            [
                *self._exports,
                "paths=$(mktemp) || exit 1",
                f'{listing} > "$paths"',
                f"{shlex.join(self._setup_argv)} || exit $?",
                f"mkdir -p {lab}",
                f'{listing} | python3 -I -c {shlex.quote(_NEW_ROOTS)} "$paths" > {lab}/{_SETUP_ROOTS}',
            ]
        )
        try:
            result, tree = self._ctx.run_command(seed, script, self._grants, timeout_seconds=_COMMAND_TIMEOUT_SECONDS)
        except BoxFailureError as error:
            raise ServiceError(f"The {name} could not run in a box. Start a new run. {error}") from error
        except TreeRejectedError as error:
            raise ServiceError(f"The {name} left files that the run refuses. {error}") from error
        if result.timed_out or result.exit_code != 0:
            raise ServiceError(_command_failure(name, result))
        try:
            data = self._ctx.read_file(tree, f"{self._lab}/{_SETUP_ROOTS}", MAX_COMMAND_BYTES // 2)
        except FileTooLargeError:
            raise ServiceError(f"The {name} added too many top-level paths. Put its output in fewer folders.") from None
        names = (data or b"").decode("utf-8", "replace").split("\0")
        # The lab folder that the listing's own ``mkdir`` made is no setup output.
        roots = [path for path in (name.removeprefix("./") for name in names if name) if path != self._lab]
        stripped = _remove(self._ctx, tree, [self._lab, *roots])
        # The same box round trip on the seed gives the tree to compare with, without the hidden paths.
        untouched = None if stripped is None else _round_trip(self._ctx, seed)
        if untouched is None:
            raise ServiceError(f"The check of the {name} could not run in a box. Start a new run.")
        if stripped != untouched:
            changed, shortened = _changed_files(self._ctx, untouched, stripped)
            if changed:
                named = f": {', '.join(changed)}" + (" (list shortened)" if shortened else "") + "."
            else:
                named = (
                    ". The check names no file: the change is in a link, an empty folder or a file that it did not"
                    " read."
                )
            raise ServiceError(
                f"The {name} changed or removed files of the repository{named}"
                " Those changes would enter every version. Make the setup write only new files."
            )
        if roots:
            del self._full[seed]
            self._last[seed] = tree
        else:
            self._full[seed] = stripped
        return roots


def _remove(ctx: EngineContext, tree: str, paths: list[str]) -> str | None:
    """Remove paths from a tree with one ``rm`` command in a box.

    Args:
        ctx: The engine context, with its command call.
        tree: The tree.
        paths: The paths, relative to the repository root.

    Returns:
        The tree without the paths, or ``None`` when the box failed.

    Raises:
        ServiceError: When ``rm`` fails or runs out of time, for example on a read-only folder of the setup.
    """
    assert ctx.run_command is not None
    try:
        result, after = ctx.run_command(tree, f"rm -rf -- {shlex.join(paths)}", ())
    except (TreeRejectedError, BoxFailureError):
        return None
    if result.timed_out or result.exit_code != 0:
        raise ServiceError(
            _command_failure("removal of the files that [proposer] setup added", result)
            + " Make the setup write only files that it can remove."
        )
    return after


def _changed_files(ctx: EngineContext, seed: str, tree: str) -> tuple[list[str], bool]:
    """Name the files that differ between the seed and a tree, for the error of a setup that changed the seed.

    Args:
        ctx: The engine context, with its file calls.
        seed: The seed's tree.
        tree: The setup's tree without the lab folder and the setup's output.

    Returns:
        The added, removed and changed files and the files whose executable mode changed, sorted, at most
        ``_NAMED_FILES``. It compares the content of at most ``_COMPARED_FILES`` common files, and the size of a
        file above ``_COMPARED_BYTES``. Empty when only a link or an empty folder differs. Then whether a cap
        shortened the list.
    """
    assert ctx.list_files is not None
    assert ctx.read_file is not None
    before = set(ctx.list_files(seed) or ())
    after = set(ctx.list_files(tree) or ())
    modes = set(ctx.list_files(seed, executable=True) or ()) ^ set(ctx.list_files(tree, executable=True) or ())
    changed = (before ^ after) | modes
    common = sorted((before & after) - changed)
    compared = 0
    for path in common[:_COMPARED_FILES]:
        if len(changed) >= _NAMED_FILES:
            break
        compared += 1
        if _content(ctx, seed, path) != _content(ctx, tree, path):
            changed.add(path)
    return sorted(changed)[:_NAMED_FILES], len(changed) > _NAMED_FILES or compared < len(common)


def _content(ctx: EngineContext, tree: str, path: str) -> bytes | int | None:
    """Read a file to compare, or give its size when it is above ``_COMPARED_BYTES``.

    The engine context gives no blob id, so the size is the only cheap sign that a large file changed. A large
    file whose size stays the same counts as unchanged, so it cannot push a changed file out of the named files.

    Args:
        ctx: The engine context, with its file read.
        tree: The tree.
        path: The file.

    Returns:
        The file bytes, its size in bytes when it is above the cap, or ``None`` when it is missing.
    """
    assert ctx.read_file is not None
    try:
        return ctx.read_file(tree, path, _COMPARED_BYTES)
    except FileTooLargeError as error:
        return error.size


def _round_trip(ctx: EngineContext, tree: str) -> str | None:
    """Pass a tree through a box with no change, so it compares with a tree that a box command left.

    Args:
        ctx: The engine context, with its command call.
        tree: The tree.

    Returns:
        The tree after the box, or ``None`` when the box failed.
    """
    assert ctx.run_command is not None
    try:
        result, after = ctx.run_command(tree, "true", ())
    except (TreeRejectedError, BoxFailureError):
        return None
    return after if result.exit_code == 0 and not result.timed_out else None


def _command_failure(name: str, result: CommandResult) -> str:
    """Describe a failed box command for the run's log or error.

    Args:
        name: The command's name in the message.
        result: The command's result.

    Returns:
        The message, with the end of the command's stderr, or of its stdout when stderr is empty.
    """
    how = "ran out of time" if result.timed_out else f"failed with exit code {result.exit_code}"
    detail = (result.stderr or result.stdout or "").strip()[-_COMMAND_ERROR_CHARS:]
    return f"The {name} {how}." + (f" {detail}" if detail else "")
