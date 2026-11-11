"""Verify selectable models require reasoning effort controls."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ...billing.cli_dispatch import THINKING_LABEL_ROLE, CliDispatcher
from ...billing.operation_pricing import UnpricedOperationError
from ...local.cli_catalog import CLI_MODELS
from .. import interview_model, model_catalog, model_router
from ..errors import DomainError
from ..model_catalog import CatalogModel, ModelCatalogResponse


def _model(name: str = "openrouter/acme/thinker", **kwargs: object) -> CatalogModel:
    """Build a selectable model with optional metadata overrides.

    Args:
        name: The model's catalog id.
        kwargs: Fields to override.

    Returns:
        A catalog model.
    """
    return CatalogModel(
        **{
            "value": name,
            "label": name,
            "provider": "openrouter",
            "available": True,
            "supports_thinking": True,
            "reasoning_efforts": ["low", "high"],
            **kwargs,
        }
    )


@pytest.mark.parametrize(
    ("thinking", "levels", "expected"),
    [
        (False, None, False),
        (False, ["high"], False),
        (True, None, True),
        (True, [], False),
        (True, ["none"], False),
        (True, ["unknown"], False),
        (True, ["none", "low"], True),
    ],
)
def test_effort_eligibility(thinking: bool, levels: list[str] | None, expected: bool) -> None:
    """Keep only supported thinking with a usable ladder or the existing unknown-ladder fallback.

    Args:
        thinking: Whether the model advertises reasoning.
        levels: Declared effort levels.
        expected: Expected eligibility.
    """
    assert (
        model_catalog.supports_effort_control(_model(supports_thinking=thinking, reasoning_efforts=levels)) is expected
    )


def test_filtered_defaults_and_raw_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace an excluded default while preserving its raw metadata for enrichment.

    Args:
        monkeypatch: Replace the provider cache.
    """
    removed = _model("openrouter/acme/fixed", reasoning_efforts=[], is_default=True, is_strongest=True)
    kept = _model()
    raw = ModelCatalogResponse(providers=[], models=[removed, kept])
    monkeypatch.setattr(model_catalog, "_cached_response", raw)
    shown = model_catalog.catalog_snapshot()
    assert shown is not None
    assert len(shown.models) == 1
    assert shown.models[0].is_default
    assert shown.models[0].is_strongest
    assert model_catalog.agent_model_id() == kept.value
    assert model_catalog.strongest_model_id() == kept.value
    assert raw.models == [removed, kept]
    assert not kept.is_default
    twin = _model("acme/fixed", reasoning_efforts=None)
    assert model_catalog.with_platform_reasoning([twin])[0].reasoning_efforts == []


def test_stale_managed_pick_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject a saved selection even if an unfiltered catalog still contains it.

    Args:
        monkeypatch: Supply provider metadata without a usable effort ladder.
    """
    old = _model(reasoning_efforts=[])
    monkeypatch.setattr(model_catalog, "get_catalog_cached", lambda: ModelCatalogResponse(providers=[], models=[old]))
    monkeypatch.setattr(model_catalog, "_hidden_model_values", frozenset())
    monkeypatch.setattr(model_router.settings, "local_mode", False)
    with pytest.raises(DomainError, match=r"models\.unknown_model"):
        model_router.route_menu_model(old.value)


@pytest.mark.parametrize("slug", ["anthropic/claude-haiku-4.5", "anthropic/claude-sonnet-4.5"])
def test_removed_cli_model_cannot_run_user_turn(slug: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject removed CLI selections at both routing and dispatch boundaries.

    Args:
        slug: Removed Claude model.
        monkeypatch: Enable local routing without starting a process.
    """
    monkeypatch.setattr(model_router.settings, "local_mode", True)
    assert slug not in CLI_MODELS["claude"]
    with pytest.raises(DomainError):
        model_router.route_menu_model(f"subscription/claude/{slug}")
    with pytest.raises(UnpricedOperationError):
        CliDispatcher(None, "claude", slug, "agent", SimpleNamespace(local_mode=True))


def test_internal_label_keeps_fixed_haiku() -> None:
    """Keep the fixed label model without exposing it as a user selection."""
    dispatcher = CliDispatcher(
        None, "claude", "anthropic/claude-haiku-4.5", THINKING_LABEL_ROLE, SimpleNamespace(local_mode=True)
    )
    assert dispatcher.model == "anthropic/claude-haiku-4.5"


@pytest.mark.parametrize("levels", [[], ["none"], ["low", "high"]])
def test_live_byok_metadata_checks_stale_and_new_picks(levels: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """Validate a model discovered after the bundled registry without running a model.

    Args:
        levels: Live provider effort levels.
        monkeypatch: Replace vault access and the metadata request.
    """
    name = "openrouter/acme/new-thinker"
    monkeypatch.setattr(model_router.settings, "local_mode", True)
    vault = SimpleNamespace(has_verified_connection=lambda *args: True)
    monkeypatch.setattr(model_router, "ProviderKeyVault", lambda **kwargs: vault)
    monkeypatch.setattr(model_router, "resolve_byok_model_config", lambda config, **kwargs: config)
    monkeypatch.setattr(model_router, "get_byok_catalog_cached", lambda: ModelCatalogResponse(providers=[], models=[]))
    probe = Mock(
        return_value={
            "acme/new-thinker": {
                "type": "chat",
                "supported_parameters": ["reasoning"],
                "reasoning": {"supported_efforts": levels},
            }
        }
    )
    monkeypatch.setattr(model_router, "probe_byok_provider_models", probe)
    if levels == ["low", "high"]:
        assert (
            model_router.route_local_byok_model(name, job_store=SimpleNamespace(engine=object()), username="test").name
            == name
        )
    else:
        with pytest.raises(DomainError):
            model_router.route_local_byok_model(name, job_store=SimpleNamespace(engine=object()), username="test")
    probe.assert_called_once()


def test_interview_drops_stale_no_effort_pick(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exclude an old measured pick after its effort metadata removes support.

    Args:
        monkeypatch: Replace the cached pick and prevent background requests.
    """
    old = _model(reasoning_efforts=[], featured=True, input_cost_per_token=1, output_cost_per_token=1)
    catalog = ModelCatalogResponse(providers=[], models=[old])
    monkeypatch.setattr(interview_model, "catalog_snapshot", lambda: catalog)
    monkeypatch.setattr(interview_model, "_pick", old.value)
    monkeypatch.setattr(interview_model, "_kick_refresh", lambda: None)
    monkeypatch.setattr(interview_model.settings, "interview_agent_model", "")
    monkeypatch.setattr(interview_model, "agent_model_id", lambda configured: "fallback")
    assert interview_model.small_candidates(catalog) == []
    assert interview_model.interview_model_id() == "fallback"


def test_replacement_default_keeps_score_per_input_dollar_policy() -> None:
    """Choose the eligible replacement by the existing input-price ranking."""
    removed = _model("openrouter/acme/fixed", reasoning_efforts=[], is_default=True)
    best_input_value = _model(
        "openrouter/acme/value",
        featured=True,
        intelligence_index=10,
        input_cost_per_token=1,
        output_cost_per_token=100,
    )
    expensive_input = _model(
        "openrouter/acme/expensive",
        featured=True,
        intelligence_index=10,
        input_cost_per_token=2,
        output_cost_per_token=0.01,
    )
    result = model_catalog.with_effort_controls(
        ModelCatalogResponse(providers=[], models=[removed, best_input_value, expensive_input])
    )
    assert next(model.value for model in result.models if model.is_default) == best_input_value.value
