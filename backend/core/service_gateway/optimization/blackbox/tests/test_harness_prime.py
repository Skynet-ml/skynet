"""Tests for the Prime Agent harness effort levels: the spec declares Pi's levels, and each one passes unchanged."""

from __future__ import annotations

import json

import pytest

from core.models.blackbox import BLACKBOX_HARNESS_PRIME

from ..harness import GatewayConfig
from ..harnesses import REGISTRY
from ..harnesses.pi import PI_EFFORTS
from ..harnesses.prime import SPEC

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="k")
_MODELS_FILE = ".app/prime/models.json"


def test_spec_declares_the_pi_thinking_levels() -> None:
    """Prime Agent 0.9.7 bundles Pi's level list and clamp, so it declares the same levels."""
    assert REGISTRY[BLACKBOX_HARNESS_PRIME] is SPEC
    assert SPEC.supported_efforts == PI_EFFORTS


@pytest.mark.parametrize("effort", PI_EFFORTS)
def test_declared_effort_reaches_the_gateway_unchanged(effort: str) -> None:
    """Each level selects its own Prime Agent level and maps back to the same wire value.

    Args:
        effort: A declared Skynet level.
    """
    launch = SPEC.launch("m", _GATEWAY, reasoning_effort=effort)

    entry = json.loads(launch.files[_MODELS_FILE])["providers"]["app"]["models"][0]
    level = "off" if effort == "none" else effort
    assert entry["thinkingLevelMap"] == {level: effort}
    assert f'--model "$APP_MODEL:{level}"' in launch.run_command


@pytest.mark.parametrize("effort", [None, ""])
def test_no_effort_keeps_the_prime_agent_default(effort: str | None) -> None:
    """No effort sends no level map and no thinking suffix, so Prime Agent keeps its default.

    Args:
        effort: No choice.
    """
    launch = SPEC.launch("m", _GATEWAY, reasoning_effort=effort)

    entry = json.loads(launch.files[_MODELS_FILE])["providers"]["app"]["models"][0]
    assert "thinkingLevelMap" not in entry
    assert entry["reasoning"] is False
    assert '--model "$APP_MODEL"' in launch.run_command
