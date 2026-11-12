"""Bound the setup check's outer box by the request's own timeouts."""

from core.service_gateway.optimization.blackbox.agent_eval import READINESS_LIFETIME_SECONDS, case_lifetime_seconds
from core.service_gateway.optimization.blackbox.preflight import preflight_lifetime_seconds
from core.service_gateway.optimization.blackbox.sandbox import KILL_GRACE_SECONDS

SCORER = {"kind": "python", "metric_code": "def metric(candidate, case):\n    return 1.0\n"}


def test_case_lifetime_gives_each_setup_step_its_own_allowance() -> None:
    """Every step carries its timeout plus the KILL grace, on top of the box overhead."""
    assert case_lifetime_seconds(100.0, setup_steps=0) == 60 + 100 + KILL_GRACE_SECONDS
    assert case_lifetime_seconds(100.0, setup_steps=3) == 60 + 100 + KILL_GRACE_SECONDS + 3 * (600 + KILL_GRACE_SECONDS)


def test_preflight_lifetime_follows_the_scorer_timeout() -> None:
    """A text target's check needs the fixed overhead plus two scorer boxes."""
    default = preflight_lifetime_seconds({"scorer": SCORER})
    slow = preflight_lifetime_seconds({"scorer": {**SCORER, "timeout_seconds": 600}})

    assert default == 600 + 2 * (60 + 60)
    assert slow == 600 + 2 * (600 + 60)


def test_preflight_lifetime_adds_the_agent_case_and_its_readiness_probe() -> None:
    """An agent target adds the readiness box and one case with every setup step it could carry."""
    payload = {"scorer": SCORER, "target": {"kind": "agent", "model": "gpt", "timeout_seconds": 300}}

    lifetime = preflight_lifetime_seconds(payload)

    assert lifetime == 600 + 2 * (60 + 60) + READINESS_LIFETIME_SECONDS + case_lifetime_seconds(300.0, setup_steps=3)
