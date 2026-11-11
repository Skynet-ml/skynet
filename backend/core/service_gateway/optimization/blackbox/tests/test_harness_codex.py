"""Tests for the Codex harness effort levels: pass-through, refusal and the default."""

from __future__ import annotations

import tomllib

import pytest

from core.exceptions import ServiceError

from ..harness import GatewayConfig
from ..harnesses import REGISTRY
from ..harnesses.codex import CODEX_EFFORTS, SPEC

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="secret-key")
_CONFIG = ".app/codex/config.toml"


@pytest.mark.parametrize("effort", ["none", "minimal", "low", "medium", "high", "xhigh", "max"])
def test_supported_effort_passes_to_codex_unchanged(effort: str) -> None:
    """Write each supported level into the Codex config as the user picked it.

    Args:
        effort: The user's effort level.
    """
    config = tomllib.loads(SPEC.launch("m", _GATEWAY, reasoning_effort=effort).files[_CONFIG])
    assert config["model_reasoning_effort"] == effort


@pytest.mark.parametrize("effort", ["ultra", "persistent", "auto", "turbo", "HIGH", " high"])
def test_unsupported_effort_is_refused(effort: str) -> None:
    """Refuse a level Codex does not send unchanged, so no run gets an effort the user did not pick.

    Args:
        effort: An effort level outside :data:`CODEX_EFFORTS`.
    """
    with pytest.raises(ServiceError, match="does not support the effort level"):
        SPEC.launch("m", _GATEWAY, reasoning_effort=effort)


@pytest.mark.parametrize("effort", [None, ""])
def test_no_effort_keeps_the_codex_default(effort: str | None) -> None:
    """Leave the effort out of the config, so Codex keeps its own default.

    Args:
        effort: No effort level.
    """
    config = tomllib.loads(SPEC.launch("m", _GATEWAY, reasoning_effort=effort).files[_CONFIG])
    assert "model_reasoning_effort" not in config


def test_spec_declares_the_codex_efforts_for_the_submit_check() -> None:
    """Declare the levels on the spec, so submit refuses any other level before paid work."""
    assert REGISTRY["codex"].supported_efforts == CODEX_EFFORTS
    assert CODEX_EFFORTS == ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def test_codex_lists_each_proposer_mcp_server_at_the_gateway_relay_path() -> None:
    """Give each [proposer] server only a gateway relay url: no real url, header or key (SKY-610)."""
    config = tomllib.loads(SPEC.launch("m", _GATEWAY, mcp_servers=("search", "my tools")).files[_CONFIG])

    assert config["mcp_servers"] == {
        "search": {"url": "https://gw.example/v1/mcp/search"},
        "my tools": {"url": "https://gw.example/v1/mcp/my%20tools"},
    }


def test_codex_without_proposer_mcp_servers_lists_none() -> None:
    """Leave MCP out of the config when the proposer's file lists no server."""
    assert "mcp_servers" not in tomllib.loads(SPEC.launch("m", _GATEWAY).files[_CONFIG])
