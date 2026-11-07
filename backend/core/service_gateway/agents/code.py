"""Shared streaming and model helpers for Skynet's ReAct agents.

The generalist agent, the served-program chat, the interviews and the
tagger build on these pieces: the reply and tool-call stream listeners
(:class:`ReactReplyStream`, :class:`ReasoningStreamListener`), the error
formatting for a failed turn, the reply-language lookup and
:func:`_build_agent_lm`.

The agents run on whatever LiteLLM-compatible model is configured via
``settings.code_agent_model`` (default: the model catalog's best-value
pick). Users can point it at an internal gateway via
``CODE_AGENT_BASE_URL``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import dspy
import jiter

from ...api.model_catalog import agent_model_id
from ...config import settings
from ...local.cli_catalog import parse_subscription_model_id
from ...models import ModelConfig
from ..language_models import (
    apply_model_reasoning_config,
    apply_reasoning_effort,
    build_language_model,
)
from ..react_compat import native_tool_calling_active
from .constants import REASONING_FIELD

logger = logging.getLogger(__name__)

# ReActV2 termination reasons that mean the agent actually answered.
_ANSWERED_TERMINATIONS = frozenset({"submit", "forced_submit"})


class AgentStoppedError(RuntimeError):
    """The agent loop ended without calling ``submit`` and without a reply."""


def _require_answer(
    prediction: dspy.Prediction | None,
    reply_text: str,
    *,
    next_step: str = "Check the editor, then send your message again or pick a different model.",
) -> None:
    """Fail a turn that ended without the agent ever answering.

    A reasoning model that spends its whole output budget thinking returns no
    tool call; ReActV2 then breaks out of the loop and its forced ``submit``
    can come back empty too. Reporting that as a finished turn shows the user
    a bare "Done." while nothing they asked for happened.

    Args:
        prediction: The loop's final prediction; ``None`` when none arrived.
        reply_text: The reply streamed or carried by the final prediction.
        next_step: The sentence telling the user what to do next.

    Raises:
        AgentStoppedError: When the reply is empty and the loop did not end
            on a ``submit`` call.
    """
    if reply_text.strip():
        return
    reason = getattr(prediction, "termination_reason", None) if prediction is not None else "no_prediction"
    if reason in _ANSWERED_TERMINATIONS:
        return
    logger.warning("code agent stopped without answering: termination_reason=%s", reason)
    raise AgentStoppedError(
        "The model stopped before it answered, usually because it ran out of room while thinking. " + next_step
    )


def _format_agent_error(exc: BaseException) -> str:
    """Produce a short, user-facing message from an agent failure.

    Walks through ``BaseExceptionGroup`` to the first leaf exception so
    structured-concurrency groups resolve to their underlying cause.
    Network-level failures are rewritten to a friendlier message.

    Args:
        exc: The exception (possibly a ``BaseExceptionGroup``) raised by the agent.

    Returns:
        A short string suitable for surfacing to the end user.
    """
    leaf = exc
    while isinstance(leaf, BaseExceptionGroup) and leaf.exceptions:
        leaf = leaf.exceptions[0]
    text = str(leaf).strip()
    name = type(leaf).__name__
    if not text:
        return name or "code agent failed"
    if isinstance(leaf, AgentStoppedError):
        return text
    if "Cannot connect to host" in text or "nodename nor servname" in text:
        return "Can't reach the model provider. Check your network and try again."
    return f"{name}: {text}" if name not in text else text


# Provider phrasings for a prompt that overflowed the model's context window;
# matched case-insensitively across the exception chain because the overflow
# often surfaces as a plain provider 400 rather than litellm's typed error.
_CONTEXT_OVERFLOW_PATTERNS = (
    "context window",
    "context length",
    "context_length_exceeded",
    "prompt is too long",
    "input is too long",
    "too many tokens",
    "maximum prompt length",
)


def _agent_error_payload(exc: BaseException) -> dict[str, str]:
    """Build the ``error`` SSE event data for an agent failure.

    Alongside the human-readable text, attaches a stable machine ``code`` the
    frontend maps to a localized message. Currently classified:
    ``context_too_long`` — the turn overflowed the model's context window
    (litellm's ``ContextWindowExceededError`` or an equivalent provider
    message anywhere in the exception chain).

    Args:
        exc: The exception (possibly a ``BaseExceptionGroup``) raised by the agent.

    Returns:
        Event data with ``error`` text and, when classified, a ``code``.
    """
    payload = {"error": _format_agent_error(exc)}
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        leaf = stack.pop()
        if id(leaf) in seen:
            continue
        seen.add(id(leaf))
        text = str(leaf).lower()
        if type(leaf).__name__ == "ContextWindowExceededError" or any(
            pattern in text for pattern in _CONTEXT_OVERFLOW_PATTERNS
        ):
            payload["code"] = "context_too_long"
            break
        if isinstance(leaf, BaseExceptionGroup):
            stack.extend(leaf.exceptions)
        stack.extend(linked for linked in (leaf.__cause__, leaf.__context__) if linked is not None)
    return payload


_LOCALE_LANGUAGE_NAMES = {
    "ar": "Arabic",
    "de": "German",
    "en": "English",
    "es": "Spanish",
    "fa": "Persian",
    "fr": "French",
    "he": "Hebrew",
    "hi": "Hindi",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "pt": "Portuguese",
    "ru": "Russian",
    "tr": "Turkish",
    "uk": "Ukrainian",
    "yue": "Cantonese",
    "zh": "Chinese",
}


def _reply_language(locale: str | None) -> str:
    """Resolve a UI locale code to the language name the agent replies in.

    Args:
        locale: UI locale code sent by the client (e.g. ``he``, ``en-GB``,
            ``zh-Hans``), or None when the client sent none.

    Returns:
        An English language name (e.g. ``"Hebrew"``). Unknown or missing
        locales fall back to Hebrew, the product's base language.
    """
    primary = (locale or "").split("-", 1)[0].strip().lower()
    return _LOCALE_LANGUAGE_NAMES.get(primary, "Hebrew")


def _extract_reasoning_token(chunk: object) -> str | None:
    """Pull a thinking/reasoning token from a raw LiteLLM streaming chunk.

    Handles the conventions in the wild:
      - LiteLLM-normalized: ``delta.reasoning_content`` (string). Emitted by
        Fireworks, DeepSeek, OpenAI o-series, and most reasoning providers.
      - OpenRouter passthrough: ``delta.reasoning`` (string) on responses that
        skip LiteLLM's normalization (direct/BYOK OpenRouter calls).
      - Detail blocks: ``delta.reasoning_details`` — MiniMax ``reasoning_split``
        and OpenRouter both use it; blocks carry ``text`` (``reasoning.text``)
        or ``summary`` (``reasoning.summary``, OpenAI-style summarized CoT).

    Args:
        chunk: A streaming chunk object from LiteLLM.

    Returns:
        The reasoning token text, or ``None`` when the chunk has no reasoning content.
    """
    choices = getattr(chunk, "choices", None)
    if not choices:
        return None
    delta = getattr(choices[0], "delta", None)
    if delta is None:
        return None
    content = getattr(delta, "reasoning_content", None)
    if isinstance(content, str) and content:
        return content
    plain = getattr(delta, "reasoning", None)
    if isinstance(plain, str) and plain:
        return plain
    details = getattr(delta, "reasoning_details", None)
    if isinstance(details, list) and details:
        parts: list[str] = []
        for block in details:
            if isinstance(block, dict):
                text = block.get("text") or block.get("summary")
            else:
                text = getattr(block, "text", None) or getattr(block, "summary", None)
            if isinstance(text, str) and text:
                parts.append(text)
        if parts:
            return "".join(parts)
    return None


class _SubmitArgExtractor:
    """Recover token-level streaming of a ReActV2 submit argument.

    ReActV2 dropped V1's separate ``extract`` step: the user's output fields
    are now arguments of an internal ``submit`` tool call carried in the
    ``tool_calls`` field of the inner predict. The raw text streamed for
    that field is JSON of the shape
    ``{"tool_calls": [{"name": "submit", "args": {<arg>: "..."}}]}``. We
    feed every chunk from a regular ``StreamListener`` bound to
    ``tool_calls`` through ``feed``; partial JSON parsing pulls out the
    growing value of ``args[<target_arg>]`` so we can re-emit
    token-by-token deltas the same way V1's ``extract.predict`` listener
    did.

    One instance per agent turn. Call ``reset`` whenever the bound stream
    listener finishes a field (its ``is_last_chunk`` signal) so the next
    loop iteration starts with a clean accumulator.
    """

    def __init__(self, target_arg: str, *, exclusive_submit: bool = False):
        """Bind the extractor to a specific submit argument name.

        Args:
            target_arg: Name of the ``submit`` arg to extract — e.g.
                ``assistant_message`` for the generalist agent or
                ``reply`` for the code agent.
            exclusive_submit: Suppress the reply when submit appears beside
                a non-submit tool, and flag already streamed text for
                retraction (see :meth:`take_retraction`).
        """
        self._target_arg = target_arg
        self._exclusive_submit = exclusive_submit
        self._buffer = ""
        self._emitted_chars = 0
        self._retract = False

    def feed(self, chunk: str, *, final: bool = False) -> str | None:
        """Append a chunk and return the newly streamed slice of the target arg.

        Args:
            chunk: Latest streaming chunk from the ``tool_calls`` listener.
            final: Whether this is the field's terminal chunk. Unused: the
                reply streams before the field ends.

        Returns:
            The new characters of ``args[target_arg]`` since the last
            successful feed, or ``None`` when the partial JSON has not
            yet exposed any additional content for the target arg.
        """
        if not chunk:
            return None
        self._buffer += chunk
        try:
            parsed = jiter.from_json(
                self._buffer.encode("utf-8"),
                partial_mode="trailing-strings",
            )
        except ValueError:
            return None
        if not isinstance(parsed, dict):
            return None
        tool_calls = parsed.get("tool_calls")
        if not isinstance(tool_calls, list):
            return None
        if self._exclusive_submit and any(
            isinstance(call, dict) and call.get("name") != "submit" for call in tool_calls
        ):
            # The reply streams while submit is the only call so far, so the
            # chat shows it as it is written; a sibling tool written after it
            # makes the loop drop that submit, and the caller takes the
            # streamed text back.
            if self._emitted_chars:
                self._retract = True
                self._emitted_chars = 0
            return None
        for call in tool_calls:
            if not isinstance(call, dict) or call.get("name") != "submit":
                continue
            args = call.get("args")
            if not isinstance(args, dict):
                continue
            value = args.get(self._target_arg)
            if not isinstance(value, str):
                continue
            if len(value) <= self._emitted_chars:
                return None
            delta = value[self._emitted_chars :]
            self._emitted_chars = len(value)
            return delta
        return None

    def reset(self) -> None:
        """Drop accumulated state between ReAct loop iterations."""
        self._buffer = ""
        self._emitted_chars = 0

    def take_retraction(self) -> bool:
        """Return whether streamed reply text must be taken back, and clear the flag."""
        retract, self._retract = self._retract, False
        return retract


class _NativeSubmitArgExtractor:
    """Recover token-level streaming of a submit argument under native function calling.

    Sibling of :class:`_SubmitArgExtractor` for the native path. When native
    function calling is active the provider streams the ``submit`` tool's
    arguments directly — raw JSON of the shape ``{<arg>: "..."}`` — without
    DSPy's ``{"tool_calls": [{"name": ..., "args": ...}]}`` text envelope. The
    incremental-emit machinery is identical; only the JSON shape differs, so
    this navigates ``parsed[target_arg]`` rather than the nested envelope.

    One instance per agent turn; call ``reset`` between loop iterations.
    """

    def __init__(self, target_arg: str):
        """Bind the extractor to a specific submit argument name.

        Args:
            target_arg: Name of the ``submit`` arg to extract — e.g. ``reply``
                for the code agent or ``assistant_message`` for the generalist.
        """
        self._target_arg = target_arg
        self._buffer = ""
        self._emitted_chars = 0

    def feed(self, chunk: str) -> str | None:
        """Append a chunk and return the newly streamed slice of the target arg.

        Args:
            chunk: Latest fragment of the submit call's ``arguments`` JSON.

        Returns:
            The new characters of ``parsed[target_arg]`` since the last
            successful feed, or ``None`` when the partial JSON has not yet
            exposed any additional content for the target arg.
        """
        if not chunk:
            return None
        self._buffer += chunk
        try:
            parsed = jiter.from_json(
                self._buffer.encode("utf-8"),
                partial_mode="trailing-strings",
            )
        except ValueError:
            return None
        if not isinstance(parsed, dict):
            return None
        value = parsed.get(self._target_arg)
        if not isinstance(value, str):
            return None
        if len(value) <= self._emitted_chars:
            return None
        delta = value[self._emitted_chars :]
        self._emitted_chars = len(value)
        return delta

    def reset(self) -> None:
        """Drop accumulated state between ReAct loop iterations."""
        self._buffer = ""
        self._emitted_chars = 0


# Synthetic stream field carrying the native tool-call deltas of the loop's
# non-submit tools, so drafts can stream while the submit reply keeps its own.
TOOL_ARGS_FIELD = "tool_args"


class _ToolArgDraftExtractor:
    """Stream chosen string arguments of the loop's tool calls while the model writes them.

    The ReAct loop only runs a tool once its call is complete, so an
    ``edit_run`` that rewrites a whole file would reach the editor in one
    piece. This decodes the growing call instead: under the text protocol
    from the ``tool_calls`` field's ``{"tool_calls": [{"name", "args"}]}``
    JSON, under native function calling from the per-index deltas
    :class:`NativeToolArgsStreamListener` forwards. Each targeted argument
    comes back as ``(tool, offset, delta)``, where ``offset`` is where the
    delta starts in that call's argument, so the reader can rebuild the draft
    and start over on a new call (offset 0).

    One instance per agent turn; state resets on each field's last chunk.
    """

    def __init__(self, targets: dict[str, str], *, native: bool, keys: dict[str, str] | None = None) -> None:
        """Bind the extractor to the tool arguments worth streaming.

        Args:
            targets: Tool name mapped to the string argument to stream, e.g.
                ``{"edit_run": "new_code"}``.
            native: Whether the loop uses native function calling.
            keys: Tool name mapped to a second argument naming what the draft
                is for, e.g. ``{"write_file": "path"}``. Such a tool's drafts
                come back as ``"<tool>:<key>"`` and only once the key is
                written, so the reader knows which file they belong to.
        """
        self._targets = targets
        self._keys = keys or {}
        self._native = native
        self._buffer = ""
        self._calls: dict[int, dict[str, str]] = {}
        self._emitted: dict[int, int] = {}

    def _deltas(self, calls: list[tuple[int, str, str]]) -> list[tuple[str, int, str]]:
        """Return the unseen tail of each targeted argument.

        Args:
            calls: ``(position, tool name, argument so far)`` per call.

        Returns:
            ``(tool, offset, delta)`` for every call whose argument grew.
        """
        out: list[tuple[str, int, str]] = []
        for position, name, value in calls:
            emitted = self._emitted.get(position, 0)
            if len(value) <= emitted:
                continue
            out.append((name, emitted, value[emitted:]))
            self._emitted[position] = len(value)
        return out

    def _label(self, name: str, args: dict[str, Any]) -> str | None:
        """Name a call's draft, adding its key argument when the tool has one.

        Args:
            name: The tool name.
            args: The call's arguments decoded so far.

        Returns:
            ``name``, ``"<name>:<key>"``, or ``None`` while the key is still
            missing (the argument after it has not started, so it may be cut).
        """
        key_arg = self._keys.get(name)
        if key_arg is None:
            return name
        key = args.get(key_arg)
        return f"{name}:{key}" if isinstance(key, str) and key else None

    def _text_calls(self) -> list[tuple[int, str, str]]:
        """Decode the targeted arguments out of the text protocol's partial JSON.

        Returns:
            ``(position, tool name, argument so far)`` per targeted call.
        """
        try:
            parsed = jiter.from_json(self._buffer.encode("utf-8"), partial_mode="trailing-strings")
        except ValueError:
            return []
        tool_calls = parsed.get("tool_calls") if isinstance(parsed, dict) else None
        if not isinstance(tool_calls, list):
            return []
        found: list[tuple[int, str, str]] = []
        for position, call in enumerate(tool_calls):
            if not isinstance(call, dict):
                continue
            name = call.get("name")
            args = call.get("args")
            arg = self._targets.get(name) if isinstance(name, str) else None
            if arg is None or not isinstance(args, dict):
                continue
            value = args.get(arg)
            label = self._label(name, args)
            if isinstance(value, str) and label is not None:
                found.append((position, label, value))
        return found

    def _native_calls(self, chunk: str) -> list[tuple[int, str, str]]:
        """Fold one batch of native tool-call deltas in and decode the targeted arguments.

        Args:
            chunk: JSON list of ``{"index", "name", "arguments"}`` deltas.

        Returns:
            ``(index, tool name, argument so far)`` per targeted call touched.
        """
        try:
            deltas = json.loads(chunk)
        except ValueError:
            return []
        touched: list[int] = []
        for delta in deltas if isinstance(deltas, list) else []:
            index = delta.get("index") if isinstance(delta, dict) else None
            if not isinstance(index, int):
                continue
            call = self._calls.setdefault(index, {"name": "", "arguments": ""})
            if delta.get("name"):
                call["name"] = str(delta["name"])
            call["arguments"] += str(delta.get("arguments") or "")
            touched.append(index)
        found: list[tuple[int, str, str]] = []
        for index in dict.fromkeys(touched):
            call = self._calls[index]
            arg = self._targets.get(call["name"])
            if arg is None or not call["arguments"]:
                continue
            try:
                parsed = jiter.from_json(call["arguments"].encode("utf-8"), partial_mode="trailing-strings")
            except ValueError:
                continue
            value = parsed.get(arg) if isinstance(parsed, dict) else None
            label = self._label(call["name"], parsed) if isinstance(parsed, dict) else None
            if isinstance(value, str) and label is not None:
                found.append((index, label, value))
        return found

    def feed(self, chunk: dspy.streaming.StreamResponse) -> list[tuple[str, int, str]]:
        """Take one stream response and return the targeted arguments' new text.

        Args:
            chunk: A non-reasoning ``StreamResponse`` from the loop.

        Returns:
            ``(tool, offset, delta)`` per targeted argument that grew; empty
            for chunks of any other field.
        """
        field = TOOL_ARGS_FIELD if self._native else "tool_calls"
        if chunk.signature_field_name != field:
            return []
        if self._native:
            out = self._deltas(self._native_calls(chunk.chunk)) if chunk.chunk else []
        else:
            self._buffer += chunk.chunk or ""
            out = self._deltas(self._text_calls()) if chunk.chunk else []
        if chunk.is_last_chunk:
            self.reset()
        return out

    def reset(self) -> None:
        """Drop accumulated state between ReAct loop iterations."""
        self._buffer = ""
        self._calls = {}
        self._emitted = {}


class NativeToolArgsStreamListener(dspy.streaming.StreamListener):
    """StreamListener that forwards every native tool-call delta of the loop predictor.

    :class:`NativeToolCallStreamListener` keeps only the ``submit`` call, the
    reply. This sibling forwards all calls' ``index`` / ``name`` /
    ``arguments`` fragments, as a JSON list on the synthetic
    :data:`TOOL_ARGS_FIELD`, for :class:`_ToolArgDraftExtractor` to decode
    the drafts of the tools it targets. The turn's ``finish_reason`` marks
    the last chunk so the extractor resets between loop iterations.
    """

    def __init__(self, predict: dspy.Predict, allow_reuse: bool = True):
        """Bind the listener to the loop predictor and mark the synthetic field.

        Args:
            predict: ReAct's inner loop predictor.
            allow_reuse: Whether the listener can fire more than once (required
                for ReAct's inner predict, which fires per loop iteration).
        """
        super().__init__(signature_field_name=TOOL_ARGS_FIELD, predict=predict, allow_reuse=allow_reuse)
        self.predict_name = TOOL_ARGS_FIELD

    def receive(self, chunk: object) -> dspy.streaming.StreamResponse | None:
        """Re-emit a LiteLLM chunk's tool-call deltas as one stream response.

        Args:
            chunk: A raw LiteLLM streaming chunk.

        Returns:
            A :class:`dspy.streaming.StreamResponse` whose ``chunk`` is the JSON
            list of deltas, marked ``is_last_chunk`` on the turn's
            ``finish_reason``, or ``None`` when the chunk carries no tool call.
        """
        try:
            choice = chunk.choices[0]
        except (AttributeError, IndexError, TypeError):
            return None
        delta = getattr(choice, "delta", None)
        deltas = []
        for call in (getattr(delta, "tool_calls", None) if delta is not None else None) or []:
            function = getattr(call, "function", None)
            deltas.append(
                {
                    "index": getattr(call, "index", None),
                    "name": getattr(function, "name", None) if function is not None else None,
                    "arguments": getattr(function, "arguments", None) if function is not None else None,
                }
            )
        last = bool(getattr(choice, "finish_reason", None))
        if not deltas and not last:
            return None
        return dspy.streaming.StreamResponse(
            predict_name=self.predict_name,
            signature_field_name=TOOL_ARGS_FIELD,
            chunk=json.dumps(deltas) if deltas else "",
            is_last_chunk=last,
        )

    def finalize(self) -> None:
        """No-op — the last chunk rides the turn's finish_reason."""
        return


class ReasoningStreamListener(dspy.streaming.StreamListener):
    """StreamListener subclass that harvests provider reasoning tokens.

    DSPy's built-in ``StreamListener`` only reads ``delta.content``. For
    reasoning-capable providers (Fireworks/MiniMax/DeepSeek/o-series) the
    model's thinking arrives on ``delta.reasoning_content`` or, for native
    MiniMax, on ``delta.reasoning_details`` — channels DSPy drops on the
    floor. This subclass intercepts those and emits them as synthetic
    ``StreamResponse`` events with ``signature_field_name = REASONING_FIELD``.

    The listener is manually bound to a specific ``Predict`` via the
    ``predict=...`` constructor arg so DSPy's auto-resolution never tries
    to find the synthetic field on the signature (it doesn't exist there).
    ``allow_reuse=True`` is required when bound to ReAct's inner predict
    because that predict fires once per loop iteration.
    """

    def __init__(self, predict: dspy.Predict, allow_reuse: bool = False):
        """Bind the listener to a specific Predict and mark the synthetic reasoning field.

        Args:
            predict: The :class:`dspy.Predict` whose reasoning chunks should be intercepted.
            allow_reuse: Whether the listener can fire more than once (required for
                ReAct's inner predict, which fires per loop iteration).
        """
        super().__init__(
            signature_field_name=REASONING_FIELD,
            predict=predict,
            allow_reuse=allow_reuse,
        )
        self.predict_name = REASONING_FIELD

    def receive(self, chunk: object) -> dspy.streaming.StreamResponse | None:
        """Extract a reasoning token from a LiteLLM chunk and re-emit it as a stream response.

        Args:
            chunk: A raw LiteLLM streaming chunk.

        Returns:
            A synthetic :class:`dspy.streaming.StreamResponse` carrying the
            reasoning token, or ``None`` when no token is present.
        """
        token = _extract_reasoning_token(chunk)
        if not token:
            return None
        return dspy.streaming.StreamResponse(
            predict_name=self.predict_name,
            signature_field_name=REASONING_FIELD,
            chunk=token,
            is_last_chunk=False,
        )

    def finalize(self) -> None:
        """No-op — reasoning has no terminal aggregate event.

        Kept as an explicit method so the listener satisfies the streamer's
        aggregator protocol alongside peers that do emit a final summary.
        """
        return


class NativeToolCallStreamListener(dspy.streaming.StreamListener):
    """StreamListener that harvests a native ``submit`` tool call's arguments.

    When native function calling is active, ReActV2's inner predict emits its
    tool calls through the provider API rather than as text in the ``tool_calls``
    field, so DSPy's built-in listener — which reads only ``delta.content`` —
    never sees them. This subclass reads the raw ``delta.tool_calls`` deltas,
    tracks the call index whose function name is ``submit``, and re-emits that
    call's streaming ``arguments`` JSON as synthetic ``StreamResponse`` events
    on the ``tool_calls`` field. It emits on the same field name the text path
    uses so the reply bridge routes both paths identically; the arguments there
    are the bare ``{<arg>: ...}`` object that :class:`_NativeSubmitArgExtractor`
    decodes. Parallel non-submit tool calls stream on other indices and are
    ignored — they execute, they are not the reply.

    ``allow_reuse=True`` is required because the inner predict fires once per
    ReAct loop iteration; per-turn index tracking is reset when a turn's
    ``finish_reason`` arrives.
    """

    def __init__(self, predict: dspy.Predict, allow_reuse: bool = True):
        """Bind the listener to a specific Predict and mark the tool_calls field.

        Args:
            predict: The :class:`dspy.Predict` (ReAct's inner loop predictor)
                whose native tool-call deltas should be intercepted.
            allow_reuse: Whether the listener can fire more than once (required
                for ReAct's inner predict, which fires per loop iteration).
        """
        super().__init__(
            signature_field_name="tool_calls",
            predict=predict,
            allow_reuse=allow_reuse,
        )
        self.predict_name = "tool_calls"
        self._submit_index: int | None = None
        self._pending: dict[int, str] = {}

    def _consume_tool_calls(self, tool_calls: list) -> str | None:
        """Pull the submit call's newest ``arguments`` fragment from a delta's tool calls.

        Args:
            tool_calls: The ``delta.tool_calls`` list from one streaming chunk —
                objects carrying ``index``, ``function.name`` and streaming
                ``function.arguments`` fragments.

        Returns:
            The concatenated new ``arguments`` text for the submit call in this
            delta, or ``None`` when the delta contributes nothing to it. Args
            arriving before the submit index is known are buffered per index and
            flushed once that index reveals the ``submit`` name.
        """
        out: list[str] = []
        for call in tool_calls:
            index = getattr(call, "index", None)
            function = getattr(call, "function", None)
            name = getattr(function, "name", None) if function is not None else None
            args = getattr(function, "arguments", None) if function is not None else None
            if name == "submit":
                self._submit_index = index
                buffered = self._pending.pop(index, "")
                if buffered:
                    out.append(buffered)
            if args:
                if index == self._submit_index:
                    out.append(args)
                elif self._submit_index is None and index is not None:
                    self._pending[index] = self._pending.get(index, "") + args
        return "".join(out) if out else None

    def receive(self, chunk: object) -> dspy.streaming.StreamResponse | None:
        """Extract the submit call's argument delta from a LiteLLM chunk.

        Args:
            chunk: A raw LiteLLM streaming chunk.

        Returns:
            A synthetic :class:`dspy.streaming.StreamResponse` on the
            ``tool_calls`` field carrying the submit call's newest ``arguments``
            text, marked ``is_last_chunk`` when the turn's ``finish_reason``
            arrives (so the reply bridge resets between loop iterations), or
            ``None`` when the chunk carries nothing for the submit call.
        """
        try:
            choice = chunk.choices[0]
        except (AttributeError, IndexError, TypeError):
            return None
        delta = getattr(choice, "delta", None)
        tool_calls = getattr(delta, "tool_calls", None) if delta is not None else None
        emitted = self._consume_tool_calls(tool_calls) if tool_calls else None
        if getattr(choice, "finish_reason", None):
            terminal = dspy.streaming.StreamResponse(
                predict_name=self.predict_name,
                signature_field_name="tool_calls",
                chunk=emitted or "",
                is_last_chunk=True,
            )
            self._submit_index = None
            self._pending = {}
            return terminal
        if emitted:
            return dspy.streaming.StreamResponse(
                predict_name=self.predict_name,
                signature_field_name="tool_calls",
                chunk=emitted,
                is_last_chunk=False,
            )
        return None

    def finalize(self) -> None:
        """No-op — the reply's terminal chunk rides the turn's finish_reason.

        Kept explicit so the listener satisfies the streamer's aggregator
        protocol alongside peers that emit a final summary.
        """
        return


class ReactReplyStream:
    """Stream a ReActV2 program's reply out of its ``submit`` tool call.

    ReActV2 carries the reply as a ``submit`` tool-call argument streamed on the
    inner ``react`` predictor's ``tool_calls`` field, either as provider tool-call
    deltas (native function calling) or as DSPy's text protocol. This hides that
    difference so the agent loops stay identical: build ``listeners()`` once,
    then feed every non-reasoning ``StreamResponse`` through ``reply_delta``.
    """

    def __init__(
        self,
        program: dspy.Module,
        reply_field: str,
        lm: dspy.LM | None = None,
        *,
        tool_drafts: dict[str, str] | None = None,
        tool_draft_keys: dict[str, str] | None = None,
    ):
        """Bind to a constructed ReActV2 program and the signature's reply field.

        Args:
            program: The constructed ReActV2 program (or subclass).
            reply_field: Output field carrying the user-visible reply, a
                ``submit`` argument.
            lm: The LM the loop runs on; defaults to the active ``dspy.settings.lm``.
            tool_drafts: Tool name mapped to a string argument to stream while
                the call is written (see :meth:`tool_draft_deltas`); ``None``
                streams none.
            tool_draft_keys: Tool name mapped to the argument naming its
                draft's target (see :class:`_ToolArgDraftExtractor`).
        """
        self._program = program
        lm = lm or getattr(dspy.settings, "lm", None)
        # The adapter only sends native tools when the LM also claims support;
        # otherwise it silently falls back to the text protocol, and a native
        # listener would never see the reply.
        self._native = native_tool_calling_active() and (lm is None or bool(lm.supports_function_calling))
        self._stream_field = "tool_calls"
        if self._native:
            self._extractor: _NativeSubmitArgExtractor | _SubmitArgExtractor = _NativeSubmitArgExtractor(reply_field)
        else:
            self._extractor = _SubmitArgExtractor(
                reply_field,
                exclusive_submit=bool(getattr(self._program.react, "_serial_tool_calls", False)),
            )
        self._drafts = (
            _ToolArgDraftExtractor(tool_drafts, native=self._native, keys=tool_draft_keys) if tool_drafts else None
        )

    @property
    def native(self) -> bool:
        """Whether the loop's tool calls stream as native deltas rather than DSPy's text protocol."""
        return self._native

    def listeners(self, *, tool_args: bool = False) -> list[dspy.streaming.StreamListener]:
        """Return the reply + reasoning stream listeners for this program.

        Args:
            tool_args: Also forward every native tool-call delta on
                :data:`TOOL_ARGS_FIELD`, as streamed tool drafts do, for a
                caller that reads the calls while they are written.

        Returns:
            With native function calling: a :class:`NativeToolCallStreamListener`
            reading the provider's ``tool_calls`` deltas. With the text protocol:
            a built-in ``tool_calls`` listener bound to the reused inner
            predictor. Both carry the same reasoning listener on the loop
            predictor.
        """
        if self._native:
            reply_listener: dspy.streaming.StreamListener = NativeToolCallStreamListener(
                predict=self._program.react,
                allow_reuse=True,
            )
        else:
            reply_listener = dspy.streaming.StreamListener(
                signature_field_name="tool_calls",
                predict=self._program.react,
                allow_reuse=True,
            )
        listeners = [
            reply_listener,
            ReasoningStreamListener(predict=self._program.react, allow_reuse=True),
        ]
        if (self._drafts is not None or tool_args) and self._native:
            listeners.append(NativeToolArgsStreamListener(predict=self._program.react, allow_reuse=True))
        return listeners

    def tool_draft_deltas(self, chunk: dspy.streaming.StreamResponse) -> list[tuple[str, int, str]]:
        """Return the new text of the streamed tool arguments in a chunk.

        Args:
            chunk: A non-reasoning ``StreamResponse`` from the wrapped program.

        Returns:
            ``(tool, offset, delta)`` per targeted argument that grew, where
            ``offset`` is the delta's start in that call's argument; empty
            when no drafts are streamed or the chunk carries none.
        """
        return self._drafts.feed(chunk) if self._drafts is not None else []

    def reply_delta(self, chunk: dspy.streaming.StreamResponse) -> str | None:
        """Return the newly streamed reply text from a chunk, or ``None``.

        The chunk is partial ``submit`` JSON decoded incrementally. Chunks for
        any field other than ``tool_calls`` return ``None``.

        Args:
            chunk: A non-reasoning ``StreamResponse`` from the wrapped program.

        Returns:
            The new reply substring since the last call, or ``None`` when this
            chunk carries no fresh reply content.
        """
        if chunk.signature_field_name != self._stream_field:
            return None
        if isinstance(self._extractor, _SubmitArgExtractor):
            delta = self._extractor.feed(
                chunk.chunk,
                final=chunk.is_last_chunk,
            )
        else:
            delta = self._extractor.feed(chunk.chunk)
        if chunk.is_last_chunk:
            self._extractor.reset()
        return delta

    def take_retraction(self) -> bool:
        """Return whether the reply streamed so far must be taken back.

        Only a serial-tool loop on the text protocol streams a reply it can
        later drop (a submit written before a sibling tool call). The caller
        clears the shown reply when this returns ``True``.

        Returns:
            ``True`` once per retraction, else ``False``.
        """
        if isinstance(self._extractor, _SubmitArgExtractor):
            return self._extractor.take_retraction()
        return False


def _build_agent_lm(
    model_name_override: str | None = None,
    reasoning_effort: str | None = None,
    max_tokens: int = 32000,
    model_config: ModelConfig | None = None,
) -> dspy.LM:
    """Construct the LM used by the code agent from global settings.

    Args:
        model_name_override: Optional LiteLLM id chosen by the caller (the
            interview composer's model menu); ``None`` runs the configured
            code-agent model.
        reasoning_effort: Explicit effort level for the chosen model; ``None``
            keeps the model's default.
        max_tokens: Completion budget, reasoning included; short-answer
            callers (the onboarding intake) lower it to bound latency and cost.
        model_config: Optional fully resolved config that wins over
            ``model_name_override``. The Mac app passes a vault-backed BYOK
            copy here, so the key stays out of the model id the turn reports.

    Reasoning knobs we send, by provider:

    - **Native MiniMax** (``minimax/...`` or a MiniMax base_url): forward
      ``extra_body={"reasoning_split": true}`` so the provider emits its
      interleaved ``<think>`` reasoning in a clean ``reasoning_details``
      channel. Thinking depth is always max on this endpoint — no knob.
    - **OpenRouter MiniMax** (``openrouter/minimax/...``, the shipped default)
      **and Fireworks-hosted MiniMax** (``fireworks_ai/.../minimax-*``):
      reasoning arrives inline in the assistant content as ``<think>…</think>``
      blocks. Neither host honours ``reasoning_split``, so we send nothing.
    - **Everything else** (``openai/gpt-4o-mini`` etc.): no reasoning knob.

    Returns:
        A configured :class:`dspy.LM` instance for the code agent.
    """
    if model_config is not None:
        config = model_config.model_copy(update={"max_tokens": max_tokens})
        config = apply_reasoning_effort(config, reasoning_effort)
        return build_language_model(apply_model_reasoning_config(config), disable_cache=True)
    model_name = model_name_override or agent_model_id(settings.code_agent_model)
    lower = model_name.lower()
    extra: dict = {}
    is_native_minimax = lower.startswith("minimax/") or (
        "minimax" in lower and "fireworks" not in lower and "openrouter" not in lower
    )
    if is_native_minimax:
        extra["extra_body"] = {"reasoning_split": True}
    subscription = parse_subscription_model_id(model_name)
    config = ModelConfig(
        name=subscription[1] if subscription else model_name,
        token_source="subscription" if subscription else None,
        cli_provider=subscription[0] if subscription else None,
        base_url=None if subscription else settings.code_agent_base_url or None,
        # Reasoning models spend their thinking tokens against this same
        # budget, and on real datasets the reasoning alone can run
        # thousands of tokens; keep enough headroom that code is never cut
        # off mid-artifact (a truncation costs a repair LLM call plus two
        # more multi-second subprocess validations). A chat step that rewrites
        # a whole scorer often drafts it in its reasoning first and then emits
        # it again as the tool argument, which overran 16000.
        max_tokens=max_tokens,
        extra=extra,
    )
    config = apply_reasoning_effort(config, reasoning_effort)
    # A caller-chosen model can be anything in the catalog — run it through
    # the generic reasoning-model knobs (mandatory temperature/effort for
    # OpenAI reasoning models); the configured default keeps its hand-tuned
    # MiniMax setup untouched.
    if model_name_override:
        config = apply_model_reasoning_config(config)
    return build_language_model(config, disable_cache=True)
