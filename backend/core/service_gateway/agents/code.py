"""Shared streaming and model helpers for Skynet's ReAct agents.

The generalist agent, the Pi bridge, the interviews and the tagger build
on these pieces: the reasoning stream listener
(:class:`ReasoningStreamListener`), the native ``submit`` argument
extractor, the error formatting for a failed turn, the reply-language
lookup and :func:`_build_agent_lm`.

The agents run on whatever LiteLLM-compatible model is configured via
``settings.code_agent_model`` (default: the model catalog's best-value
pick). Users can point it at an internal gateway via
``CODE_AGENT_BASE_URL``.
"""

from __future__ import annotations

import logging

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


class _NativeSubmitArgExtractor:
    """Recover token-level streaming of a submit argument under native function calling.

    With native function calling the provider streams the ``submit`` tool's
    arguments directly, as raw JSON of the shape ``{<arg>: "..."}``. This
    parses the partial JSON and returns the new characters of
    ``parsed[target_arg]`` as they arrive.

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
