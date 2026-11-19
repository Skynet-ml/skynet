"""Tests for the Cline CLI harness: registry entry, gateway-only launch, protected env and output parsing."""

from __future__ import annotations

import pytest

from core.exceptions import ServiceError
from core.models.blackbox import (
    BLACKBOX_HARNESS_CLINE,
    BlackboxProposer,
)

from ..harnesses import REGISTRY
from ..harnesses.cline import CLINE_THINKING_LEVELS
from ..service import _check_proposer_effort


def test_supported_levels_are_the_pinned_release_thinking_levels() -> None:
    """Declare exactly the ``--thinking`` levels of Cline 3.0.74, on the spec the submit check reads."""
    assert CLINE_THINKING_LEVELS == ("none", "low", "medium", "high", "xhigh")
    assert REGISTRY[BLACKBOX_HARNESS_CLINE].supported_efforts == CLINE_THINKING_LEVELS


@pytest.mark.parametrize("effort", [None, "none", "low", "medium", "high", "xhigh"])
def test_submit_accepts_the_supported_levels(effort: str | None) -> None:
    """Let a Cline proposer submit with no effort or a level Cline supports."""
    _check_proposer_effort(BlackboxProposer(harness=BLACKBOX_HARNESS_CLINE), effort)


@pytest.mark.parametrize("effort", ["minimal", "max", "High"])
def test_submit_refuses_an_unsupported_level_before_paid_work(effort: str) -> None:
    """Refuse at submit a level Cline does not support, before the run reserves budget."""
    with pytest.raises(ServiceError, match=f"cline proposer does not support the '{effort}' effort level"):
        _check_proposer_effort(BlackboxProposer(harness=BLACKBOX_HARNESS_CLINE), effort)
