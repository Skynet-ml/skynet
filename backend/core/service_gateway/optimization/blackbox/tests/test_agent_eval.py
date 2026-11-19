"""Tests for the shared sandbox helpers: gateway resolution."""

from __future__ import annotations

from typing import Any

import pytest

from core.config import Settings

from .. import agent_eval as agent_eval_mod
from ..agent_eval import (
    _GATEWAY_MISSING,
    agent_target_unavailable_reason,
    gateway_from_settings,
)
from ..harness import GatewayConfig


def _gateway_settings(**values: Any) -> Settings:
    """Build settings with every gateway field cleared, then the given overrides.

    Init kwargs outrank the dotenv file, so this ignores any ambient LiteLLM config.

    Args:
        **values: Field overrides.

    Returns:
        The settings.
    """
    fields: dict[str, Any] = {
        "blackbox_agent_gateway_url": None,
        "blackbox_agent_gateway_api_key": None,
        "litellm_proxy_url": None,
        "litellm_proxy_api_key": None,
    }
    fields.update(values)
    return Settings(**fields)


def test_gateway_from_settings_prefers_the_dedicated_gateway() -> None:
    """The dedicated agent-gateway settings win over the LiteLLM fallback and lose the trailing slash."""
    settings = _gateway_settings(
        blackbox_agent_gateway_url="https://dedicated/",
        blackbox_agent_gateway_api_key="dk",
        litellm_proxy_url="https://litellm",
        litellm_proxy_api_key="lk",
    )

    gateway = gateway_from_settings(settings)

    assert gateway == GatewayConfig(url="https://dedicated", api_key="dk")


def test_gateway_from_settings_falls_back_to_litellm() -> None:
    """When the dedicated gateway is unset, the LiteLLM proxy is used."""
    settings = _gateway_settings(litellm_proxy_url="https://litellm/", litellm_proxy_api_key="lk")

    assert gateway_from_settings(settings) == GatewayConfig(url="https://litellm", api_key="lk")


def test_gateway_from_settings_is_none_without_a_url_or_key() -> None:
    """A missing URL or key yields no gateway."""
    assert gateway_from_settings(_gateway_settings()) is None
    assert gateway_from_settings(_gateway_settings(blackbox_agent_gateway_url="https://x")) is None
    assert gateway_from_settings(_gateway_settings(blackbox_agent_gateway_api_key="k")) is None


def test_agent_target_unavailable_reason_orders_sandbox_before_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing sandbox is reported first; then a missing gateway; then nothing."""
    monkeypatch.setattr(agent_eval_mod, "sandbox_unavailable_reason", lambda settings: "no sandbox")
    assert agent_target_unavailable_reason(_gateway_settings()) == "no sandbox"

    monkeypatch.setattr(agent_eval_mod, "sandbox_unavailable_reason", lambda settings: None)
    assert agent_target_unavailable_reason(_gateway_settings()) == _GATEWAY_MISSING

    configured = _gateway_settings(blackbox_agent_gateway_url="https://x", blackbox_agent_gateway_api_key="k")
    assert agent_target_unavailable_reason(configured) is None
