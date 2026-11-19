"""Tests for the Code Puppy harness: registry entry, gateway-only launch, protected env and output parsing."""

from __future__ import annotations

from ..harnesses.code_puppy import CODE_PUPPY_EFFORTS, SPEC


def test_spec_declares_the_levels_the_launch_accepts() -> None:
    """Submit and the launch check one constant, so both refuse the same levels."""
    assert SPEC.supported_efforts == CODE_PUPPY_EFFORTS == ("none", "minimal", "low", "medium", "high", "xhigh", "max")
