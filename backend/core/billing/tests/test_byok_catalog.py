"""Tests for the account-scoped BYOK model catalog.

The catalog must list only models the account's verified keys can actually
serve: a provider's static registry entries narrowed to its live ``/models``
listing, a custom endpoint's own discovery, and nothing at all for providers
the account holds no verified key for.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import create_engine

from core.api.model_catalog import CatalogModel, CatalogProvider, ModelCatalogResponse
from core.api.routers.billing import _byok_catalog_for_user, _invalidate_byok_user_catalog
from core.api.routers.models import DiscoverModelsResponse
from core.billing.byok_vault import ProviderKeyVault
from core.config import settings
from core.storage.models import Base

ALICE = "alice@example.com"


@pytest.fixture
def vault(monkeypatch: pytest.MonkeyPatch) -> Iterator[ProviderKeyVault]:
    """Yield a configured vault backed by an isolated in-memory database.

    Args:
        monkeypatch: Pytest helper used to configure the encryption key.

    Yields:
        A provider-key vault whose tables exist for the test.
    """
    _invalidate_byok_user_catalog()
    monkeypatch.setattr("core.billing.byok_vault._pinned_address", lambda *args, **kwargs: "8.8.8.8")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        settings,
        "byok_vault_key",
        SecretStr(Fernet.generate_key().decode("utf-8")),
    )
    yield ProviderKeyVault(engine=engine)
    Base.metadata.drop_all(engine)
    _invalidate_byok_user_catalog()


def _static_catalog() -> ModelCatalogResponse:
    """Return a two-provider stand-in for the bundled BYOK registry."""
    return ModelCatalogResponse(
        providers=[
            CatalogProvider(slug="openai", label="OpenAI"),
            CatalogProvider(slug="openrouter", label="OpenRouter"),
        ],
        models=[
            CatalogModel(value="openai/gpt-4o", label="gpt-4o", provider="openai", available=True),
            CatalogModel(value="openai/gpt-4.1", label="gpt-4.1", provider="openai", available=True),
            CatalogModel(
                value="openrouter/anthropic/claude-3-haiku",
                label="anthropic/claude-3-haiku",
                provider="openrouter",
                available=True,
            ),
        ],
    )


def _save_verified(vault: ProviderKeyVault, provider: str, secret: str, **kwargs: str) -> None:
    """Save a key that verifies without touching the network."""
    with patch.object(ProviderKeyVault, "_probe", return_value="verified"):
        vault.save_key(ALICE, provider, secret, **kwargs)


def test_custom_catalog_fetches_models_with_stored_secret(vault: ProviderKeyVault) -> None:
    """A verified custom connection discovers models without exposing its key."""
    _save_verified(
        vault,
        "custom",
        "private-secret",
        label="Private inference",
        api_base="https://inference.example/v1",
    )
    discovered = DiscoverModelsResponse(
        models=["private-chat", "openai/second-chat"],
        base_url="https://inference.example/v1",
    )
    with (
        patch(
            "core.api.routers.billing.get_byok_catalog_cached",
            return_value=ModelCatalogResponse(providers=[], models=[]),
        ),
        patch(
            "core.api.routers.billing.discover_models_at_endpoint",
            return_value=discovered,
        ) as discover,
    ):
        catalog = _byok_catalog_for_user(vault, ALICE)

    discover.assert_called_once_with("https://inference.example/v1", "private-secret", limit=None)
    assert [provider.slug for provider in catalog.providers] == ["custom"]
    assert {(model.value, model.byok_provider) for model in catalog.models} == {
        ("openai/private-chat", "custom"),
        ("openai/second-chat", "custom"),
    }
    assert "private-secret" not in catalog.model_dump_json()


def test_custom_catalog_is_scoped_to_the_authenticated_account(
    vault: ProviderKeyVault,
) -> None:
    """Another account cannot see models discovered through someone else's connection."""
    _save_verified(vault, "custom", "private-secret", api_base="https://inference.example/v1")
    with patch(
        "core.api.routers.billing.get_byok_catalog_cached",
        return_value=ModelCatalogResponse(providers=[], models=[]),
    ):
        catalog = _byok_catalog_for_user(vault, "bob@example.com")

    assert catalog.providers == []
    assert catalog.models == []


def test_only_providers_with_a_verified_key_are_listed(vault: ProviderKeyVault) -> None:
    """A direct OpenAI key surfaces OpenAI models only — never OpenRouter's."""
    _save_verified(vault, "openai", "sk-openai")
    with (
        patch("core.api.routers.billing.get_byok_catalog_cached", return_value=_static_catalog()),
        patch("core.api.routers.billing.probe_byok_provider_models", return_value=None),
    ):
        catalog = _byok_catalog_for_user(vault, ALICE)

    assert [provider.slug for provider in catalog.providers] == ["openai"]
    assert {model.value for model in catalog.models} == {"openai/gpt-4o", "openai/gpt-4.1"}
    assert all(model.byok_provider == "openai" for model in catalog.models)


def test_unverified_key_contributes_nothing(vault: ProviderKeyVault) -> None:
    """A key that failed verification does not unlock its provider's models."""
    response = SimpleNamespace(status_code=401, is_success=False)
    with patch("core.billing.byok_vault.httpx.get", return_value=response):
        vault.save_key(ALICE, "openai", "sk-bad")
    with patch("core.api.routers.billing.get_byok_catalog_cached", return_value=_static_catalog()):
        catalog = _byok_catalog_for_user(vault, ALICE)

    assert catalog.providers == []
    assert catalog.models == []


def test_live_listing_narrows_static_models_and_adds_declared_chat_models(vault: ProviderKeyVault) -> None:
    """Only models the key's ``/models`` listing reports survive; new chat models join."""
    _save_verified(vault, "openrouter", "sk-or")
    deployed = {
        "anthropic/claude-3-haiku": {"architecture": {"output_modalities": ["text"]}},
        "openai/gpt-5-preview": {
            "architecture": {"output_modalities": ["text"], "input_modalities": ["text", "image"]},
            "context_length": 400000,
        },
        "openai/text-embedding-3-small": {"architecture": {"output_modalities": ["embeddings"]}},
        "mystery/model": {},
    }
    with (
        patch("core.api.routers.billing.get_byok_catalog_cached", return_value=_static_catalog()),
        patch("core.api.routers.billing.probe_byok_provider_models", return_value=deployed) as probe,
    ):
        catalog = _byok_catalog_for_user(vault, ALICE)

    probe.assert_called_once_with("openrouter", "sk-or")
    assert [provider.slug for provider in catalog.providers] == ["openrouter"]
    by_value = {model.value: model for model in catalog.models}
    assert set(by_value) == {"openrouter/anthropic/claude-3-haiku", "openrouter/openai/gpt-5-preview"}
    added = by_value["openrouter/openai/gpt-5-preview"]
    assert added.supports_vision is True
    assert added.max_input_tokens == 400000
    assert added.byok_provider == "openrouter"


def test_probe_failure_falls_back_to_static_models(vault: ProviderKeyVault) -> None:
    """When the provider cannot be listed, its static registry models stand in."""
    _save_verified(vault, "openrouter", "sk-or")
    with (
        patch("core.api.routers.billing.get_byok_catalog_cached", return_value=_static_catalog()),
        patch("core.api.routers.billing.probe_byok_provider_models", return_value=None),
    ):
        catalog = _byok_catalog_for_user(vault, ALICE)

    assert {model.value for model in catalog.models} == {"openrouter/anthropic/claude-3-haiku"}


def test_custom_endpoint_replaces_the_provider_static_models(vault: ProviderKeyVault) -> None:
    """An OpenAI key pointed at a custom gateway lists that gateway's models, not the registry."""
    _save_verified(vault, "openai", "sk-gw", api_base="https://gateway.example/v1")
    discovered = DiscoverModelsResponse(models=["gateway-chat"], base_url="https://gateway.example/v1")
    with (
        patch("core.api.routers.billing.get_byok_catalog_cached", return_value=_static_catalog()),
        patch("core.api.routers.billing.discover_models_at_endpoint", return_value=discovered),
        patch("core.api.routers.billing.probe_byok_provider_models") as probe,
    ):
        catalog = _byok_catalog_for_user(vault, ALICE)

    probe.assert_not_called()
    assert [(provider.slug, provider.default_base_url) for provider in catalog.providers] == [
        ("openai", "https://gateway.example/v1")
    ]
    assert [(model.value, model.byok_provider) for model in catalog.models] == [("openai/gateway-chat", "openai")]


def test_catalog_is_cached_per_account_until_keys_change(vault: ProviderKeyVault) -> None:
    """Repeated reads reuse the live listing; a key change refetches."""
    _save_verified(vault, "openai", "sk-openai")
    with (
        patch("core.api.routers.billing.get_byok_catalog_cached", return_value=_static_catalog()),
        patch("core.api.routers.billing.probe_byok_provider_models", return_value=None) as probe,
    ):
        first = _byok_catalog_for_user(vault, ALICE)
        second = _byok_catalog_for_user(vault, ALICE)
        assert second is first
        assert probe.call_count == 1

        _save_verified(vault, "openrouter", "sk-or")
        third = _byok_catalog_for_user(vault, ALICE)

    assert probe.call_count == 3
    assert [provider.slug for provider in third.providers] == ["openai", "openrouter"]
