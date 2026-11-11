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
