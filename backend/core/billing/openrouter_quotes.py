"""Bound final OpenRouter text, embeddings and decisions requests using live endpoint prices and routing caps."""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import httpx

from .operation_pricing import ChargePolicy, OperationQuote, UnpricedOperationError, exact_nonnegative, operation_quote

_RATE_FIELDS = {
    "prompt",
    "completion",
    "request",
    "image",
    "input_cache_read",
    "input_cache_write",
    "input_cache_write_1h",
    "internal_reasoning",
}
# _check_text_request rejects audio parts, the audio field, plugins, web search options, provider
# tools and media output modalities, so a text request cannot be billed under these categories
# even though most Gemini, Claude and audio-capable endpoints publish nonzero rates for them.
_UNREACHABLE_RATE_FIELDS = {"audio", "input_audio_cache", "audio_output", "web_search", "image_output"}
# Conditional rows (long-context tiers, weekend or off-peak overrides) carry their trigger next to
# the rates they replace; the bound takes the maximum over every row rather than predicting the trigger.
_CONDITION_FIELDS = {
    "tiers",
    "overrides",
    "min_context",
    "max_context",
    "min_prompt_tokens",
    "utc_days",
    "utc_start",
    "utc_end",
    "discount",
}
_UNSUPPORTED_REQUEST_FIELDS = {"plugins", "web_search_options", "audio", "image_config", "models", "route"}
# Byte-level BPE tokenizers emit at least one UTF-8 byte per token, so the serialized body bounds
# the tokens its text becomes. Providers add what the body does not carry: chat-template role
# markers and Anthropic's tool-use system prompt (about 350 tokens). Measured live on Claude Haiku
# 4.5 and Gemini 3 Flash, plain, multi-turn, Hebrew and system bodies stayed under their byte count
# and one forced tool came to 214 tokens over it; this allowance leaves a wide margin above that.
_WRAPPER_ALLOWANCE_TOKENS = 2048
# Images bill input tokens the body's bytes do not bound (a URL is short, its pixels are not).
_IMAGE_INPUT_TOKENS = 8192


@dataclass(frozen=True)
class PricedRequest:
    """Retain the exact final body with the reservation that authorizes it."""

    body: dict[str, Any]
    quote: OperationQuote


def resolve_model_slug(model: str, *, client: httpx.Client) -> str:
    """Resolve legacy short model names only when the provider catalog has one exact match.

    Args:
        model: User-selected full slug or an older API's short model identifier.
        client: Trusted non-retrying transport used only for public catalog reads.

    Returns:
        The same model's qualified identifier; an empty deferred selection stays empty.

    Raises:
        UnpricedOperationError: When a short name is unavailable or ambiguous.
    """
    name = model.strip("/").removeprefix("openrouter/")
    if not name or "/" in name:
        return name
    response = client.get("https://openrouter.ai/api/v1/models")
    response.raise_for_status()
    matches = {
        row["id"]
        for row in response.json().get("data", [])
        if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"].rsplit("/", 1)[-1] == name
    }
    if len(matches) != 1:
        raise UnpricedOperationError(
            "Choose an exact provider/model; this short model name is unavailable or ambiguous."
        )
    return matches.pop()


def fetch_endpoint_prices(model: str, *, client: httpx.Client) -> dict[str, Any]:
    """Read current public endpoint pricing without invoking a model.

    Args:
        model: Exact OpenRouter model slug, without a LiteLLM provider prefix.
        client: Bounded HTTP client owned by the trusted dispatch adapter.

    Returns:
        Provider endpoint limits and rates for this exact model.

    Raises:
        UnpricedOperationError: When the catalog cannot verify this model.
    """
    if not model or model.startswith("openrouter/") or "/" not in model:
        raise UnpricedOperationError("An exact provider/model is required for protected execution.")
    response = client.get(f"https://openrouter.ai/api/v1/models/{quote(model, safe='/')}/endpoints")
    response.raise_for_status()
    result = response.json().get("data")
    if not isinstance(result, dict) or result.get("id") != model or not result.get("endpoints"):
        raise UnpricedOperationError("The requested model has no verified endpoint pricing.")
    return result


def _positive_integer(value: Any, label: str) -> int:
    """Require an explicit positive integral token bound."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise UnpricedOperationError(f"An explicit positive {label} is required.")
    return value


def _check_text_request(body: Mapping[str, Any]) -> int:
    """Reject unbounded generation categories and count every input image for its price cap."""
    if any(body.get(key) for key in _UNSUPPORTED_REQUEST_FIELDS):
        raise UnpricedOperationError("This request includes a cost category without verified coverage.")
    if body.get("service_tier") not in {None, "default"}:
        raise UnpricedOperationError("This service tier has no verified price bound.")
    if body.get("modalities") not in (None, ["text"]):
        raise UnpricedOperationError("Media generation requires a separately verified price bound.")
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict) or tool.get("type", "function") not in {"function", "custom"}:
            raise UnpricedOperationError("Provider-operated tools require a separately verified price bound.")

    def images(content: Any) -> int:
        """Count input images including those nested in an Anthropic tool result."""
        count = 0
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict) or part.get("type") not in {
                    "text",
                    "image",
                    "image_url",
                    "tool_use",
                    "tool_result",
                    "thinking",
                    "redacted_thinking",
                }:
                    raise UnpricedOperationError("This input modality has no verified price bound.")
                if part["type"] in {"image", "image_url"}:
                    count += 1
                elif part["type"] == "tool_result":
                    count += images(part.get("content"))
        return count

    image_count = sum(
        images(message.get("content")) for message in body.get("messages") or [] if isinstance(message, dict)
    )
    if body.get("n", 1) != 1 or body.get("best_of", 1) != 1:
        raise UnpricedOperationError("Multiple completions require independently reserved requests.")
    if "max_tokens" in body and "max_completion_tokens" in body and body["max_tokens"] != body["max_completion_tokens"]:
        raise UnpricedOperationError("Conflicting output token limits cannot authorize work.")
    return image_count


def _input_bound(body: Mapping[str, Any], image_count: int) -> int:
    """Bound the prompt tokens a request can bill by its own serialized size.

    Args:
        body: Final request body, before routing caps are added.
        image_count: Input images, each priced as a full image's worth of tokens.

    Returns:
        Upper bound on billed prompt tokens, before the endpoint context caps it.
    """
    size = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return size + _WRAPPER_ALLOWANCE_TOKENS + image_count * _IMAGE_INPUT_TOKENS


def _requests_hour_cache(value: Any) -> bool:
    """Report whether any cache breakpoint asks for the one-hour cache write.

    Args:
        value: Any part of a request body.

    Returns:
        True when a ``cache_control`` block sets ``ttl`` to ``1h``.
    """
    if isinstance(value, dict):
        control = value.get("cache_control")
        if isinstance(control, dict) and control.get("ttl") == "1h":
            return True
        return any(_requests_hour_cache(item) for item in value.values())
    if isinstance(value, list):
        return any(_requests_hour_cache(item) for item in value)
    return False


def _row_reachable(row: Mapping[str, Any], input_bound: int | None) -> bool:
    """Keep a conditional row unless its prompt-size trigger exceeds what the request can send.

    Args:
        row: One base, tier or override pricing row.
        input_bound: Most prompt tokens this request can bill, or None to keep every row.

    Returns:
        False only for a long-context row the request is too small to trigger.
    """
    if input_bound is None:
        return True
    threshold = max(_threshold(row.get("min_prompt_tokens")), _threshold(row.get("min_context")))
    return threshold <= input_bound


def _threshold(value: Any) -> int:
    """Read a published prompt-size trigger, treating anything unreadable as always reachable.

    Args:
        value: ``min_prompt_tokens`` or ``min_context`` as published.

    Returns:
        The trigger in tokens, or 0.
    """
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _rate_maxima(pricing: Any, input_bound: int | None = None) -> dict[str, Decimal]:
    """Include published long-context tiers, scheduled overrides and cache rates in the maximum bound.

    Args:
        pricing: One endpoint's published pricing, as a mapping or a list of tier rows.
        input_bound: Most prompt tokens the request can bill; tiers that start above it are skipped.

    Returns:
        Highest published rate per chargeable category across the base row and every conditional row.

    Raises:
        UnpricedOperationError: When prompt or completion prices are missing, a conditional row is
            malformed, or a category a text request could incur has no covered rate.
    """
    if isinstance(pricing, list):
        if not pricing or any(not isinstance(row, dict) for row in pricing):
            raise UnpricedOperationError("Unrecognized tiered pricing cannot authorize work.")
        pricing = {**pricing[0], "tiers": pricing[1:]}
    if not isinstance(pricing, dict) or pricing.get("prompt") is None or pricing.get("completion") is None:
        raise UnpricedOperationError("Both prompt and completion prices must be verified.")
    rows = [pricing]
    for key in ("tiers", "overrides"):
        nested = pricing.get(key) or []
        if not isinstance(nested, list) or any(not isinstance(row, dict) for row in nested):
            raise UnpricedOperationError("Unrecognized tiered pricing cannot authorize work.")
        rows.extend(nested)
    for row in rows:
        for key, value in row.items():
            if key in _RATE_FIELDS | _UNREACHABLE_RATE_FIELDS | _CONDITION_FIELDS:
                continue
            if exact_nonnegative(value) != 0:
                raise UnpricedOperationError(f"The provider reports an uncovered price category: {key}.")
    rows = [row for row in rows if row is pricing or _row_reachable(row, input_bound)]
    return {
        field: max(exact_nonnegative(row.get(field, pricing.get(field, "0"))) for row in rows) for field in _RATE_FIELDS
    }


def price_text_request(request: Mapping[str, Any], catalog: Mapping[str, Any], policy: ChargePolicy) -> PricedRequest:
    """Reserve an enforceable upper bound for one fully resolved text request.

    Input is bounded by the body's own UTF-8 size plus a provider wrapper
    allowance, capped at the endpoint context. That byte bound covers system
    prompts, tools and cached input without trusting a client token estimate.
    Long-context tiers the bound cannot reach, the one-hour cache write unless
    a breakpoint asks for it, and ``/fast`` endpoints unless routing names
    them are left out of the hold.

    Args:
        request: Final OpenAI or Anthropic compatible body after SDK overrides.
        catalog: Fresh endpoint response for the exact routed model.
        policy: Approved managed or BYOK cent conversion.

    Returns:
        A copied request with price and endpoint caps, and its immutable quote.

    Raises:
        UnpricedOperationError: When the request cannot be bounded without changing its meaning.
    """
    body = copy.deepcopy(dict(request))
    image_count = _check_text_request(body)
    if "image" in (catalog.get("architecture") or {}).get("output_modalities", []) and body.get("modalities") != [
        "text"
    ]:
        raise UnpricedOperationError("Image generation requires separately bounded output pricing.")
    if body.get("model") != catalog.get("id"):
        raise UnpricedOperationError("The resolved model differs from its verified price catalog.")
    output_limit = _positive_integer(body.get("max_completion_tokens", body.get("max_tokens")), "output token limit")
    routing = body.get("provider") or {}
    if not isinstance(routing, dict):
        raise UnpricedOperationError("Unrecognized provider routing cannot authorize work.")
    routing = copy.deepcopy(routing)
    selected = set(routing.get("only") or [])
    ignored = set(routing.get("ignore") or [])
    byte_bound = _input_bound(body, image_count)
    cache_rates = ["prompt", "input_cache_read", "input_cache_write"]
    if _requests_hour_cache(body):
        cache_rates.append("input_cache_write_1h")
    candidates = []
    for endpoint in catalog.get("endpoints") or []:
        tag = endpoint.get("tag")
        provider = endpoint.get("provider_name")
        names = {tag, provider, str(tag).split("/")[0]}
        if selected and not names.intersection(selected):
            continue
        if names.intersection(ignored):
            continue
        if not isinstance(tag, str) or not tag:
            raise UnpricedOperationError("A priced endpoint is missing its routing identity.")
        # A fast variant bills a multiple of its base rates. Leaving it out of the pinned `only`
        # list keeps every other call's hold at base rates; a caller who names the tag still gets it.
        if tag.endswith("/fast") and tag not in selected:
            continue
        context = _positive_integer(endpoint.get("context_length"), "endpoint context bound")
        maximum_output = endpoint.get("max_completion_tokens")
        if maximum_output is not None and output_limit > _positive_integer(maximum_output, "endpoint output bound"):
            continue
        input_tokens = min(context, byte_bound)
        rates = _rate_maxima(endpoint.get("pricing") or {}, input_tokens)
        prices = endpoint.get("pricing") or {}
        base_prices = prices[0] if isinstance(prices, list) else prices
        if image_count and base_prices.get("image") is None:
            raise UnpricedOperationError("Image inputs require an explicit provider image price.")
        input_rate = max(rates[key] for key in cache_rates)
        maximum_usd = (
            input_tokens * input_rate
            + output_limit * (rates["completion"] + rates["internal_reasoning"])
            + rates["request"]
            + image_count * rates["image"]
        )
        candidates.append((endpoint, rates, maximum_usd, input_tokens))
    if not candidates:
        raise UnpricedOperationError("No selected provider supports the requested output limit with verified pricing.")
    caps = {
        "prompt": max(rates["prompt"] for _, rates, _, _ in candidates) * 1_000_000,
        "completion": max(rates["completion"] for _, rates, _, _ in candidates) * 1_000_000,
        "request": max(rates["request"] for _, rates, _, _ in candidates),
        "image": max(rates["image"] for _, rates, _, _ in candidates),
    }
    user_caps = routing.get("max_price") or {}
    for name, value in user_caps.items():
        amount = exact_nonnegative(value)
        caps[name] = min(caps.get(name, amount), amount)
    routing["max_price"] = {name: float(value) for name, value in caps.items()}
    routing["only"] = [endpoint["tag"] for endpoint, _, _, _ in candidates]
    routing["allow_fallbacks"] = False
    body["provider"] = routing
    maximum = max(amount for _, _, amount, _ in candidates) * Decimal("1.05")
    evidence = {
        "provider": "openrouter",
        "model": body["model"],
        "retrieved_at": time.time(),
        "source": f"https://openrouter.ai/api/v1/models/{body['model']}/endpoints",
        "input_bound": "request UTF-8 bytes plus provider wrapper allowance, capped at endpoint context",
        "input_bound_tokens": byte_bound,
        "wrapper_allowance_tokens": _WRAPPER_ALLOWANCE_TOKENS,
        "max_output_tokens": output_limit,
        "input_image_count": image_count,
        "endpoints": [
            {
                "tag": endpoint["tag"],
                "context_length": endpoint["context_length"],
                "input_tokens": input_tokens,
                "pricing": endpoint["pricing"],
            }
            for endpoint, _, _, input_tokens in candidates
        ],
        "maximum_provider_usd": str(maximum),
        "maximum_openrouter_byok_fee_fraction": "0.05",
    }
    return PricedRequest(body, operation_quote(body, maximum, policy, evidence))


def price_embedding_request(
    request: Mapping[str, Any], catalog: Mapping[str, Any], policy: ChargePolicy
) -> PricedRequest:
    """Reserve an enforceable upper bound for one embeddings request.

    Each input is bounded by the endpoint's full context window, since the
    tokenizer that bills it is the provider's, not the guest's.

    Args:
        request: Final OpenAI-compatible ``/embeddings`` body.
        catalog: Fresh endpoint response for the exact routed model.
        policy: Approved managed or BYOK cent conversion.

    Returns:
        A copied request with price and endpoint caps, and its immutable quote.

    Raises:
        UnpricedOperationError: When the request cannot be bounded without changing its meaning.
    """
    body = copy.deepcopy(dict(request))
    if any(body.get(key) for key in _UNSUPPORTED_REQUEST_FIELDS):
        raise UnpricedOperationError("This request includes a cost category without verified coverage.")
    if body.get("model") != catalog.get("id"):
        raise UnpricedOperationError("The resolved model differs from its verified price catalog.")
    if "embeddings" not in (catalog.get("architecture") or {}).get("output_modalities", []):
        raise UnpricedOperationError("This model is not an embeddings model.")
    inputs = body.get("input")
    inputs = [inputs] if isinstance(inputs, str) else inputs
    if not isinstance(inputs, list) or not inputs or not all(isinstance(item, str) for item in inputs):
        raise UnpricedOperationError("An embeddings request must embed one or more texts.")
    routing = body.get("provider") or {}
    if not isinstance(routing, dict):
        raise UnpricedOperationError("Unrecognized provider routing cannot authorize work.")
    routing = copy.deepcopy(routing)
    selected = set(routing.get("only") or [])
    ignored = set(routing.get("ignore") or [])
    candidates = []
    for endpoint in catalog.get("endpoints") or []:
        tag = endpoint.get("tag")
        names = {tag, endpoint.get("provider_name"), str(tag).split("/")[0]}
        if (selected and not names.intersection(selected)) or names.intersection(ignored):
            continue
        if not isinstance(tag, str) or not tag:
            raise UnpricedOperationError("A priced endpoint is missing its routing identity.")
        context = _positive_integer(endpoint.get("context_length"), "endpoint context bound")
        rates = _rate_maxima(endpoint.get("pricing") or {})
        candidates.append((endpoint, rates, len(inputs) * (context * rates["prompt"] + rates["request"])))
    if not candidates:
        raise UnpricedOperationError("No selected provider has verified embeddings pricing.")
    caps = {
        "prompt": max(rates["prompt"] for _, rates, _ in candidates) * 1_000_000,
        "request": max(rates["request"] for _, rates, _ in candidates),
    }
    for name, value in (routing.get("max_price") or {}).items():
        amount = exact_nonnegative(value)
        caps[name] = min(caps.get(name, amount), amount)
    routing["max_price"] = {name: float(value) for name, value in caps.items()}
    routing["only"] = [endpoint["tag"] for endpoint, _, _ in candidates]
    routing["allow_fallbacks"] = False
    body["provider"] = routing
    maximum = max(amount for _, _, amount in candidates) * Decimal("1.05")
    evidence = {
        "provider": "openrouter",
        "model": body["model"],
        "retrieved_at": time.time(),
        "source": f"https://openrouter.ai/api/v1/models/{body['model']}/endpoints",
        "input_bound": "maximum endpoint context per input",
        "input_count": len(inputs),
        "endpoints": [
            {"tag": endpoint["tag"], "context_length": endpoint["context_length"], "pricing": endpoint["pricing"]}
            for endpoint, _, _ in candidates
        ],
        "maximum_provider_usd": str(maximum),
        "maximum_openrouter_byok_fee_fraction": "0.05",
    }
    return PricedRequest(body, operation_quote(body, maximum, policy, evidence))


def price_decision_request(
    request: Mapping[str, Any], catalog: Mapping[str, Any], policy: ChargePolicy
) -> PricedRequest:
    """Reserve an enforceable upper bound for one text-only ``/decisions`` request.

    A decision answer is a choice or a score, not generated text, so only
    the prompt can grow the bill. The bound takes the UTF-8 size of the whole
    body, the same bound as a text request, capped at the endpoint context.

    Args:
        request: Final Decisions API body with ``model``, ``state`` and ``questions``.
        catalog: Fresh endpoint response for the exact routed model.
        policy: Approved managed cent conversion.

    Returns:
        A copied request with price and endpoint caps, and its immutable quote.

    Raises:
        UnpricedOperationError: When the model is not a priced decisions model,
            the state holds media, or an endpoint bills a category the bound
            cannot cover.
    """
    body = copy.deepcopy(dict(request))
    if body.get("model") != catalog.get("id"):
        raise UnpricedOperationError("The resolved model differs from its verified price catalog.")
    if "decisions" not in (catalog.get("architecture") or {}).get("output_modalities", []):
        raise UnpricedOperationError("This model is not a decisions model.")
    if set(body) - {"model", "state", "questions", "provider"} or not body.get("questions"):
        raise UnpricedOperationError("This request includes a field without verified coverage.")
    state = body.get("state")
    if not isinstance(state, (str, dict)) and not (
        isinstance(state, list) and all(isinstance(item, str) for item in state)
    ):
        # Images and files bill tokens their bytes do not bound.
        raise UnpricedOperationError("A decisions request must hold text state only.")
    routing = body.get("provider") or {}
    if not isinstance(routing, dict):
        raise UnpricedOperationError("Unrecognized provider routing cannot authorize work.")
    routing = copy.deepcopy(routing)
    selected = set(routing.get("only") or [])
    ignored = set(routing.get("ignore") or [])
    input_bound = _input_bound({key: value for key, value in body.items() if key != "provider"}, 0)
    candidates = []
    for endpoint in catalog.get("endpoints") or []:
        tag = endpoint.get("tag")
        names = {tag, endpoint.get("provider_name"), str(tag).split("/")[0]}
        if (selected and not names.intersection(selected)) or names.intersection(ignored):
            continue
        if not isinstance(tag, str) or not tag:
            raise UnpricedOperationError("A priced endpoint is missing its routing identity.")
        context = _positive_integer(endpoint.get("context_length"), "endpoint context bound")
        rates = _rate_maxima(endpoint.get("pricing") or {}, input_bound)
        if any(value != 0 for name, value in rates.items() if name not in {"prompt", "request"}):
            raise UnpricedOperationError("A decisions endpoint bills a category without a verified bound.")
        candidates.append((endpoint, rates, min(input_bound, context) * rates["prompt"] + rates["request"]))
    if not candidates:
        raise UnpricedOperationError("No selected provider has verified decisions pricing.")
    caps = {
        "prompt": max(rates["prompt"] for _, rates, _ in candidates) * 1_000_000,
        "request": max(rates["request"] for _, rates, _ in candidates),
    }
    for name, value in (routing.get("max_price") or {}).items():
        amount = exact_nonnegative(value)
        caps[name] = min(caps.get(name, amount), amount)
    routing["max_price"] = {name: float(value) for name, value in caps.items()}
    routing["only"] = [endpoint["tag"] for endpoint, _, _ in candidates]
    routing["allow_fallbacks"] = False
    body["provider"] = routing
    maximum = max(amount for _, _, amount in candidates) * Decimal("1.05")
    evidence = {
        "provider": "openrouter",
        "model": body["model"],
        "retrieved_at": time.time(),
        "source": f"https://openrouter.ai/api/v1/models/{body['model']}/endpoints",
        "input_bound": input_bound,
        "endpoints": [
            {"tag": endpoint["tag"], "context_length": endpoint["context_length"], "pricing": endpoint["pricing"]}
            for endpoint, _, _ in candidates
        ],
        "maximum_provider_usd": str(maximum),
        "maximum_openrouter_byok_fee_fraction": "0.05",
    }
    return PricedRequest(body, operation_quote(body, maximum, policy, evidence))
