"""Tests for the measured interview-model pick."""

from __future__ import annotations

import pytest

from core.api import interview_model
from core.api.model_catalog import CatalogModel, ModelCatalogResponse


def _model(value: str, price: float) -> CatalogModel:
    """Build a featured, available, priced OpenRouter catalog entry.

    Args:
        value: Catalog id.
        price: Per-token price, split evenly between input and output.

    Returns:
        The entry.
    """
    return CatalogModel(
        value=value,
        label=value,
        provider="openrouter",
        available=True,
        featured=True,
        supports_thinking=True,
        input_cost_per_token=price / 2,
        output_cost_per_token=price / 2,
    )


def _endpoint(first_token_ms: float, tokens_per_second: float, uptime: float = 100.0) -> dict:
    """Build one OpenRouter endpoint stats entry.

    Args:
        first_token_ms: Median latency.
        tokens_per_second: Median throughput.
        uptime: Last-day uptime percent.

    Returns:
        The entry.
    """
    return {
        "latency_last_30m": {"p50": first_token_ms},
        "throughput_last_30m": {"p50": tokens_per_second},
        "uptime_last_1d": uptime,
    }


@pytest.fixture
def scores(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    """Stub the catalog's featured benchmark scores.

    Args:
        monkeypatch: Pytest monkeypatch.

    Returns:
        The mutable score map the module reads.
    """
    table: dict[str, float] = {}
    monkeypatch.setattr(interview_model, "featured_score", table.get)
    return table


def test_small_candidates_keep_the_cheaper_half(scores: dict[str, float]) -> None:
    """Only featured models priced at or under the median survive, cheapest first."""
    models = [_model(f"openrouter/m{i}", float(i)) for i in range(1, 5)]
    scores.update({m.value: 40.0 for m in models})
    catalog = ModelCatalogResponse(models=models, providers=[])
    assert [m.value for m in interview_model.small_candidates(catalog)] == ["openrouter/m1", "openrouter/m2"]


def test_small_candidates_skip_unbenchmarked_and_non_openrouter(scores: dict[str, float]) -> None:
    """A model without a score or outside OpenRouter cannot be ranked."""
    models = [_model("openrouter/a", 1.0), _model("openrouter/b", 1.0), _model("openai/c", 1.0)]
    scores.update({"openrouter/a": 40.0, "openai/c": 40.0})
    catalog = ModelCatalogResponse(models=models, providers=[])
    assert [m.value for m in interview_model.small_candidates(catalog)] == ["openrouter/a"]


def test_reply_ms_takes_the_fastest_healthy_endpoint() -> None:
    """Endpoints below the uptime floor or without stats are ignored."""
    endpoints = [
        _endpoint(100, 150, uptime=90.0),
        _endpoint(500, 150),
        _endpoint(300, 75),
        {"latency_last_30m": None},
    ]
    assert interview_model.reply_ms(endpoints) == pytest.approx(1500.0)
    assert interview_model.reply_ms([]) is None


def test_endpoint_speed_reports_the_fastest_healthy_endpoint() -> None:
    """The speed comes from the endpoint that finishes a short reply first, not the highest throughput."""
    endpoints = [_endpoint(5000, 400), _endpoint(500, 150), _endpoint(100, 150, uptime=90.0)]
    assert interview_model.endpoint_speed(endpoints) == (150.0, 500.0)
    assert interview_model.endpoint_speed([]) is None


def test_choose_picks_the_fastest_frontier_model(scores: dict[str, float]) -> None:
    """A dominated model drops out; the fastest survivor wins."""
    fast = _model("openrouter/fast", 1.0)
    slow_strong = _model("openrouter/slow-strong", 1.0)
    dominated = _model("openrouter/dominated", 2.0)
    scores.update({fast.value: 40.0, slow_strong.value: 45.0, dominated.value: 39.0})
    times = {fast.value: 1000.0, slow_strong.value: 3000.0, dominated.value: 900.0}
    assert interview_model.choose([fast, slow_strong, dominated], times) == "openrouter/dominated"
    times[dominated.value] = 1200.0
    assert interview_model.choose([fast, slow_strong, dominated], times) == "openrouter/fast"
    assert interview_model.choose([fast], {}) is None


def test_interview_model_id_prefers_the_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit INTERVIEW_AGENT_MODEL setting wins over the measured pick."""
    monkeypatch.setattr(interview_model.settings, "interview_agent_model", "openrouter/pinned")
    assert interview_model.interview_model_id() == "openrouter/pinned"


def test_interview_model_id_falls_back_without_a_pick(
    monkeypatch: pytest.MonkeyPatch, scores: dict[str, float]
) -> None:
    """Before the first measured pick it returns the cheapest small candidate."""
    monkeypatch.setattr(interview_model.settings, "interview_agent_model", "")
    monkeypatch.setattr(interview_model, "_pick", None)
    monkeypatch.setattr(interview_model, "_kick_refresh", lambda: None)
    models = [_model("openrouter/cheap", 1.0), _model("openrouter/pricey", 5.0)]
    scores.update({m.value: 40.0 for m in models})
    monkeypatch.setattr(interview_model, "catalog_snapshot", lambda: ModelCatalogResponse(models=models, providers=[]))
    assert interview_model.interview_model_id() == "openrouter/cheap"


def test_interview_model_id_runs_the_catalog_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The catalog's best-value default wins over the fast pick, so every composer shows one model."""
    monkeypatch.setattr(interview_model.settings, "interview_agent_model", "")
    monkeypatch.setattr(interview_model, "_pick", "openrouter/fast")
    monkeypatch.setattr(interview_model, "_kick_refresh", lambda: None)
    default = _model("openrouter/best-value", 3.0).model_copy(update={"is_default": True})
    catalog = ModelCatalogResponse(models=[_model("openrouter/fast", 1.0), default], providers=[])
    monkeypatch.setattr(interview_model, "catalog_snapshot", lambda: catalog)
    assert interview_model.interview_model_id() == "openrouter/best-value"


def test_with_interview_default_flags_one_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """The catalog copy marks exactly the current pick."""
    monkeypatch.setattr(interview_model, "interview_model_id", lambda: "openrouter/b")
    catalog = ModelCatalogResponse(models=[_model("openrouter/a", 1.0), _model("openrouter/b", 1.0)], providers=[])
    flagged = interview_model.with_interview_default(catalog)
    assert [m.is_interview_default for m in flagged.models] == [False, True]
    assert not any(m.is_interview_default for m in catalog.models)


def test_with_interview_default_attaches_measured_speeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Measured models carry throughput, first-token time and short-reply time; unmeasured ones carry None."""
    monkeypatch.setattr(interview_model, "interview_model_id", lambda: "openrouter/a")
    monkeypatch.setattr(interview_model, "_speeds", {"openrouter/a": (120.0, 450.0)})
    catalog = ModelCatalogResponse(models=[_model("openrouter/a", 1.0), _model("openrouter/b", 1.0)], providers=[])
    a, b = interview_model.with_interview_default(catalog).models
    assert (a.throughput_tps, a.ttft_ms, a.latency_ms) == (120.0, 450.0, 450.0 + 150 / 120.0 * 1000)
    assert (b.throughput_tps, b.ttft_ms, b.latency_ms) == (None, None, None)


def test_refresh_keeps_the_last_speed_of_a_model_whose_fetch_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A partial refresh updates the measured models and leaves the rest as they were."""
    catalog = ModelCatalogResponse(models=[_model("openrouter/a", 1.0), _model("openrouter/b", 1.0)], providers=[])
    monkeypatch.setattr(interview_model, "catalog_snapshot", lambda: catalog)
    monkeypatch.setattr(interview_model, "_measure", lambda models: {"openrouter/a": (200.0, 300.0)})
    monkeypatch.setattr(interview_model, "_speeds", {"openrouter/a": (100.0, 500.0), "openrouter/b": (80.0, 600.0)})
    for name, value in (("_pick", None), ("_picked_at", 0.0), ("_measured_at", 0.0)):
        monkeypatch.setattr(interview_model, name, value)
    interview_model._refresh()
    assert interview_model._speeds == {"openrouter/a": (200.0, 300.0), "openrouter/b": (80.0, 600.0)}
