"""Tests for the scorer gateway resolution and the scorer usage ledger."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.billing.model_gateway import ROUTE_KEY
from core.exceptions import ServiceError
from core.models.common import ModelConfig

from .. import sandbox_scorer as sandbox_scorer_mod
from ..harness import GatewayConfig
from ..runner import GatewayClient, ScorerError
from ..sandbox_scorer import ScorerGateway, ScorerUsage, scorer_gateway
from .mocks import FakeGateway


def test_unprotected_byok_endpoint_accepts_declared_billing_name() -> None:
    """A custom endpoint accepts its original provider-qualified declaration."""
    with FakeGateway(reply="ok") as gateway:
        config = ModelConfig(
            name="openai/gpt-4.1-mini", token_source="byok", base_url=gateway.url, extra={"api_key": "test-key"}
        )
        bound = scorer_gateway(config, SimpleNamespace(lm_request_timeout_seconds=33.0))
        payload = bound.runner_payload()
        client = GatewayClient(payload["url"], payload["model"], declared_model=payload.get("declared_model"))
        assert client("prompt", model="openai/gpt-4.1-mini") == "ok"
        with pytest.raises(ScorerError, match="must match"):
            client("prompt", model="openai/other")
    assert gateway.requests[0]["body"]["model"] == "gpt-4.1-mini"


def test_scorer_usage_records_entries_in_the_lm_history_shape() -> None:
    """Missing counters default to zero so the shared usage helpers can read the ledger."""
    ledger = ScorerUsage("m")

    ledger.record([{"prompt_tokens": 2}, {}])

    assert ledger.model == "m"
    assert ledger.history == [
        {"usage": {"prompt_tokens": 2, "completion_tokens": 0, "total_tokens": 0}},
        {"usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}},
    ]
    assert ledger.by_model() == {"m": (2, 0)}


def test_scorer_gateway_for_managed_models_uses_the_shared_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """Managed models go through the LiteLLM gateway with the ``openrouter/`` prefix stripped."""
    monkeypatch.setattr(
        sandbox_scorer_mod,
        "gateway_from_settings",
        lambda settings: GatewayConfig(url="http://gw/v1", api_key="gw-key"),
    )
    config = ModelConfig(
        name="openrouter/openai/gpt-4o", temperature=0.3, max_tokens=50, extra={"reasoning_effort": "high"}
    )

    gateway = scorer_gateway(config, SimpleNamespace(lm_request_timeout_seconds=33.0))

    assert gateway == ScorerGateway(
        url="http://gw/v1",
        model="openai/gpt-4o",
        api_key="gw-key",
        billing_model="openrouter/openai/gpt-4o",
        temperature=0.3,
        max_tokens=50,
        reasoning_effort="high",
        timeout_seconds=33.0,
        gateway_reasoning=True,
    )
    assert gateway.runner_payload() == {
        "url": "http://gw/v1",
        "model": "openai/gpt-4o",
        "declared_model": "openrouter/openai/gpt-4o",
        "temperature": 0.3,
        "max_tokens": 50,
        "reasoning_effort": "high",
        "timeout_seconds": 33.0,
        "gateway_reasoning": True,
    }


def test_scorer_gateway_for_managed_models_needs_a_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a reachable gateway a managed scorer model is a configuration error."""
    monkeypatch.setattr(sandbox_scorer_mod, "gateway_from_settings", lambda settings: None)

    with pytest.raises(ServiceError, match="LITELLM_PROXY_URL"):
        scorer_gateway(ModelConfig(name="openrouter/openai/gpt-4o"), SimpleNamespace(lm_request_timeout_seconds=33.0))


def test_subscription_gateway_passes_cli_route_to_runner() -> None:
    """Keep the source and billing name when the protected route changes the model name."""
    config = ModelConfig(
        name="subscription/claude/anthropic/claude-opus-5.5",
        token_source="subscription",
        cli_provider="claude",
        extra={ROUTE_KEY: {"url": "http://gw/v1", "model": "anthropic/claude-opus-5.5", "token": "key"}},
    )
    payload = scorer_gateway(config, SimpleNamespace(lm_request_timeout_seconds=33.0)).runner_payload()
    assert payload["subscription"] is True
    assert payload["model"] == "anthropic/claude-opus-5.5"
    assert payload["declared_model"] == config.name


@pytest.mark.parametrize(
    ("config", "url", "model"),
    [
        (
            ModelConfig(name="openrouter/anthropic/claude", token_source="byok", extra={"api_key": "byok"}),
            "https://openrouter.ai/api/v1",
            "anthropic/claude",
        ),
        (
            ModelConfig(
                name="openai/gpt-4o",
                token_source="byok",
                base_url="https://proxy.example/v1/",
                extra={"api_key": "byok"},
            ),
            "https://proxy.example/v1",
            "gpt-4o",
        ),
        (
            ModelConfig(
                name="gpt-4o", token_source="byok", extra={"api_key": "byok", "api_base": "https://proxy.example/v1"}
            ),
            "https://proxy.example/v1",
            "gpt-4o",
        ),
    ],
)
def test_scorer_gateway_for_byok_models_talks_to_the_provider_directly(
    config: ModelConfig, url: str, model: str
) -> None:
    """BYOK models use the user's key against OpenRouter or their own base URL.

    Args:
        config: The BYOK model.
        url: Expected gateway URL.
        model: Expected wire model name.
    """
    gateway = scorer_gateway(config, SimpleNamespace(lm_request_timeout_seconds=12.0))

    assert (gateway.url, gateway.model, gateway.api_key, gateway.billing_model) == (url, model, "byok", config.name)
    assert gateway.timeout_seconds == 12.0


def test_scorer_gateway_for_byok_models_needs_a_reachable_endpoint() -> None:
    """A BYOK model with neither an OpenRouter prefix nor a base URL cannot be reached from a box."""
    with pytest.raises(ServiceError, match="OpenRouter or a base_url"):
        scorer_gateway(
            ModelConfig(name="gpt-4o", token_source="byok", extra={"api_key": "byok"}),
            SimpleNamespace(lm_request_timeout_seconds=1.0),
        )
