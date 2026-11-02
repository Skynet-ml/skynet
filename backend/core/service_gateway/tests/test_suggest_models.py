"""Tests for the models that write next-message suggestions, hosted and in the Mac app."""

from __future__ import annotations

import pytest

from core.api import suggest_model
from core.api.model_catalog import CatalogModel, ModelCatalogResponse
from core.billing import cli_dispatch
from core.constants import TOKEN_SOURCE_BYOK
from core.local.cli_catalog import cli_model_arg
from core.models import ModelConfig
from core.service_gateway.agents import generalist
from core.service_gateway.language_models import SUBSCRIPTION_ROLE_KEY

INTERVIEW_MODEL = "openrouter/google/gemini-3.5-flash"
AGENT_MODEL = "openrouter/anthropic/claude-opus-5.5"


class _LM:
    """A stand-in LM that keeps the attributes the builders set."""

    side_call = False


def _entry(value: str, price_in: float, price_out: float, **fields: object) -> CatalogModel:
    """Build a priced catalog entry.

    Args:
        value: Catalog id.
        price_in: Input USD per token.
        price_out: Output USD per token.
        **fields: Other catalog fields.

    Returns:
        The entry.
    """
    return CatalogModel(
        value=value,
        label=value,
        provider="acme",
        input_cost_per_token=price_in,
        output_cost_per_token=price_out,
        **fields,
    )


CATALOG = ModelCatalogResponse(
    providers=[],
    models=[
        _entry(AGENT_MODEL, 5e-6, 2.5e-5),
        _entry(INTERVIEW_MODEL, 3e-7, 2.5e-6),
        _entry("openrouter/acme/tiny", 7e-8, 2e-7, reasoning_efforts=["none", "low"]),
        _entry("openrouter/acme/thinker", 1e-8, 1e-8, reasoning_mandatory=True),
        _entry("openrouter/acme/hybrid", 1e-7, 5e-7, reasoning_default_enabled=True),
    ],
)


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> list[ModelConfig]:
    """Record each config a builder makes an LM from, against the fake catalog.

    Args:
        monkeypatch: Replaces the LM builder, the catalog and the default models.

    Returns:
        The configs, in build order.
    """
    configs: list[ModelConfig] = []

    def build(model_config: ModelConfig, *, disable_cache: bool = False) -> _LM:
        """Record the config.

        Args:
            model_config: The config.
            disable_cache: Unused.

        Returns:
            A stand-in LM.
        """
        configs.append(model_config)
        return _LM()

    monkeypatch.setattr(generalist, "build_language_model", build)
    monkeypatch.setattr(generalist, "catalog_snapshot", lambda: CATALOG)
    monkeypatch.setattr(generalist, "interview_model_id", lambda: INTERVIEW_MODEL)
    return configs


def test_hosted_suggestion_caps_output_and_turns_reasoning_off(built: list[ModelConfig]) -> None:
    """Build the hosted suggestion on the given model with a one-line cap and no thinking."""
    lm = generalist.build_hosted_suggest_lm("openrouter/acme/hybrid", 12.0)

    config = built[0]
    assert config.name == "openrouter/acme/hybrid"
    assert config.max_tokens == 60
    assert config.extra["extra_body"] == {"reasoning": {"enabled": False}}
    assert "reasoning_effort" not in config.extra
    assert config.extra["timeout"] == 12.0
    assert config.extra["num_retries"] == 0
    assert lm.side_call is True


def test_hosted_suggestion_uses_effort_none_when_offered(built: list[ModelConfig]) -> None:
    """A model with a ``none`` effort gets it, so it answers without thinking."""
    generalist.build_hosted_suggest_lm("openrouter/acme/tiny", 12.0)
    assert built[0].extra["reasoning_effort"] == "none"
    assert built[0].max_tokens == 60


def test_openai_reasoning_model_gets_no_cap_dspy_would_refuse(built: list[ModelConfig]) -> None:
    """dspy refuses an OpenAI reasoning model below 16000 tokens, so its cap is left off."""
    generalist.build_hosted_suggest_lm("openrouter/openai/gpt-5-mini", 12.0)
    assert built[0].max_tokens is None


def test_byok_suggestion_runs_on_the_scored_pick_on_the_key(
    built: list[ModelConfig], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A saved OpenRouter key writes on the scored pick, never the turn or interview model."""
    monkeypatch.setattr(generalist, "byok_suggest_model", lambda: "openrouter/acme/tiny")
    turn = ModelConfig(
        name=AGENT_MODEL,
        token_source=TOKEN_SOURCE_BYOK,
        byok_provider="openrouter",
        extra={"api_key": "sk-or-fake", "reasoning_effort": "high"},
    )
    generalist.build_local_suggest_lm(turn, 12.0)

    config = built[0]
    assert config.name == "openrouter/acme/tiny"
    assert config.name not in {AGENT_MODEL, INTERVIEW_MODEL}
    assert config.token_source == TOKEN_SOURCE_BYOK
    assert config.extra["api_key"] == "sk-or-fake"
    assert config.extra["reasoning_effort"] == "none"
    assert config.max_tokens == 60
    # The user's key must never reach the managed gateway.
    assert config.base_url is None


@pytest.mark.parametrize(
    ("cli", "model"),
    [("claude", "anthropic/claude-haiku-5.5"), ("codex", "openai/gpt-6-luna")],
)
def test_cli_suggestion_runs_on_the_scored_pick_of_the_cli(
    built: list[ModelConfig], monkeypatch: pytest.MonkeyPatch, cli: str, model: str
) -> None:
    """A CLI subscription writes on that CLI's scored pick, in the suggestion pool."""
    monkeypatch.setattr(
        generalist, "cli_suggest_model", {"claude": "anthropic/claude-haiku-5.5", "codex": "openai/gpt-6-luna"}.get
    )
    turn = ModelConfig(name="anthropic/claude-opus-5.5", token_source="subscription", cli_provider=cli)
    generalist.build_local_suggest_lm(turn, 12.0)

    config = built[0]
    assert config.name == model
    assert cli_model_arg(cli, model)
    assert config.cli_provider == cli
    assert config.token_source == "subscription"
    assert config.extra[SUBSCRIPTION_ROLE_KEY] == cli_dispatch.NEXT_PROMPT_ROLE


@pytest.mark.parametrize(
    ("cli", "model"),
    [("claude", "anthropic/claude-haiku-5.5"), ("codex", "openai/gpt-6-luna")],
)
def test_mac_app_empty_catalog_still_builds_a_cli_suggestion_model(
    built: list[ModelConfig], monkeypatch: pytest.MonkeyPatch, cli: str, model: str
) -> None:
    """The Mac app's catalog has no models (no platform key), and a signed-in CLI still suggests."""
    monkeypatch.setattr(suggest_model, "_picks", {})
    monkeypatch.setattr(suggest_model, "catalog_snapshot", lambda: ModelCatalogResponse(providers=[], models=[]))
    monkeypatch.setattr(suggest_model, "measured_speeds", dict)
    turn = ModelConfig(name="anthropic/claude-opus-5.5", token_source="subscription", cli_provider=cli)
    assert generalist.build_local_suggest_lm(turn, 12.0) is not None
    assert built[0].name == model
    assert built[0].cli_provider == cli


def test_no_engine_gives_no_suggestion_model(built: list[ModelConfig]) -> None:
    """A turn with no saved key and no CLI gets no suggestion model, never a managed one."""
    assert generalist.build_local_suggest_lm(None, 12.0) is None
    assert generalist.build_local_suggest_lm(ModelConfig(name=AGENT_MODEL), 12.0) is None
    assert built == []


def test_no_pick_gives_no_suggestion_model(built: list[ModelConfig], monkeypatch: pytest.MonkeyPatch) -> None:
    """When a route has no pick, the Mac app shows no suggestion."""
    monkeypatch.setattr(generalist, "byok_suggest_model", lambda: None)
    monkeypatch.setattr(generalist, "cli_suggest_model", lambda cli: None)
    byok = ModelConfig(name=AGENT_MODEL, token_source=TOKEN_SOURCE_BYOK, extra={"api_key": "sk-or-fake"})
    cli = ModelConfig(name=AGENT_MODEL, token_source="subscription", cli_provider="claude")
    assert generalist.build_local_suggest_lm(byok, 12.0) is None
    assert generalist.build_local_suggest_lm(cli, 12.0) is None
    assert built == []
