"""Tests for the mini-swe-agent harness: registry entry, pinned install, gateway-only launch, protected env and output parsing."""

from __future__ import annotations

import pytest

from core.api.model_catalog import REASONING_EFFORTS
from core.exceptions import ServiceError
from core.models.blackbox import (
    BLACKBOX_HARNESS_MINI_SWE_AGENT,
    BlackboxProposer,
)

from ..harnesses import REGISTRY
from ..harnesses.mini_swe_agent import (
    MINI_SWE_AGENT_EFFORTS,
)
from ..service import _check_proposer_effort

# mini-swe-agent is not a backend dependency. When it is installed, a test
# checks its real built-in config and model class against the gateway.
try:
    from minisweagent.config import builtin_config_dir, get_config_from_spec
    from minisweagent.models import get_model
    from minisweagent.utils.serialize import recursive_merge
except ImportError:
    builtin_config_dir = get_config_from_spec = get_model = recursive_merge = None

# Stand-in for uv: ``tool dir`` names the tool folder, and ``tool install``
# puts a fake tool Python there that reports the pinned version.
_FAKE_UV = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_LOG"
case "$1 $2" in
  "tool dir") echo "${FAKE_TOOL_DIR:-$HOME/uv-tools}" ;;
  "tool install")
    mkdir -p "$HOME/uv-tools/mini-swe-agent/bin"
    printf '#!/bin/sh\\necho 2.4.6\\n' > "$HOME/uv-tools/mini-swe-agent/bin/python"
    chmod +x "$HOME/uv-tools/mini-swe-agent/bin/python" ;;
esac
"""


def test_spec_declares_every_skynet_effort_level() -> None:
    """Declare all of Skynet's effort levels, so submit offers and checks exactly these."""
    assert REGISTRY[BLACKBOX_HARNESS_MINI_SWE_AGENT].supported_efforts == REASONING_EFFORTS
    assert MINI_SWE_AGENT_EFFORTS == REASONING_EFFORTS


@pytest.mark.parametrize(("effort", "accepted"), [(None, True), ("max", True), ("none", True), ("ultra", False)])
def test_submit_checks_the_effort_before_paid_work(effort: str | None, accepted: bool) -> None:
    """Accept a declared level or no level at submit, and refuse any other before the run reserves budget."""
    proposer = BlackboxProposer(harness=BLACKBOX_HARNESS_MINI_SWE_AGENT)
    if accepted:
        _check_proposer_effort(proposer, effort)
    else:
        with pytest.raises(ServiceError, match="does not support the 'ultra' effort level"):
            _check_proposer_effort(proposer, effort)
