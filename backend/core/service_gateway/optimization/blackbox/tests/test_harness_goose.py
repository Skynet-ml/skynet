"""Tests for the Goose CLI harness: registry entry, pinned install, gateway-only launch, protected env and output parsing."""

from __future__ import annotations

import pytest

from core.exceptions import ServiceError
from core.models.blackbox import (
    BLACKBOX_HARNESS_GOOSE,
    BlackboxProposer,
)

from ..harnesses import REGISTRY
from ..service import _check_proposer_effort


def test_goose_declares_its_effort_levels_for_the_submit_check() -> None:
    """Declare goose's levels on its registry entry, so submit refuses the rest before paid work."""
    assert REGISTRY[BLACKBOX_HARNESS_GOOSE].supported_efforts == ("none", "low", "medium", "high", "max")


@pytest.mark.parametrize(("effort", "accepted"), [("high", True), (None, True), ("xhigh", False), ("minimal", False)])
def test_submit_check_uses_the_goose_levels(effort: str | None, accepted: bool) -> None:
    """Refuse at submit an effort goose has no level for, and accept a supported one or none."""
    proposer = BlackboxProposer(harness=BLACKBOX_HARNESS_GOOSE)
    if accepted:
        _check_proposer_effort(proposer, effort)
    else:
        with pytest.raises(ServiceError, match=f"goose proposer does not support the '{effort}' effort level"):
            _check_proposer_effort(proposer, effort)
