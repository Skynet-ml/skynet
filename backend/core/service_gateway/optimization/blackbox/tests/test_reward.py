"""The accuracy, speed and cost reward: a fixed, continuous blend of trusted measurements (SKY-69)."""

from __future__ import annotations

import sys
from itertools import pairwise
from typing import Any

import pytest

from .. import runner
from ..workspace_harness import RewardSpecError, Workspace, blend_reward, reference_utility, reward_spec
from .mocks import FakeGateway

CFCA_SPEC: dict[str, Any] = {
    "weights": {"accuracy": 0.35, "speed": 0.35, "cost": 0.30},
    "accuracy": {"meaning": "Distance from the true location", "unit": "km", "reference": 25},
    "speed": {"meaning": "Run time of one response", "unit": "ms", "reference": 2000},
    "cost": {"meaning": "Model cost of one response", "unit": "USD", "reference": 0.001},
}
SPEC = reward_spec(CFCA_SPEC)


def _observed(time_ms: float | None = 2000.0, cost_usd: float | None = 0.001, reason: str | None = None) -> dict:
    """Build a trusted observation with the given run time and service cost."""
    return {
        "timing": {"task_ms": time_ms},
        "cost": {"usd": cost_usd, "status": "metered" if cost_usd is not None else "unavailable", "reason": reason},
    }


def _total(accuracy: float | None, time_ms: float | None = 2000.0, cost_usd: float | None = 0.001) -> float:
    """Return the reward of one response."""
    return blend_reward(SPEC, accuracy, _observed(time_ms, cost_usd))[0]


@pytest.fixture(autouse=True)
def _restore_helper_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo the harness's ``import skynet`` binding after each test."""
    monkeypatch.setitem(sys.modules, runner.HELPER_MODULE_NAME, None)


def test_weights_are_kept_as_agreed_and_normalized() -> None:
    """35/35/30 stays 35/35/30; the same blend given in percent is the same spec."""
    assert SPEC["weights"] == pytest.approx({"accuracy": 0.35, "speed": 0.35, "cost": 0.30})
    percent = reward_spec({**CFCA_SPEC, "weights": {"accuracy": 35, "speed": 35, "cost": 30}})
    assert percent["weights"] == pytest.approx(SPEC["weights"])


@pytest.mark.parametrize(
    ("worse", "better"),
    [
        # Above and below the 25 km reference.
        ({"accuracy": 60.0}, {"accuracy": 40.0}),
        ({"accuracy": 10.0}, {"accuracy": 2.0}),
        # Above and below the 2000 ms reference.
        ({"time_ms": 6000.0}, {"time_ms": 3000.0}),
        ({"time_ms": 900.0}, {"time_ms": 300.0}),
        # Above and below the $0.001 reference.
        ({"cost_usd": 0.004}, {"cost_usd": 0.002}),
        ({"cost_usd": 0.0005}, {"cost_usd": 0.0001}),
    ],
)
def test_improving_one_dimension_alone_raises_the_reward(worse: dict, better: dict) -> None:
    """Each improvement counts, on both sides of its reference, with the others fixed."""
    base = {"accuracy": 25.0, "time_ms": 2000.0, "cost_usd": 0.001}
    assert _total(**{**base, **better}) > _total(**{**base, **worse})


def test_the_utility_is_continuous_with_no_plateau_or_cutoff() -> None:
    """Even very slow or very cheap responses keep moving the score."""
    reference = 2000.0
    points = [0.0, 1.0, 500.0, 1999.0, 2000.0, 2001.0, 10_000.0, 60_000.0, 600_000.0]
    utilities = [reference_utility(point, reference) for point in points]
    assert utilities[0] == 1.0
    assert reference_utility(reference, reference) == 0.5
    assert all(a > b for a, b in pairwise(utilities))


def test_a_higher_total_does_not_mean_every_dimension_improved() -> None:
    """A more accurate but slower, costlier response wins overall and shows its losses."""
    before_score, before = blend_reward(SPEC, 40.0, _observed(1500.0, 0.0008))
    after_score, after = blend_reward(SPEC, 1.0, _observed(2500.0, 0.0012))

    assert after_score > before_score
    assert after["scores"]["accuracy"] > before["scores"]["accuracy"]
    assert after["scores"]["speed"] < before["scores"]["speed"]
    assert after["scores"]["cost"] < before["scores"]["cost"]


def test_an_empty_fast_cheap_response_is_scored_not_excluded() -> None:
    """No accuracy floor: a missing answer scores 0 on accuracy and keeps its speed and cost."""
    score, side_info = blend_reward(SPEC, None, _observed(100.0, 0.0))

    assert side_info["scores"]["accuracy"] == 0.0
    assert side_info["reward"]["unavailable"] == ["accuracy"]
    assert score == pytest.approx(0.35 * 2000 / 2100 + 0.30)
    assert "missing" in side_info["feedback"]


@pytest.mark.parametrize("accuracy", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_accuracy_is_unavailable_never_a_nan_total(accuracy: float) -> None:
    """A NaN or infinite accuracy (an output of "nan,nan") scores accuracy 0, as the setup preview does."""
    score, side_info = blend_reward(SPEC, accuracy, _observed())

    assert score == pytest.approx(blend_reward(SPEC, None, _observed())[0])
    assert side_info["reward"]["unavailable"] == ["accuracy"]
    assert side_info["reward"]["raw"]["accuracy"] is None


def test_an_infinite_weight_is_refused() -> None:
    """A weight of ``1e400`` would make every weight NaN; the spec is refused as the preview refuses it."""
    with pytest.raises(RewardSpecError):
        reward_spec({**CFCA_SPEC, "weights": {"accuracy": float("inf"), "speed": 1, "cost": 1}})


def test_unavailable_time_or_cost_scores_that_component_zero_and_says_so() -> None:
    """An unmeasured time or an unpriced call never looks free or instant."""
    _, no_time = blend_reward(SPEC, 0.0, _observed(None, 0.001))
    _, no_cost = blend_reward(SPEC, 0.0, _observed(2000.0, None, reason="1 of 1 call(s) returned no provider cost"))

    assert no_time["scores"]["speed"] == 0.0
    assert no_time["reward"]["unavailable"] == ["speed"]
    assert no_cost["scores"]["cost"] == 0.0
    assert no_cost["reward"]["unavailable"] == ["cost"]
    assert "no provider cost" in no_cost["feedback"]


def test_zero_cost_scores_full_cost_utility() -> None:
    """A response that made no model call costs nothing, and that is measured, not missing."""
    _, side_info = blend_reward(SPEC, 0.0, _observed(2000.0, 0.0))
    assert side_info["scores"]["cost"] == 1.0
    assert side_info["reward"]["unavailable"] == []


def test_raw_measurements_stay_beside_the_components() -> None:
    """The side info keeps raw values, components, weights and contributions that sum to the score."""
    score, side_info = blend_reward(SPEC, 12.5, _observed(1234.0, 0.00042))
    reward = side_info["reward"]

    assert reward["raw"] == {"accuracy": 12.5, "time_ms": 1234.0, "cost_usd": 0.00042}
    assert sum(reward["contributions"].values()) == pytest.approx(score)
    for name, component in reward["components"].items():
        assert reward["contributions"][name] == pytest.approx(SPEC["weights"][name] * component)


def test_a_candidate_score_never_depends_on_other_candidates() -> None:
    """References are fixed in the spec, so a new extreme candidate cannot reorder the others."""
    first = [_total(5.0, 1800.0, 0.0009), _total(30.0, 900.0, 0.0004)]
    blend_reward(SPEC, 10_000.0, _observed(900_000.0, 5.0))
    blend_reward(SPEC, 0.0, _observed(1.0, 0.0))
    again = [_total(5.0, 1800.0, 0.0009), _total(30.0, 900.0, 0.0004)]

    assert again == first


def test_a_score_accuracy_without_a_reference_is_used_as_written() -> None:
    """Without ``accuracy.reference`` the metric passes a 0-1 score."""
    spec = reward_spec({**CFCA_SPEC, "accuracy": {"meaning": "Exact match", "unit": "score"}})
    _, side_info = blend_reward(spec, 0.75, _observed())
    assert side_info["scores"]["accuracy"] == 0.75


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {"weights": {"accuracy": 1, "speed": 1}},
        {**CFCA_SPEC, "weights": {"accuracy": 0, "speed": 0, "cost": 0}},
        {**CFCA_SPEC, "weights": {"accuracy": -1, "speed": 1, "cost": 1}},
        {**CFCA_SPEC, "speed": {"reference": 0}},
        {**CFCA_SPEC, "cost": {}},
        {**CFCA_SPEC, "accuracy": {"reference": -5}},
    ],
)
def test_an_invalid_spec_is_refused(raw: Any) -> None:
    """A spec without usable weights and positive references never scores anything."""
    with pytest.raises(RewardSpecError):
        reward_spec(raw)


GEOCODE_RUN = (
    "import dspy\n"
    "from skynet import llm\n"
    "program = dspy.Predict('message -> coordinates')\n"
    "def run(artifact, input):\n"
    "    return llm(program.signature.instructions, input['message'])\n"
)
GEOCODE_METRIC = """
import math
from skynet import reward

REWARD_SPEC = {
    "weights": {"accuracy": 0.35, "speed": 0.35, "cost": 0.30},
    "accuracy": {"meaning": "Distance from the true location", "unit": "km", "reference": 25},
    "speed": {"meaning": "Run time of one response", "unit": "ms", "reference": 2000},
    "cost": {"meaning": "Model cost of one response", "unit": "USD", "reference": 0.001},
}


def _point(text):
    try:
        lat, lon = (float(part) for part in str(text).split(","))
    except ValueError:
        return None
    return lat, lon


def _km(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def score(input, output, expected=None):
    got = _point(output)
    if got is None:
        return reward(REWARD_SPEC, None, "no coordinates in the output")
    # An ambiguous place lists every acceptable point; the nearest one counts.
    targets = [_point(t) for t in expected["coordinates"].split("|")]
    distance = min(_km(got, target) for target in targets if target)
    return reward(REWARD_SPEC, distance, f"{distance:.1f} km from the nearest true point")
"""


def _geocode_workspace(url: str) -> Workspace:
    """Load the geocoding fixture against a fake task model at ``url``."""
    task = {"url": url, "model": "task", "api_key": "task-key", "timeout_seconds": 10}
    return Workspace(
        {
            "run_code": GEOCODE_RUN,
            "metric_code": GEOCODE_METRIC,
            "files": {},
            "artifact_kind": "dspy_program",
            "input_fields": ["message"],
            "gateways": {"task": task, "metric": None},
        }
    )


def _geocode(message: str, coordinates: str) -> dict[str, Any]:
    """Build one geocoding call's case."""
    return {"input": {"message": message}, "expected": {"coordinates": coordinates}}


def test_geocoding_metric_scores_from_trusted_time_and_cost() -> None:
    """The fixture's reward reads time and cost from the observation, not from the output."""
    with FakeGateway("32.0853,34.7818", cost=0.0004, delay_seconds=0.05) as gateway:
        reply = _geocode_workspace(gateway.url).handle(
            {"candidate": "Return lat,lon.", "case": _geocode("Meet me in Tel Aviv", "32.0853,34.7818")}
        )

    observed = reply["observation"]
    reward = reply["side_info"]["reward"]
    assert reward["raw"]["time_ms"] == observed["timing"]["task_ms"] >= 50
    assert reward["raw"]["cost_usd"] == pytest.approx(0.0004)
    assert reward["raw"]["accuracy"] == pytest.approx(0.0, abs=1e-6)
    assert reply["side_info"]["scores"]["accuracy"] == pytest.approx(1.0)
    assert reply["score"] == pytest.approx(sum(reward["contributions"].values()))


def test_geocoding_ambiguous_place_takes_the_nearest_true_point() -> None:
    """Springfield has several true points; the nearest one scores the answer."""
    with FakeGateway("39.7817,-89.6501", cost=0.0004) as gateway:
        reply = _geocode_workspace(gateway.url).handle(
            {"candidate": "p", "case": _geocode("Springfield", "42.1015,-72.5898|39.7817,-89.6501")}
        )
    assert reply["side_info"]["reward"]["raw"]["accuracy"] == pytest.approx(0.0, abs=1e-6)


def test_geocoding_missing_coordinates_and_missing_price_are_explicit() -> None:
    """No coordinates and no provider cost both score 0 on their component, never dropped."""
    with FakeGateway("I am not sure", cost=None) as gateway:
        reply = _geocode_workspace(gateway.url).handle({"candidate": "p", "case": _geocode("somewhere", "0,0")})

    reward = reply["side_info"]["reward"]
    assert sorted(reward["unavailable"]) == ["accuracy", "cost"]
    assert reward["components"]["accuracy"] == 0.0
    assert reward["components"]["cost"] == 0.0
    assert reward["components"]["speed"] > 0.0
    assert reply["score"] > 0.0


def test_geocoding_spoofed_speed_in_the_output_changes_nothing() -> None:
    """The run code cannot make itself look fast or cheap: it has no say over time or cost."""
    spoof_run = (
        "import time\n"
        "import dspy\n"
        "from skynet import llm\n"
        "program = dspy.Predict('message -> coordinates')\n"
        "def run(artifact, input):\n"
        "    time.sleep(0.3)\n"
        "    print('task_ms=1 cost=0')\n"
        "    return llm(program.signature.instructions, input['message'])\n"
    )
    with FakeGateway("32.0853,34.7818", cost=0.0004) as gateway:
        workspace = _geocode_workspace(gateway.url)
        workspace.run_code = spoof_run
        reply = workspace.handle({"candidate": "p", "case": _geocode("Tel Aviv", "32.0853,34.7818")})
    assert reply["side_info"]["reward"]["raw"]["time_ms"] >= 300


def test_geocoding_failed_call_fails_the_run_stage() -> None:
    """A model call that keeps failing fails the run; the scorer then gives it the lowest score."""
    with FakeGateway("x", statuses=[500] * 20) as gateway:
        reply = _geocode_workspace(gateway.url).handle({"candidate": "p", "case": _geocode("Tel Aviv", "0,0")})
    assert reply["stage"] == "run"
    assert "score" not in reply
    assert reply["observation"]["failed_calls"] >= 1
