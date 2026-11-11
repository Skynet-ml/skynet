"""Unit tests for the model catalog DTOs and ``get_catalog`` helper."""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import litellm
import pytest

from ...config import settings
from .. import model_catalog as mc
from ..errors import DomainError
from ..model_catalog import (  # type: ignore[attr-defined]
    CatalogModel,
    CatalogProvider,
    ModelCatalogResponse,
    _make_label,
    _provider_data_centers,
    get_catalog,
)


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("gpt-4o-mini", "gpt-4o-mini"),
        ("openai/gpt-4o-mini", "gpt-4o-mini"),
        ("together_ai/mistral-7b-instruct-v0.1", "mistral-7b-instruct-v0.1"),
        ("anthropic/claude-3-5-haiku-20241022", "claude-3-5-haiku-20241022"),
    ],
    ids=["no_prefix", "openai_prefix", "together_ai_prefix", "anthropic_prefix"],
)
def test_make_label_strips_provider_prefix(model_id: str, expected: str) -> None:
    """``_make_label`` strips known provider prefixes from a model id."""
    assert _make_label(model_id) == expected


def test_make_label_leaves_id_without_slash_untouched() -> None:
    """An id with no provider prefix is returned unchanged."""
    assert _make_label("gpt-4o") == "gpt-4o"


def test_catalog_model_defaults() -> None:
    """Optional fields default to ``False`` / ``None`` on ``CatalogModel``."""
    m = CatalogModel(value="openai/gpt-4o", label="gpt-4o", provider="openai")
    assert m.supports_thinking is False
    assert m.supports_vision is False
    assert m.available is False
    assert m.max_input_tokens is None
    assert m.data_center is None
    assert m.input_cost_per_token is None
    assert m.output_cost_per_token is None


def test_catalog_model_stores_all_fields() -> None:
    """Explicitly-supplied fields are preserved on ``CatalogModel``."""
    m = CatalogModel(
        value="openai/gpt-4o",
        label="gpt-4o",
        provider="openai",
        supports_thinking=True,
        available=True,
        max_input_tokens=128000,
    )
    assert m.supports_thinking is True
    assert m.available is True
    assert m.max_input_tokens == 128000


def test_catalog_provider_has_env_key_defaults_false() -> None:
    """``has_env_key`` defaults to ``False`` and ``data_center`` to ``None``."""
    p = CatalogProvider(slug="openai", label="OpenAI")
    assert p.has_env_key is False
    assert p.data_center is None


def test_catalog_provider_stores_env_var_and_url() -> None:
    """Provider env vars, base URLs and data center round-trip."""
    p = CatalogProvider(
        slug="openai",
        label="OpenAI",
        data_center="EU",
        env_var="OPENAI_API_KEY",
        default_base_url="https://api.openai.com/v1",
        has_env_key=True,
    )
    assert p.env_var == "OPENAI_API_KEY"
    assert p.default_base_url == "https://api.openai.com/v1"
    assert p.has_env_key is True
    assert p.data_center == "EU"


def test_model_catalog_response_empty_lists() -> None:
    """An empty response carries empty ``providers`` and ``models`` lists."""
    resp = ModelCatalogResponse(providers=[], models=[])
    assert resp.providers == []
    assert resp.models == []


def test_model_catalog_response_preserves_order() -> None:
    """Model insertion order is preserved on the response."""
    models = [
        CatalogModel(value="openai/gpt-4o-mini", label="gpt-4o-mini", provider="openai", available=True),
        CatalogModel(
            value="anthropic/claude-3-5-haiku-20241022",
            label="claude-3-5-haiku-20241022",
            provider="anthropic",
            available=True,
        ),
    ]
    resp = ModelCatalogResponse(providers=[], models=models)
    assert resp.models[0].value == "openai/gpt-4o-mini"
    assert resp.models[1].provider == "anthropic"


def test_get_catalog_returns_correct_types(monkeypatch: pytest.MonkeyPatch) -> None:
    """``get_catalog`` returns a structured response of providers and models.

    Monkeypatches ``litellm.get_valid_models`` to avoid API keys and to make
    results deterministic. Asserts only structural invariants -- specific
    models change as LiteLLM updates its registry.
    """
    fake_cost: dict = dict(litellm.model_cost)
    fake_cost["fakeprovider-model-a"] = {
        "mode": "chat",
        "litellm_provider": "openrouter",
        "supports_reasoning": False,
        "max_input_tokens": 4096,
        "input_cost_per_token": 0,
        "output_cost_per_token": 0,
    }

    monkeypatch.setattr(litellm, "model_cost", fake_cost)
    monkeypatch.setattr(litellm, "get_valid_models", lambda: ["fakeprovider-model-a"])
    monkeypatch.setattr(mc, "_probe_all_providers", dict)

    result = get_catalog()

    assert isinstance(result, ModelCatalogResponse)
    assert isinstance(result.providers, list)
    assert isinstance(result.models, list)
    # The injected model should be available and visible
    values = [m.value for m in result.models]
    assert any("fakeprovider-model-a" in v for v in values)


def test_get_catalog_surfaces_per_token_costs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive LiteLLM per-token costs ride onto the catalog; zero/absent → None."""
    fake_cost: dict = {
        "priced-model": {
            "mode": "chat",
            "litellm_provider": "openrouter",
            "max_input_tokens": 4096,
            "input_cost_per_token": 1.5e-7,
            "output_cost_per_token": 6e-7,
        },
        "freebie-model": {
            "mode": "chat",
            "litellm_provider": "openrouter",
            "max_input_tokens": 4096,
            "input_cost_per_token": 0,  # zero/absent must surface as None, not free
        },
    }
    monkeypatch.setattr(litellm, "model_cost", fake_cost)
    monkeypatch.setattr(litellm, "get_valid_models", lambda: ["priced-model", "freebie-model"])
    monkeypatch.setattr(mc, "_probe_all_providers", dict)

    by_value = {m.value: m for m in get_catalog().models}
    priced = by_value["openrouter/priced-model"]
    assert priced.input_cost_per_token == 1.5e-7
    assert priced.output_cost_per_token == 6e-7
    freebie = by_value["openrouter/freebie-model"]
    assert freebie.input_cost_per_token is None
    assert freebie.output_cost_per_token is None


def test_get_catalog_only_returns_available_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """Models that aren't reported by ``get_valid_models`` are filtered out."""
    fake_cost: dict = {
        "only-in-registry": {
            "mode": "chat",
            "litellm_provider": "openrouter",
            "supports_reasoning": False,
            "max_input_tokens": 4096,
            "input_cost_per_token": 0,
            "output_cost_per_token": 0,
        }
    }
    monkeypatch.setattr(litellm, "model_cost", fake_cost)
    monkeypatch.setattr(litellm, "get_valid_models", list)
    monkeypatch.setattr(mc, "_probe_all_providers", dict)

    result = get_catalog()

    assert result.models == []


def test_get_catalog_deduplicates_dated_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dated variants are dropped when the base model id is also present.

    A dated variant (``gpt-4o-2024-08-06``) is excluded when the base name
    (``gpt-4o``) exists in the cost table.
    """
    fake_cost: dict = {
        "gpt-4o": {
            "mode": "chat",
            "litellm_provider": "openrouter",
            "supports_reasoning": False,
            "max_input_tokens": 128000,
            "input_cost_per_token": 0,
            "output_cost_per_token": 0,
        },
        "gpt-4o-2024-08-06": {
            "mode": "chat",
            "litellm_provider": "openrouter",
            "supports_reasoning": False,
            "max_input_tokens": 128000,
            "input_cost_per_token": 0,
            "output_cost_per_token": 0,
        },
    }
    monkeypatch.setattr(litellm, "model_cost", fake_cost)
    monkeypatch.setattr(litellm, "get_valid_models", lambda: ["gpt-4o", "gpt-4o-2024-08-06"])
    monkeypatch.setattr(mc, "_probe_all_providers", dict)

    result = get_catalog()

    values = [m.value for m in result.models]
    # Dated variant must not appear
    assert not any("2024-08-06" in v for v in values)
    # Base must appear
    assert any(v.endswith("gpt-4o") for v in values)


def test_get_catalog_filters_out_non_chat_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only chat-mode entries are relevant for DSPy LMs; others are filtered."""
    fake_cost: dict = {
        "text-embedding-ada-002": {
            "mode": "embedding",
            "litellm_provider": "openrouter",
            "max_input_tokens": 8192,
            "input_cost_per_token": 0,
            "output_cost_per_token": 0,
        },
    }
    monkeypatch.setattr(litellm, "model_cost", fake_cost)
    monkeypatch.setattr(litellm, "get_valid_models", lambda: ["text-embedding-ada-002"])
    monkeypatch.setattr(mc, "_probe_all_providers", dict)

    result = get_catalog()

    assert result.models == []


def test_get_catalog_propagates_supports_vision_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """``supports_vision`` is propagated from the cost table to the catalog."""
    fake_cost: dict = {
        "vision-model": {
            "mode": "chat",
            "litellm_provider": "openrouter",
            "supports_reasoning": False,
            "supports_vision": True,
            "max_input_tokens": 128000,
            "input_cost_per_token": 0,
            "output_cost_per_token": 0,
        },
        "text-only-model": {
            "mode": "chat",
            "litellm_provider": "openrouter",
            "supports_reasoning": False,
            "max_input_tokens": 8192,
            "input_cost_per_token": 0,
            "output_cost_per_token": 0,
        },
    }
    monkeypatch.setattr(litellm, "model_cost", fake_cost)
    monkeypatch.setattr(litellm, "get_valid_models", lambda: ["vision-model", "text-only-model"])
    monkeypatch.setattr(mc, "_probe_all_providers", dict)

    result = get_catalog()

    by_value = {m.value: m for m in result.models}
    assert by_value["openrouter/vision-model"].supports_vision is True
    assert by_value["openrouter/text-only-model"].supports_vision is False


def test_get_catalog_handles_valid_models_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure inside ``get_valid_models`` is swallowed into an empty list.

    If ``get_valid_models`` raises, the catalog returns an empty models list
    rather than propagating the exception.
    """
    fake_cost: dict = {
        "gpt-4o": {
            "mode": "chat",
            "litellm_provider": "openrouter",
            "supports_reasoning": False,
            "max_input_tokens": 128000,
            "input_cost_per_token": 0,
            "output_cost_per_token": 0,
        },
    }
    monkeypatch.setattr(litellm, "model_cost", fake_cost)
    monkeypatch.setattr(litellm, "get_valid_models", lambda: (_ for _ in ()).throw(RuntimeError("API error")))
    monkeypatch.setattr(mc, "_probe_all_providers", dict)

    # Should not raise
    result = get_catalog()
    assert isinstance(result, ModelCatalogResponse)
    # model is not in valid_set so it's unavailable → filtered out
    assert result.models == []


def test_single_endpoint_provider_has_none_data_center(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider with one endpoint emits exactly one entry, ``data_center=None``.

    Preserves the historical single-DC wire shape so existing clients keep
    working.
    """
    centers = _provider_data_centers("openrouter")
    assert len(centers) == 1
    assert centers[0].label is None

    fake_cost: dict = {
        "openrouter/vendor/chat-model": {
            "mode": "chat",
            "litellm_provider": "openrouter",
            "supports_reasoning": False,
            "max_input_tokens": 128000,
            "input_cost_per_token": 0,
            "output_cost_per_token": 0,
        }
    }
    monkeypatch.setattr(litellm, "model_cost", fake_cost)
    monkeypatch.setattr(litellm, "get_valid_models", lambda: ["openrouter/vendor/chat-model"])
    monkeypatch.setattr(mc, "_probe_all_providers", dict)

    result = get_catalog()

    entries = [m for m in result.models if m.provider == "openrouter"]
    assert len(entries) == 1
    assert entries[0].data_center is None
    providers = [p for p in result.providers if p.slug == "openrouter"]
    assert len(providers) == 1
    assert providers[0].data_center is None


def test_platform_catalog_is_openrouter_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-OpenRouter providers never reach the hosted catalog.

    Other providers' keys may exist for non-LLM features (OpenAI powers
    Whisper dictation) and agent or embedding gateways may be configured —
    none may leak chat models into the platform-billed menu.
    """
    monkeypatch.setattr(settings, "code_agent_base_url", "https://llm.internal/v1")
    monkeypatch.setattr(settings, "embeddings_base_url", "https://embed.internal/v1")

    fake_cost: dict = {
        "gpt-4o": {
            "mode": "chat",
            "litellm_provider": "openai",
            "supports_reasoning": False,
            "max_input_tokens": 128000,
            "input_cost_per_token": 0,
            "output_cost_per_token": 0,
        },
        "claude-3-5-haiku": {
            "mode": "chat",
            "litellm_provider": "anthropic",
            "supports_reasoning": False,
            "max_input_tokens": 200000,
            "input_cost_per_token": 0,
            "output_cost_per_token": 0,
        },
        "openrouter/vendor/chat-model": {
            "mode": "chat",
            "litellm_provider": "openrouter",
            "supports_reasoning": False,
            "max_input_tokens": 128000,
            "input_cost_per_token": 0,
            "output_cost_per_token": 0,
        },
    }
    monkeypatch.setattr(litellm, "model_cost", fake_cost)
    monkeypatch.setattr(
        litellm,
        "get_valid_models",
        lambda: ["gpt-4o", "claude-3-5-haiku", "openrouter/vendor/chat-model"],
    )
    monkeypatch.setattr(mc, "_probe_all_providers", dict)

    result = get_catalog()

    assert {m.provider for m in result.models} == {"openrouter"}
    assert {p.slug for p in result.providers} == {"openrouter"}


def test_configured_gateways_add_no_data_center(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent and embedding base URLs never surface as an extra provider endpoint."""
    monkeypatch.setattr(settings, "code_agent_base_url", "https://llm.internal/v1")
    monkeypatch.setattr(settings, "embeddings_base_url", "https://embed.internal/v1")

    centers = _provider_data_centers("openai")
    assert [(c.label, c.base_url) for c in centers] == [(None, "https://api.openai.com/v1")]


def test_get_catalog_adds_probe_only_models_with_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """Probe-reported models absent from LiteLLM's registry are added with metadata.

    Simulates an OpenRouter ``/models`` probe that returns two MiniMax models
    LiteLLM never listed: a vision/reasoning one and a text-only one. Both must
    appear in the catalog, provider-prefixed, with vision/thinking flags and the
    context window derived defensively from the raw probe item.
    """
    monkeypatch.setattr(litellm, "model_cost", {})
    monkeypatch.setattr(litellm, "get_valid_models", list)

    probe = {
        ("openrouter", None): {
            "minimax/minimax-m3": {
                "id": "minimax/minimax-m3",
                "architecture": {
                    "input_modalities": ["text", "image", "video"],
                    "output_modalities": ["text"],
                },
                "supported_parameters": ["reasoning", "temperature"],
                "context_length": 200000,
            },
            "minimax/minimax-m2.7": {
                "id": "minimax/minimax-m2.7",
                "architecture": {
                    "input_modalities": ["text"],
                    "output_modalities": ["text"],
                },
                "supported_parameters": ["temperature"],
                "context_length": 100000,
            },
        }
    }
    monkeypatch.setattr(mc, "_probe_all_providers", lambda: probe)

    result = get_catalog()

    by_value = {m.value: m for m in result.models}
    assert "openrouter/minimax/minimax-m3" in by_value
    assert "openrouter/minimax/minimax-m2.7" in by_value

    m3 = by_value["openrouter/minimax/minimax-m3"]
    assert m3.supports_vision is True
    assert m3.supports_thinking is True
    assert m3.available is True
    assert m3.max_input_tokens == 200000
    assert m3.provider == "openrouter"
    assert m3.label == "minimax-m3"

    m27 = by_value["openrouter/minimax/minimax-m2.7"]
    assert m27.supports_vision is False
    assert m27.supports_thinking is False

    assert any(p.slug == "openrouter" for p in result.providers)


def test_get_catalog_surfaces_per_model_reasoning_efforts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each model carries the effort levels and default its probe item declares.

    Levels come back weakest first whatever order the provider lists them in,
    unknown levels are dropped, a reasoning model with no effort control gets
    an empty list, and a registry model the probe lists inherits its levels.
    """
    fake_cost: dict = {
        "openrouter/z-ai/glm-5.3": {
            "mode": "chat",
            "litellm_provider": "openrouter",
            "supports_reasoning": False,
        }
    }
    monkeypatch.setattr(litellm, "model_cost", fake_cost)
    monkeypatch.setattr(litellm, "get_valid_models", list)

    probe = {
        ("openrouter", None): {
            "z-ai/glm-5.3": {
                "id": "z-ai/glm-5.3",
                "reasoning": {"supported_efforts": ["max", "high", "low"], "default_effort": "max"},
            },
            "anthropic/claude-haiku-4.5": {
                "id": "anthropic/claude-haiku-4.5",
                "supported_parameters": ["reasoning"],
                "reasoning": {"mandatory": True},
            },
            "openai/gpt-6-sol": {
                "id": "openai/gpt-6-sol",
                "supported_parameters": ["reasoning", "reasoning_effort"],
                "reasoning": {
                    "supported_efforts": ["max", "xhigh", "high", "medium", "low", "none", "ultra"],
                    "default_effort": "medium",
                    "default_enabled": True,
                },
            },
            "minimax/minimax-m3": {"id": "minimax/minimax-m3", "supported_parameters": ["reasoning"]},
        }
    }
    monkeypatch.setattr(mc, "_probe_all_providers", lambda: probe)

    by_value = {m.value: m for m in get_catalog().models}

    glm = by_value["openrouter/z-ai/glm-5.3"]
    assert glm.reasoning_efforts == ["low", "high", "max"]
    assert glm.default_reasoning_effort == "max"
    assert glm.supports_thinking is True

    sol = by_value["openrouter/openai/gpt-6-sol"]
    assert sol.reasoning_efforts == ["none", "low", "medium", "high", "xhigh", "max"]
    assert sol.default_reasoning_effort == "medium"
    assert sol.reasoning_mandatory is False
    assert sol.reasoning_default_enabled is True

    minimax = by_value["openrouter/minimax/minimax-m3"]
    assert minimax.supports_thinking is True
    assert minimax.reasoning_efforts == []
    assert minimax.default_reasoning_effort is None
    assert minimax.reasoning_mandatory is False
    assert minimax.reasoning_default_enabled is False

    haiku = by_value["openrouter/anthropic/claude-haiku-4.5"]
    assert haiku.reasoning_efforts == []
    assert haiku.reasoning_mandatory is True
    assert haiku.reasoning_default_enabled is True


def test_probe_item_reasoning_is_unknown_without_probe_data() -> None:
    """A model the probe never described reports unknown controls, not an empty ladder."""
    assert set(mc._probe_item_reasoning({}).values()) == {None}


def test_get_catalog_trusts_the_probe_over_the_registry_reasoning_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """A registry model the probe lists without reasoning is not offered thinking."""
    fake_cost: dict = {
        "openrouter/acme/chat-1": {"mode": "chat", "litellm_provider": "openrouter", "supports_reasoning": True}
    }
    monkeypatch.setattr(litellm, "model_cost", fake_cost)
    monkeypatch.setattr(litellm, "get_valid_models", list)
    probe = {("openrouter", None): {"acme/chat-1": {"id": "acme/chat-1", "supported_parameters": ["tools"]}}}
    monkeypatch.setattr(mc, "_probe_all_providers", lambda: probe)

    model = next(m for m in get_catalog().models if m.value == "openrouter/acme/chat-1")
    assert model.supports_thinking is False
    assert model.reasoning_efforts == []


def test_get_catalog_skips_non_text_probe_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """A probe item whose output modalities lack ``"text"`` is skipped.

    An embedding/image model (output_modalities without ``"text"``) must not
    leak into the chat catalog, while an item with no modality info defaults
    to being included.
    """
    monkeypatch.setattr(litellm, "model_cost", {})
    monkeypatch.setattr(litellm, "get_valid_models", list)

    probe = {
        ("openrouter", None): {
            "vendor/image-gen": {
                "id": "vendor/image-gen",
                "architecture": {"input_modalities": ["text"], "output_modalities": ["image"]},
            },
            "vendor/bare-chat": {"id": "vendor/bare-chat"},
        }
    }
    monkeypatch.setattr(mc, "_probe_all_providers", lambda: probe)

    result = get_catalog()

    values = {m.value for m in result.models}
    assert "openrouter/vendor/image-gen" not in values
    assert "openrouter/vendor/bare-chat" in values


def test_get_catalog_does_not_duplicate_registry_models_from_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model present in both the registry and the probe is emitted only once."""
    fake_cost: dict = {
        "openrouter/vendor/known": {
            "mode": "chat",
            "litellm_provider": "openrouter",
            "supports_reasoning": False,
            "max_input_tokens": 8192,
            "input_cost_per_token": 0,
            "output_cost_per_token": 0,
        }
    }
    monkeypatch.setattr(litellm, "model_cost", fake_cost)
    monkeypatch.setattr(litellm, "get_valid_models", lambda: ["openrouter/vendor/known"])

    probe = {
        ("openrouter", None): {
            "vendor/known": {"id": "vendor/known", "context_length": 999999},
        }
    }
    monkeypatch.setattr(mc, "_probe_all_providers", lambda: probe)

    result = get_catalog()

    matches = [m for m in result.models if m.value == "openrouter/vendor/known"]
    assert len(matches) == 1


def test_background_refresh_clears_flag_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed background refresh releases the in-flight guard so it can re-trigger.

    Regression: the guard was only cleared on the success path, so one transient
    failure wedged ``_refresh_in_flight`` True and disabled background refresh for
    the rest of the process's life.
    """

    def boom() -> mc.ModelCatalogResponse:
        raise RuntimeError("catalog source unreachable")

    monkeypatch.setattr(mc, "get_catalog", boom)
    monkeypatch.setattr(mc, "_refresh_in_flight", True, raising=False)
    mc._refresh_catalog_in_background()
    assert mc._refresh_in_flight is False


def test_background_refresh_swaps_cache_and_clears_flag_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful refresh swaps the fresh snapshot in and releases the guard."""
    fresh = mc.ModelCatalogResponse(providers=[], models=[])
    prev = mc._cached_response
    monkeypatch.setattr(mc, "get_catalog", lambda: fresh)
    monkeypatch.setattr(mc, "_refresh_in_flight", True, raising=False)
    try:
        mc._refresh_catalog_in_background()
        assert mc._cached_response is fresh
        assert mc._refresh_in_flight is False
    finally:
        mc._cached_response = prev


def test_probe_byok_provider_models_uses_native_listing_with_user_key() -> None:
    """A user's own key is presented to the provider's native ``/models`` URL."""
    with patch.object(mc, "_fetch_models_index", return_value={"gpt-4o": {}}) as fetch:
        assert mc.probe_byok_provider_models("openai", "sk-user") == {"gpt-4o": {}}
    fetch.assert_called_once_with("openai", "https://api.openai.com/v1/models", "sk-user")


def test_probe_byok_provider_models_skips_unknown_providers() -> None:
    """An unknown provider returns ``None`` so the static list stands."""
    with patch.object(mc, "_fetch_json") as fetch:
        assert mc.probe_byok_provider_models("not-a-provider", "sk-user") is None
    fetch.assert_not_called()


def test_probe_byok_anthropic_uses_native_listing() -> None:
    """Anthropic's own listing is read with its key header and every model counts as chat."""
    body = {"data": [{"id": "claude-opus-5-5", "type": "model"}]}
    with patch.object(mc, "_fetch_json", return_value=body) as fetch:
        assert mc.probe_byok_provider_models("anthropic", "sk-ant") == {"claude-opus-5-5": {"type": "chat"}}
    assert fetch.call_args.args[2]["x-api-key"] == "sk-ant"


def test_probe_byok_gemini_keeps_only_chat_models() -> None:
    """Gemini's listing drops embedders, image and speech models, and strips the ``models/`` prefix."""
    body = {
        "models": [
            {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"], "thinking": True},
            {"name": "models/gemini-3.8-flash-image", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-embedding-2", "supportedGenerationMethods": ["embedContent"]},
        ]
    }
    with patch.object(mc, "_fetch_json", return_value=body):
        index = mc.probe_byok_provider_models("gemini", "g-key")
    assert index is not None
    assert list(index) == ["gemini-3.8-flash"]
    assert index["gemini-3.8-flash"]["supports_reasoning"] is True


def test_probe_byok_native_listing_failure_returns_none() -> None:
    """A failed native listing falls back to the static registry."""
    with patch.object(mc, "_fetch_json", return_value=None):
        assert mc.probe_byok_provider_models("cohere_chat", "co-key") is None


_DAY = 24 * 3600
_NOW = 1_800_000_000


def _listed(created: int, *, score: float | None = None, **extra: object) -> dict:
    """Build an OpenRouter-shaped listing item for the featured heuristic.

    Args:
        created: Release time as a unix timestamp.
        score: Artificial Analysis intelligence index, when benchmarked.
        **extra: Fields that override the defaults.

    Returns:
        A probe item that passes every featured filter unless overridden.
    """
    item: dict = {
        "created": created,
        "architecture": {"output_modalities": ["text"]},
        "supported_parameters": ["tools", "reasoning"],
        "pricing": {"prompt": "0.000001", "completion": "0.000004"},
    }
    if score is not None:
        item["benchmarks"] = {"artificial_analysis": {"intelligence_index": score}}
    item.update(extra)
    return item


def test_featured_picks_newest_release_of_each_model_line() -> None:
    """Each line contributes its newest release; older versions and variants drop."""
    listing = {
        "acme/opus-5.5": _listed(_NOW, score=60),
        "acme/opus-5": _listed(_NOW - 60 * _DAY, score=55),
        "acme/haiku-4.5": _listed(_NOW - 300 * _DAY, score=40, pricing={"prompt": "0.0000002"}),
        "acme/opus-5.5-pro": _listed(_NOW),
        "acme/opus-5.5:free": _listed(_NOW, score=60),
        "acme/relic-1": _listed(_NOW - 400 * _DAY, score=30),
        "acme/painter-2": _listed(_NOW, score=50, architecture={"output_modalities": ["image", "text"]}),
        "acme/open-27b": _listed(_NOW, score=45),
    }
    assert mc._featured_probe_ids(listing) == {"acme/opus-5.5", "acme/haiku-4.5"}


def test_featured_skips_models_far_behind_the_frontier() -> None:
    """A model scoring under the bar's share of the best benchmark is not featured."""
    listing = {
        "lead/big-2": _listed(_NOW, score=60),
        "tail/small-1": _listed(_NOW, score=20),
    }
    assert mc._featured_probe_ids(listing) == {"lead/big-2"}


def test_featured_is_empty_without_benchmarks() -> None:
    """A listing without release dates or benchmarks features nothing."""
    assert mc._featured_probe_ids({"gw/model": {"id": "gw/model"}}) == set()


def test_featured_caps_each_lab() -> None:
    """A lab with many live lines contributes at most the cap, newest first."""
    listing = {f"acme/line{chr(97 + i)}-1": _listed(_NOW - i * _DAY, score=50) for i in range(6)}
    assert mc._featured_probe_ids(listing) == {f"acme/line{chr(97 + i)}-1" for i in range(mc._FEATURED_PER_LAB)}


def test_get_catalog_flags_featured_and_prices_probe_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """Probe-only models carry the featured flag and their listed prices."""
    monkeypatch.setattr(litellm, "model_cost", {})
    monkeypatch.setattr(litellm, "get_valid_models", list)
    probe = {
        ("openrouter", None): {
            "acme/opus-5.5": _listed(_NOW, score=60),
            "acme/haiku-4": _listed(_NOW - _DAY, score=20),
        }
    }
    monkeypatch.setattr(mc, "_probe_all_providers", lambda: probe)

    by_value = {m.value: m for m in get_catalog().models}
    assert by_value["openrouter/acme/opus-5.5"].featured is True
    assert by_value["openrouter/acme/haiku-4"].featured is False
    assert by_value["openrouter/acme/opus-5.5"].input_cost_per_token == pytest.approx(1e-6)
    assert by_value["openrouter/acme/opus-5.5"].output_cost_per_token == pytest.approx(4e-6)


def test_byok_models_borrow_reasoning_from_their_openrouter_twin(monkeypatch: pytest.MonkeyPatch) -> None:
    """A registry id with dashes and a date matches the dotted OpenRouter id."""
    twin = CatalogModel(
        value="openrouter/anthropic/claude-opus-4.5",
        label="anthropic/claude-opus-4.5",
        provider="openrouter",
        supports_thinking=True,
        reasoning_efforts=["low", "medium", "high"],
        default_reasoning_effort="high",
        reasoning_mandatory=True,
        reasoning_default_enabled=True,
        available=True,
    )
    monkeypatch.setattr(mc, "_cached_response", ModelCatalogResponse(providers=[], models=[twin]))
    byok = CatalogModel(
        value="anthropic/claude-opus-4-5-20251101",
        label="claude-opus-4-5-20251101",
        provider="anthropic",
        available=True,
    )
    unknown = CatalogModel(value="anthropic/claude-mystery", label="claude-mystery", provider="anthropic")

    enriched, untouched = mc.with_platform_reasoning([byok, unknown])
    assert enriched.supports_thinking is True
    assert enriched.reasoning_efforts == ["low", "medium", "high"]
    assert enriched.default_reasoning_effort == "high"
    assert enriched.reasoning_mandatory is True
    assert untouched is unknown


def test_get_catalog_hides_removed_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """Withdrawn models and OpenRouter routers never reach the menu, even when OpenRouter lists them."""
    entry = {
        "mode": "chat",
        "litellm_provider": "openrouter",
        "supports_reasoning": True,
        "max_input_tokens": 4096,
        "input_cost_per_token": 0,
        "output_cost_per_token": 0,
    }
    ids = [
        "openrouter/openai/gpt-5.6-terra",
        "openrouter/~openai/gpt-terra-latest",
        "openrouter/x-ai/grok-build-0.1",
        "openrouter/auto",
        "openrouter/openrouter/free",
        "openrouter/typesafe/jev-router",
        "openrouter/fake/kept-model",
    ]
    monkeypatch.setattr(litellm, "model_cost", {mid: dict(entry) for mid in ids})
    monkeypatch.setattr(litellm, "get_valid_models", lambda: list(ids))
    monkeypatch.setattr(mc, "_probe_all_providers", dict)

    values = [m.value for m in get_catalog().models]

    assert values == ["openrouter/fake/kept-model"]


def test_featured_drops_models_beaten_on_score_and_price() -> None:
    """A lab's model that another of its models beats on score and price is redundant."""
    listing = {
        "acme/ultra-2": _listed(_NOW, score=60, pricing={"prompt": "0.000004"}),
        "acme/max-1": _listed(_NOW, score=55, pricing={"prompt": "0.00001"}),
        "acme/flash-3": _listed(_NOW, score=45, pricing={"prompt": "0.0000003"}),
    }
    assert mc._featured_probe_ids(listing) == {"acme/ultra-2", "acme/flash-3"}


def test_featured_new_release_inherits_its_lines_score() -> None:
    """An unbenchmarked new release replaces its benchmarked predecessor on release day."""
    listing = {
        "acme/spark-1.2": _listed(_NOW - 30 * _DAY, score=50),
        "acme/spark-1.3": _listed(_NOW),
        "acme/novel-1": _listed(_NOW),
    }
    assert mc._featured_probe_ids(listing) == {"acme/spark-1.3"}


def test_best_value_default_prefers_score_per_dollar_among_default_thinkers() -> None:
    """The default is the featured default-on thinker with the best score per input dollar."""
    thinks = {"reasoning": {"default_enabled": True}}
    listing = {
        "acme/ultra-2": _listed(_NOW, score=60, pricing={"prompt": "0.000004"}, **thinks),
        "acme/flash-3": _listed(_NOW, score=45, pricing={"prompt": "0.0000003"}, **thinks),
        "beta/cheap-1": _listed(_NOW, score=44, pricing={"prompt": "0.0000001"}),
        "beta/mid-2": _listed(_NOW, score=50, pricing={"prompt": "0.000001"}, **thinks),
    }
    featured = mc._featured_probe_ids(listing)
    assert "beta/cheap-1" in featured
    winner = mc._best_value_probe_id(listing, featured)
    assert winner is not None
    assert winner[0] == "acme/flash-3"


def test_lab_tier_ranks_top_labs_first_and_chinese_labs_last() -> None:
    """The tier reads the vendor out of catalog values, probe ids and direct-provider ids alike."""
    assert mc.lab_tier("openrouter/anthropic/claude-haiku-5.5") == 0
    assert mc.lab_tier("openai/gpt-6-luna") == 0
    assert mc.lab_tier("openrouter/google/gemini-3.8-flash") == 0
    assert mc.lab_tier("openrouter/x-ai/grok-4.7") == 1
    assert mc.lab_tier("openrouter/meta/muse-spark-1.3") == 1
    assert mc.lab_tier("openrouter/z-ai/glm-5.3-flash") == 2
    assert mc.lab_tier("deepseek/deepseek-v4.1-flash") == 2
    assert mc.lab_tier("openrouter/moonshotai/kimi-k3") == 2
    assert mc.lab_tier("openrouter/qwen/qwen3.8-max-0902") == 2


def test_best_value_prefers_a_top_lab_over_a_cheaper_chinese_lab() -> None:
    """Lab tier ranks before score per dollar, so a far cheaper Chinese model loses to a top lab's."""
    thinks = {"reasoning": {"default_enabled": True}}
    listing = {
        "z-ai/glm-flash": _listed(_NOW, score=42, pricing={"prompt": "0.00000003"}, **thinks),
        "acme/mid-1": _listed(_NOW, score=46, pricing={"prompt": "0.0000002"}, **thinks),
        "openai/luna": _listed(_NOW, score=38, pricing={"prompt": "0.0000001"}, **thinks),
        "openai/sol": _listed(_NOW, score=52, pricing={"prompt": "0.000002"}, **thinks),
    }
    featured = mc._featured_probe_ids(listing)
    assert {"z-ai/glm-flash", "acme/mid-1", "openai/luna"} <= featured
    winner = mc._best_value_probe_id(listing, featured)
    assert winner is not None
    assert winner[0] == "openai/luna"

    without_top = {k: v for k, v in listing.items() if not k.startswith("openai/")}
    winner = mc._best_value_probe_id(without_top, mc._featured_probe_ids(without_top))
    assert winner is not None
    assert winner[0] == "acme/mid-1"


def test_best_value_default_on_the_current_listing_is_claude_haiku() -> None:
    """On today's featured models the default leaves GLM 5.3 Flash for Claude Haiku 5.5.

    Haiku only thinks on request, but it can reason, and an unpicked effort
    sends it low (see ``default_effort_for``), so it qualifies and wins the
    top labs on score per dollar.
    """
    thinks = {"reasoning": {"default_enabled": True}}
    opt_in = {"reasoning": {"supported_efforts": ["low", "medium", "high"]}}
    listing = {
        "anthropic/claude-haiku-5.5": _listed(_NOW, score=43.4, pricing={"prompt": "0.0000001"}, **opt_in),
        "anthropic/claude-sonnet-5.5": _listed(_NOW, score=56.0, pricing={"prompt": "0.000002"}, **thinks),
        "openai/gpt-6-luna": _listed(_NOW, score=38.1, pricing={"prompt": "0.0000001"}, **thinks),
        "openai/gpt-6.1-sol": _listed(_NOW, score=51.8, pricing={"prompt": "0.000002"}, **thinks),
        "google/gemini-3.8-flash": _listed(_NOW, score=40.9, pricing={"prompt": "0.00000075"}, **thinks),
        "z-ai/glm-5.3-flash": _listed(_NOW, score=41.8, pricing={"prompt": "0.0000000261"}, **thinks),
        "deepseek/deepseek-v4.1-flash": _listed(_NOW, score=39.5, pricing={"prompt": "0.00000015"}, **thinks),
    }
    winner = mc._best_value_probe_id(listing, set(listing))
    assert winner is not None
    assert winner[0] == "anthropic/claude-haiku-5.5"


def test_best_value_default_is_none_without_default_thinkers() -> None:
    """No featured model can reason, so no default is flagged."""
    listing = {"acme/ultra-2": _listed(_NOW, score=60)}
    assert mc._best_value_probe_id(listing, mc._featured_probe_ids(listing)) is None


def test_strongest_is_the_highest_index_default_thinker() -> None:
    """Strongest skips featured models that do not think by default, however high they score."""
    thinks = {"reasoning": {"default_enabled": True}}
    listing = {
        "acme/ultra-2": _listed(_NOW, score=60, pricing={"prompt": "0.000004"}, **thinks),
        "acme/flash-3": _listed(_NOW, score=45, pricing={"prompt": "0.0000003"}, **thinks),
        "beta/genius-1": _listed(_NOW, score=70),
    }
    featured = mc._featured_probe_ids(listing)
    assert "beta/genius-1" in featured
    assert mc._strongest_probe_id(listing, featured) == ("acme/ultra-2", 60)


def test_strongest_new_release_inherits_its_predecessors_score() -> None:
    """An unbenchmarked new release carries its line's score and takes strongest on release day."""
    thinks = {"reasoning": {"default_enabled": True}}
    listing = {
        "acme/ultra-2": _listed(_NOW - 30 * _DAY, score=60, **thinks),
        "acme/ultra-3": _listed(_NOW, **thinks),
        "beta/mid-2": _listed(_NOW, score=55, **thinks),
    }
    featured = mc._featured_probe_ids(listing)
    assert mc._intelligence_by_probe_id(listing)["acme/ultra-3"] == 60
    assert mc._strongest_probe_id(listing, featured) == ("acme/ultra-3", 60)


def test_strongest_is_none_without_default_thinkers() -> None:
    """No featured model reasons by default, so nothing is strongest."""
    listing = {"acme/ultra-2": _listed(_NOW, score=60)}
    assert mc._strongest_probe_id(listing, mc._featured_probe_ids(listing)) is None


def test_get_catalog_flags_one_strongest_and_carries_indexes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exactly one model is strongest, and an inherited score reaches the catalog entry."""
    monkeypatch.setattr(litellm, "model_cost", {})
    monkeypatch.setattr(litellm, "get_valid_models", list)
    thinks = {"reasoning": {"default_enabled": True}}
    probe = {
        ("openrouter", None): {
            "acme/ultra-2": _listed(_NOW - 30 * _DAY, score=60, **thinks),
            "acme/ultra-3": _listed(_NOW, **thinks),
            "beta/mid-2": _listed(_NOW, score=55, pricing={"prompt": "0.0000002"}, **thinks),
        }
    }
    monkeypatch.setattr(mc, "_probe_all_providers", lambda: probe)

    by_value = {m.value: m for m in get_catalog().models}
    assert [v for v, m in by_value.items() if m.is_strongest] == ["openrouter/acme/ultra-3"]
    assert by_value["openrouter/acme/ultra-3"].intelligence_index == 60
    assert by_value["openrouter/beta/mid-2"].intelligence_index == 55
    assert by_value["openrouter/beta/mid-2"].is_default is True
    assert all(m.artificial_analysis_url is None for m in by_value.values())


def test_practical_keeps_benchmarked_models_and_newest_of_each_line() -> None:
    """Benchmarked models and a line's unbenchmarked newest release stay; the tail drops."""
    listing = {
        "acme/opus-5.5": _listed(_NOW, score=60),
        "acme/opus-5.6": _listed(_NOW),
        "acme/opus-5": _listed(_NOW - 60 * _DAY),
        "hobby/finetune-7": _listed(_NOW),
        "acme/lyria-3": _listed(_NOW, architecture={"output_modalities": ["text", "audio"]}),
        "acme/notools-1": _listed(_NOW, score=50, supported_parameters=["reasoning"]),
        "acme/ancient-1": _listed(_NOW - 800 * _DAY, score=40),
        "acme/opus-5.5:free": _listed(_NOW, score=60),
    }
    assert mc._practical_probe_ids(listing) == {"acme/opus-5.5", "acme/opus-5.6"}


def test_practical_keeps_everything_without_benchmarks() -> None:
    """A gateway listing that carries no benchmarks is not filtered at all."""
    assert mc._practical_probe_ids({"gw/model": _listed(_NOW)}) is None


def test_get_catalog_treats_impractical_models_without_effort_as_withdrawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep an old hidden pick without effort controls valid, but route it as withdrawn."""
    monkeypatch.setattr(litellm, "model_cost", {})
    monkeypatch.setattr(litellm, "get_valid_models", list)
    probe = {
        ("openrouter", None): {
            "acme/opus-5.5": _listed(_NOW, score=60),
            "hobby/finetune-7": _listed(_NOW),
        }
    }
    monkeypatch.setattr(mc, "_probe_all_providers", lambda: probe)

    catalog = get_catalog()
    monkeypatch.setattr(mc, "get_catalog_cached", lambda: catalog)

    assert [m.value for m in catalog.models] == ["openrouter/acme/opus-5.5"]
    assert not mc.is_hidden_model("openrouter/hobby/finetune-7")
    assert mc.is_effortless_model("openrouter/hobby/finetune-7")
    assert mc.is_removed_model("openrouter/hobby/finetune-7")
    mc.require_known_model("openrouter/hobby/finetune-7")
    with pytest.raises(DomainError):
        mc.require_known_model("openrouter/hobby/never-listed")


def test_a_stored_pick_without_effort_control_still_validates(monkeypatch: pytest.MonkeyPatch) -> None:
    """A listed model without an effort control leaves the menus but passes validation."""
    plain = CatalogModel(value="openrouter/vendor/plain", label="plain", provider="openrouter", available=True)
    thinker = CatalogModel(
        value="openrouter/vendor/thinker",
        label="thinker",
        provider="openrouter",
        available=True,
        supports_thinking=True,
    )
    monkeypatch.setattr(mc, "_cached_response", ModelCatalogResponse(providers=[], models=[plain, thinker]))
    monkeypatch.setattr(mc, "_cached_at_monotonic", float("inf"))

    assert [m.value for m in mc.get_catalog_cached().models] == ["openrouter/vendor/thinker"]
    assert mc.is_effortless_model("openrouter/vendor/plain")
    assert not mc.is_effortless_model("openrouter/vendor/thinker")
    mc.require_known_model("openrouter/vendor/plain")
    mc.require_known_model("openrouter/vendor/plain", role="judge")


def test_a_stored_effortless_pick_validates_before_the_first_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a cold start the effort check builds the catalog, so the pick gets no 422."""
    plain = CatalogModel(value="openrouter/vendor/plain", label="plain", provider="openrouter", available=True)
    monkeypatch.setattr(mc, "_cached_response", None)
    monkeypatch.setattr(mc, "get_catalog", lambda: ModelCatalogResponse(providers=[], models=[plain]))

    assert mc.is_effortless_model("openrouter/vendor/plain")
    mc.require_known_model("openrouter/vendor/plain")


def test_a_cold_effort_check_during_the_prewarm_waits_for_its_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cold check while the boot prewarm builds joins that build instead of starting a second one."""
    plain = CatalogModel(value="openrouter/vendor/plain", label="plain", provider="openrouter", available=True)
    started = threading.Event()
    release = threading.Event()
    builds: list[int] = []

    def slow_build() -> ModelCatalogResponse:
        """Count the build and hold it until the test releases it.

        Returns:
            A catalog with one model without an effort control.
        """
        builds.append(1)
        started.set()
        release.wait(5)
        return ModelCatalogResponse(providers=[], models=[plain])

    monkeypatch.setattr(mc, "_cached_response", None)
    monkeypatch.setattr(mc, "_refresh_in_flight", False)
    monkeypatch.setattr(mc, "_refresh_thread", None)
    monkeypatch.setattr(mc, "get_catalog", slow_build)
    monkeypatch.setattr(mc, "zero_retention_models", set)

    mc.prewarm_catalog()
    assert started.wait(5)
    answers: list[bool] = []
    checker = threading.Thread(target=lambda: answers.append(mc.is_effortless_model("openrouter/vendor/plain")))
    checker.start()
    release.set()
    checker.join(5)

    assert answers == [True]
    assert len(builds) == 1


def test_two_cold_effort_checks_at_once_share_one_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second cold check never joins the refresh thread before it starts."""
    plain = CatalogModel(value="openrouter/vendor/plain", label="plain", provider="openrouter", available=True)
    starting = threading.Event()
    builds: list[int] = []

    class SlowStartThread(threading.Thread):
        """Widen the gap between storing the refresh thread and starting it."""

        def start(self) -> None:
            """Signal the test, wait, then start the thread."""
            starting.set()
            time.sleep(0.2)
            super().start()

    def build() -> ModelCatalogResponse:
        """Count the build.

        Returns:
            A catalog with one model without an effort control.
        """
        builds.append(1)
        return ModelCatalogResponse(providers=[], models=[plain])

    monkeypatch.setattr(mc, "_cached_response", None)
    monkeypatch.setattr(mc, "_refresh_in_flight", False)
    monkeypatch.setattr(mc, "_refresh_thread", None)
    monkeypatch.setattr(mc, "get_catalog", build)
    monkeypatch.setattr(mc, "zero_retention_models", set)
    monkeypatch.setattr(mc, "Thread", SlowStartThread)

    answers: list[bool] = []
    errors: list[BaseException] = []

    def check() -> None:
        """Run one cold effort check and record its answer or error."""
        try:
            answers.append(mc.is_effortless_model("openrouter/vendor/plain"))
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=check)
    second = threading.Thread(target=check)
    first.start()
    assert starting.wait(5)
    second.start()
    first.join(5)
    second.join(5)

    assert errors == []
    assert answers == [True, True]
    assert len(builds) == 1


def test_the_strongest_model_follows_the_operator_then_the_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator-set id wins; else the catalog's strongest model; else the best-value agent model."""
    best = CatalogModel(
        value="openrouter/vendor/best", label="best", provider="openrouter", supports_thinking=True, is_default=True
    )
    strongest = CatalogModel(
        value="openrouter/vendor/strong",
        label="strong",
        provider="openrouter",
        supports_thinking=True,
        is_strongest=True,
    )
    monkeypatch.setattr(mc, "_cached_response", ModelCatalogResponse(providers=[], models=[best, strongest]))

    assert mc.strongest_model_id(" vendor/picked ") == "vendor/picked"
    assert mc.strongest_model_id() == "openrouter/vendor/strong"

    monkeypatch.setattr(mc, "_cached_response", ModelCatalogResponse(providers=[], models=[best]))
    assert mc.strongest_model_id() == "openrouter/vendor/best"


def test_the_lowest_effort_from_low_is_shared_by_the_unpicked_effort() -> None:
    """Below-``low`` efforts are skipped, and a model that thinks by default still gets its catalog default."""

    def row(**reasoning: object) -> CatalogModel:
        """Build one catalog row with the given reasoning fields.

        Args:
            **reasoning: Reasoning fields of the row.

        Returns:
            The row.
        """
        return CatalogModel(value="openrouter/vendor/m", label="m", provider="openrouter", **reasoning)

    on_request = row(reasoning_efforts=["minimal", "low", "high"], reasoning_default_enabled=False)
    assert mc.lowest_effort_from_low(on_request) == "low"
    assert mc.unpicked_effort(on_request) == "low"
    assert mc.lowest_effort_from_low(row(reasoning_efforts=["minimal"])) is None
    assert mc.lowest_effort_from_low(row()) is None
    by_default = row(
        reasoning_efforts=["minimal", "low", "high"], reasoning_default_enabled=True, default_reasoning_effort="high"
    )
    assert mc.lowest_effort_from_low(by_default) == "low"
    assert mc.unpicked_effort(by_default) == "high"


_JEV = "openrouter/typesafe/jev-router"


@pytest.mark.parametrize("role", ["hook", "judge"])
def test_jev_is_selectable_for_the_classifier_roles(monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    """TypeSafe Jev passes validation and joins the menu for the hook and judge roles."""
    kept = CatalogModel(value="openrouter/acme/opus-5.5", label="opus-5.5", provider="openrouter", available=True)
    catalog = ModelCatalogResponse(providers=[], models=[kept])
    monkeypatch.setattr(mc, "get_catalog_cached", lambda: catalog)

    mc.require_known_model(_JEV, role=role)
    values = [m.value for m in mc.models_for_role(role)]

    assert values == ["openrouter/acme/opus-5.5", _JEV]
    assert mc.is_removed_model(_JEV)


@pytest.mark.parametrize("role", [None, "agent", "optimizer", "metric"])
def test_jev_stays_hidden_for_every_other_role(monkeypatch: pytest.MonkeyPatch, role: str | None) -> None:
    """Every other role, and a call that names no role, refuses Jev and leaves it off the menu."""
    kept = CatalogModel(value="openrouter/acme/opus-5.5", label="opus-5.5", provider="openrouter", available=True)
    catalog = ModelCatalogResponse(providers=[], models=[kept])
    monkeypatch.setattr(mc, "get_catalog_cached", lambda: catalog)

    with pytest.raises(mc.DomainError):
        mc.require_known_model(_JEV, role=role)
    assert [m.value for m in mc.models_for_role(role)] == ["openrouter/acme/opus-5.5"]


def test_classifier_roles_do_not_unlock_other_routers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The classifier exception names Jev only; other routers stay refused for the judge."""
    empty = ModelCatalogResponse(providers=[], models=[])
    monkeypatch.setattr(mc, "get_catalog_cached", lambda: empty)

    with pytest.raises(mc.DomainError):
        mc.require_known_model("openrouter/other/some-router", role="judge")
