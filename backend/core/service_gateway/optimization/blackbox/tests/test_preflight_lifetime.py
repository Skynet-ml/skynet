"""Bound the setup check's outer box by the request's own timeouts."""

from core.service_gateway.optimization.blackbox.preflight import judge_lifetime_seconds, preflight_lifetime_seconds

SCORER = {"kind": "python", "metric_code": "def metric(candidate, case):\n    return 1.0\n"}


def test_preflight_lifetime_follows_the_scorer_timeout() -> None:
    """A check needs the fixed overhead plus two scorer boxes."""
    default = preflight_lifetime_seconds({"scorer": SCORER})
    slow = preflight_lifetime_seconds({"scorer": {**SCORER, "timeout_seconds": 600}})

    assert default == 600 + 2 * (60 + 60)
    assert slow == 600 + 2 * (600 + 60)


def test_judge_lifetime_grows_with_the_cases() -> None:
    """A judge call (SKY-696) needs the setup box plus one box for each case, or one without cases."""
    assert judge_lifetime_seconds({"scorer": SCORER}, 0) == 600 + 2 * (60 + 60)
    assert judge_lifetime_seconds({"scorer": SCORER}, 4) == 600 + 5 * (60 + 60)
