"""Check the BYOK provider routing table without contacting providers."""

from __future__ import annotations

import pytest

from core.billing.byok_upstreams import OPENROUTER, UPSTREAMS, resolve_upstream
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


@pytest.mark.parametrize(("slug", "prefix"), BYOK_PROVIDER_SLUGS)
def test_resolve_provider(slug: str, prefix: str) -> None:
    """Select the provider that owns each model prefix."""
    assert resolve_upstream({"name": f"{prefix}/example"}) == UPSTREAMS[slug]


def test_openrouter_without_base() -> None:
    """Keep the OpenRouter default when no custom base was saved."""
    assert resolve_upstream({"name": "openrouter/openai/example"}) == OPENROUTER


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
