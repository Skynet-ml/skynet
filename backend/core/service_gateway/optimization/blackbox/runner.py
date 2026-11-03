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
"""

from __future__ import annotations

import base64
import inspect
import io
import json
import os
import ssl
import sys
import time
import types
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
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
# Frames one ``log()`` event on stderr; the host turns each into a run-log line.
EVENT_PREFIX = "APP_SCORER_EVENT "
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
        "Helpers for scorer code: llm(prompt, input=None, images=None), Image(...) and log(event, **fields).",
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


def accepts_case(fn: Callable[..., Any]) -> bool:
    """Return True when ``fn`` takes a second positional argument for the case.

    Args:
        fn: The scorer callable.

    Returns:
        True for ``fn(candidate, case)`` shapes, False for ``fn(candidate)``.
    """
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return True
    positional = [p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL)]
    return len(positional) >= 2 or any(p.kind == p.VAR_POSITIONAL for p in positional)


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
    """Compare a user model name with the model bound to its gateway route."""

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


def main(argv: list[str] | None = None) -> int:
    """Run one scorer call from ``<call_dir>/input.json`` into ``<call_dir>/output.json``.

    The scorer source comes from the box's code file unless the input carries it.

    Args:
        argv: Command-line arguments; ``sys.argv`` when unset.

    Returns:
        The process exit code: 0 whenever the output was written.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        sys.stderr.write("usage: app_runner.py <call_dir>\n")
        return 2
    call_dir = Path(args[0])
    payload = json.loads((call_dir / INPUT_FILE).read_text(encoding="utf-8"))
    if "code" not in payload:
        code_path = Path(__file__).resolve().with_name(CODE_FILE)
        payload["code"] = code_path.read_text(encoding="utf-8") if code_path.is_file() else ""
    result = run_call(payload)
    partial = call_dir / (OUTPUT_FILE + ".part")
    partial.write_text(json.dumps(result), encoding="utf-8")
    partial.replace(call_dir / OUTPUT_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
