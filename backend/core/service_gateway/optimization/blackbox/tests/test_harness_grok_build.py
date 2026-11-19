"""Tests for the Grok Build CLI harness: registry entry, pinned install, gateway-only launch, effort and output parsing."""

from __future__ import annotations

import pytest

from core.exceptions import ServiceError
from core.models.blackbox import (
    BLACKBOX_HARNESS_GROK_BUILD,
    BlackboxProposer,
)

from ..harnesses import REGISTRY
from ..harnesses.grok_build import GROK_EFFORTS, GROK_SHA256
from ..service import _check_proposer_effort


def test_pinned_digests_are_the_release_binaries() -> None:
    """Pin the sha256 of the 1.0.77 Linux binaries, taken from xAI's downloads on 2026-11-10."""
    assert GROK_SHA256 == {
        "x86_64": "b702950aa5827d855e493278ab6215a4e0f5bceb5bba6c6243a9f55cdd143b0a",
        "aarch64": "b30eee14a656124ef3c9da8577b5e783ef2cffabeb2b679c5c49633e77eed574",
    }


def test_submit_accepts_only_the_declared_grok_efforts() -> None:
    """Declare Grok's levels on the spec, so submit refuses any other level before paid work."""
    proposer = BlackboxProposer(harness=BLACKBOX_HARNESS_GROK_BUILD)

    assert REGISTRY[BLACKBOX_HARNESS_GROK_BUILD].supported_efforts == GROK_EFFORTS
    for effort in (*GROK_EFFORTS, None):
        _check_proposer_effort(proposer, effort)
    with pytest.raises(ServiceError, match="grok_build proposer does not support the 'turbo' effort level"):
        _check_proposer_effort(proposer, "turbo")
