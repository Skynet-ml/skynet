"""Tests for the next-message suggestion model pick."""

from __future__ import annotations

import logging

import pytest

from .. import suggest_model
from ..model_catalog import CatalogModel, ModelCatalogResponse
from ..suggest_model import (
    byok_suggest_model,
    cli_suggest_model,
    hosted_suggest_model,
    score_models,
)


def _model(
    value: str,
    *,
    price_in: float | None = 1e-6,
    price_out: float | None = 4e-6,
    intelligence: float | None = 30.0,
    available: bool = True,
    mandatory: bool = False,
) -> CatalogModel:
    """Build a catalog entry for the pick.

    Args:
        value: Catalog id.
        price_in: Input USD per token.
        price_out: Output USD per token.
        intelligence: Artificial Analysis intelligence index.
        available: Whether the platform can route it.
        mandatory: Whether the model must think.

    Returns:
        The entry.
    """
    return CatalogModel(
        value=value,
        label=value,
        provider="acme",
        input_cost_per_token=price_in,
        output_cost_per_token=price_out,
        intelligence_index=intelligence,
        available=available,
        reasoning_mandatory=mandatory,
    )


FAST_CHEAP_WEAK = _model("openrouter/acme/tiny", price_in=5e-8, price_out=2e-7, intelligence=5.0)
BALANCED = _model("openrouter/acme/small", price_in=1e-7, price_out=5e-7, intelligence=35.0)
SLOW_DEAR_SMART = _model("openrouter/acme/large", price_in=5e-6, price_out=2.5e-5, intelligence=60.0)
SPEEDS = {
    FAST_CHEAP_WEAK.value: (300.0, 200.0),
    BALANCED.value: (150.0, 400.0),
    SLOW_DEAR_SMART.value: (40.0, 1500.0),
}


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start each test with no cached pick.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(suggest_model, "_picks", {})


def test_weights_sum_to_one() -> None:
    """The weights split the score, so a total stays between 0 and 1."""
    weights = suggest_model.WEIGHT_SPEED + suggest_model.WEIGHT_COST + suggest_model.WEIGHT_INTELLIGENCE
    assert weights == pytest.approx(1)


def test_balanced_model_beats_the_extremes() -> None:
    """A fast, cheap and capable model beats the fastest weak one and the smartest slow one."""
    scores = score_models([FAST_CHEAP_WEAK, BALANCED, SLOW_DEAR_SMART], SPEEDS)
    assert [s.value for s in scores] == [BALANCED.value, FAST_CHEAP_WEAK.value, SLOW_DEAR_SMART.value]
    best = scores[0]
    assert 0.0 <= best.total <= 1.0
    assert best.reply_ms == pytest.approx(400.0 + 60 / 150.0 * 1000)
    assert best.call_cost_usd == pytest.approx(1e-7 * 1500 + 5e-7 * 60)


def test_intelligence_weight_decides_between_equal_speed_and_cost() -> None:
    """With the same speed and price, the higher intelligence index wins."""
    dull = _model("openrouter/acme/dull", intelligence=10.0)
    sharp = _model("openrouter/acme/sharp", intelligence=40.0)
    speeds = {dull.value: (100.0, 500.0), sharp.value: (100.0, 500.0)}
    assert [s.value for s in score_models([dull, sharp], speeds)] == [sharp.value, dull.value]


def test_only_routable_benchmarked_models_qualify() -> None:
    """Unavailable, non-OpenRouter, unpriced, unbenchmarked and must-think models never win."""
    excluded = [
        _model("openrouter/acme/offline", available=False),
        _model("anthropic/claude-haiku-5.5"),
        _model("openrouter/acme/unpriced", price_in=None),
        _model("openrouter/acme/no-aa", intelligence=None),
        _model("openrouter/acme/thinker", mandatory=True),
    ]
    speeds = {m.value: (500.0, 100.0) for m in excluded} | SPEEDS
    assert {s.value for s in score_models([*excluded, BALANCED], speeds)} == {BALANCED.value}


def test_unmeasured_models_drop_out_when_others_were_measured() -> None:
    """A model with no measured speed cannot win on a speed nobody measured."""
    unmeasured = _model("openrouter/acme/unmeasured", price_in=1e-9, price_out=1e-9, intelligence=90.0)
    assert unmeasured.value not in {s.value for s in score_models([unmeasured, BALANCED], SPEEDS)}


def test_no_speeds_rescales_cost_and_intelligence() -> None:
    """Before the first speed measurement, the pick scores cost and intelligence only."""
    scores = score_models([FAST_CHEAP_WEAK, BALANCED, SLOW_DEAR_SMART], {})
    assert all(s.speed is None and s.reply_ms is None for s in scores)
    weight = suggest_model.WEIGHT_COST + suggest_model.WEIGHT_INTELLIGENCE
    best = scores[0]
    assert best.total == pytest.approx(
        (suggest_model.WEIGHT_COST * best.cost + suggest_model.WEIGHT_INTELLIGENCE * best.intelligence) / weight
    )


def test_hosted_pick_reads_the_catalog_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hosted pick is the top score, and a second call reuses it without scoring again."""
    catalog = ModelCatalogResponse(providers=[], models=[FAST_CHEAP_WEAK, BALANCED, SLOW_DEAR_SMART])
    reads: list[int] = []

    def snapshot() -> ModelCatalogResponse:
        """Count catalog reads.

        Returns:
            The fake catalog.
        """
        reads.append(1)
        return catalog

    monkeypatch.setattr(suggest_model, "catalog_snapshot", snapshot)
    monkeypatch.setattr(suggest_model, "measured_speeds", lambda: SPEEDS)
    assert hosted_suggest_model() == BALANCED.value
    assert hosted_suggest_model() == BALANCED.value
    assert reads == [1]


def test_hosted_pick_without_artificial_analysis_data_is_none(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """With no benchmarked model the pick is ``None`` and a warning says why."""
    catalog = ModelCatalogResponse(providers=[], models=[_model("openrouter/acme/no-aa", intelligence=None)])
    monkeypatch.setattr(suggest_model, "catalog_snapshot", lambda: catalog)
    monkeypatch.setattr(suggest_model, "measured_speeds", dict)
    with caplog.at_level(logging.WARNING, logger=suggest_model.__name__):
        assert hosted_suggest_model() is None
    assert "Artificial Analysis" in caplog.text


def test_hosted_pick_before_the_catalog_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Before the catalog prewarm lands there is no pick, and the call does not wait."""
    monkeypatch.setattr(suggest_model, "catalog_snapshot", lambda: None)
    assert hosted_suggest_model() is None


def _use_catalog(monkeypatch: pytest.MonkeyPatch, models: list[CatalogModel], speeds: dict) -> None:
    """Serve ``models`` and ``speeds`` to the pick.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        models: Catalog entries.
        speeds: Measured speeds by id.
    """
    catalog = ModelCatalogResponse(providers=[], models=models)
    monkeypatch.setattr(suggest_model, "catalog_snapshot", lambda: catalog)
    monkeypatch.setattr(suggest_model, "measured_speeds", lambda: speeds)


def test_byok_pick_scores_models_the_platform_cannot_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """The user's key reaches every OpenRouter model, so a model with no platform key still scores."""
    keyless = _model("openrouter/acme/small", price_in=1e-7, price_out=5e-7, intelligence=35.0, available=False)
    _use_catalog(monkeypatch, [FAST_CHEAP_WEAK, keyless, SLOW_DEAR_SMART], SPEEDS)
    assert byok_suggest_model() == keyless.value
    assert hosted_suggest_model() == FAST_CHEAP_WEAK.value


def test_cli_pick_scores_only_that_clis_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each CLI picks among its own models, by the same score, and gets its slug back."""
    monkeypatch.setattr(
        suggest_model,
        "CLI_MODELS",
        {"claude": {"acme/large": "large", "acme/small": "small"}, "codex": {"acme/tiny": "tiny"}},
    )
    _use_catalog(monkeypatch, [FAST_CHEAP_WEAK, BALANCED, SLOW_DEAR_SMART], SPEEDS)
    assert cli_suggest_model("claude") == "acme/small"
    assert cli_suggest_model("codex") == "acme/tiny"


def test_each_route_has_its_own_cache_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cached pick for one route never answers for another route."""
    monkeypatch.setattr(suggest_model, "CLI_MODELS", {"claude": {"acme/large": "large"}, "codex": {}})
    _use_catalog(monkeypatch, [BALANCED, SLOW_DEAR_SMART], SPEEDS)
    assert hosted_suggest_model() == BALANCED.value
    assert cli_suggest_model("claude") == "acme/large"
    assert set(suggest_model._picks) == {suggest_model.HOSTED_ROUTE, "claude"}
    _use_catalog(monkeypatch, [], {})
    assert hosted_suggest_model() == BALANCED.value
    assert byok_suggest_model() == "openrouter/anthropic/claude-haiku-5.5"
    assert suggest_model._picks[suggest_model.HOSTED_ROUTE][0] == BALANCED.value


def test_cli_without_benchmarks_falls_back_to_its_cheapest_model(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A CLI with no benchmarked model still suggests, on its cheapest priced model."""
    monkeypatch.setattr(
        suggest_model, "CLI_MODELS", {"claude": {"acme/dear": "dear", "acme/cheap": "cheap"}, "codex": {}}
    )
    dear = _model("openrouter/acme/dear", price_in=5e-6, price_out=2e-5, intelligence=None)
    cheap = _model("openrouter/acme/cheap", price_in=1e-7, price_out=5e-7, intelligence=None)
    _use_catalog(monkeypatch, [dear, cheap], {})
    with caplog.at_level(logging.INFO, logger=suggest_model.__name__):
        assert cli_suggest_model("claude") == "acme/cheap"
        assert cli_suggest_model("codex") == "openai/gpt-6-luna"
    assert "Artificial Analysis" in caplog.text


@pytest.mark.parametrize("catalog", [ModelCatalogResponse(providers=[], models=[]), None])
def test_mac_app_catalog_still_gives_each_engine_a_model(
    monkeypatch: pytest.MonkeyPatch, catalog: ModelCatalogResponse | None
) -> None:
    """The Mac app's catalog is empty (no platform key) or not built; a CLI or a key still suggests."""
    monkeypatch.setattr(suggest_model, "catalog_snapshot", lambda: catalog)
    monkeypatch.setattr(suggest_model, "measured_speeds", dict)
    assert cli_suggest_model("claude") == "anthropic/claude-haiku-5.5"
    assert cli_suggest_model("codex") == "openai/gpt-6-luna"
    assert byok_suggest_model() == "openrouter/anthropic/claude-haiku-5.5"
    assert hosted_suggest_model() is None


def test_fixed_cli_fallbacks_are_models_the_cli_runs() -> None:
    """Each CLI's fixed fallback is a model in that CLI's own list."""
    for cli in ("claude", "codex"):
        slug = suggest_model._FIXED_FALLBACKS[cli].removeprefix("openrouter/")
        assert slug in suggest_model.CLI_MODELS[cli]
