"""Describe the fixed provider destinations allowed for BYOK model calls."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit

from ..provider_registry import LITELLM_TO_BYOK_PROVIDER
from .operation_pricing import UnpricedOperationError


@dataclass(frozen=True)
class ProviderUpstream:
    """Keep one provider's model protocol and credential rules together."""

    slug: str
    base_url: str
    auth: str
    paths: frozenset[str]
    embeddings: bool
    extra_headers: tuple[tuple[str, str], ...] = ()


_CHAT = frozenset({"/chat/completions"})
_RESPONSES = _CHAT | {"/responses"}
_MESSAGES = _CHAT | {"/messages"}
OPENROUTER = ProviderUpstream("openrouter", "https://openrouter.ai/api/v1", "bearer", _RESPONSES, True)

# These compatibility endpoints and embedding capabilities have not been tested with live keys.
UPSTREAMS = {
    "openrouter": OPENROUTER,
    "openai": ProviderUpstream("openai", "https://api.openai.com/v1", "bearer", _RESPONSES, True),
    "anthropic": ProviderUpstream(
        "anthropic",
        "https://api.anthropic.com/v1",
        "x-api-key",
        _MESSAGES,
        False,
        (("anthropic-version", "2023-06-01"),),
    ),
    "google": ProviderUpstream(
        "google", "https://generativelanguage.googleapis.com/v1beta/openai", "bearer", _CHAT, False
    ),
    "cohere": ProviderUpstream("cohere", "https://api.cohere.ai/compatibility/v1", "bearer", _CHAT, False),
    "xai": ProviderUpstream("xai", "https://api.x.ai/v1", "bearer", _CHAT, False),
    "groq": ProviderUpstream("groq", "https://api.groq.com/openai/v1", "bearer", _CHAT, False),
    "deepseek": ProviderUpstream("deepseek", "https://api.deepseek.com/beta", "bearer", _CHAT, False),
    "together": ProviderUpstream("together", "https://api.together.ai/v1", "bearer", _CHAT, False),
    "mistral": ProviderUpstream("mistral", "https://api.mistral.ai/v1", "bearer", _CHAT, False),
    "moonshot": ProviderUpstream("moonshot", "https://api.moonshot.ai/v1", "bearer", _CHAT, False),
    "fireworks": ProviderUpstream("fireworks", "https://api.fireworks.ai/inference/v1", "bearer", _CHAT, False),
    "cerebras": ProviderUpstream("cerebras", "https://api.cerebras.ai/v1", "bearer", _CHAT, False),
    "novita": ProviderUpstream("novita", "https://api.novita.ai/v3/openai", "bearer", _CHAT, False),
    "deepinfra": ProviderUpstream("deepinfra", "https://api.deepinfra.com/v1/openai", "bearer", _CHAT, False),
    "sambanova": ProviderUpstream("sambanova", "https://api.sambanova.ai/v1", "bearer", _CHAT, False),
    "nebius": ProviderUpstream("nebius", "https://api.studio.nebius.ai/v1", "bearer", _CHAT, False),
    "minimax": ProviderUpstream("minimax", "https://api.minimax.io/v1", "bearer", _CHAT, False),
    "zai": ProviderUpstream("zai", "https://api.z.ai/api/paas/v4", "bearer", _CHAT, False),
    "meta": ProviderUpstream("meta", "https://api.llama.com/compat/v1", "bearer", _CHAT, False),
    "gmi": ProviderUpstream("gmi", "https://api.gmi-serving.com/v1", "bearer", _CHAT, False),
    "crusoe": ProviderUpstream(
        "crusoe", "https://managed-inference-api-proxy.crusoecloud.com/v1", "bearer", _CHAT, False
    ),
    "friendliai": ProviderUpstream("friendliai", "https://api.friendli.ai/serverless/v1", "bearer", _CHAT, False),
    "morph": ProviderUpstream("morph", "https://api.morphllm.com/v1", "bearer", _CHAT, False),
}


def validate_custom_base(base: str) -> None:
    """Reject endpoint forms that cannot be safely scoped to one HTTPS origin.

    Args:
        base: Custom provider endpoint.

    Returns:
        None when the endpoint is valid.

    Raises:
        ValueError: When the endpoint is not a safe HTTPS URL.
    """
    try:
        parsed = urlsplit(base)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or "?" in base
            or "#" in base
        ):
            raise ValueError
        _ = parsed.port
    except ValueError as error:
        raise ValueError("Custom provider endpoints require HTTPS without credentials, query or fragment.") from error


def resolve_upstream(config: Mapping[str, Any]) -> ProviderUpstream:
    """Resolve a BYOK model config to its fixed provider or custom OpenAI base.

    Args:
        config: Resolved model configuration.

    Returns:
        The provider protocol and endpoint.

    Raises:
        UnpricedOperationError: When the provider or custom endpoint is unsupported.
    """
    extra = config.get("extra") or {}
    model = str(extra.get("model") or config.get("name") or "")
    prefix = model.split("/", 1)[0]
    slug = str(config.get("byok_provider") or LITELLM_TO_BYOK_PROVIDER.get(prefix, prefix))
    base = extra.get("api_base") or extra.get("base_url") or config.get("base_url")
    if base:
        try:
            validate_custom_base(str(base))
        except ValueError as error:
            raise UnpricedOperationError(str(error)) from error
        if slug == "openrouter" and str(base).rstrip("/") in {"https://openrouter.ai", OPENROUTER.base_url}:
            return OPENROUTER
        if prefix != "openai":
            raise UnpricedOperationError("Custom provider endpoints require an openai/ model prefix.")
        return replace(
            UPSTREAMS["openai"], slug="custom", base_url=str(base).rstrip("/"), paths=_CHAT, embeddings=False
        )
    if slug not in UPSTREAMS:
        raise UnpricedOperationError("This BYOK provider is not supported.")
    return UPSTREAMS[slug]
