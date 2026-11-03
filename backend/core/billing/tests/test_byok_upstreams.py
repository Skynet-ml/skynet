"""Check the BYOK provider routing table without contacting providers."""

from __future__ import annotations

import pytest

from core.api.errors import DomainError
from core.billing.byok_upstreams import OPENROUTER, UPSTREAMS, resolve_upstream, validate_byok_novelty
from core.billing.byok_vault import _PROVIDER_PROBES
from core.billing.operation_pricing import UnpricedOperationError
from core.provider_registry import BYOK_PROVIDER_SLUGS


def test_upstreams_cover_vault_providers() -> None:
    """Keep the supported providers aligned with the vault."""
    assert UPSTREAMS.keys() == _PROVIDER_PROBES.keys()


@pytest.mark.parametrize("slug", UPSTREAMS)
def test_only_openai_and_openrouter_support_shinka_embeddings(slug: str) -> None:
    """Only route Shinka's OpenAI embedding model to compatible providers."""
    assert UPSTREAMS[slug].embeddings is (slug in {"openai", "openrouter"})


def test_custom_upstream_allows_only_chat() -> None:
    """Do not assume custom endpoints support embeddings or Responses."""
    upstream = resolve_upstream({"name": "openai/model", "base_url": "https://example.com/v1"})
    assert upstream.paths == frozenset({"/chat/completions"})
    assert upstream.embeddings is False


def test_novelty_validation_uses_saved_custom_base() -> None:
    """Reject the custom endpoint even before credentials are resolved."""
    config = {"name": "openai/example", "token_source": "byok"}
    with pytest.raises(DomainError) as error:
        validate_byok_novelty(config, True, api_bases={"openai": "https://public.example/v1"})
    assert error.value.code == "submission.byok_novelty_unsupported"


@pytest.mark.parametrize("name", ["gpt-4o", "unknown/model"])
def test_novelty_unknown_provider_is_domain_error(name: str) -> None:
    """Return a client error for a BYOK model with no provider."""
    with pytest.raises(DomainError) as error:
        validate_byok_novelty({"name": name, "token_source": "byok"}, True)
    assert error.value.code == "submission.byok_provider_unsupported"


def test_together_xyz_is_canonical_alias() -> None:
    """Route the catalog's Together endpoint with Together auth."""
    assert (
        resolve_upstream({"name": "together_ai/model", "base_url": "https://api.together.xyz/v1"})
        == UPSTREAMS["together"]
    )


@pytest.mark.parametrize("base", ["https://api.deepseek.com", "https://api.deepseek.com/v1"])
@pytest.mark.parametrize("suffix", ["", "/"])
def test_deepseek_base_aliases_are_canonical(base: str, suffix: str) -> None:
    """Use DeepSeek's fixed protocol for either saved endpoint alias."""
    assert resolve_upstream({"name": "deepseek/model", "base_url": base + suffix}) == UPSTREAMS["deepseek"]


@pytest.mark.parametrize(
    ("provider", "base"),
    [
        ("deepseek", "https://API.deepseek.com/v1"),
        ("deepseek", "https://api.deepseek.com:443/v1"),
        ("groq", "https://api.groq.com:443/openai/v1"),
    ],
)
def test_canonical_endpoint_normalizes_host_and_port(provider: str, base: str) -> None:
    """Match equivalent canonical host spelling and default HTTPS port."""
    assert resolve_upstream({"name": f"{provider}/model", "base_url": base}) == UPSTREAMS[provider]


def test_custom_endpoint_retains_original_spelling() -> None:
    """Keep custom endpoint spelling and case-sensitive paths unchanged."""
    base = "https://API.example.com:443/V1/"
    upstream = resolve_upstream({"name": "openai/private", "base_url": base})
    assert upstream.slug == "custom"
    assert upstream.base_url == base.rstrip("/")


@pytest.mark.parametrize(("slug", "prefix"), BYOK_PROVIDER_SLUGS)
def test_resolve_provider(slug: str, prefix: str) -> None:
    """Select the provider that owns each model prefix."""
    assert resolve_upstream({"name": f"{prefix}/example"}) == UPSTREAMS[slug]


def test_openrouter_without_base() -> None:
    """Keep the OpenRouter default when no custom base was saved."""
    assert resolve_upstream({"name": "openrouter/openai/example"}) == OPENROUTER


@pytest.mark.parametrize("slug", ["openai", "anthropic", "together"])
def test_canonical_provider_base_preserves_protocol(slug: str) -> None:
    """Use the fixed provider protocol when its canonical URL is saved."""
    upstream = UPSTREAMS[slug]
    assert resolve_upstream({"name": f"{slug}/example", "base_url": upstream.base_url + "/"}) == upstream


@pytest.mark.parametrize("slug", ["anthropic", "together"])
def test_noncanonical_provider_base_is_rejected(slug: str) -> None:
    """Reject noncanonical URLs for native provider prefixes."""
    with pytest.raises(UnpricedOperationError):
        resolve_upstream({"name": f"{slug}/example", "base_url": "https://other.example/v1"})


def test_openrouter_supports_messages() -> None:
    """Preserve the OpenRouter Messages API for BYOK routes."""
    assert "/messages" in OPENROUTER.paths


@pytest.mark.parametrize(
    "base",
    [
        "http://example.com",
        "https://u:p@example.com",
        "https://@example.com",
        "https://example.com?q=1",
        "https://example.com/#f",
    ],
)
def test_reject_unsafe_url_shapes(base: str) -> None:
    """Reject custom URLs that cannot be safely pinned."""
    with pytest.raises(UnpricedOperationError):
        resolve_upstream({"name": "openai/example", "base_url": base})


def test_reject_unknown_provider() -> None:
    """Reject a provider with no fixed endpoint."""
    with pytest.raises(UnpricedOperationError):
        resolve_upstream({"name": "unknown/example"})
