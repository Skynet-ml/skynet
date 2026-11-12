"""Tests for thinking summaries in local mode: built on the user's agent engine, never on a managed model."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, get_args

import pytest

from core import config
from core.api import model_router
from core.billing import cli_dispatch
from core.billing.tests.test_cli_runners import CLAUDE_SUCCESS, calls, fake_cli, local_settings, printing
from core.constants import TOKEN_SOURCE_BYOK
from core.local.cli_catalog import CLI_MODELS, CliProvider, cli_model_arg
from core.models import ModelConfig
from core.service_gateway import language_models
from core.service_gateway.agents import generalist
from core.service_gateway.agents.generalist import WizardState
from core.service_gateway.language_models import build_language_model

INTERVIEW_MODEL = "google/gemini-3.5-flash"
REASONING = "The question column holds the input and the answer column holds the expected output for each row."
LABEL = "Matching columns to inputs\nThe question column becomes the input."


class _LabelLM:
    """A summary model stand-in that answers every call with one label."""

    side_call = False

    def __call__(self, messages: list[dict]) -> list[str]:
        """Answer a label call.

        Args:
            messages: The call's chat messages.

        Returns:
            One label.
        """
        return [LABEL]


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> list[ModelConfig]:
    """Record each config the summary path builds an LM from, and fail any managed build.

    Args:
        monkeypatch: Replaces the LM builder and the managed summary builder.

    Returns:
        The configs, in build order.
    """
    configs: list[ModelConfig] = []

    def build(model_config: ModelConfig, *, disable_cache: bool = False) -> _LabelLM:
        """Record the config and return a label model."""
        configs.append(model_config)
        return _LabelLM()

    def managed() -> None:
        """Fail the test if the managed summary model is built."""
        raise AssertionError("local mode built the managed summary model")

    monkeypatch.setattr(generalist.settings, "local_mode", True)
    monkeypatch.setattr(generalist, "build_language_model", build)
    monkeypatch.setattr(generalist, "_build_summary_lm", managed)
    monkeypatch.setattr(generalist, "interview_model_id", lambda: INTERVIEW_MODEL)
    return configs


def _byok_turn() -> ModelConfig:
    """Return a turn config as ``route_local_byok_model`` resolves it, with a high effort.

    Returns:
        A BYOK OpenRouter config that carries a fake key.
    """
    return ModelConfig(
        name="anthropic/claude-opus-5.5",
        token_source=TOKEN_SOURCE_BYOK,
        byok_provider="openrouter",
        extra={"api_key": "sk-or-fake", "reasoning_effort": "high", "extra_body": {"provider": {"sort": "price"}}},
    )


async def test_local_summary_runs_on_the_saved_openrouter_key(built: list[ModelConfig]) -> None:
    """Label thinking with a small model on the turn's saved OpenRouter key, metered with the turn."""
    sink: list = []
    summaries = generalist._thinking_summaries("English", asyncio.Queue(), sink, _byok_turn())

    assert await summaries._summarize(REASONING) == generalist._clean_summary(LABEL)
    assert await summaries._summarize(REASONING) == generalist._clean_summary(LABEL)
    assert len(built) == 1
    summary = built[0]
    assert summary.name == INTERVIEW_MODEL
    assert summary.token_source == TOKEN_SOURCE_BYOK
    assert summary.byok_provider == "openrouter"
    assert summary.extra["api_key"] == "sk-or-fake"
    assert summary.extra.get("reasoning_effort") != "high"
    assert summary.extra["extra_body"]["provider"] == {"sort": "price"}
    assert summary.extra["timeout"] == generalist._SUMMARY_TIMEOUT_SECONDS
    assert summary.extra["num_retries"] == 0
    # The user's key must never reach the managed gateway.
    assert summary.base_url is None
    assert len(sink) == 1
    assert sink[0].side_call is True


async def test_local_summary_drops_the_connection_reasoning_body(
    built: list[ModelConfig], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep a saved connection's ``extra_body`` reasoning out of the label, so only the no-thinking settings apply."""
    monkeypatch.setattr(generalist, "_no_thinking_extra", lambda model: {"reasoning_effort": "none"})
    turn = _byok_turn()
    turn.extra["extra_body"] = {"provider": {"sort": "price"}, "reasoning": {"effort": "high"}}
    summaries = generalist._thinking_summaries("English", asyncio.Queue(), [], turn)

    assert await summaries._summarize(REASONING) == generalist._clean_summary(LABEL)
    summary = built[0]
    assert summary.extra["reasoning_effort"] == "none"
    assert summary.extra["extra_body"] == {"provider": {"sort": "price"}}
    # The turn keeps its own reasoning body.
    assert turn.extra["extra_body"]["reasoning"] == {"effort": "high"}


async def test_local_summary_drops_the_connection_top_level_reasoning(
    built: list[ModelConfig], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep a saved connection's top-level ``reasoning`` and ``thinking`` params out of the label."""
    monkeypatch.setattr(generalist, "_no_thinking_extra", lambda model: {"reasoning_effort": "none"})
    turn = _byok_turn()
    turn.extra["reasoning"] = {"effort": "high"}
    turn.extra["thinking"] = {"type": "enabled", "budget_tokens": 4096}
    summaries = generalist._thinking_summaries("English", asyncio.Queue(), [], turn)

    assert await summaries._summarize(REASONING) == generalist._clean_summary(LABEL)
    summary = built[0]
    assert summary.extra["reasoning_effort"] == "none"
    assert "reasoning" not in summary.extra
    assert "thinking" not in summary.extra
    # The turn keeps its own params.
    assert turn.extra["reasoning"] == {"effort": "high"}
    assert turn.extra["thinking"] == {"type": "enabled", "budget_tokens": 4096}


async def test_local_turn_labels_thinking_on_its_own_saved_key(
    built: list[ModelConfig], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pass the turn's config from ``run_generalist_agent`` to the label, so a local BYOK turn gets labels."""

    async def fake_drive(*, emit: Any, **_kwargs: Any) -> str:
        """Stream one stretch of thinking, then reply.

        Args:
            emit: The turn's emitter.

        Returns:
            The reply.
        """
        emit({"event": "reasoning_patch", "data": {"chunk": REASONING}})
        await asyncio.sleep(0)
        return "ok"

    monkeypatch.setattr(generalist, "_drive_generalist_agent", fake_drive)
    monkeypatch.setattr(generalist, "served_model_from", lambda lm: None)

    events = [
        ev
        async for ev in generalist.run_generalist_agent(
            wizard_state=WizardState(),
            chat_history=[],
            user_message="go",
            model_config=_byok_turn(),
            locale="en",
            usage_sink=[],
        )
    ]

    labels = [e["data"]["text"] for e in events if e["event"] == "thinking_summary"]
    assert labels == [generalist._clean_summary(LABEL)]
    turn, summary = built
    assert turn.name == "anthropic/claude-opus-5.5"
    assert summary.name == INTERVIEW_MODEL
    assert summary.token_source == TOKEN_SOURCE_BYOK
    assert summary.extra["api_key"] == "sk-or-fake"


async def test_local_summary_runs_on_the_selected_cli(built: list[ModelConfig]) -> None:
    """Label thinking on the turn's CLI subscription, with a small model that CLI runs."""
    turn = ModelConfig(name="anthropic/claude-opus-5.5", token_source="subscription", cli_provider="claude")
    sink: list = []
    summaries = generalist._thinking_summaries("English", asyncio.Queue(), sink, turn)

    assert await summaries._summarize(REASONING) == generalist._clean_summary(LABEL)
    assert len(built) == 1
    summary = built[0]
    assert summary.token_source == "subscription"
    assert summary.cli_provider == "claude"
    assert cli_model_arg("claude", summary.name, internal=True)
    assert summary.name != turn.name
    assert len(sink) == 1


@pytest.mark.parametrize("cli", list(CLI_MODELS))
def test_every_cli_has_a_summary_model_it_runs(cli: str) -> None:
    """Name a summary model in each CLI's own catalog, so no CLI engine is left without labels."""
    assert cli_model_arg(cli, generalist._LOCAL_SUMMARY_MODELS[cli], internal=True)  # type: ignore[arg-type,index]


@pytest.mark.parametrize(
    "turn",
    [None, ModelConfig(name="anthropic/claude-opus-5.5")],
    ids=["no-engine", "managed-config"],
)
async def test_local_summary_is_off_with_no_usable_engine(built: list[ModelConfig], turn: ModelConfig | None) -> None:
    """Build no model and give no model label when the turn has no BYOK key or CLI.

    Args:
        built: Records any LM build.
        turn: The turn's config.
    """
    sink: list = []
    summaries = generalist._thinking_summaries("English", asyncio.Queue(), sink, turn)

    assert await summaries._summarize(REASONING) is None
    assert built == []
    assert sink == []


async def test_hosted_summary_stays_on_the_managed_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep hosted labels on the managed summary model, also for a BYOK turn."""
    managed = _LabelLM()
    monkeypatch.setattr(generalist.settings, "local_mode", False)
    monkeypatch.setattr(generalist, "_build_summary_lm", lambda: managed)
    sink: list = []
    summaries = generalist._thinking_summaries("English", asyncio.Queue(), sink, _byok_turn())

    assert await summaries._summarize(REASONING) == generalist._clean_summary(LABEL)
    assert sink == [managed]


async def test_local_cli_summary_answers_through_the_fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Get a label from a fake claude CLI through the loopback gateway, at zero billed cost."""
    fake_cli(tmp_path, "claude", printing({**CLAUDE_SUCCESS, "result": LABEL}))
    current = local_settings(tmp_path)
    for module in (config, language_models, model_router, generalist):
        monkeypatch.setattr(module, "settings", current)
    monkeypatch.setattr(cli_dispatch, "_gateway", None)
    monkeypatch.setattr(cli_dispatch, "_list_prices", lambda model: None)
    turn = ModelConfig(name="anthropic/claude-opus-5.5", token_source="subscription", cli_provider="claude")
    sink: list[Any] = []
    summaries = generalist._thinking_summaries("English", asyncio.Queue(), sink, turn)

    assert await summaries._summarize(REASONING) == generalist._clean_summary(LABEL)
    assert len(sink) == 1
    assert sink[0].subscription is True
    assert sink[0].usage_totals.reported_cost_usd == 0.0
    log = (tmp_path / "claude.log").read_text()
    assert cli_model_arg("claude", generalist._LOCAL_SUMMARY_MODELS["claude"], internal=True) in log


def test_label_models_cover_exactly_the_local_clis() -> None:
    """Name a label model for each CLI local mode offers, and none for a removed CLI like Droid."""
    assert set(generalist._LOCAL_SUMMARY_MODELS) == set(get_args(CliProvider))
    for cli, model in generalist._LOCAL_SUMMARY_MODELS.items():
        assert cli_model_arg(cli, model, internal=True)


def test_slow_cli_label_never_holds_the_turns_cli_slots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a slow CLI label from waiting for or holding the CLI slots the turn's own calls use."""
    label_seconds = 10
    behavior = f"if 'Summarize' in stdin: time.sleep({label_seconds})\nprint(json.dumps({CLAUDE_SUCCESS!r}))"
    fake_cli(tmp_path, "claude", behavior)
    current = local_settings(tmp_path)
    for module in (config, language_models, model_router, generalist):
        monkeypatch.setattr(module, "settings", current)
    monkeypatch.setattr(cli_dispatch, "_gateway", None)
    monkeypatch.setattr(cli_dispatch, "_list_prices", lambda model: None)
    monkeypatch.setattr(cli_dispatch, "_LABEL_CLI_TIMEOUT_SECONDS", 2.0, raising=False)
    monkeypatch.setattr(generalist, "_SUMMARY_TIMEOUT_SECONDS", 1.5)
    monkeypatch.setattr(generalist, "_SUMMARY_HEDGE_SECONDS", 0.3)
    turn = ModelConfig(name="anthropic/claude-opus-5.5", token_source="subscription", cli_provider="claude")
    summaries = generalist._thinking_summaries("English", asyncio.Queue(), None, turn)

    assert asyncio.run(summaries._hedged(0, REASONING)) is None
    turn_slots = cli_dispatch._semaphore("claude", current.cli_max_concurrency)
    assert turn_slots._value == current.cli_max_concurrency

    agent = build_language_model(
        ModelConfig(name="anthropic/claude-haiku-5.5", token_source="subscription", cli_provider="claude"),
        disable_cache=True,
    )
    started = time.monotonic()
    agent("ping")
    # Waiting for the abandoned label would take about 8 s; the margin is for a busy test machine.
    assert time.monotonic() - started < 4.0
    label_slot = cli_dispatch._semaphore(f"claude:{cli_dispatch.THINKING_LABEL_ROLE}", 1)
    # The label CLI is stopped at its 2 s limit, well before its 10 s sleep ends.
    assert label_slot.acquire(timeout=6.0)
    label_slot.release()
    labels = [line for line in (tmp_path / "claude.log").read_text().splitlines() if "Summarize" in line]
    assert len(labels) == 1


def test_reasoning_off_side_call_gets_no_default_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the catalog's default effort off a side call that turned reasoning off, so it does not think."""
    monkeypatch.setattr(language_models, "default_effort_for", lambda model: "low")
    side = build_language_model(
        ModelConfig(
            name="openrouter/anthropic/claude-haiku-4.5", extra={"extra_body": {"reasoning": {"enabled": False}}}
        ),
        disable_cache=True,
    )
    turn = build_language_model(ModelConfig(name="openrouter/anthropic/claude-haiku-4.5"), disable_cache=True)

    assert "reasoning_effort" not in side.kwargs
    assert side.kwargs["extra_body"]["reasoning"] == {"enabled": False}
    assert turn.kwargs["reasoning_effort"] == "low"


def test_cli_suggestion_runs_while_a_label_holds_the_label_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run a next-message suggestion in its own CLI pool, with a longer limit than a label."""
    fake_cli(tmp_path, "claude", printing(CLAUDE_SUCCESS))
    current = local_settings(tmp_path)
    for module in (config, language_models, model_router, generalist):
        monkeypatch.setattr(module, "settings", current)
    monkeypatch.setattr(cli_dispatch, "_gateway", None)
    monkeypatch.setattr(cli_dispatch, "_list_prices", lambda model: None)
    monkeypatch.setattr(generalist, "cli_suggest_model", lambda cli: "anthropic/claude-haiku-5.5")
    turn = ModelConfig(name="anthropic/claude-opus-5.5", token_source="subscription", cli_provider="claude")
    lm = generalist.build_local_suggest_lm(turn, timeout=30.0)
    label_slot = cli_dispatch._semaphore(f"claude:{cli_dispatch.THINKING_LABEL_ROLE}", 1)
    assert label_slot.acquire(blocking=False)
    try:
        answer = lm(messages=[{"role": "user", "content": "ping"}])
    finally:
        label_slot.release()

    assert answer
    assert answer[0]
    assert lm.kwargs["timeout"] == 30.0
    assert cli_dispatch._NEXT_PROMPT_CLI_TIMEOUT_SECONDS > cli_dispatch._LABEL_CLI_TIMEOUT_SECONDS
    build_language_model(
        ModelConfig(name="anthropic/claude-haiku-5.5", token_source="subscription", cli_provider="claude"),
        disable_cache=True,
    )("ping")
    side, turn_call = calls(tmp_path, "claude")
    # Claude Code thinks by default; a side call turns it off, a turn keeps it.
    assert side["env"]["MAX_THINKING_TOKENS"] == "0"
    assert "MAX_THINKING_TOKENS" not in turn_call["env"]
