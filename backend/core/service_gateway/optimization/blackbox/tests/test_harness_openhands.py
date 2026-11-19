"""Tests for the OpenHands harness: registry entry, pinned install, gateway-only launch, task file, final state, protected env and output parsing."""

from __future__ import annotations

from core.models.blackbox import BLACKBOX_HARNESS_OPENHANDS

from ..harnesses import REGISTRY
from ..harnesses.openhands import (
    OPENHANDS_EFFORTS,
)


def test_spec_declares_the_litellm_effort_levels() -> None:
    """Declare the pinned LiteLLM ``reasoning_effort`` levels, so submit refuses any other level (SKY-585)."""
    assert REGISTRY[BLACKBOX_HARNESS_OPENHANDS].supported_efforts == (
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
    )
    assert REGISTRY[BLACKBOX_HARNESS_OPENHANDS].supported_efforts == OPENHANDS_EFFORTS
