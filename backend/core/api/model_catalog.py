"""Dynamic model catalog powered by LiteLLM's ``model_cost`` registry.

LiteLLM ships ``model_prices_and_context_window.json`` — ~2600 models with
metadata (provider, mode, context window, ``supports_reasoning``, etc.).
We filter to chat-mode models, de-duplicate dated variants, and mark which
providers have active API keys via ``litellm.get_valid_models()``. The
hosted (platform-billed) catalog is restricted to OpenRouter — the
platform's sole LLM provider; the BYOK catalog still spans every provider
a user may bring a key for.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock, Thread
from typing import Any, Literal, get_args

import litellm
from pydantic import BaseModel, Field

from ..billing import openrouter_prices
from ..billing.data_policy import zero_retention_models
from ..billing.model_terms import excluded_from_managed
from ..config import DEFAULT_AGENT_MODEL_ID, settings
from ..provider_registry import BYOK_CATALOG_PREFIXES
from .errors import DomainError

logger = logging.getLogger(__name__)


class CatalogModel(BaseModel):
    """A single model entry exposed to the frontend."""

    value: str = Field(
        ...,
        description=(
            "Fully-qualified LiteLLM model id including the provider prefix "
            "(e.g. 'openai/gpt-4o-mini', 'anthropic/claude-3-5-sonnet'). Pass "
            "this string verbatim when submitting a run or writing model_name "
            "into wizard_state — dspy.LM rejects un-prefixed ids."
        ),
    )
    label: str = Field(
        ...,
        description=(
            "Display-only label (provider prefix stripped). Never use as a model_name; use ``value`` instead."
        ),
    )
    provider: str = Field(..., description="Provider slug for grouping (e.g. 'openai').")
    byok_provider: str | None = Field(
        default=None,
        description="Stored-connection slug to persist for a custom BYOK model.",
    )
    data_center: str | None = Field(
        default=None,
        description=(
            "Data-center label this model resolves through when the provider "
            "exposes more than one endpoint. "
            "``None`` for single-endpoint providers."
        ),
    )
    supports_thinking: bool = Field(default=False, description="Model supports reasoning_effort.")
    reasoning_efforts: list[str] | None = Field(
        default=None,
        description=(
            "Reasoning-effort levels this model accepts, weakest first (e.g. "
            "['none', 'low', 'medium', 'high', 'xhigh', 'max']). Empty when the "
            "model reasons with no effort control or does not reason; None when "
            "the provider did not say, so clients fall back to a per-family ladder."
        ),
    )
    default_reasoning_effort: str | None = Field(
        default=None,
        description="Effort the provider applies when none is sent, or None when unknown.",
    )
    reasoning_mandatory: bool | None = Field(
        default=None,
        description="Model always reasons and rejects effort 'none'; None when unknown.",
    )
    reasoning_default_enabled: bool | None = Field(
        default=None,
        description="Model reasons when no effort is sent; None when unknown.",
    )
    featured: bool = Field(
        default=False,
        description=(
            "One of the newest models of a leading lab, picked from the provider's "
            "own release dates and benchmarks; model pickers list these first."
        ),
    )
    is_default: bool = Field(
        default=False,
        description=(
            "The featured model that thinks by default with the best benchmark "
            "score per dollar; runs the composer's Auto mode. At most one per catalog."
        ),
    )
    is_interview_default: bool = Field(
        default=False,
        description=(
            "The model agent interviews run when the user picks none: the fastest "
            "of the cheaper half of the featured models, by OpenRouter's live "
            "latency and throughput. At most one per catalog."
        ),
    )
    is_strongest: bool = Field(
        default=False,
        description=(
            "The featured model that thinks by default with the highest Artificial "
            "Analysis intelligence index; the reflection model's Automatic setting "
            "runs it. At most one per catalog."
        ),
    )
    display_name: str | None = Field(
        default=None,
        description=(
            "The provider's human-readable model name without the lab prefix "
            "(e.g. 'Claude Haiku 5.5'). Display only; None when the listing gives none."
        ),
    )
    supports_vision: bool = Field(
        default=False,
        description="Model accepts image inputs (required when the dataset has a dspy.Image column).",
    )
    lab_tier: int = Field(
        default=1,
        description=(
            "Rank of the model's lab in best-value orderings, read from its vendor: "
            "0 for the top labs (Anthropic, OpenAI, Google), 2 for Chinese labs, 1 otherwise. "
            "Best value sorts on it first and on score per dollar second."
        ),
    )
    available: bool = Field(default=False, description="True if backend has an API key for this model.")
    max_input_tokens: int | None = Field(default=None, description="Context window size.")
    input_cost_per_token: float | None = Field(
        default=None,
        description=(
            "Provider input (prompt) cost per token in USD, before the usage markup: "
            "the provider's live listing when probed, else LiteLLM's price table. "
            "None when unpriced; the client falls back to a conservative rate. "
            "Drives the per-model pre-run cost estimate."
        ),
    )
    output_cost_per_token: float | None = Field(
        default=None,
        description="Provider output (completion) cost per token in USD, or None when unpriced.",
    )
    intelligence_index: float | None = Field(
        default=None,
        description=(
            "Artificial Analysis intelligence index from OpenRouter's listing. A new "
            "release OpenRouter has not benchmarked yet carries its line's previous "
            "score. None when the model is unrated."
        ),
    )
    artificial_analysis_url: str | None = Field(
        default=None,
        description=(
            "The model's Artificial Analysis page, only once a fetch has confirmed the page "
            "exists and its title names this model; None otherwise, and clients then show "
            "no link."
        ),
    )
    throughput_tps: float | None = Field(
        default=None,
        description=(
            "Median output tokens per second of the model's fastest healthy OpenRouter "
            "endpoint over the last 30 minutes, or None when OpenRouter reports none."
        ),
    )
    ttft_ms: float | None = Field(
        default=None,
        description=(
            "Median milliseconds to the first token on that same endpoint over the "
            "last 30 minutes, or None when OpenRouter reports none."
        ),
    )
    latency_ms: float | None = Field(
        default=None,
        description=(
            "Estimated milliseconds to a complete 150-token reply on that same endpoint: "
            "the median time to the first token plus 150 tokens at the median output "
            "speed. None when either median is missing."
        ),
    )
    zero_retention: bool = Field(
        default=False,
        description=(
            "At least one OpenRouter endpoint serves this model under a zero-data-retention "
            "agreement, so it stays available under every model data privacy setting."
        ),
    )


class CatalogProvider(BaseModel):
    """A provider section in the catalog.

    A provider with a single endpoint emits one entry with
    ``data_center=None``. A provider that fans out across several endpoints
    emits one entry per data center, each with a distinct ``default_base_url`` and a
    populated ``data_center`` label.
    """

    slug: str
    label: str
    data_center: str | None = None
    env_var: str | None = None
    default_base_url: str | None = None
    has_env_key: bool = False


class ModelCatalogResponse(BaseModel):
    """Response for GET /models."""

    providers: list[CatalogProvider]
    models: list[CatalogModel]


def supports_effort_control(model: CatalogModel) -> bool:
    """Check whether a model supports the shared effort control.

    Args:
        model: Catalog metadata, including an optional provider effort ladder.

    Returns:
        Whether thinking and a usable effort level are available. Unknown
        ladders retain the client's existing family fallback.
    """
    return model.supports_thinking and (
        model.reasoning_efforts is None
        or any(level in REASONING_EFFORTS and level != "none" for level in model.reasoning_efforts)
    )


def with_effort_controls(catalog: ModelCatalogResponse) -> ModelCatalogResponse:
    """Keep selectable models without changing the source metadata snapshot.

    Args:
        catalog: The raw provider catalog.

    Returns:
        A catalog containing only models with effort controls.
    """
    models = [model for model in catalog.models if supports_effort_control(model)]
    if len(models) == len(catalog.models):
        return catalog
    if models and any(model.is_default for model in catalog.models) and not any(model.is_default for model in models):
        default = min(
            models,
            key=lambda model: (
                not model.featured,
                model.lab_tier,
                model.input_cost_per_token / model.intelligence_index
                if model.input_cost_per_token and model.intelligence_index
                else float("inf"),
                model.value,
            ),
        )
        models = [model.model_copy(update={"is_default": model.value == default.value}) for model in models]
    if (
        models
        and any(model.is_strongest for model in catalog.models)
        and not any(model.is_strongest for model in models)
    ):
        strongest = max(models, key=lambda model: model.intelligence_index or 0)
        models = [model.model_copy(update={"is_strongest": model.value == strongest.value}) for model in models]
    providers = {(model.provider, model.data_center) for model in models}
    return catalog.model_copy(
        update={
            "models": models,
            "providers": [
                provider for provider in catalog.providers if (provider.slug, provider.data_center) in providers
            ],
        }
    )


class _DataCenter(BaseModel):
    """One reachable endpoint for a provider.

    ``label`` is ``None`` for a provider's sole/native endpoint (renders as
    just the provider name) and a short human string for any additional
    center. ``models_url`` is the OpenAI-compatible
    ``/models`` probe target, or ``None`` to fall back to LiteLLM's API-key
    heuristic.
    """

    label: str | None = None
    base_url: str | None = None
    models_url: str | None = None
    env_var: str | None = None


# Each provider declares one or more data centers. Historically every
# provider had exactly one endpoint; that maps to a single ``_DataCenter``
# with ``label=None`` so existing single-DC behaviour (and the
# ``data_center=None`` wire shape) is preserved.
#
# A ``models_url`` of ``None`` skips the live probe and falls back to
# LiteLLM's API-key heuristic — used for providers with bespoke auth
# (Anthropic), non-OpenAI shapes (Gemini, Ollama), or no public /models
# endpoint we can rely on (Cohere, Volcengine).
_PROVIDER_META: dict[str, tuple[str, list[_DataCenter]]] = {
    "openai": (
        "OpenAI",
        [
            _DataCenter(
                base_url="https://api.openai.com/v1",
                models_url="https://api.openai.com/v1/models",
                env_var="OPENAI_API_KEY",
            )
        ],
    ),
    "anthropic": (
        "Anthropic",
        [_DataCenter(base_url="https://api.anthropic.com", env_var="ANTHROPIC_API_KEY")],
    ),
    "gemini": ("Google Gemini", [_DataCenter(env_var="GEMINI_API_KEY")]),
    "groq": (
        "Groq",
        [
            _DataCenter(
                base_url="https://api.groq.com/openai/v1",
                models_url="https://api.groq.com/openai/v1/models",
                env_var="GROQ_API_KEY",
            )
        ],
    ),
    "deepseek": (
        "DeepSeek",
        [
            _DataCenter(
                base_url="https://api.deepseek.com",
                models_url="https://api.deepseek.com/v1/models",
                env_var="DEEPSEEK_API_KEY",
            )
        ],
    ),
    "xai": (
        "SpaceXAI (Grok)",
        [
            _DataCenter(
                base_url="https://api.x.ai/v1",
                models_url="https://api.x.ai/v1/models",
                env_var="XAI_API_KEY",
            )
        ],
    ),
    "together_ai": (
        "Together AI",
        [
            _DataCenter(
                base_url="https://api.together.xyz/v1",
                models_url="https://api.together.xyz/v1/models",
                env_var="TOGETHERAI_API_KEY",
            )
        ],
    ),
    "openrouter": (
        "OpenRouter",
        [
            _DataCenter(
                base_url="https://openrouter.ai/api/v1",
                models_url="https://openrouter.ai/api/v1/models",
                env_var="OPENROUTER_API_KEY",
            )
        ],
    ),
    "cerebras": (
        "Cerebras",
        [
            _DataCenter(
                models_url="https://api.cerebras.ai/v1/models",
                env_var="CEREBRAS_API_KEY",
            )
        ],
    ),
    "fireworks_ai": (
        "Fireworks AI",
        [
            _DataCenter(
                models_url="https://api.fireworks.ai/inference/v1/models",
                env_var="FIREWORKS_AI_API_KEY",
            )
        ],
    ),
    "cohere_chat": ("Cohere", [_DataCenter(env_var="COHERE_API_KEY")]),
    "mistral": (
        "Mistral",
        [
            _DataCenter(
                models_url="https://api.mistral.ai/v1/models",
                env_var="MISTRAL_API_KEY",
            )
        ],
    ),
    "moonshot": (
        "Moonshot (Kimi)",
        [
            _DataCenter(
                models_url="https://api.moonshot.cn/v1/models",
                env_var="MOONSHOT_API_KEY",
            )
        ],
    ),
    "volcengine": ("Volcengine", [_DataCenter(env_var="VOLCENGINE_API_KEY")]),
    "novita": (
        "Novita AI",
        [
            _DataCenter(
                models_url="https://api.novita.ai/v3/openai/models",
                env_var="NOVITA_API_KEY",
            )
        ],
    ),
    "deepinfra": (
        "DeepInfra",
        [
            _DataCenter(
                base_url="https://api.deepinfra.com/v1/openai",
                models_url="https://api.deepinfra.com/v1/openai/models",
                env_var="DEEPINFRA_API_KEY",
            )
        ],
    ),
    "sambanova": (
        "SambaNova",
        [
            _DataCenter(
                base_url="https://api.sambanova.ai/v1",
                models_url="https://api.sambanova.ai/v1/models",
                env_var="SAMBANOVA_API_KEY",
            )
        ],
    ),
    "nebius": (
        "Nebius AI Studio",
        [
            _DataCenter(
                base_url="https://api.studio.nebius.ai/v1",
                models_url="https://api.studio.nebius.ai/v1/models",
                env_var="NEBIUS_API_KEY",
            )
        ],
    ),
    "minimax": (
        "MiniMax",
        [
            _DataCenter(
                base_url="https://api.minimax.io/v1",
                models_url="https://api.minimax.io/v1/models",
                env_var="MINIMAX_API_KEY",
            )
        ],
    ),
    "zai": (
        "Z.AI (GLM)",
        [
            _DataCenter(
                base_url="https://api.z.ai/api/paas/v4",
                models_url="https://api.z.ai/api/paas/v4/models",
                env_var="ZAI_API_KEY",
            )
        ],
    ),
    "meta_llama": (
        "Meta Llama",
        [
            _DataCenter(
                base_url="https://api.llama.com/compat/v1",
                models_url="https://api.llama.com/compat/v1/models",
                env_var="LLAMA_API_KEY",
            )
        ],
    ),
    "gmi": (
        "GMI Cloud",
        [
            _DataCenter(
                base_url="https://api.gmi-serving.com/v1",
                models_url="https://api.gmi-serving.com/v1/models",
                env_var="GMI_API_KEY",
            )
        ],
    ),
    "crusoe": (
        "Crusoe",
        [
            _DataCenter(
                base_url="https://managed-inference-api-proxy.crusoecloud.com/v1",
                models_url="https://managed-inference-api-proxy.crusoecloud.com/v1/models",
                env_var="CRUSOE_API_KEY",
            )
        ],
    ),
    "friendliai": (
        "FriendliAI",
        [
            _DataCenter(
                base_url="https://api.friendli.ai/serverless/v1",
                models_url="https://api.friendli.ai/serverless/v1/models",
                env_var="FRIENDLI_TOKEN",
            )
        ],
    ),
    "morph": (
        "Morph",
        [
            _DataCenter(
                base_url="https://api.morphllm.com/v1",
                models_url="https://api.morphllm.com/v1/models",
                env_var="MORPH_API_KEY",
            )
        ],
    ),
    "ollama": ("Ollama (self-hosted)", [_DataCenter(base_url="http://localhost:11434")]),
}

# The platform brokers every LLM call through OpenRouter, so the hosted
# catalog lists only its models — other providers' keys may exist for
# non-LLM features (OpenAI powers Whisper dictation) without their chat
# models leaking into the menu. The full ``_PROVIDER_META`` table stays:
# the BYOK catalog still needs the other providers' labels.
_PLATFORM_PROVIDERS: frozenset[str] = frozenset({"openrouter"})

# Weakest first; the order the effort menus list levels in.
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
REASONING_EFFORTS: tuple[str, ...] = get_args(ReasoningEffort)

_DATE_SUFFIX_RE = re.compile(r"-\d{4}-\d{2}-\d{2}$")

# Snapshot suffixes across naming schemes: ``-2025-08-07`` (OpenAI),
# ``-20251101`` (Anthropic) and ``-0813`` / ``-2512`` (OpenRouter, Mistral).
_ANY_DATE_SUFFIX_RE = re.compile(r"-(\d{8}|\d{4}-\d{2}-\d{2}|\d{4})$")
# Open-weight size tags ("-27b", "-2.4t-a95b") mark checkpoints of a family,
# not the lab's headline models.
_PARAM_SIZE_RE = re.compile(r"\d+(\.\d+)?[bt]\b")
_VERSION_RE = re.compile(r"\d+(\.\d+)*")
_FAMILY_NOISE_RE = re.compile(r"-(preview|latest|exp)\b")
# Models withdrawn from the product: never offered, whatever the probe lists.
# OpenRouter's own routers (auto, free, bodybuilder) and third-party routers
# pick a model per request, which the menu's Auto modes already do.
_REMOVED_MODEL_RE = re.compile(
    r"gpt-5\.6-terra|gpt-terra-latest|grok-build"
    r"|^openrouter/(openrouter/|auto$)|-router$"
)


def is_removed_model(name: str) -> bool:
    """Return whether a model id was withdrawn from the product.

    Args:
        name: Catalog model id, e.g. ``openrouter/x-ai/grok-build``.

    Returns:
        ``True`` when the id matches the removed-model pattern, or names a
        catalog model without an effort control, which the menus no longer
        offer.
    """
    return bool(_REMOVED_MODEL_RE.search(name)) or is_effortless_model(name)


def is_effortless_model(name: str) -> bool:
    """Return whether a model id is a real catalog model without an effort control.

    The menus offer only models with an effort control. Saved prefs, stored
    conversations, tagging sessions and hardening jobs may still name one of
    the others; they must not fail validation on every turn.

    Args:
        name: Catalog model id.

    Returns:
        ``True`` when the last catalog build listed or hid the id and the
        model has no usable effort control.
    """
    if _cached_response is None:
        # Before the boot prewarm lands, wait for its build (or start one):
        # otherwise a stored effortless pick fails validation once with a 422.
        # Joining the shared refresh thread keeps it to one build at a time.
        _kick_background_refresh()
        refresh = _refresh_thread
        if refresh is not None:
            refresh.join()
    if name in _hidden_effortless_values:
        return True
    snapshot = _cached_response
    return snapshot is not None and any(
        model.value == name and not supports_effort_control(model) for model in snapshot.models
    )


# D50: TypeSafe Jev is a router, so the removed-model rule hides it, but its
# typed labels suit the classification roles (the watch hook and the exploit
# judge). Only those roles may pick it; every other role keeps it hidden.
CLASSIFIER_ROLES: frozenset[str] = frozenset({"hook", "judge"})
CLASSIFIER_ONLY_MODELS: frozenset[str] = frozenset({"openrouter/typesafe/jev-router"})


# Featured-model selection over the OpenRouter listing: the agent panels'
# shortlist. Each model line contributes its newest release, which must reach
# a share of the best benchmark and not be beaten on both score and price by
# another model of its lab; each lab then offers its strongest few.
_FEATURED_WINDOW_SECONDS = 365 * 24 * 3600
_FEATURED_SCORE_RATIO = 0.6
_FEATURED_PER_LAB = 3
# The full menus offer a lab's releases from the last two years.
_PRACTICAL_WINDOW_SECONDS = 2 * 365 * 24 * 3600

# Best value ranks by lab before score per dollar, keyed by OpenRouter vendor
# slug or LiteLLM provider prefix. Unlisted vendors sit in the middle tier.
_TOP_LAB_TIER = 0
_DEFAULT_LAB_TIER = 1
_LAB_TIERS: dict[str, int] = {
    **dict.fromkeys(("anthropic", "openai", "google", "gemini", "vertex_ai"), _TOP_LAB_TIER),
    **dict.fromkeys(
        (
            "z-ai",
            "zai",
            "zhipuai",
            "deepseek",
            "qwen",
            "dashscope",
            "alibaba",
            "moonshotai",
            "moonshot",
            "minimax",
            "bytedance",
            "bytedance-seed",
            "volcengine",
            "xiaomi",
            "stepfun",
            "tencent",
            "meituan",
            "inclusionai",
            "baidu",
        ),
        2,
    ),
}

# Real OpenRouter models the last catalog build left out as impractical. Menus
# never offer them, but a session or saved pick made before they dropped out
# keeps running rather than failing validation.
_hidden_model_values: frozenset[str] = frozenset()

# Real models the last build hid as impractical that also lack an effort
# control. Like the listed models without one, a stored pick of them is
# treated as withdrawn (see :func:`is_effortless_model`).
_hidden_effortless_values: frozenset[str] = frozenset()

# Benchmark score of each featured model, from the last catalog build. The
# interview-model pick ranks on it without the catalog exposing the score.
_featured_scores: dict[str, float] = {}

# LiteLLM provider prefixes offered for bring-your-own-key. The BYOK catalog
# lists these providers' registry models regardless of platform API keys, since
# a BYOK run authenticates with the user's own key. Sourced from the canonical
# ``core.provider_registry`` — a stdlib-only leaf, so this stays clear of the
# Stripe import chain ``core.billing`` pulls at import time — which the vault
# shares, so the offered prefixes and the savable slugs can never drift apart.
_BYOK_CATALOG_PROVIDERS: frozenset[str] = BYOK_CATALOG_PREFIXES


def _provider_data_centers(provider_slug: str) -> list[_DataCenter]:
    """Return the data centers for ``provider_slug``.

    Args:
        provider_slug: LiteLLM provider key (e.g. ``"openai"``).

    Returns:
        A copy of the provider's static endpoint list.
    """
    return list(_PROVIDER_META[provider_slug][1])


def _make_label(model_id: str) -> str:
    """Strip the provider prefix from a model ID for display.

    Args:
        model_id: A LiteLLM model ID, optionally prefixed with ``provider/``.

    Returns:
        The bare model name with any leading ``provider/`` removed.
    """
    name = model_id.split("/", 1)[-1] if "/" in model_id else model_id
    return name


def _positive_cost(meta: dict, key: str) -> float | None:
    """Return a positive per-token cost (USD) from a LiteLLM meta block, or None.

    Args:
        meta: A ``litellm.model_cost`` entry.
        key: ``"input_cost_per_token"`` or ``"output_cost_per_token"``.

    Returns:
        The cost as a float when present and positive, else ``None`` so the client
        falls back to a default rate rather than treating the model as free.
    """
    value = meta.get(key)
    return float(value) if isinstance(value, (int, float)) and value > 0 else None


def _probe_prefixed_id(provider_slug: str, model_id: str) -> str:
    """Provider-prefix a probe-reported model ID for the catalog ``value``.

    Mirrors the ``prefixed_id`` shape the ``litellm.model_cost`` loop emits so
    a probe-discovered model de-dups cleanly against a registry entry: a bare
    ID gets ``provider_slug/`` prepended; an ID that already starts with that
    prefix is passed through unchanged. Fireworks reports
    ``accounts/fireworks/models/...`` (no provider prefix) so it correctly
    becomes ``fireworks_ai/accounts/fireworks/models/...``, while OpenRouter
    reports bare ``vendor/model`` so it becomes ``openrouter/vendor/model``.

    Args:
        provider_slug: LiteLLM provider key (e.g. ``"openrouter"``).
        model_id: The model ID exactly as the probe reported it.

    Returns:
        The provider-prefixed catalog ``value`` for ``model_id``.
    """
    if model_id.startswith(f"{provider_slug}/"):
        return model_id
    return f"{provider_slug}/{model_id}"


def _probe_item_supports_vision(item: dict) -> bool:
    """Decide whether a probe item accepts image input.

    Reads OpenRouter's ``architecture.input_modalities`` list first (vision
    when it contains ``"image"``), then falls back to a top-level
    ``supports_vision`` / ``vision`` boolean. Defaults to ``False`` when no
    modality signal is present.

    Args:
        item: The raw provider item dict (may be empty for string entries).

    Returns:
        ``True`` when the model plausibly accepts images.
    """
    arch = item.get("architecture")
    if isinstance(arch, dict):
        modalities = arch.get("input_modalities")
        if isinstance(modalities, list):
            return "image" in modalities
    return bool(item.get("supports_vision") or item.get("vision"))


def _probe_item_supports_thinking(item: dict) -> bool:
    """Decide whether a probe item supports reasoning/thinking.

    Reads OpenRouter's ``supported_parameters`` list (thinking when it
    contains ``"reasoning"``), then falls back to a top-level
    ``supports_reasoning`` boolean. Defaults to ``False``.

    Args:
        item: The raw provider item dict (may be empty for string entries).

    Returns:
        ``True`` when the model exposes a reasoning capability.
    """
    params = item.get("supported_parameters")
    if isinstance(params, list):
        return "reasoning" in params
    return bool(item.get("supports_reasoning"))


def _probe_item_reasoning(item: dict) -> dict[str, Any]:
    """Read the reasoning controls a probe item declares.

    OpenRouter publishes ``reasoning.supported_efforts``, ``default_effort``,
    ``mandatory`` and ``default_enabled`` per model, and the sets differ
    widely (GLM offers ``max/high/low`` with no ``medium``; Gemini stops at
    ``high``; GPT-5 reasons unless sent ``none``), so a single global effort
    list would offer levels the provider rejects. Levels the backend does not
    accept are dropped.

    Args:
        item: The raw provider item dict (empty when the probe did not list it).

    Returns:
        ``CatalogModel`` keyword arguments: the accepted effort levels weakest
        first (empty for a model with no effort control), the default effort,
        and the mandatory / default-enabled flags. Every value is ``None``
        when the item carries no probe data at all.
    """
    if not item:
        return {
            "reasoning_efforts": None,
            "default_reasoning_effort": None,
            "reasoning_mandatory": None,
            "reasoning_default_enabled": None,
        }
    reasoning = item.get("reasoning")
    reasoning = reasoning if isinstance(reasoning, dict) else {}
    raw = reasoning.get("supported_efforts")
    declared = set(raw) if isinstance(raw, list) else set()
    efforts = [e for e in REASONING_EFFORTS if e in declared]
    default = reasoning.get("default_effort")
    return {
        "reasoning_efforts": efforts,
        "default_reasoning_effort": default if default in efforts else None,
        "reasoning_mandatory": bool(reasoning.get("mandatory")),
        "reasoning_default_enabled": bool(reasoning.get("mandatory") or reasoning.get("default_enabled")),
    }


def _probe_item_max_input_tokens(item: dict) -> int | None:
    """Extract a context-window size from a probe item, if present.

    Args:
        item: The raw provider item dict (may be empty for string entries).

    Returns:
        The first of ``context_length`` / ``context_window`` /
        ``max_input_tokens`` that is an ``int``, else ``None``.
    """
    for key in ("context_length", "context_window", "max_input_tokens"):
        val = item.get(key)
        if isinstance(val, int):
            return val
    return None


def _probe_item_cost(item: dict, key: str) -> float | None:
    """Read a positive per-token price (USD) from a probe item's ``pricing``.

    Args:
        item: The raw provider item dict.
        key: OpenRouter pricing key, ``"prompt"`` or ``"completion"``.

    Returns:
        The price as a float when present and positive, else ``None``.
    """
    pricing = item.get("pricing")
    if not isinstance(pricing, dict):
        return None
    try:
        value = float(pricing.get(key) or 0)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _probe_item_intelligence(item: dict) -> float | None:
    """Read the Artificial Analysis intelligence index OpenRouter attaches.

    Args:
        item: The raw provider item dict.

    Returns:
        The index as a float, or ``None`` when the model is unbenchmarked.
    """
    benchmarks = item.get("benchmarks")
    if not isinstance(benchmarks, dict):
        return None
    analysis = benchmarks.get("artificial_analysis")
    if not isinstance(analysis, dict):
        return None
    value = analysis.get("intelligence_index")
    return float(value) if isinstance(value, (int, float)) else None


def _probe_item_display_name(item: dict) -> str | None:
    """Read the listing's human-readable model name, without its lab prefix.

    OpenRouter names read ``"Anthropic: Claude Haiku 5.5"``; the logo beside
    the name already shows the lab.

    Args:
        item: The raw provider item dict.

    Returns:
        The bare name, or ``None`` when the listing carries none.
    """
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    return name.split(":", 1)[-1].strip() or None


def featured_score(value: str) -> float | None:
    """Return the benchmark score of a featured model from the last catalog build.

    Args:
        value: A catalog model id.

    Returns:
        The Artificial Analysis intelligence index, or ``None`` when the model
        is not featured or unbenchmarked.
    """
    return _featured_scores.get(value)


def _model_family(model_id: str) -> str:
    """Collapse a model name to its product line, dropping versions and dates.

    ``claude-opus-5.5`` and ``claude-opus-4.8`` share the line
    ``claude-opus``; ``gemini-3.8-flash`` and ``gemini-3.5-flash-lite`` do
    not, so each line keeps its own newest release.

    Args:
        model_id: Model name without the vendor prefix.

    Returns:
        The version-free family key.
    """
    name = _FAMILY_NOISE_RE.sub("", _ANY_DATE_SUFFIX_RE.sub("", model_id.lower()))
    return re.sub(r"-?#+-?", "-", _VERSION_RE.sub("#", name)).strip("-")


def _practical_candidates(deployed: dict[str, dict]) -> dict[str, dict[str, dict]]:
    """Group the listing's usable chat models by lab.

    Usable means a dated, priced, non-expiring ``vendor/model`` that outputs
    only text and calls tools. Free twins (``:free``), aliases (``~latest``),
    withdrawn ids and media generators that also emit text drop out.

    Args:
        deployed: Probe index of ``vendor/model`` id to raw item.

    Returns:
        ``{vendor: {model id: item}}``.
    """
    candidates: dict[str, dict[str, dict]] = {}
    for model_id, item in deployed.items():
        if "/" not in model_id or ":" in model_id or model_id.startswith("~"):
            continue
        if _REMOVED_MODEL_RE.search(f"openrouter/{model_id}") or excluded_from_managed(model_id):
            continue
        if not isinstance(item.get("created"), int) or item.get("expiration_date"):
            continue
        params = item.get("supported_parameters")
        if not isinstance(params, list) or "tools" not in params:
            continue
        arch = item.get("architecture")
        outputs = arch.get("output_modalities") if isinstance(arch, dict) else None
        # Image or audio generators list text output too, but aren't chat partners.
        if outputs != ["text"] or _probe_item_cost(item, "prompt") is None:
            continue
        candidates.setdefault(model_id.split("/", 1)[0], {})[model_id] = item
    return candidates


def _scored_lines(items: dict[str, dict]) -> dict[str, tuple[str, dict, float | None]]:
    """Map each of one lab's model lines to its newest release and score.

    A release OpenRouter has not benchmarked yet stands on its line's previous
    score, so it replaces its predecessor on release day. A variant id that
    extends another listed id (``-pro`` twins, contributor tiers) counts only
    when it carries its own benchmark.

    Args:
        items: One lab's candidates, model id to raw item.

    Returns:
        ``{family: (model id, item, score or None)}``.
    """
    latest_by_family: dict[str, tuple[str, dict, float | None]] = {}
    for model_id, item in sorted(items.items(), key=lambda kv: kv[1]["created"]):
        score = _probe_item_intelligence(item)
        is_variant = any(model_id.startswith(f"{other}-") for other in items if other != model_id)
        if is_variant and score is None:
            continue
        family = _model_family(model_id.split("/", 1)[1])
        if score is None and family in latest_by_family:
            score = latest_by_family[family][2]
        latest_by_family[family] = (model_id, item, score)
    return latest_by_family


def _practical_probe_ids(deployed: dict[str, dict]) -> set[str] | None:
    """Pick the models worth offering in a menu from a listing.

    A model qualifies when it is a usable chat model (see
    :func:`_practical_candidates`), was released in the last two years, and
    either carries a benchmark or is the newest release of a benchmarked line.
    That keeps every lab's real lineup and drops the long tail of hobby
    fine-tunes, retired snapshots and music or image generators.

    Args:
        deployed: Probe index of ``vendor/model`` id to raw item.

    Returns:
        The probe ids to offer, or ``None`` when the listing carries no
        benchmarks to judge by (non-OpenRouter gateways), meaning keep all.
    """
    candidates = _practical_candidates(deployed)
    dated = [item["created"] for items in candidates.values() for item in items.values()]
    if not dated:
        return None
    oldest_allowed = max(dated) - _PRACTICAL_WINDOW_SECONDS
    practical: set[str] = set()
    for items in candidates.values():
        practical.update(m for m, item in items.items() if _probe_item_intelligence(item) is not None)
        practical.update(m for m, _item, score in _scored_lines(items).values() if score is not None)
    kept = {m for m in practical if deployed[m]["created"] >= oldest_allowed}
    return kept or None


def _featured_probe_ids(deployed: dict[str, dict]) -> set[str]:
    """Pick the best current models of each lab from a listing.

    Works purely from the metadata OpenRouter publishes (release time,
    pricing, tool support, benchmarks), so new releases surface and old ones
    retire without a hand-kept list. Variants (``:free``, ``-pro`` twins,
    contributor tiers) drop out: a model whose id extends another listed id
    counts only when it carries its own benchmark. A new release OpenRouter
    has not benchmarked yet stands on its line's previous score, so it
    replaces its predecessor on release day instead of vanishing.

    Args:
        deployed: Probe index of ``vendor/model`` id to raw item.

    Returns:
        The probe ids to feature; empty when the listing carries no release
        dates or benchmarks (non-OpenRouter gateways).
    """
    candidates = _practical_candidates(deployed)
    for items in candidates.values():
        for model_id in [m for m in items if _PARAM_SIZE_RE.search(m.split("/", 1)[1])]:
            del items[model_id]
    if not any(candidates.values()):
        return set()

    newest = max(item["created"] for items in candidates.values() for item in items.values())
    lines_by_lab: dict[str, list[tuple[str, dict, float]]] = {}
    for vendor, items in candidates.items():
        lines_by_lab[vendor] = [
            (model_id, item, score)
            for model_id, item, score in _scored_lines(items).values()
            if score is not None and newest - item["created"] <= _FEATURED_WINDOW_SECONDS
        ]
    scores = [score for lines in lines_by_lab.values() for _, _, score in lines]
    if not scores:
        return set()
    bar = max(scores) * _FEATURED_SCORE_RATIO

    featured: set[str] = set()
    for lines in lines_by_lab.values():
        strong = [line for line in lines if line[2] >= bar]
        frontier = [line for line in strong if not any(_dominates(other, line) for other in strong)]
        frontier.sort(key=lambda line: (line[2], line[1]["created"]), reverse=True)
        featured.update(model_id for model_id, _, _ in frontier[:_FEATURED_PER_LAB])
    return featured


def lab_tier(model_id: str) -> int:
    """Rank a model's lab for best-value orderings: top labs first, Chinese labs last.

    Args:
        model_id: A catalog value (``openrouter/vendor/model``,
            ``provider/model``) or an OpenRouter probe id (``vendor/model``).

    Returns:
        0 for Anthropic, OpenAI and Google, 2 for Chinese labs, 1 otherwise.
    """
    parts = model_id.split("/")
    vendor = parts[1] if parts[0] == "openrouter" and len(parts) > 2 else parts[0]
    return _LAB_TIERS.get(vendor.lstrip("~").lower(), _DEFAULT_LAB_TIER)


def _best_value_probe_id(deployed: dict[str, dict], featured: set[str]) -> tuple[str, tuple[int, float]] | None:
    """Pick the featured model of the best-ranked lab with the best benchmark score per input dollar.

    Only models that can reason qualify: a non-thinker would answer hard
    turns off the cuff. An opt-in thinker still thinks under Auto, because
    :func:`default_effort_for` sends it a low effort. The lab tier (see
    :func:`lab_tier`) ranks first, score per dollar second.

    Args:
        deployed: Probe index of ``vendor/model`` id to raw item.
        featured: Probe ids already chosen by :func:`_featured_probe_ids`.

    Returns:
        ``(probe id, (negated lab tier, score per dollar))`` of the winner,
        so larger ranks better, or ``None`` when no featured model can reason
        with a benchmark and a price.
    """
    best: tuple[str, tuple[int, float]] | None = None
    for probe_id in sorted(featured):
        item = deployed.get(probe_id)
        reasoning = _probe_item_reasoning(item) if item else {}
        if not reasoning.get("reasoning_default_enabled") and not reasoning.get("reasoning_efforts"):
            continue
        score = _probe_item_intelligence(item)
        price = _probe_item_cost(item, "prompt")
        if score is None or not price:
            continue
        rank = (-lab_tier(probe_id), score / price)
        if best is None or rank > best[1]:
            best = (probe_id, rank)
    return best


def _intelligence_by_probe_id(deployed: dict[str, dict]) -> dict[str, float]:
    """Map each scored model of a listing to its intelligence index.

    A model's own benchmark wins; a line's newest release OpenRouter has not
    benchmarked yet carries its predecessor's score (see :func:`_scored_lines`).

    Args:
        deployed: Probe index of ``vendor/model`` id to raw item.

    Returns:
        ``{probe id: index}`` for every model with an own or inherited score.
    """
    scores = {
        probe_id: score for probe_id, item in deployed.items() if (score := _probe_item_intelligence(item)) is not None
    }
    for items in _practical_candidates(deployed).values():
        for model_id, _item, score in _scored_lines(items).values():
            if score is not None:
                scores.setdefault(model_id, score)
    return scores


def _strongest_probe_id(deployed: dict[str, dict], featured: set[str]) -> tuple[str, float] | None:
    """Pick the featured model with the highest intelligence index that thinks by default.

    Reflection Automatic runs this model: reflection turns are few and decide
    what the optimizer tries next, so strength beats price there. A new
    release that inherits its predecessor's score qualifies on release day;
    a tie goes to the newer release.

    Args:
        deployed: Probe index of ``vendor/model`` id to raw item.
        featured: Probe ids already chosen by :func:`_featured_probe_ids`.

    Returns:
        ``(probe id, index)`` of the winner, or ``None`` when no featured
        model thinks by default with a score.
    """
    scores = _intelligence_by_probe_id(deployed)
    best: tuple[str, float, int] | None = None
    for probe_id in sorted(featured):
        item = deployed.get(probe_id)
        score = scores.get(probe_id)
        if not item or score is None or not _probe_item_reasoning(item).get("reasoning_default_enabled"):
            continue
        created = item.get("created") if isinstance(item.get("created"), int) else 0
        if best is None or (score, created) > (best[1], best[2]):
            best = (probe_id, score, created)
    return (best[0], best[1]) if best else None


def _dominates(a: tuple[str, dict, float], b: tuple[str, dict, float]) -> bool:
    """Report whether model ``a`` makes ``b`` redundant: as good or better and no pricier, strictly on one.

    Args:
        a: ``(probe id, item, score)`` of the challenger.
        b: ``(probe id, item, score)`` of the model under test.

    Returns:
        ``True`` when ``a`` scores at least as high and costs at most as much
        per input token as ``b``, and beats it on one of the two.
    """
    price_a = _probe_item_cost(a[1], "prompt") or 0.0
    price_b = _probe_item_cost(b[1], "prompt") or 0.0
    return a[2] >= b[2] and price_a <= price_b and (a[2] > b[2] or price_a < price_b)


def _reasoning_match_key(model_value: str) -> str:
    """Normalize a model id so one model matches across providers' spellings.

    LiteLLM writes ``anthropic/claude-opus-4-5-20251101`` where OpenRouter
    writes ``openrouter/anthropic/claude-opus-4.5``; both reduce to
    ``claude-opus-4-5``.

    Args:
        model_value: A catalog ``value`` or bare model id.

    Returns:
        The lowercase last path segment with the date dropped and dots as
        dashes.
    """
    name = model_value.rsplit("/", 1)[-1].lower()
    return _ANY_DATE_SUFFIX_RE.sub("", name).replace(".", "-")


def with_platform_reasoning(models: list[CatalogModel]) -> list[CatalogModel]:
    """Fill unknown reasoning controls from the platform's OpenRouter snapshot.

    The BYOK catalog comes from LiteLLM's static registry, which knows only
    whether a model reasons, not its effort ladder or defaults. OpenRouter
    publishes those per model, so a direct-provider model borrows them from
    its OpenRouter twin. Models already carrying a ladder, and every model
    when no platform snapshot exists yet, pass through unchanged.

    Args:
        models: Catalog entries to enrich.

    Returns:
        The entries, with reasoning fields copied from a matching platform
        model where the entry had none.
    """
    snapshot = _cached_response
    if snapshot is None:
        return models
    by_key: dict[str, CatalogModel] = {}
    for model in snapshot.models:
        if model.reasoning_efforts is None:
            continue
        key = _reasoning_match_key(model.value)
        exact = model.value.rsplit("/", 1)[-1].lower().replace(".", "-") == key
        if exact or key not in by_key:
            by_key[key] = model
    enriched: list[CatalogModel] = []
    for model in models:
        twin = by_key.get(_reasoning_match_key(model.value)) if model.reasoning_efforts is None else None
        if twin is None:
            enriched.append(model)
            continue
        enriched.append(
            model.model_copy(
                update={
                    "supports_thinking": twin.supports_thinking,
                    "reasoning_efforts": twin.reasoning_efforts,
                    "default_reasoning_effort": twin.default_reasoning_effort,
                    "reasoning_mandatory": twin.reasoning_mandatory,
                    "reasoning_default_enabled": twin.reasoning_default_enabled,
                }
            )
        )
    return enriched


def _probe_item_is_chat(item: dict) -> bool:
    """Decide whether a probe item plausibly outputs text (is a chat model).

    Skips embedding/image-generation/TTS models by checking OpenRouter's
    ``architecture.output_modalities``: a model is chat when that list
    contains ``"text"``. When no modality info exists at all the item is
    treated as chat (the conservative default for providers that don't
    annotate modalities).

    Args:
        item: The raw provider item dict (may be empty for string entries).

    Returns:
        ``True`` to include the model, ``False`` to skip a clearly non-chat one.
    """
    arch = item.get("architecture")
    if isinstance(arch, dict):
        modalities = arch.get("output_modalities")
        if isinstance(modalities, list):
            return "text" in modalities
    return True


def _probe_deployed_models(provider_slug: str, data_center: _DataCenter) -> dict[str, dict] | None:
    """Query a single data center's OpenAI-compatible ``/models`` endpoint.

    Args:
        provider_slug: LiteLLM provider key (e.g. ``"openai"``, ``"fireworks_ai"``).
        data_center: The specific endpoint to probe (its own ``models_url``
            and ``env_var``).

    Returns:
        A mapping of each deployed model ID to the raw provider item dict
        describing it (an empty dict for bare-string entries), or ``None``
        when no live check is configured for it, the API key is missing, the
        request fails, or the response shape is unexpected. The caller should
        then fall back to the LiteLLM ``valid_set`` heuristic. Membership
        checks (``id in result``) continue to work against the dict keys.
    """
    url = data_center.models_url
    if not url:
        return None
    env_var = data_center.env_var
    if not env_var:
        return None
    api_key = os.getenv(env_var)
    if not api_key:
        return None
    return _fetch_models_index(provider_slug, url, api_key)


def _fetch_json(provider_slug: str, url: str, headers: dict[str, str]) -> Any:
    """GET a provider listing and decode its JSON body.

    Args:
        provider_slug: LiteLLM provider key, used only for log context.
        url: The listing endpoint.
        headers: Request headers, credential included.

    Returns:
        The decoded body, or ``None`` when the request or decoding fails.
    """
    # Fireworks (and some other providers) reject the default
    # ``Python-urllib/3.x`` UA with 403 — set an explicit one.
    headers = {"Accept": "application/json", "User-Agent": "skynet-catalog/0.1", **headers}
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=4) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        return json.loads(body)
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("model-list probe for %s failed: %s", provider_slug, exc)
        return None


def _fetch_models_index(provider_slug: str, url: str, api_key: str) -> dict[str, dict] | None:
    """Fetch an OpenAI-compatible ``/models`` listing with an explicit key.

    Args:
        provider_slug: LiteLLM provider key, used only for log context.
        url: The provider's ``/models`` endpoint.
        api_key: Bearer credential presented to the endpoint.

    Returns:
        A mapping of each listed model ID to its raw item dict (empty for
        bare-string entries), or ``None`` when the request fails or the
        response shape is unexpected.
    """
    data = _fetch_json(provider_slug, url, {"Authorization": f"Bearer {api_key}"})
    raw = data.get("data") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return None

    items: dict[str, dict] = {}
    for item in raw:
        if isinstance(item, dict):
            val = item.get("id") or item.get("name")
            if isinstance(val, str) and val:
                items[val] = item
        elif isinstance(item, str):
            items[item] = {}
    return items


# Gemini lists image, speech, embedding and video generators beside its chat
# models, all under ``generateContent``; only the names tell them apart.
_GEMINI_NON_CHAT_RE = re.compile(r"image|tts|audio|embed|aqa|imagen|veo|robotics|computer-use")


def _fetch_native_models_index(provider_slug: str, api_key: str) -> dict[str, dict] | None:
    """List a key's models from a provider whose listing isn't OpenAI-compatible.

    Every returned item is stamped ``type: chat`` so a model the static
    registry has never heard of still reaches the picker.

    Args:
        provider_slug: ``"anthropic"``, ``"gemini"`` or ``"cohere_chat"``.
        api_key: The user's provider secret, sent only in a request header.

    Returns:
        The chat models keyed by bare model ID, or ``None`` for any other
        provider or when the request fails.
    """
    items: dict[str, dict] = {}
    if provider_slug == "anthropic":
        data = _fetch_json(
            provider_slug,
            "https://api.anthropic.com/v1/models?limit=1000",
            {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        )
        raw = data.get("data") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            return None
        for item in raw:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                items[item["id"]] = {"type": "chat"}
        return items
    if provider_slug == "gemini":
        data = _fetch_json(
            provider_slug,
            "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000",
            {"x-goog-api-key": api_key},
        )
        raw = data.get("models") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            return None
        for item in raw:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            model_id = item["name"].removeprefix("models/")
            methods = item.get("supportedGenerationMethods")
            if not isinstance(methods, list) or "generateContent" not in methods:
                continue
            if _GEMINI_NON_CHAT_RE.search(model_id):
                continue
            items[model_id] = {
                "type": "chat",
                "supports_reasoning": bool(item.get("thinking")),
                "context_length": item.get("inputTokenLimit"),
            }
        return items
    if provider_slug == "cohere_chat":
        data = _fetch_json(
            provider_slug,
            "https://api.cohere.com/v1/models?endpoint=chat&page_size=1000",
            {"Authorization": f"Bearer {api_key}"},
        )
        raw = data.get("models") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            return None
        for item in raw:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                items[item["name"]] = {"type": "chat", "context_length": item.get("context_length")}
        return items
    return None


def _probe_all_providers() -> dict[tuple[str, str | None], dict[str, dict] | None]:
    """Probe every configured data center in parallel, capped at 8 workers.

    Each ``(provider, data center)`` pair is probed independently since they
    are distinct endpoints that may serve different model sets.

    Returns:
        A mapping of ``(provider_slug, data_center_label)`` to a dict of
        deployed model ID → raw provider item reported by that endpoint, or
        ``None`` when the live probe was skipped or failed for it.
    """
    results: dict[tuple[str, str | None], dict[str, dict] | None] = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {}
        for slug in _PLATFORM_PROVIDERS:
            for dc in _provider_data_centers(slug):
                futures[executor.submit(_probe_deployed_models, slug, dc)] = (slug, dc.label)
        for fut in as_completed(futures):
            results[futures[fut]] = fut.result()
    return results


def get_catalog() -> ModelCatalogResponse:
    """Build a model catalog from LiteLLM's bundled model registry.

    Filters to ``mode == "chat"`` models, de-duplicates dated variants, and
    marks each model ``available`` by either the provider's live ``/models``
    probe (when reachable) or LiteLLM's API-key heuristic. Models that fail
    a successful live probe are dropped from the catalog.

    Returns:
        A :class:`ModelCatalogResponse` containing the active providers and
        the sorted list of available chat models.
    """

    cost: dict[str, dict] = litellm.model_cost
    try:
        valid_set: set[str] = set(litellm.get_valid_models())
    except (OSError, RuntimeError, KeyError, ValueError, AttributeError, TypeError) as exc:
        # LiteLLM probes provider env vars + endpoints; the failure surface is
        # network-shaped (OSError, RuntimeError) or shape-shaped (Key/Value/
        # Attribute/Type). Catching the union avoids hiding real bugs while
        # still letting the catalog degrade gracefully when one provider
        # misbehaves.
        logger.warning("litellm.get_valid_models() failed: %s; marking none as available", exc)
        valid_set = set()

    deployed_by_dc = _probe_all_providers()
    for (probe_slug, _dc_label), deployed in deployed_by_dc.items():
        if probe_slug == "openrouter" and deployed:
            openrouter_prices.remember(deployed.values())

    seen_providers: dict[tuple[str, str | None], CatalogProvider] = {}
    models: list[CatalogModel] = []
    base_names_seen: set[str] = set()
    # Tracks ``(catalog value, dc label)`` already emitted from the registry so
    # the probe-discovery pass below doesn't re-add a model LiteLLM knew about.
    emitted: set[tuple[str, str | None]] = set()

    for model_id, meta in cost.items():
        if meta.get("mode") != "chat":
            continue

        provider_slug: str = meta.get("litellm_provider", "unknown")

        if provider_slug not in _PLATFORM_PROVIDERS:
            continue

        base_name = _DATE_SUFFIX_RE.sub("", model_id)
        if base_name != model_id and base_name in cost:
            continue

        if base_name in base_names_seen:
            continue
        base_names_seen.add(base_name)

        # dspy.LM rejects un-prefixed IDs — always emit ``provider/model``.
        prefixed_id = model_id if "/" in model_id else f"{provider_slug}/{model_id}"

        # Probe responses use the provider's native ID shape (e.g.
        # ``accounts/fireworks/models/...``), not the ``fireworks_ai/...``
        # catalog prefix — match against the un-prefixed form.
        canonical_id = model_id.split("/", 1)[1] if model_id.startswith(f"{provider_slug}/") else model_id

        provider_label = _PROVIDER_META[provider_slug][0]
        for dc in _provider_data_centers(provider_slug):
            deployed = deployed_by_dc.get((provider_slug, dc.label))
            if deployed is not None:
                available = canonical_id in deployed or model_id in deployed
                if not available:
                    continue
            else:
                available = model_id in valid_set or prefixed_id in valid_set

            provider_key = (provider_slug, dc.label)
            if provider_key not in seen_providers:
                has_key = bool(dc.env_var and os.getenv(dc.env_var))
                seen_providers[provider_key] = CatalogProvider(
                    slug=provider_slug,
                    label=provider_label,
                    data_center=dc.label,
                    env_var=dc.env_var,
                    default_base_url=dc.base_url,
                    has_env_key=has_key,
                )

            emitted.add((prefixed_id, dc.label))
            probe_item = (deployed or {}).get(canonical_id) or (deployed or {}).get(model_id) or {}
            reasoning = _probe_item_reasoning(probe_item)
            # The live probe is what the provider actually accepts; LiteLLM's
            # static flag only decides for models the probe did not list.
            if probe_item:
                thinks = _probe_item_supports_thinking(probe_item) or bool(reasoning["reasoning_efforts"])
            else:
                thinks = bool(meta.get("supports_reasoning"))
            models.append(
                CatalogModel(
                    value=prefixed_id,
                    label=_make_label(model_id),
                    provider=provider_slug,
                    data_center=dc.label,
                    supports_thinking=thinks,
                    **reasoning,
                    supports_vision=bool(meta.get("supports_vision")),
                    available=available,
                    max_input_tokens=meta.get("max_input_tokens"),
                    # The live listing is what the provider bills today; LiteLLM's
                    # static table lags it and only fills in unprobed models.
                    input_cost_per_token=_probe_item_cost(probe_item, "prompt")
                    or _positive_cost(meta, "input_cost_per_token"),
                    output_cost_per_token=_probe_item_cost(probe_item, "completion")
                    or _positive_cost(meta, "output_cost_per_token"),
                )
            )

    # Second pass: surface chat models the live probe reports that LiteLLM's
    # static registry never listed (e.g. brand-new OpenRouter models). The
    # registry pass above can only emit models present in ``litellm.model_cost``;
    # this pass closes that gap from the provider's own ``/models`` response.
    for (probe_slug, dc_label), deployed in deployed_by_dc.items():
        if deployed is None:
            continue
        provider_label = _PROVIDER_META[probe_slug][0]
        dc = next(
            (c for c in _provider_data_centers(probe_slug) if c.label == dc_label),
            None,
        )
        if dc is None:
            continue
        for probe_id, item in deployed.items():
            if not _probe_item_is_chat(item):
                continue
            value = _probe_prefixed_id(probe_slug, probe_id)
            if (value, dc_label) in emitted:
                continue
            emitted.add((value, dc_label))

            provider_key = (probe_slug, dc_label)
            if provider_key not in seen_providers:
                has_key = bool(dc.env_var and os.getenv(dc.env_var))
                seen_providers[provider_key] = CatalogProvider(
                    slug=probe_slug,
                    label=provider_label,
                    data_center=dc_label,
                    env_var=dc.env_var,
                    default_base_url=dc.base_url,
                    has_env_key=has_key,
                )

            reasoning = _probe_item_reasoning(item)
            models.append(
                CatalogModel(
                    value=value,
                    label=_make_label(probe_id),
                    provider=probe_slug,
                    data_center=dc_label,
                    supports_thinking=_probe_item_supports_thinking(item),
                    **reasoning,
                    supports_vision=_probe_item_supports_vision(item),
                    available=True,
                    max_input_tokens=_probe_item_max_input_tokens(item),
                    input_cost_per_token=_probe_item_cost(item, "prompt"),
                    output_cost_per_token=_probe_item_cost(item, "completion"),
                )
            )

    global _hidden_model_values, _hidden_effortless_values, _featured_scores
    featured_values: set[str] = set()
    practical_by_dc: dict[tuple[str, str | None], set[str]] = {}
    best_value: tuple[str, tuple[int, float]] | None = None
    strongest: tuple[str, float] | None = None
    display_names: dict[str, str] = {}
    scores: dict[str, float] = {}
    indexes: dict[str, float] = {}
    for (probe_slug, dc_label), deployed in deployed_by_dc.items():
        if not deployed:
            continue
        for probe_id, item in deployed.items():
            name = _probe_item_display_name(item)
            if name:
                display_names[_probe_prefixed_id(probe_slug, probe_id)] = name
        for probe_id, index in _intelligence_by_probe_id(deployed).items():
            indexes[_probe_prefixed_id(probe_slug, probe_id)] = index
        practical_ids = _practical_probe_ids(deployed)
        if practical_ids is not None:
            practical_by_dc[(probe_slug, dc_label)] = {
                _probe_prefixed_id(probe_slug, probe_id) for probe_id in practical_ids
            }
        featured_ids = _featured_probe_ids(deployed)
        featured_values.update(_probe_prefixed_id(probe_slug, probe_id) for probe_id in featured_ids)
        for probe_id in featured_ids:
            score = _probe_item_intelligence(deployed[probe_id])
            if score is not None:
                scores[_probe_prefixed_id(probe_slug, probe_id)] = score
        winner = _best_value_probe_id(deployed, featured_ids)
        if winner and (best_value is None or winner[1] > best_value[1]):
            best_value = (_probe_prefixed_id(probe_slug, winner[0]), winner[1])
        strong = _strongest_probe_id(deployed, featured_ids)
        if strong and (strongest is None or strong[1] > strongest[1]):
            strongest = (_probe_prefixed_id(probe_slug, strong[0]), strong[1])
    default_value = best_value[0] if best_value else None
    strongest_value = strongest[0] if strongest else None

    def _practical(m: CatalogModel) -> bool:
        allowed = practical_by_dc.get((m.provider, m.data_center))
        return allowed is None or m.value in allowed

    _hidden_model_values = frozenset(
        m.value
        for m in models
        if m.available and supports_effort_control(m) and not _REMOVED_MODEL_RE.search(m.value) and not _practical(m)
    )
    _hidden_effortless_values = frozenset(
        m.value
        for m in models
        if m.available
        and not supports_effort_control(m)
        and not _REMOVED_MODEL_RE.search(m.value)
        and not _practical(m)
    )
    models = [m for m in models if _practical(m) and not excluded_from_managed(m.value)]
    models = [
        m.model_copy(
            update={
                "display_name": display_names.get(m.value),
                "intelligence_index": indexes.get(m.value),
                "lab_tier": lab_tier(m.value),
                **(
                    {
                        "featured": True,
                        "is_default": m.value == default_value,
                        "is_strongest": m.value == strongest_value,
                    }
                    if m.value in featured_values
                    else {}
                ),
            }
        )
        for m in models
        if m.available and not _REMOVED_MODEL_RE.search(m.value)
    ]
    _featured_scores = scores

    models.sort(key=lambda m: (m.provider, m.data_center or "", m.value))

    available_dcs = {(m.provider, m.data_center) for m in models}
    providers = sorted(
        (p for p in seen_providers.values() if (p.slug, p.data_center) in available_dcs or not p.env_var),
        key=lambda p: (not p.has_env_key, p.slug, p.data_center or ""),
    )

    return ModelCatalogResponse(providers=providers, models=models)


_cached_response: ModelCatalogResponse | None = None
_cached_at_monotonic: float = 0.0
_cache_lock = Lock()
_refresh_in_flight = False
_refresh_thread: Thread | None = None


def catalog_snapshot() -> ModelCatalogResponse | None:
    """Return the last built catalog without ever building one.

    Returns:
        The cached catalog, or ``None`` before the boot prewarm lands.
    """
    return with_effort_controls(_cached_response) if _cached_response is not None else None


def lowest_effort_from_low(entry: CatalogModel) -> str | None:
    """Return the lowest effort from ``low`` up on a catalog model's effort ladder.

    Args:
        entry: The model's catalog row.

    Returns:
        That effort, or ``None`` when the row lists no effort from ``low`` up.
    """
    floor = REASONING_EFFORTS.index("low")
    return next(
        (e for e in entry.reasoning_efforts or [] if e in REASONING_EFFORTS and REASONING_EFFORTS.index(e) >= floor),
        None,
    )


def unpicked_effort(entry: CatalogModel) -> str | None:
    """Return the effort to send a catalog model when the caller picked none.

    An unset effort sends OpenRouter no ``reasoning`` param, so an opt-in
    thinker (Claude, Gemini) would answer without thinking at all. Such a
    model gets the lowest effort from ``low`` up on its ladder; a model
    that thinks by default gets its catalog default.

    Args:
        entry: The model's catalog row.

    Returns:
        The catalog default effort when the model thinks by default, its
        low effort when it only thinks on request, else ``None`` (no
        reasoning control).
    """
    if entry.reasoning_default_enabled:
        return entry.default_reasoning_effort
    return lowest_effort_from_low(entry)


def default_effort_for(model: str | None) -> str | None:
    """Return the effort to send ``model`` when the caller picked none.

    Reads only the built catalog, so an LM build never blocks on a cold
    catalog build. See :func:`unpicked_effort` for the policy.

    Args:
        model: Catalog model id (``CatalogModel.value``), or ``None``.

    Returns:
        :func:`unpicked_effort` of the model's catalog row, or ``None`` for
        an unknown model or before the catalog is built.
    """
    snapshot = _cached_response
    if not model or snapshot is None:
        return None
    entry = next((m for m in snapshot.models if m.value == model), None)
    return unpicked_effort(entry) if entry is not None else None


def agent_model_id(configured: str = "") -> str:
    """Return the model a server-side agent or background job runs on.

    Reads only the already-built catalog: background jobs and engines must
    never block on a cold catalog build, so before the boot prewarm lands
    they run the static fallback.

    Args:
        configured: An operator-set model id (e.g. ``CODE_AGENT_MODEL``);
            it wins whenever non-empty.

    Returns:
        The configured id, else the catalog's flagged best-value default,
        else :data:`DEFAULT_AGENT_MODEL_ID`.
    """
    if configured.strip():
        return configured.strip()
    snapshot = catalog_snapshot()
    default = next((m.value for m in snapshot.models if m.is_default), None) if snapshot else None
    return default or DEFAULT_AGENT_MODEL_ID


def strongest_model_id(configured: str = "") -> str:
    """Return the model a server-side job runs when it needs the most careful reading.

    Reads only the already-built catalog, like :func:`agent_model_id`.

    Args:
        configured: An operator-set model id (e.g. ``RULE_JUDGE_MODEL``);
            it wins whenever non-empty.

    Returns:
        The configured id, else the catalog's flagged strongest model, else
        :func:`agent_model_id`.
    """
    if configured.strip():
        return configured.strip()
    snapshot = catalog_snapshot()
    strongest = next((m.value for m in snapshot.models if m.is_strongest), None) if snapshot else None
    return strongest or agent_model_id()


def _refresh_catalog_in_background() -> None:
    """Rebuild the catalog and swap it into the cache; never raises.

    Run from a daemon thread by :func:`get_catalog_cached` when the cache
    is stale-but-present and by :func:`prewarm_catalog` at server boot.
    The ``_refresh_in_flight`` guard makes repeated concurrent triggers
    no-ops so a burst of requests doesn't spawn a thundering herd of
    refresh threads.
    """
    global _cached_response, _cached_at_monotonic, _refresh_in_flight
    fresh: ModelCatalogResponse | None = None
    try:
        fresh = get_catalog()
    except Exception:
        logger.exception("Model catalog background refresh failed; keeping previous snapshot")
    finally:
        # Clearing in finally (not just the success path) is the point: a failed
        # refresh must still release the guard, or it wedges True and no future
        # stale read can ever re-trigger a refresh for the process's lifetime.
        with _cache_lock:
            if fresh is not None:
                _cached_response = fresh
                _cached_at_monotonic = time.monotonic()
            _refresh_in_flight = False
    if fresh is not None:
        logger.info(
            "Model catalog refreshed in background: %d providers, %d models",
            len(fresh.providers),
            len(fresh.models),
        )


def _kick_background_refresh() -> None:
    """Spawn the background refresh thread if one isn't already running."""
    global _refresh_in_flight, _refresh_thread
    with _cache_lock:
        if _refresh_in_flight:
            return
        _refresh_in_flight = True
        # Set with the flag, so a caller that sees the flag can join this thread.
        thread = _refresh_thread = Thread(target=_refresh_catalog_in_background, name="catalog-refresh", daemon=True)
    thread.start()


def with_zero_retention(catalog: ModelCatalogResponse) -> ModelCatalogResponse:
    """Flag the models OpenRouter can serve with zero data retention.

    Args:
        catalog: A cached catalog, left unmodified.

    Returns:
        A copy whose models carry the current ``zero_retention`` flag.
    """
    zdr = zero_retention_models()
    return catalog.model_copy(
        update={
            "models": [
                model.model_copy(update={"zero_retention": model.value.removeprefix("openrouter/") in zdr})
                for model in catalog.models
            ]
        }
    )


def get_catalog_cached() -> ModelCatalogResponse:
    """Return the catalog with stale-while-revalidate semantics.

    Behaviour matrix:
      * Cache present + fresh (within ``model_catalog_ttl_seconds``) →
        return immediately.
      * Cache present + stale → return the stale snapshot immediately and
        kick off a background refresh. Subsequent calls keep returning
        the stale snapshot until the refresh lands, then they see the
        new one. A single in-flight refresh guard prevents thundering
        herds.
      * Cache absent (true cold start) → synchronously build. This path
        only fires when the boot-time :func:`prewarm_catalog` hasn't
        landed yet — under normal operation the boot prewarm beats any
        user request.
      * TTL of ``0`` disables caching entirely (every call rebuilds).

    Returns:
        The cached ``ModelCatalogResponse`` — possibly stale but never
        empty after the first successful build.
    """
    global _cached_response, _cached_at_monotonic
    ttl = float(settings.model_catalog_ttl_seconds)
    now = time.monotonic()
    fresh = ttl > 0.0 and _cached_response is not None and (now - _cached_at_monotonic) < ttl
    if fresh:
        return with_effort_controls(_cached_response)  # type: ignore[arg-type]
    if _cached_response is not None:
        # Stale but usable — serve it now, refresh in the background.
        if ttl > 0.0:
            _kick_background_refresh()
        return with_effort_controls(_cached_response)

    with _cache_lock:
        # Re-check under the lock so we don't rebuild twice when callers
        # race in during the first cold-start miss.
        if _cached_response is not None:
            return with_effort_controls(_cached_response)
        _cached_response = get_catalog()
        _cached_at_monotonic = time.monotonic()
        logger.info(
            "Model catalog built (cold start): %d providers, %d models",
            len(_cached_response.providers),
            len(_cached_response.models),
        )
        return with_effort_controls(_cached_response)


def prewarm_catalog() -> None:
    """Trigger the first catalog build in a background thread.

    Called from the FastAPI lifespan startup hook so the cache is hot
    by the time the first ``list_models_for_agent`` request lands. The
    boot itself is unaffected (we don't await this); workers come online
    in parallel with the catalog probe.
    """
    Thread(target=zero_retention_models, daemon=True).start()
    if _cached_response is not None:
        return
    _kick_background_refresh()


def get_byok_catalog() -> ModelCatalogResponse:
    """Build the bring-your-own-key model catalog from LiteLLM's registry.

    Unlike :func:`get_catalog`, availability is not gated on a platform API key:
    every chat model of a BYOK-offered provider is listed and marked available,
    because a BYOK run pays with the user's own key, not the platform's. The
    client narrows this to the providers the signed-in user has actually
    connected. There is no live network probe — purely the bundled
    ``litellm.model_cost`` registry — so the build is cheap and deterministic.

    Returns:
        A :class:`ModelCatalogResponse` of every BYOK-offered provider's chat
        models, each ``available=True``, grouped by provider.
    """
    cost: dict[str, dict] = litellm.model_cost
    seen_providers: dict[str, CatalogProvider] = {}
    models: list[CatalogModel] = []
    base_names_seen: set[str] = set()

    for model_id, meta in cost.items():
        if meta.get("mode") != "chat":
            continue
        provider_slug: str = meta.get("litellm_provider", "unknown")
        if provider_slug not in _BYOK_CATALOG_PROVIDERS:
            continue

        base_name = _DATE_SUFFIX_RE.sub("", model_id)
        if base_name != model_id and base_name in cost:
            continue
        if base_name in base_names_seen:
            continue
        base_names_seen.add(base_name)

        # dspy.LM rejects un-prefixed IDs — always emit ``provider/model``.
        prefixed_id = model_id if "/" in model_id else f"{provider_slug}/{model_id}"

        if provider_slug not in seen_providers:
            seen_providers[provider_slug] = CatalogProvider(
                slug=provider_slug,
                label=_PROVIDER_META[provider_slug][0],
                data_center=None,
                env_var=None,
                default_base_url=None,
                has_env_key=False,
            )

        models.append(
            CatalogModel(
                value=prefixed_id,
                label=_make_label(model_id),
                provider=provider_slug,
                data_center=None,
                supports_thinking=bool(meta.get("supports_reasoning")),
                supports_vision=bool(meta.get("supports_vision")),
                available=True,
                max_input_tokens=meta.get("max_input_tokens"),
                input_cost_per_token=_positive_cost(meta, "input_cost_per_token"),
                output_cost_per_token=_positive_cost(meta, "output_cost_per_token"),
            )
        )

    models = [m for m in models if not _REMOVED_MODEL_RE.search(m.value)]
    models.sort(key=lambda m: (m.provider, m.value))
    providers = sorted(seen_providers.values(), key=lambda p: p.slug)
    return ModelCatalogResponse(providers=providers, models=models)


_byok_cached: ModelCatalogResponse | None = None
# The enriched view, tagged with the platform snapshot it borrowed from so a
# catalog refresh re-derives it.
_byok_enriched: tuple[ModelCatalogResponse, ModelCatalogResponse] | None = None


def get_byok_catalog_cached() -> ModelCatalogResponse:
    """Return the BYOK catalog, built once per process.

    The bundled registry is static for a process's lifetime, so a single lazy
    build (no TTL, no background refresh) is enough — unlike the platform catalog
    whose availability depends on live provider probes. Reasoning controls
    are layered on from the current platform snapshot, re-derived whenever
    that snapshot refreshes.

    Returns:
        The cached BYOK :class:`ModelCatalogResponse`.
    """
    global _byok_cached, _byok_enriched
    if _byok_cached is None:
        _byok_cached = get_byok_catalog()
    snapshot = _cached_response
    if snapshot is None:
        return with_effort_controls(_byok_cached)
    if _byok_enriched is None or _byok_enriched[0] is not snapshot:
        enriched = _byok_cached.model_copy(update={"models": with_platform_reasoning(_byok_cached.models)})
        _byok_enriched = (snapshot, enriched)
    return with_effort_controls(_byok_enriched[1])


def probe_byok_provider_models(provider_slug: str, api_key: str) -> dict[str, dict] | None:
    """List the models a user's own key can reach at a provider's native endpoint.

    Args:
        provider_slug: LiteLLM provider key (e.g. ``"openai"``, ``"openrouter"``).
        api_key: The user's decrypted provider secret.

    Returns:
        The provider's model index keyed by model ID, or ``None`` for an
        unknown provider or a failed request — the caller then keeps the
        static list.
    """
    meta = _PROVIDER_META.get(provider_slug)
    if meta is None:
        return None
    url = next((dc.models_url for dc in meta[1] if dc.label is None and dc.models_url), None)
    if not url:
        return _fetch_native_models_index(provider_slug, api_key)
    return _fetch_models_index(provider_slug, url, api_key)


def _probe_item_declares_chat(item: dict) -> bool:
    """Decide whether a probe item explicitly identifies itself as a chat model.

    Stricter than :func:`_probe_item_is_chat`: an item with no modality or type
    signal is *not* chat here, because this gate admits models the static
    registry has never heard of and OpenAI's listing mixes in embeddings,
    Whisper and DALL·E entries that carry no annotation at all.

    Args:
        item: The raw provider item dict.

    Returns:
        ``True`` only when the item declares text output or a chat type.
    """
    arch = item.get("architecture")
    if isinstance(arch, dict):
        modalities = arch.get("output_modalities")
        if isinstance(modalities, list):
            return "text" in modalities
    return item.get("type") == "chat"


def narrow_models_to_served(
    provider_slug: str,
    static_models: list[CatalogModel],
    deployed: dict[str, dict],
    *,
    byok_provider: str,
) -> list[CatalogModel]:
    """Keep only the static models a live ``/models`` listing actually serves.

    A registry entry survives when its bare ID (or full prefixed value) appears
    in ``deployed``. Listed models absent from the registry are appended only
    when the item explicitly declares itself a chat model, so a key that
    unlocks a brand-new model still surfaces it without admitting embeddings
    or audio endpoints.

    Args:
        provider_slug: LiteLLM provider key the static entries belong to.
        static_models: Registry-derived entries for that provider.
        deployed: Live model index from :func:`probe_byok_provider_models`.
        byok_provider: Vault slug stamped on every returned model.

    Returns:
        The served subset of ``static_models`` followed by explicit-chat
        probe-only models, all tagged with ``byok_provider``.
    """
    kept: list[CatalogModel] = []
    seen: set[str] = set()
    for model in static_models:
        bare = model.value.removeprefix(f"{provider_slug}/")
        if bare in deployed or model.value in deployed:
            item = deployed.get(bare, deployed.get(model.value, {}))
            reasoning = _probe_item_reasoning(item) if "reasoning" in item else {}
            kept.append(model.model_copy(update={"byok_provider": byok_provider, **reasoning}))
            seen.add(bare)
    for model_id, item in deployed.items():
        bare = model_id.removeprefix(f"{provider_slug}/")
        if bare in seen or not _probe_item_declares_chat(item):
            continue
        seen.add(bare)
        kept.append(
            CatalogModel(
                value=_probe_prefixed_id(provider_slug, model_id),
                label=_make_label(model_id),
                provider=provider_slug,
                byok_provider=byok_provider,
                supports_thinking=_probe_item_supports_thinking(item),
                **(_probe_item_reasoning(item) if "reasoning" in item else {}),
                supports_vision=_probe_item_supports_vision(item),
                available=True,
                max_input_tokens=_probe_item_max_input_tokens(item),
            )
        )
    kept.sort(key=lambda m: m.value)
    return [model for model in with_platform_reasoning(kept) if supports_effort_control(model)]


def is_hidden_model(name: str) -> bool:
    """Return whether a model id is real but left out of the menus as impractical.

    Args:
        name: Catalog model id.

    Returns:
        ``True`` when the last catalog build dropped it, so picks made before
        it dropped out keep working.
    """
    return name in _hidden_model_values


def models_for_role(role: str | None = None) -> list[CatalogModel]:
    """Return the catalog models one model role may pick.

    Args:
        role: The role the model fills, e.g. ``"hook"`` or ``"judge"``;
            ``None`` for a role with no extra models.

    Returns:
        The catalog models, plus the classifier-only models for the
        classifier roles.
    """
    models = list(get_catalog_cached().models)
    if role in CLASSIFIER_ROLES:
        listed = {m.value for m in models}
        models += [
            CatalogModel(value=value, label=_make_label(value), provider="openrouter", available=True)
            for value in sorted(CLASSIFIER_ONLY_MODELS - listed)
        ]
    return models


def require_known_model(model: str | None, *, role: str | None = None) -> None:
    """Reject a model id that is not in the curated catalog.

    Guards every endpoint that lets the client pick the LM for a
    platform-billed call: only catalog models may spend the account balance.

    Args:
        model: LiteLLM model id; empty/None passes (the server default runs).
        role: The role the model fills; the classifier roles also accept the
            classifier-only models.

    Raises:
        DomainError: 422 when the id is not a catalog model.
    """
    name = str(model or "").strip()
    if role in CLASSIFIER_ROLES and name in CLASSIFIER_ONLY_MODELS:
        return
    if (
        name
        and not is_hidden_model(name)
        and not is_effortless_model(name)
        and all(entry.value != name for entry in get_catalog_cached().models)
    ):
        raise DomainError("models.unknown_model", status=422)
