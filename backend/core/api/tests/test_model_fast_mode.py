"""Verify priority discovery, composer routing, and the actual price reservation."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api import model_fast_mode as fast
from core.api.model_catalog import CatalogModel, ModelCatalogResponse
from core.api.routers import models as models_router
from core.api.routers.models import create_models_router
from core.api.tests.conftest import bypass_auth
from core.billing.cli_dispatch import CLI_FAST_KEY
from core.billing.openrouter_quotes import price_text_request
from core.billing.tests.test_openrouter_quotes import FAST_CATALOG, POLICY, REQUEST
from core.models import ModelConfig

MODEL = "openrouter/anthropic/claude-opus-5.5"


def test_discovery_lists_only_available_priority_tiers_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exclude default, flex, ultrafast, and unavailable endpoints from fast mode.

    Args:
        monkeypatch: Replace the public metadata transport.
    """
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        """Return representative public endpoint metadata."""
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "id": MODEL.removeprefix("openrouter/"),
                    "endpoints": [
                        {"tag": "anthropic", "service_tier": None},
                        {"tag": "anthropic/fast", "service_tier": "priority", "status": 0},
                        {"tag": "other/priority", "service_tier": "fast", "status": 0},
                        {"tag": "other/flex", "service_tier": "flex"},
                        {"tag": "other/ultrafast", "service_tier": "ultrafast"},
                        {"tag": "down/fast", "service_tier": "priority", "status": -1},
                    ],
                }
            },
        )

    factory = httpx.Client
    monkeypatch.setattr(
        fast.httpx, "Client", lambda **kwargs: factory(transport=httpx.MockTransport(respond), **kwargs)
    )
    fast._fetch_fast_providers.cache_clear()
    try:
        assert fast.fast_providers(MODEL) == ("anthropic/fast", "other/priority")
        assert fast.fast_providers(MODEL) == ("anthropic/fast", "other/priority")
        assert fast.fast_providers("anthropic/claude-opus-5.5") == ()
        assert fast.fast_providers("openrouter/../models") == ()
        assert len(seen) == 1
        assert "authorization" not in seen[0].headers
    finally:
        fast._fetch_fast_providers.cache_clear()


@pytest.mark.parametrize(("status", "payload"), [(503, {}), (200, {"data": None}), (200, {"data": {"id": "wrong"}})])
def test_metadata_failure_disables_fast_mode(status: int, payload: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep unsupported or failed discovery off without blocking the menu.

    Args:
        status: Public API response code.
        payload: Public API response body.
        monkeypatch: Replace the public metadata transport.
    """
    factory = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json=payload))
    monkeypatch.setattr(fast.httpx, "Client", lambda **kwargs: factory(transport=transport, **kwargs))
    fast._fetch_fast_providers.cache_clear()
    try:
        assert fast.fast_providers(MODEL) == ()
    finally:
        fast._fetch_fast_providers.cache_clear()


@pytest.mark.parametrize("token_source", ["managed", "byok"])
def test_priority_preserves_credentials_and_reserves_priority_prices(
    token_source: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Carry explicit priority routing into the same price quote used by dispatch.

    Args:
        token_source: The hosted or own-key route.
        monkeypatch: Supply verified priority metadata without a network call.
    """
    monkeypatch.setattr(fast, "fast_providers", lambda model: ("anthropic/fast",))
    original = ModelConfig(
        name=MODEL,
        token_source=token_source,
        byok_provider="openrouter",
        extra={
            "api_key": "synthetic",
            "reasoning_effort": "high",
            "extra_body": {"provider": {"zdr": True}},
        },
    )
    result = fast.with_fast_mode(original, True)
    assert result.extra["api_key"] == original.extra["api_key"]
    assert result.byok_provider == original.byok_provider
    assert result.token_source == token_source
    assert result.extra["reasoning_effort"] == "high"
    assert original.extra["extra_body"] == {"provider": {"zdr": True}}
    body = result.extra["extra_body"]
    assert body["provider"] == {"only": ["anthropic/fast"], "allow_fallbacks": False, "zdr": True}
    assert CLI_FAST_KEY not in body
    priced = price_text_request({**REQUEST, **body}, FAST_CATALOG, POLICY)
    assert priced.body["provider"]["only"] == ["anthropic/fast"]
    assert priced.body["provider"]["max_price"]["prompt"] == 8


@pytest.mark.parametrize("provider", [{"only": ["other"]}, {"ignore": ["anthropic"]}])
def test_priority_respects_existing_provider_restrictions(provider: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep fast mode from overriding provider restrictions.

    Args:
        provider: An existing restriction that excludes the priority endpoint.
        monkeypatch: Supply priority metadata without a network call.
    """
    monkeypatch.setattr(fast, "fast_providers", lambda model: ("anthropic/fast",))
    config = ModelConfig(name=MODEL, extra={"extra_body": {"provider": provider}})
    assert fast.with_fast_mode(config, True) is config


def test_off_unsupported_and_cli_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Leave unsupported routes unchanged and preserve local CLI fast mode.

    Args:
        monkeypatch: Make OpenRouter metadata unavailable.
    """
    monkeypatch.setattr(fast, "fast_providers", lambda model: ())
    config = ModelConfig(name=MODEL)
    assert fast.with_fast_mode(config, False) is config
    assert fast.with_fast_mode(config, True) is config
    cli = ModelConfig(name="anthropic/claude-opus-5.5", token_source="subscription", cli_provider="claude")
    assert fast.with_fast_mode(cli, True).extra["extra_body"][CLI_FAST_KEY] is True


def test_capability_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expose a signed-in caller's catalog model support; skip OpenRouter for other ids.

    Args:
        monkeypatch: Replace metadata discovery and the catalogs with known results.
    """
    fetched: list[str] = []

    def fetch(model: str, window: int) -> tuple[str, ...]:
        """Record each metadata fetch and report one priority tier."""
        fetched.append(model)
        return ("anthropic/fast",)

    catalog = ModelCatalogResponse(
        providers=[], models=[CatalogModel(value=MODEL, label="Opus", provider="openrouter", supports_thinking=True)]
    )
    monkeypatch.setattr(fast, "_fetch_fast_providers", fetch)
    monkeypatch.setattr(models_router, "get_catalog_cached", lambda: catalog)
    monkeypatch.setattr(models_router, "get_byok_catalog_cached", lambda: ModelCatalogResponse(providers=[], models=[]))
    app = FastAPI()
    app.include_router(create_models_router())
    client = TestClient(app)
    assert client.get("/models/fast-mode", params={"model": MODEL}).status_code == 401
    bypass_auth(app)
    assert client.get("/models/fast-mode", params={"model": MODEL}).json() == {"supports_fast": True}
    assert client.get("/models/fast-mode", params={"model": "openrouter/vendor/not-listed"}).json() == {
        "supports_fast": False
    }
    assert client.get("/models/fast-mode", params={"model": "anthropic/claude-opus-5.5"}).json() == {
        "supports_fast": False
    }
    assert fetched == [MODEL]
