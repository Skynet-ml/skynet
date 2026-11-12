"""Tests for the Pi harness effort levels: the spec declares them, and each one reaches Pi unchanged."""

from __future__ import annotations

import json

import pytest

from core.models.blackbox import BLACKBOX_HARNESS_PI

from ..harness import GatewayConfig
from ..harnesses import REGISTRY
from ..harnesses.pi import PI_EFFORTS, SPEC

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="k")
_MODELS_FILE = ".app/pi/models.json"


def test_spec_declares_every_pinned_pi_thinking_level() -> None:
    """Pi 0.87.1 has one thinking level for each Skynet level, so the spec offers all seven."""
    assert REGISTRY[BLACKBOX_HARNESS_PI] is SPEC
    assert SPEC.supported_efforts == PI_EFFORTS == ("none", "minimal", "low", "medium", "high", "xhigh", "max")


@pytest.mark.parametrize("effort", PI_EFFORTS)
def test_declared_effort_reaches_the_gateway_unchanged(effort: str) -> None:
    """Each level selects its own Pi level and maps back to the same wire value, so Pi neither clamps nor remaps it.

    Args:
        effort: A declared Skynet level.
    """
    launch = SPEC.launch("m", _GATEWAY, reasoning_effort=effort)

    entry = json.loads(launch.files[_MODELS_FILE])["providers"]["app"]["models"][0]
    pi_level = "off" if effort == "none" else effort
    assert entry["thinkingLevelMap"] == {pi_level: effort}
    assert f'--model "$APP_MODEL:{pi_level}"' in launch.run_command


@pytest.mark.parametrize("effort", [None, ""])
def test_no_effort_keeps_the_pi_default(effort: str | None) -> None:
    """No effort sends no level map and no thinking suffix, so Pi keeps its default.

    Args:
        effort: No choice.
    """
    launch = SPEC.launch("m", _GATEWAY, reasoning_effort=effort)

    entry = json.loads(launch.files[_MODELS_FILE])["providers"]["app"]["models"][0]
    assert "thinkingLevelMap" not in entry
    assert entry["reasoning"] is False
    assert '--model "$APP_MODEL"' in launch.run_command
