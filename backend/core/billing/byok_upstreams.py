"""Describe the fixed provider destinations allowed for BYOK model calls."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit

from ..api.errors import DomainError
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
OPENROUTER = ProviderUpstream("openrouter", "https://openrouter.ai/api/v1", "bearer", _RESPONSES | {"/messages"}, True)

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
    "deepseek": ProviderUpstream("deepseek", "https://api.deepseek.com/v1", "bearer", _CHAT, False),
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


def byok_model_prefix(name: str, explicit_provider: str | None, *, custom: bool = False) -> str | None:
    """Identify a provider prefix without mistaking native namespaces for one.

    Args:
        name: Model identifier.
        explicit_provider: Provider supplied independently of the model name.
        custom: Whether the resolved route is a custom OpenAI-compatible endpoint.

    Returns:
        The prefix, or None when the identifier is bare.
    """
    stripped = (name or "").strip().strip("/")
    if "/" not in stripped:
        return None
    prefix = stripped.split("/", 1)[0]
    if custom:
        return prefix if prefix == "openai" else None
    if explicit_provider == "openrouter":
        return prefix if prefix == "openrouter" else None
    if explicit_provider:
        if prefix == "openrouter":
            return prefix
        return prefix if LITELLM_TO_BYOK_PROVIDER.get(prefix, prefix) == explicit_provider else None
    return prefix or None


def canonical_base(base: str) -> str:
    """Normalize an endpoint for canonical comparisons.

    Args:
        base: An HTTPS endpoint URL.

    Returns:
        The normalized URL, preserving path case.
    """
    parsed = urlsplit(base)
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    netloc = host + (f":{port}" if port is not None and port != 443 else "")
    return parsed._replace(scheme=parsed.scheme.lower(), netloc=netloc, path=parsed.path.rstrip("/")).geturl()


def deepseek_base(base: str | None) -> str | None:
    """Resolve supported DeepSeek saved bases to their dispatch endpoint.

    Args:
        base: The saved endpoint, or None for the default.

    Returns:
        The resolved endpoint, or None for an unsupported base.
    """
    normalized = canonical_base(base) if base else "https://api.deepseek.com"
    if normalized in {"https://api.deepseek.com", "https://api.deepseek.com/v1"}:
        return UPSTREAMS["deepseek"].base_url
    if normalized == "https://api.deepseek.com/beta":
        return "https://api.deepseek.com/beta"
    return None


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
        normalized_base = canonical_base(str(base))
        if slug == "openrouter" and normalized_base in {"https://openrouter.ai", OPENROUTER.base_url}:
            return OPENROUTER
        if slug == "together" and normalized_base == "https://api.together.xyz/v1":
            return UPSTREAMS["together"]
        if slug == "deepseek" and (resolved_base := deepseek_base(str(base))) is not None:
            return replace(UPSTREAMS["deepseek"], base_url=resolved_base)
        if slug in UPSTREAMS and normalized_base == canonical_base(UPSTREAMS[slug].base_url):
            return UPSTREAMS[slug]
        if slug in UPSTREAMS and slug != "openai":
            raise UnpricedOperationError(
                "This provider requires its canonical endpoint. Custom endpoints require an openai/ model prefix.",
                code="submission.byok_provider_unsupported",
            )
        if prefix != "openai":
            raise UnpricedOperationError("Custom provider endpoints require an openai/ model prefix.")
        return replace(
            UPSTREAMS["openai"], slug="custom", base_url=str(base).rstrip("/"), paths=_CHAT, embeddings=False
        )
    if slug not in UPSTREAMS:
        raise UnpricedOperationError("This BYOK provider is not supported.")
    if slug == "deepseek":
        return replace(UPSTREAMS[slug], base_url=deepseek_base(None))
    return UPSTREAMS[slug]


def validate_byok_novelty(
    config: Mapping[str, Any], novelty: bool, *, api_bases: Mapping[str, str | None] | None = None
) -> None:
    """Reject BYOK novelty when the reflection route cannot serve embeddings.

    Args:
        config: The reflection model configuration.
        novelty: Whether novelty checking is enabled.
        api_bases: Saved provider endpoints, when credentials are not yet resolved.

    Raises:
        DomainError: When the BYOK route has no embeddings support.
    """
    if not novelty or config.get("token_source") != "byok":
        return
    extra = config.get("extra") or {}
    route = extra.get("_skynet_budget_route") if isinstance(extra, Mapping) else None
    if isinstance(route, Mapping) and route.get("byok_custom_endpoint") is True:
        raise DomainError("submission.byok_novelty_unsupported", status=400)
    name = str(config.get("name") or "")
    prefix = name.split("/", 1)[0]
    slug = str(config.get("byok_provider") or LITELLM_TO_BYOK_PROVIDER.get(prefix, prefix))
    base = (api_bases or {}).get(slug)
    resolved = {**config, "base_url": base} if base else config
    try:
        upstream = resolve_upstream(resolved)
    except UnpricedOperationError as error:
        raise DomainError("submission.byok_provider_unsupported", status=400) from error
    if not upstream.embeddings:
        raise DomainError("submission.byok_novelty_unsupported", status=400)
