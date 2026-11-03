"""DSPy language model factory.

Builds ``dspy.LM`` instances from ``ModelConfig`` while filtering out
``None`` optional fields so LiteLLM does not reject the call.
"""

import contextlib
import logging
import threading
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

import dspy
import dspy.clients.lm as dspy_lm
import httpx
import litellm

from ..api.model_catalog import default_effort_for
from ..billing.cli_dispatch import CLI_EFFORT_KEY, local_subscription_route
from ..billing.model_gateway import ROUTE_KEY, raise_gateway_stop
from ..billing.operation_pricing import UnpricedOperationError
from ..billing.pricing import ModelUsage
from ..billing.runtime import UsagePendingError
from ..billing.signals import BudgetReached
from ..config import app_env, settings
from ..exceptions import InfrastructureInterruptionError, ServiceError
from ..local.cli_catalog import parse_subscription_model_id
from ..models import ModelConfig

try:
    from litellm.litellm_core_utils.streaming_handler import CustomStreamWrapper
except ImportError:  # pragma: no cover - depends on litellm internals
    CustomStreamWrapper = None

logger = logging.getLogger("app.service_gateway.language_models")

_DEFAULT_REASONING_MAX_TOKENS = 4000
"""Floor on ``max_tokens`` for chat-style replies. Below this a reasoning model
can truncate mid-``tool_calls`` and emit a malformed call that dspy's ToolCalls
parser rejects with a ValidationError."""

_OPENAI_REASONING_MAX_TOKENS = 16000
"""Mandatory ``max_tokens`` floor for OpenAI reasoning models — dspy validates
``max_tokens >= 16000`` (and ``temperature == 1.0``) at ``dspy.LM`` init."""

_TRANSIENT_GATEWAY_CODES = frozenset({"provider_interrupted", "provider_transport_interruption"})

SUBSCRIPTION_ROLE_KEY = "_skynet_subscription_role"
"""``ModelConfig.extra`` key naming a subscription model's loopback route role.
It never reaches the client; the default is ``"agent"``."""


def _gateway_failure_code(error: BaseException) -> str | None:
    """Read only a structured error code returned by Skynet's trusted model relay.

    Args:
        error: Provider SDK exception, possibly wrapping an HTTP response.

    Returns:
        Stable relay error code, or None when the provider SDK discarded it.
    """
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        response = getattr(current, "response", None)
        documents: list[Any] = []
        if isinstance(response, httpx.Response):
            with contextlib.suppress(ValueError):
                documents.append(response.json())
        for attribute in ("body", "error", "message"):
            value = getattr(current, attribute, None)
            if isinstance(value, Mapping):
                documents.append(value)
        for document in documents:
            detail = document.get("error") if isinstance(document.get("error"), Mapping) else document
            code = detail.get("type") or detail.get("code")
            if isinstance(code, str):
                return code
        pending.extend(
            nested for nested in (current.__cause__, current.__context__) if isinstance(nested, BaseException)
        )
    return None


def _is_openai_reasoning_model(model_name: str) -> bool:
    """Detect OpenAI reasoning models (gpt-5.x, o1/o3/o4 series).

    These require ``temperature=1.0`` and ``max_tokens >= 16000`` at ``dspy.LM``
    init; they also emit thinking on the ``reasoning_content`` channel when
    ``reasoning_effort`` is set. Fireworks/OpenRouter hosts of these models
    don't share the same constraints, so we scope to the ``openai/`` prefix.

    Args:
        model_name: The fully-qualified model identifier.

    Returns:
        True when ``model_name`` is an OpenAI-hosted reasoning model.
    """
    lower = model_name.lower()
    if not lower.startswith("openai/"):
        return False
    tail = lower.removeprefix("openai/")
    return tail.startswith(("gpt-5", "o1", "o3", "o4"))


def apply_model_reasoning_config(config: ModelConfig) -> ModelConfig:
    """Return a copy of ``config`` with model-specific reasoning defaults applied.

    Mirrors the provider knobs the production generalist agent relies on so any
    code path that builds a student/agent LM from a bare ``ModelConfig`` gets a
    safe ``max_tokens`` floor and the right reasoning extras — without which a
    minimax/reasoning model with no ``max_tokens`` truncates into malformed
    ``tool_calls`` (a dspy ToolCalls ValidationError).

    Defaults, by provider:

    - **Native MiniMax** (``minimax/...``): ``extra_body={"reasoning_split": true}``
      surfaces the interleaved ``<think>`` channel; ``max_tokens`` floored at 4000.
    - **OpenAI reasoning models** (``openai/gpt-5.*``, ``openai/o1|o3|o4*``):
      ``reasoning_effort="medium"``, ``temperature=1.0``, ``max_tokens`` floored
      at 16000.
    - **Everything else** (incl. Fireworks/OpenRouter MiniMax): ``max_tokens``
      floored at 4000, no reasoning knob.

    Caller-supplied values win: a larger ``max_tokens`` is never shrunk, an
    explicit ``temperature`` is never overwritten, and ``config.extra`` overrides
    the model-specific extras on conflict.

    Args:
        config: Provider-agnostic model configuration to normalize.

    Returns:
        A new ``ModelConfig`` with the reasoning defaults merged in.
    """
    lower = config.name.lower()
    model_extra: dict[str, object] = {}
    floor = _DEFAULT_REASONING_MAX_TOKENS
    temperature = config.temperature

    is_native_minimax = lower.startswith("minimax/") or (
        "minimax" in lower and "fireworks" not in lower and "openrouter" not in lower
    )
    if is_native_minimax:
        model_extra["extra_body"] = {"reasoning_split": True}
    elif _is_openai_reasoning_model(config.name):
        # A CLI route forwards its effort to the CLI, which has its own
        # default, so only the user's pick may reach it.
        if config.token_source != "subscription":
            model_extra["reasoning_effort"] = "medium"
        floor = _OPENAI_REASONING_MAX_TOKENS
        if temperature is None:
            temperature = 1.0

    max_tokens = floor if config.max_tokens is None else max(config.max_tokens, floor)
    return config.model_copy(
        update={
            "max_tokens": max_tokens,
            "temperature": temperature,
            "extra": {**model_extra, **config.extra},
        }
    )


def apply_reasoning_effort(config: ModelConfig, effort: str | None) -> ModelConfig:
    """Stamp a caller-chosen reasoning effort onto ``config``.

    The effort lands in ``extra`` so it survives
    :func:`apply_model_reasoning_config` — caller extras win over the
    model-specific defaults there, so an explicit choice overrides the
    ``"medium"`` default applied to OpenAI reasoning models.

    Args:
        config: The model configuration to annotate.
        effort: LiteLLM ``reasoning_effort`` level chosen by the user, or
            ``None``/empty to leave the model's default behaviour untouched.

    Returns:
        ``config`` unchanged when ``effort`` is falsy, else a copy with the
        effort merged into ``extra``.
    """
    if not effort:
        return config
    return config.model_copy(update={"extra": {**config.extra, "reasoning_effort": effort}})


def _apply_managed_gateway(lm_kwargs: dict[str, object]) -> None:
    """Route a managed call through the self-hosted LiteLLM proxy when configured.

    A BYOK call already carries the user's ``api_key`` (stamped onto the
    ModelConfig by the run-path bridge); a managed call does not — so the
    presence of ``api_key`` distinguishes the two here without threading the
    token source down to the LM factory. When a proxy URL is configured and the
    call is managed (no ``api_key``) and not already pinned to a specific
    endpoint (no ``base_url``), point it at the proxy via litellm's
    ``litellm_proxy/`` provider and authenticate with the managed virtual key,
    so all platform inference flows through one metered seam. A no-op when no
    proxy is configured, leaving the default dspy → provider path unchanged;
    BYOK and endpoint-pinned calls are never rerouted.

    Args:
        lm_kwargs: The ``dspy.LM`` kwargs assembled so far, mutated in place.
    """
    if "api_key" in lm_kwargs or "base_url" in lm_kwargs:
        return
    proxy_url = settings.litellm_proxy_url
    if not proxy_url:
        return
    lm_kwargs["base_url"] = proxy_url
    if settings.litellm_proxy_api_key is not None:
        lm_kwargs["api_key"] = settings.litellm_proxy_api_key.get_secret_value()
    # Address the proxy through litellm's dedicated ``litellm_proxy/`` provider so
    # the OpenRouter slug reaches it intact. Without the prefix litellm resolves
    # the bare provider segment itself (``openai/gpt-4o-mini`` -> openai provider,
    # sending just ``gpt-4o-mini``), and the proxy's ``*`` -> ``openrouter/*``
    # wildcard then can't reconstruct a real slug. A leading ``openrouter/`` is
    # dropped first because that wildcard re-adds it — otherwise an already
    # OpenRouter-prefixed id (``openrouter/minimax/...``) would double-prefix.
    model = lm_kwargs.get("model")
    if isinstance(model, str):
        lm_kwargs["model"] = f"litellm_proxy/{model.removeprefix('openrouter/')}"


def _translate_gateway_reasoning(lm_kwargs: dict[str, object]) -> None:
    """Mirror reasoning controls into OpenRouter's native ``reasoning`` param.

    Calls that reach OpenRouter (directly via an ``openrouter/`` id, or through
    the LiteLLM proxy whose wildcard fronts OpenRouter) lose the OpenAI-style
    ``reasoning_effort`` kwarg: LiteLLM doesn't map it onto OpenRouter's
    ``reasoning`` request param — so a user-picked effort was a no-op and
    opt-in thinking models (Anthropic, Gemini) never streamed reasoning.
    The effort passes through verbatim: OpenRouter now lists ``max`` for
    Claude as well, and the catalog tells clients each model's exact levels.
    ``summary="auto"`` explicitly opts into streamed reasoning summaries for
    models such as OpenAI's GPT-5 family, which do not expose raw chain-of-thought
    tokens.

    The kwarg is kept (aligned to the native value) rather than popped: dspy's
    ``Reasoning`` signature field injects ``reasoning_effort="low"`` at call
    time whenever the LM carries no effort of its own, and OpenRouter rejects
    requests whose ``reasoning_effort`` and ``reasoning.effort`` disagree.

    Args:
        lm_kwargs: The ``dspy.LM`` kwargs assembled so far, mutated in place.
    """
    model = lm_kwargs.get("model")
    if not isinstance(model, str) or not model.startswith(("litellm_proxy/", "openrouter/")):
        return
    effort = lm_kwargs.get("reasoning_effort")
    if not isinstance(effort, str) or not effort:
        return
    mapped = effort
    body = lm_kwargs.get("extra_body")
    merged = dict(body) if isinstance(body, dict) else {}
    native = merged.setdefault("reasoning", {})
    if isinstance(native, dict):
        native.setdefault("effort", mapped)
        native.setdefault("summary", "auto")
        if isinstance(native.get("effort"), str) and native["effort"]:
            mapped = native["effort"]
    lm_kwargs["reasoning_effort"] = mapped
    lm_kwargs["extra_body"] = merged


# One lock for every MeteredLM: ``LM.copy()`` shallow-copies instances, so an
# instance-level lock would be shared anyway, and contention is a few dozen
# nanoseconds per LM call against a network round-trip.
_USAGE_LOCK = threading.Lock()


@dataclass
class LmUsageTotals:
    """Running usage aggregate for one job LM and every internal copy of it.

    ``total_found``/``split_found`` distinguish "zero usage" from "usage not
    tracked" (mocked providers), mirroring the ``None`` contract of
    :func:`total_tokens_from_history` / :func:`usage_by_model_from_history`.
    """

    calls: int = 0
    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_found: bool = False
    split_found: bool = False
    reported_cost_usd: float = 0.0
    reported_input_tokens: int = 0
    reported_output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    model_ms: int = 0

    def add_split(self, usage: object, split: tuple[int, int], *, subscription: bool = False) -> None:
        """Fold one call's input/output split and its cost evidence into the totals.

        A call whose provider reported its own cost adds that cost and marks its
        tokens as covered; otherwise its cache and reasoning counts are kept so
        the charge can price them at their own rates.

        Args:
            usage: The call's ``usage`` block.
            split: The call's ``(input_tokens, output_tokens)``.
            subscription: The user's own CLI subscription paid for the call, so
                it is folded at zero cost whatever its ``usage`` carries.
        """
        self.input_tokens += split[0]
        self.output_tokens += split[1]
        self.split_found = True
        self.model_ms += _count(_field(usage, "model_ms"))
        cost = 0.0 if subscription else _usage_reported_cost(usage)
        if cost is not None:
            self.reported_cost_usd += cost
            self.reported_input_tokens += split[0]
            self.reported_output_tokens += split[1]
            return
        cache_read, cache_write, reasoning = _usage_token_details(usage)
        self.cache_read_tokens += cache_read
        self.cache_write_tokens += cache_write
        self.reasoning_tokens += reasoning

    def model_usage(self, model: str) -> ModelUsage:
        """Return these totals as a priced-usage row for ``model``.

        Args:
            model: The model id the totals belong to.

        Returns:
            The :class:`ModelUsage` carrying tokens and any reported cost.
        """
        return ModelUsage(
            model=model,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            reported_cost_usd=self.reported_cost_usd,
            reported_input_tokens=self.reported_input_tokens,
            reported_output_tokens=self.reported_output_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens,
            reasoning_tokens=self.reasoning_tokens,
        )


# None (the default everywhere, including the API process) means unmetered —
# the gate costs nothing until a job child installs it.
_job_lm_gate: threading.BoundedSemaphore | None = None


def activate_job_lm_budget(max_concurrency: int) -> None:
    """Install a process-wide ceiling on concurrent LM calls.

    Called once per job child (``run_service_in_subprocess``) so grid pair
    threads times GEPA eval threads — plus any user-supplied ``num_threads``
    — can never put more than ``max_concurrency`` LM calls in flight at once.
    Excess callers block in ``MeteredLM.forward`` until a permit frees; the
    per-call timeout still bounds each held permit, so the gate cannot wedge
    a run. Only the sync path is gated: optimizers evaluate on threads, and
    the async agent/serve surfaces run in the API process where no budget is
    installed.

    Args:
        max_concurrency: Ceiling on in-flight LM calls; ``<= 0`` removes the
            gate.
    """
    global _job_lm_gate
    _job_lm_gate = threading.BoundedSemaphore(max_concurrency) if max_concurrency > 0 else None


# None outside a job child: the API process builds LMs for every interactive
# turn and must not pin them. A job child activates it so a failed or killed
# run can still report what it spent (see job_usage_snapshot).
_job_lm_registry: "list[MeteredLM] | None" = None


def activate_job_usage_registry() -> None:
    """Start tracking every ``MeteredLM`` built in this process from now on.

    Called once per job child, next to :func:`activate_job_lm_budget`. The
    optimizer only reports usage in its final result; a run that fails, is
    cancelled, or times out never produces one, so the child reports this
    registry's running totals with each progress and error event instead.
    """
    global _job_lm_registry
    _job_lm_registry = []


def job_usage_snapshot() -> list[dict[str, Any]]:
    """Return the usage every tracked LM has accrued so far, as result-style rows.

    Rows have the shape of a run result's ``usage_by_model`` entries, so the
    worker prices a partial run exactly like a finished one. Never raises: a
    snapshot is best-effort telemetry riding on an event.

    Returns:
        One row per model with tracked usage, or ``[]`` when no registry is
        active or nothing was tracked.
    """
    registry = _job_lm_registry
    if not registry:
        return []
    try:
        usages = model_usages_from_history(*list(registry)) or []
    except Exception:
        logger.debug("job usage snapshot failed", exc_info=True)
        return []
    return [asdict(usage) for usage in usages]


class MeteredLM(dspy.LM):
    """``dspy.LM`` that aggregates usage instead of retaining call history.

    A single optimization makes thousands of LM calls, and stock ``dspy.LM``
    keeps each one's full prompt + raw response three times over (per-LM
    ``history``, module-level ``GLOBAL_HISTORY``, per-module history) for up
    to 10,000 entries — tens to hundreds of MB pinned for the whole run, and
    permanently in the API process for agent/tagging calls. The repo only
    ever consumed history as aggregate counts, so ``update_history`` reduces
    each entry to running totals and drops it.

    Because ``LM.copy()`` is a shallow copy, internal copies dspy makes (e.g.
    rollout clones) share the same ``usage_totals`` object — their calls are
    counted too, which plain history misses (``copy()`` resets ``history``).
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        """Construct the LM and attach a fresh usage aggregate."""
        super().__init__(*args, **kwargs)
        self.usage_totals = LmUsageTotals()
        self.subscription = False
        self.last_request_model: str | None = None
        self.last_response_model: str | None = None
        registry = _job_lm_registry
        if registry is not None:
            with _USAGE_LOCK:
                registry.append(self)

    @property
    def supports_function_calling(self) -> bool:
        """Report native tool-call support, judged on the model behind the proxy.

        LiteLLM's capability map has no ``litellm_proxy/`` entries, so a managed
        LM would report no support and DSPy would drop native tools for every
        managed model. The proxy fronts OpenRouter, so ask about that id.

        A subscription model reports no support: the local CLI bridge answers
        in plain text and never returns native ``tool_calls``, so DSPy must use
        its text tool protocol (SKY-245).

        Returns:
            Whether the underlying model supports native function calling.
        """
        if self.subscription:
            return False
        if isinstance(self.model, str) and self.model.startswith("litellm_proxy/"):
            return bool(litellm.supports_function_calling(model=f"openrouter/{canonical_model_id(self.model)}"))
        return super().supports_function_calling

    def forward(self, *args: object, **kwargs: object) -> object:
        """Run the LM call, honouring the job-child LM concurrency budget.

        Args:
            *args: Positional arguments forwarded to ``dspy.LM.forward``.
            **kwargs: Keyword arguments forwarded to ``dspy.LM.forward``.

        Returns:
            The upstream ``dspy.LM.forward`` result.
        """
        try:
            gate = _job_lm_gate
            if gate is None:
                return super().forward(*args, **kwargs)
            with gate:
                return super().forward(*args, **kwargs)
        except Exception as error:
            route = getattr(self, "budget_route", None)
            if route:
                raise_gateway_stop(route)
                status = getattr(error, "status_code", None)
                if status == 424:
                    raise UsagePendingError("Provider usage must settle before this operation can continue.") from error
                if status == 402 and "budget_reached" in str(error):
                    raise BudgetReached() from error
                if _gateway_failure_code(error) in _TRANSIENT_GATEWAY_CODES:
                    raise InfrastructureInterruptionError("The model provider transport was interrupted.") from error
            raise

    def update_history(self, entry: object) -> None:
        """Fold one call's usage into the running totals and discard the entry.

        Args:
            entry: The history dict stock dspy would have appended; only its
                ``usage`` block and model ids are read.
        """
        # The served-model reveal reads just these two fields of the entry
        # this class otherwise drops (see served_model_from).
        if isinstance(entry, dict):
            request_model = entry.get("model")
            response_model = entry.get("response_model")
            self.last_request_model = request_model if isinstance(request_model, str) else None
            self.last_response_model = response_model if isinstance(response_model, str) else None
        usage = entry.get("usage") if isinstance(entry, dict) else None
        total = _usage_total_tokens(usage)
        split = _usage_in_out_tokens(usage)
        with _USAGE_LOCK:
            totals = self.usage_totals
            totals.calls += 1
            if total is not None:
                totals.total_tokens += total
                totals.total_found = True
            if split is not None:
                totals.add_split(usage, split, subscription=self.subscription)


def lm_call_count(language_model: object) -> int | None:
    """Count the LM calls ``language_model`` has made.

    Prefers the ``MeteredLM`` aggregate; falls back to ``len(history)`` for
    plain/mocked LMs (and for a dspy line whose LM never invokes the
    ``update_history`` hook, where history still accumulates).

    Args:
        language_model: The LM whose calls to count.

    Returns:
        The call count, or ``None`` when the object tracks neither form.
    """
    totals = getattr(language_model, "usage_totals", None)
    if isinstance(totals, LmUsageTotals) and totals.calls:
        return totals.calls
    history = getattr(language_model, "history", None)
    return len(history) if isinstance(history, list) else None


def _apply_subscription_route(lm_kwargs: dict[str, object], config: ModelConfig, role: str) -> None:
    """Point an API-side subscription model at the local CLI endpoint.

    The effort the user picked goes in :data:`CLI_EFFORT_KEY`, so the CLI
    runs the turn at that effort. With no pick the CLI runs its own default.

    Args:
        lm_kwargs: The ``dspy.LM`` kwargs assembled so far, mutated in place.
        config: A ``token_source == "subscription"`` model config.
        role: The route's role; :data:`THINKING_LABEL_ROLE` runs in the label pool.

    Raises:
        ServiceError: Outside local mode, or when its CLI cannot run the model.
    """
    name = config.name.strip("/").removeprefix("openrouter/")
    parsed = parse_subscription_model_id(name)
    cli, slug = parsed if parsed is not None else (config.cli_provider, name)
    try:
        route = local_subscription_route(slug, cli, role)  # type: ignore[arg-type]
    except UnpricedOperationError as error:
        raise ServiceError(f"Failed to build language model '{config.name}': {error}") from error
    lm_kwargs["model"] = f"litellm_proxy/{route['model']}"
    lm_kwargs["base_url"] = route["url"]
    lm_kwargs["api_key"] = route["token"]
    lm_kwargs.pop("api_base", None)
    effort = lm_kwargs.get("reasoning_effort")
    if isinstance(effort, str) and effort:
        body = lm_kwargs.get("extra_body")
        lm_kwargs["extra_body"] = {**(body if isinstance(body, dict) else {}), CLI_EFFORT_KEY: effort}


def build_language_model(config: ModelConfig, *, disable_cache: bool = False) -> dspy.LM:
    """Construct a DSPy language model from a ModelConfig.

    Only non-None optional fields (temperature, base_url, max_tokens) are
    forwarded to ``dspy.LM`` to avoid LiteLLM rejecting unexpected None values.
    Extra kwargs from ``config.extra`` are merged in last. When a budget-relay
    call sets no output limit, a default one is added so the relay can price it.

    Args:
        config: Provider-agnostic model configuration.
        disable_cache: When True, force ``cache=False`` so retries always hit
            the provider. Used for user-facing surfaces (agents, serve) where
            replaying a cached response would defeat the regenerate action.

    Returns:
        A configured ``dspy.LM`` ready for use by an optimizer.

    Raises:
        ServiceError: When ``dspy.LM`` rejects the configuration.
    """

    model_name = config.name.strip("/")
    # Default per-request timeout guards against a provider that accepts the
    # connection but never sends a response — without it the SSL socket read
    # blocks forever and wedges the whole optimization run. Set before the
    # config.extra merge below so an explicit per-model timeout still wins.
    # dspy defaults to ``num_retries=3``; with our per-call timeout that lets a
    # hung provider burn up to ``(retries + 1) * timeout`` seconds of silence,
    # which meets or exceeds ``job_stall_timeout_seconds`` and trips the run's
    # stall watchdog *before* the call itself errors — turning a recoverable
    # per-call timeout into an opaque whole-run failure (observed: a GEPA
    # reflection call wedged ~1800s and the watchdog killed a 9-hour run). Cap
    # retries so the worst-case attempt sequence finishes under the watchdog
    # with one timeout of margin, keeping the watchdog's documented invariant
    # ("a hung call times out first") true even with retries.
    safe_attempts = max(1, int(settings.job_stall_timeout_seconds // settings.lm_request_timeout_seconds) - 1)
    lm_kwargs: dict[str, object] = {
        "model": model_name,
        "timeout": settings.lm_request_timeout_seconds,
        "num_retries": safe_attempts - 1,
    }
    if config.temperature is not None:
        lm_kwargs["temperature"] = config.temperature
    if config.base_url:
        lm_kwargs["base_url"] = config.base_url
    if config.max_tokens is not None:
        lm_kwargs["max_tokens"] = config.max_tokens
    lm_kwargs.update(config.extra)
    extra_body = lm_kwargs.get("extra_body")
    reasoning_off = (
        isinstance(extra_body, dict)
        and isinstance(extra_body.get("reasoning"), dict)
        and extra_body["reasoning"].get("enabled") is False
    )
    # A side call (thinking label, next-message suggestion) turns reasoning
    # off on purpose; a default effort would turn it back on, and the model
    # would think past the side call's short timeout.
    # A CLI route forwards its effort to the CLI, which has its own default.
    if not lm_kwargs.get("reasoning_effort") and not reasoning_off and config.token_source != "subscription":
        # Auto and the optimizer roles send no effort, which leaves an
        # opt-in thinker (Claude Haiku, the best-value default) not thinking.
        effort = default_effort_for(model_name)
        if effort:
            lm_kwargs["reasoning_effort"] = effort
    budget_route = lm_kwargs.pop(ROUTE_KEY, None)
    subscription_role = str(lm_kwargs.pop(SUBSCRIPTION_ROLE_KEY, "agent"))
    if budget_route is None and config.token_source == "subscription":
        _apply_subscription_route(lm_kwargs, config, subscription_role)
    _apply_managed_gateway(lm_kwargs)
    _translate_gateway_reasoning(lm_kwargs)
    if disable_cache:
        lm_kwargs["cache"] = False
        # ``cache=False`` only disables the client-side cache — the LiteLLM
        # proxy keeps its own Redis cache whose key ignores params like
        # ``reasoning``, so a user-facing turn could replay a stale cached
        # answer (and cached replays drop reasoning deltas entirely). The
        # body-level directive opts this request out server-side too.
        model = lm_kwargs.get("model")
        if isinstance(model, str) and model.startswith("litellm_proxy/"):
            body = lm_kwargs.get("extra_body")
            merged = dict(body) if isinstance(body, dict) else {}
            merged.setdefault("cache", {"no-cache": True})
            lm_kwargs["extra_body"] = merged
    if isinstance(budget_route, dict):
        lm_kwargs["model"] = f"litellm_proxy/{budget_route['model']}"
        lm_kwargs["base_url"] = app_env("APP_BUDGET_RELAY_URL", budget_route["url"])
        lm_kwargs["api_key"] = budget_route["token"]
        lm_kwargs.pop("api_base", None)
        if lm_kwargs.get("max_tokens") is None and lm_kwargs.get("max_completion_tokens") is None:
            # The relay prices every call up front and rejects one with no
            # output limit (SKY-217). Off the relay the provider default stays:
            # a fixed cap would undercut a direct Anthropic thinking budget.
            # dspy refuses an OpenAI reasoning model below 16000 and judges
            # that from the last segment of the model id, so ask it.
            reasoning = dspy_lm._is_openai_reasoning_model(lm_kwargs["model"])
            lm_kwargs["max_tokens"] = _OPENAI_REASONING_MAX_TOKENS if reasoning else _DEFAULT_REASONING_MAX_TOKENS
    try:
        language_model = MeteredLM(**lm_kwargs)
    except ValueError as exc:
        raise ServiceError(f"Failed to build language model '{config.name}': {exc}") from exc

    language_model.budget_route = budget_route
    language_model.declared_model = config.name
    # LiteLLM rebuilds a streamed call's usage and drops the gateway's
    # ``token_source`` mark, so the LM itself carries the zero-cost flag.
    language_model.subscription = config.token_source == "subscription"
    return language_model


def _usage_total_tokens(usage: object) -> int | None:
    """Extract a non-negative total-token count from one history ``usage`` block.

    Args:
        usage: The ``usage`` value from a ``dspy.LM`` history entry — a mapping,
            a provider usage object, or ``None``.

    Returns:
        The entry's total token count, or ``None`` when ``usage`` carries no
        recognizable token fields.
    """
    if usage is None:
        return None
    get = usage.get if isinstance(usage, dict) else lambda key: getattr(usage, key, None)
    total = get("total_tokens")
    if isinstance(total, int | float) and total > 0:
        return int(total)
    parts = [int(p) for p in (get("prompt_tokens"), get("completion_tokens")) if isinstance(p, int | float)]
    return sum(parts) if parts else None


def total_tokens_from_history(*language_models: object) -> int | None:
    """Sum the total token usage recorded across one or more LMs.

    Prefers each LM's ``MeteredLM`` running aggregate; otherwise reads the
    ``usage`` block each stock ``dspy.LM`` stamps onto every ``history`` entry,
    summing ``total_tokens`` — or ``prompt_tokens + completion_tokens`` when a
    provider omits the total. This is the per-run token figure the billing
    worker meters to Stripe.

    Args:
        *language_models: LMs whose usage to total; ``None`` entries and LMs
            tracking neither form are skipped.

    Returns:
        The summed token count, or ``None`` when no usage information is present
        (e.g. mocked LMs in tests) so callers can tell "zero usage" apart from
        "usage not tracked" and skip metering rather than bill nothing.
    """
    total = 0
    found = False
    for language_model in language_models:
        totals = getattr(language_model, "usage_totals", None)
        if isinstance(totals, LmUsageTotals):
            if totals.total_found:
                total += totals.total_tokens
                found = True
            # A metered LM with recorded calls keeps no history; skipping the
            # fallback also guards against double-counting on a dspy line
            # that appends history outside the update_history hook.
            if totals.calls:
                continue
        history = getattr(language_model, "history", None)
        if not isinstance(history, list):
            continue
        for entry in history:
            tokens = _usage_total_tokens(entry.get("usage") if isinstance(entry, dict) else None)
            if tokens is not None:
                total += tokens
                found = True
    return total if found else None


def _usage_in_out_tokens(usage: object) -> tuple[int, int] | None:
    """Split one history ``usage`` block into ``(input, output)`` token counts.

    Returns ``prompt_tokens`` / ``completion_tokens`` when either is present.
    When a provider reports only a combined ``total_tokens``, the whole amount is
    attributed to input — the cheaper side — so the fallback under-charges output
    rather than inventing a split that over-bills. Returns ``None`` when the block
    carries no recognizable token fields.

    Args:
        usage: The ``usage`` value from a ``dspy.LM`` history entry — a mapping,
            a provider usage object, or ``None``.

    Returns:
        The ``(input_tokens, output_tokens)`` pair, or ``None`` when untracked.
    """
    if usage is None:
        return None
    get = usage.get if isinstance(usage, dict) else lambda key: getattr(usage, key, None)
    prompt = get("prompt_tokens")
    completion = get("completion_tokens")
    if isinstance(prompt, int | float) or isinstance(completion, int | float):
        in_tokens = int(prompt) if isinstance(prompt, int | float) and prompt > 0 else 0
        out_tokens = int(completion) if isinstance(completion, int | float) and completion > 0 else 0
        if in_tokens or out_tokens:
            return in_tokens, out_tokens
    total = get("total_tokens")
    if isinstance(total, int | float) and total > 0:
        return int(total), 0
    return None


def _field(container: object, key: str) -> object:
    """Read ``key`` from a usage mapping or provider usage object.

    Args:
        container: A dict, a LiteLLM usage object, or ``None``.
        key: The field name.

    Returns:
        The field's value, or ``None`` when absent.
    """
    if isinstance(container, Mapping):
        return container.get(key)
    return getattr(container, key, None)


def _count(value: object) -> int:
    """Coerce a token counter to a non-negative int, treating junk as zero.

    Args:
        value: A raw usage counter.

    Returns:
        The count, or ``0`` when missing, non-numeric or negative.
    """
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return 0
    return int(value)


def _usage_reported_cost(usage: object) -> float | None:
    """Return the provider cost OpenRouter reported for one call, in USD.

    For a BYOK call ``cost`` is only OpenRouter's fee, so the upstream model
    cost is added to recover the full price (as the protected dispatcher does).

    Args:
        usage: The call's ``usage`` block.

    Returns:
        The call's full provider cost, or ``None`` when it was not reported.
    """
    if _field(usage, "token_source") == "subscription":
        # ``cost`` is the public list price, kept for the cost score; the
        # user's own subscription paid for the call, so nothing is billed.
        return 0.0
    cost = _field(usage, "cost")
    if isinstance(cost, bool) or not isinstance(cost, int | float) or cost < 0:
        return None
    if _field(usage, "is_byok"):
        upstream = _field(_field(usage, "cost_details"), "upstream_inference_cost")
        if isinstance(upstream, bool) or not isinstance(upstream, int | float) or upstream < 0:
            return None
        return float(cost) + float(upstream)
    return float(cost)


def _usage_token_details(usage: object) -> tuple[int, int, int]:
    """Extract the cache-read, cache-write and reasoning token counts of one call.

    Reads the OpenAI-shaped ``*_tokens_details`` blocks OpenRouter returns, and
    the top-level Anthropic-style cache fields LiteLLM sets for native providers.

    Args:
        usage: The call's ``usage`` block.

    Returns:
        ``(cache_read_tokens, cache_write_tokens, reasoning_tokens)``.
    """
    prompt_details = _field(usage, "prompt_tokens_details")
    cache_read = _count(_field(prompt_details, "cached_tokens")) or _count(_field(usage, "cache_read_input_tokens"))
    cache_write = (
        _count(_field(prompt_details, "cache_write_tokens"))
        or _count(_field(prompt_details, "cache_creation_tokens"))
        or _count(_field(usage, "cache_creation_input_tokens"))
    )
    reasoning = _count(_field(_field(usage, "completion_tokens_details"), "reasoning_tokens"))
    return cache_read, cache_write, reasoning


_SUMMED_FIELDS = (
    "input_tokens",
    "output_tokens",
    "reported_cost_usd",
    "reported_input_tokens",
    "reported_output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)


def model_usages_from_history(*language_models: object) -> list[ModelUsage] | None:
    """Aggregate priced-usage rows per model across LMs, with reported costs.

    Like :func:`usage_by_model_from_history`, but keeps the provider-reported
    cost and the cache and reasoning token counts that exact pricing needs.

    Args:
        *language_models: LMs whose usage to total; ``None`` entries and LMs
            tracking neither form are skipped.

    Returns:
        One :class:`ModelUsage` per model, or ``None`` when no usage
        information is present anywhere.
    """
    by_model: dict[str, LmUsageTotals] = {}
    found = False
    for language_model in language_models:
        model = getattr(language_model, "model", None) or "unknown"
        totals = getattr(language_model, "usage_totals", None)
        if isinstance(totals, LmUsageTotals):
            if totals.split_found:
                accumulator = by_model.setdefault(model, LmUsageTotals())
                with _USAGE_LOCK:
                    for name in _SUMMED_FIELDS:
                        setattr(accumulator, name, getattr(accumulator, name) + getattr(totals, name))
                accumulator.split_found = True
                found = True
            # Same skip rationale as total_tokens_from_history.
            if totals.calls:
                continue
        history = getattr(language_model, "history", None)
        if not isinstance(history, list):
            continue
        for entry in history:
            usage = entry.get("usage") if isinstance(entry, dict) else None
            split = _usage_in_out_tokens(usage)
            if split is None:
                continue
            by_model.setdefault(model, LmUsageTotals()).add_split(usage, split)
            found = True
    if not found:
        return None
    return [totals.model_usage(model) for model, totals in by_model.items()]


def usage_by_model_from_history(*language_models: object) -> dict[str, tuple[int, int]] | None:
    """Aggregate per-model ``(input, output)`` token usage across LMs' histories.

    The per-model companion to :func:`total_tokens_from_history`: it keys usage
    by each ``dspy.LM``'s ``model`` id and preserves the input/output split that
    per-model pricing needs, folding several LMs on the same model together.
    Prefers the ``MeteredLM`` aggregate, falling back to history entries.
    Stays billing-agnostic — it returns plain token counts, leaving the
    cost conversion to :mod:`core.billing.pricing`.

    Args:
        *language_models: LMs whose usage to total; ``None`` entries and LMs
            tracking neither form are skipped. An LM with no ``model``
            attribute buckets under ``"unknown"``.

    Returns:
        A ``model id → (input_tokens, output_tokens)`` mapping, or ``None`` when
        no usage information is present anywhere — mirroring
        :func:`total_tokens_from_history` so callers can tell "zero usage" from
        "usage not tracked" and skip charging rather than bill nothing.
    """
    by_model: dict[str, list[int]] = {}
    found = False
    for language_model in language_models:
        model = getattr(language_model, "model", None) or "unknown"
        totals = getattr(language_model, "usage_totals", None)
        if isinstance(totals, LmUsageTotals):
            if totals.split_found:
                accumulator = by_model.setdefault(model, [0, 0])
                accumulator[0] += totals.input_tokens
                accumulator[1] += totals.output_tokens
                found = True
            # Same skip rationale as total_tokens_from_history.
            if totals.calls:
                continue
        history = getattr(language_model, "history", None)
        if not isinstance(history, list):
            continue
        for entry in history:
            split = _usage_in_out_tokens(entry.get("usage") if isinstance(entry, dict) else None)
            if split is None:
                continue
            accumulator = by_model.setdefault(model, [0, 0])
            accumulator[0] += split[0]
            accumulator[1] += split[1]
            found = True
    if not found:
        return None
    return {model: (in_out[0], in_out[1]) for model, in_out in by_model.items()}


# Shared with deploy/litellm/custom_callbacks.py, which patches the same handler.
_SERVED_MODEL_PATCH_FLAG = "_skynet_served_model_patch"


def canonical_model_id(model: str) -> str:
    """Strip the managed-gateway transport prefix off a model id.

    LMs built for the proxy report ``litellm_proxy/<catalog id>`` while the
    scorer ledger and the pricing table use the bare catalog id, so the same
    model would otherwise surface under two spellings.

    Args:
        model: Model id as an LM reports it.

    Returns:
        The catalog-shaped id (``google/gemini-3-flash-preview``).
    """
    return model.removeprefix("litellm_proxy/")


def install_openrouter_served_model_patch() -> None:
    """Make LiteLLM streams report the model OpenRouter actually served.

    LiteLLM's ``CustomStreamWrapper`` overwrites every streamed chunk's
    ``model`` with the request id, discarding the concrete model OpenRouter's
    Auto Router names in its raw SSE chunks. Adopt the provider-reported
    model for openrouter calls — the same special-case LiteLLM already ships
    for Azure — so ``lm.history`` records the served model under
    ``response_model`` instead of echoing the router's own id. Idempotent;
    called once from ``create_app``.
    """
    if CustomStreamWrapper is None:
        logger.warning("litellm streaming internals moved; served-model reveal disabled")
        return
    handler = CustomStreamWrapper.handle_openai_chat_completion_chunk
    if getattr(handler, _SERVED_MODEL_PATCH_FLAG, False):
        return

    def _adopting_handler(self, chunk):
        """Adopt the provider-reported model before normal chunk handling."""
        served = getattr(chunk, "model", None)
        # Every platform call is an OpenRouter slug — reached directly
        # (provider "openrouter") or through the managed LiteLLM proxy, which
        # surfaces as an openai-compatible passthrough whose requested model
        # keeps the "openrouter/" marker. The proxy forwards the Auto Router's
        # concrete pick via deploy/litellm/custom_callbacks.py.
        provider = getattr(self, "custom_llm_provider", None)
        requested = getattr(self, "model", None)
        if served and (
            provider in ("openrouter", "litellm_proxy") or (isinstance(requested, str) and "openrouter/" in requested)
        ):
            self.model = served
        return handler(self, chunk)

    setattr(_adopting_handler, _SERVED_MODEL_PATCH_FLAG, True)
    CustomStreamWrapper.handle_openai_chat_completion_chunk = _adopting_handler


def served_model_from(language_model: object) -> str | None:
    """Concrete model that answered ``language_model``'s latest call, if newsworthy.

    Reads the last ``lm.history`` entry and returns its provider-reported
    ``response_model`` only when it differs from the requested id — i.e. an
    auto-routed call resolved to a concrete model. Explicit picks and
    providers that merely echo the request return ``None`` (LiteLLM strips
    the transport prefix, so a bare suffix echo is not news either).

    Args:
        language_model: A ``dspy.LM``-like object — either a ``MeteredLM``
            (which drops history but stashes the last call's model ids) or a
            stock LM with a ``history`` list.

    Returns:
        The served model id, or ``None`` when there is nothing to reveal.
    """
    served = getattr(language_model, "last_response_model", None)
    requested = getattr(language_model, "last_request_model", None)
    if not served:
        history = getattr(language_model, "history", None)
        if not history:
            return None
        entry = history[-1]
        if not isinstance(entry, dict):
            return None
        served = entry.get("response_model")
        requested = entry.get("model")
    if not served or not isinstance(served, str):
        return None
    if served == requested:
        return None
    if isinstance(requested, str) and requested.endswith(f"/{served}"):
        return None
    return served
