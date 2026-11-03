"""Tests for API-side subscription models: routing, zero billed cost, model time and the router."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import dspy
import pytest

from core import config
from core.api import model_catalog, model_router
from core.api.errors import DomainError
from core.api.model_catalog import CatalogModel, ModelCatalogResponse
from core.billing import cli_dispatch
from core.billing.metering import estimate_run_cents
from core.billing.tests.test_cli_runners import CLAUDE_SUCCESS, calls, fake_cli, local_settings, printing
from core.exceptions import ServiceError
from core.models import ModelConfig
from core.service_gateway import language_models
from core.service_gateway.agents import code, generalist
from core.service_gateway.language_models import (
    LmUsageTotals,
    MeteredLM,
    apply_model_reasoning_config,
    build_language_model,
)
from core.service_gateway.optimization.blackbox.workspace_harness import attempt_record
from core.service_gateway.optimization.unified.dspy_engine import lm_helper

MODEL = "anthropic/claude-haiku-4.5"
SUBSCRIPTION_ID = f"subscription/claude/{MODEL}"


@pytest.fixture
def local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn local mode on everywhere with a fake claude and a fresh loopback gateway."""
    fake_cli(tmp_path, "claude", printing(CLAUDE_SUCCESS))
    current = local_settings(tmp_path)
    monkeypatch.setattr(config, "settings", current)
    monkeypatch.setattr(language_models, "settings", current)
    monkeypatch.setattr(model_router, "settings", current)
    monkeypatch.setattr(cli_dispatch, "_gateway", None)
    monkeypatch.setattr(cli_dispatch, "_list_prices", lambda model: None)


@pytest.mark.usefixtures("local")
def test_subscription_config_uses_the_local_route() -> None:
    """Point a subscription model at the loopback CLI route with its own token."""
    lm = build_language_model(ModelConfig(name=MODEL, token_source="subscription", cli_provider="claude"))
    assert lm.model == f"litellm_proxy/{MODEL}"
    assert str(lm.kwargs["base_url"]).startswith("http://127.0.0.1:")
    route = cli_dispatch.local_subscription_route(MODEL, "claude", "agent")
    assert lm.kwargs["api_key"] == route["token"]


@pytest.mark.usefixtures("local")
def test_subscription_turn_effort_reaches_the_cli(tmp_path: Path) -> None:
    """Run the CLI at the turn's chosen effort, and send none when the turn has none."""
    # Claude Haiku 4.5 takes no effort, so this turn runs on a model that takes xhigh.
    picked = ModelConfig(
        name="anthropic/claude-opus-5.5",
        token_source="subscription",
        cli_provider="claude",
        extra={"reasoning_effort": "xhigh"},
    )
    lm = build_language_model(picked, disable_cache=True)
    assert lm.kwargs["extra_body"][cli_dispatch.CLI_EFFORT_KEY] == "xhigh"
    assert lm("ping") == ["pong"]
    [call] = calls(tmp_path, "claude")
    assert call["argv"][call["argv"].index("--effort") + 1] == "xhigh"
    unpicked = build_language_model(ModelConfig(name=MODEL, token_source="subscription", cli_provider="claude"))
    assert cli_dispatch.CLI_EFFORT_KEY not in (unpicked.kwargs.get("extra_body") or {})


@pytest.mark.usefixtures("local")
def test_subscription_metric_effort_override_reaches_the_cli(tmp_path: Path) -> None:
    """Send the per-call effort to the CLI without changing the next call's default."""
    model = "subscription/claude/anthropic/claude-opus-5.5"
    lm = build_language_model(
        ModelConfig(name=model, token_source="subscription", cli_provider="claude", extra={"reasoning_effort": "low"}),
        disable_cache=True,
    )
    helper = lm_helper(lm)
    assert helper("judge", model=model, reasoning_effort="high") == "pong"
    assert helper("judge") == "pong"
    first, second = calls(tmp_path, "claude")
    assert first["argv"][first["argv"].index("--effort") + 1] == "high"
    assert second["argv"][second["argv"].index("--effort") + 1] == "low"


@pytest.mark.usefixtures("local")
def test_cli_route_gets_only_the_picked_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    """Send the CLI no default effort, in chat and in the code agent, so an unpicked turn runs the CLI default."""
    slug = "openai/gpt-5.6-luna"
    monkeypatch.setattr(
        language_models,
        "local_subscription_route",
        lambda model, cli, role: {"model": model, "url": "http://127.0.0.1:1/v1", "token": "t"},
    )
    row = CatalogModel(
        value=slug,
        label=slug,
        provider="openai",
        available=True,
        reasoning_efforts=["low", "medium", "high"],
        default_reasoning_effort="high",
        reasoning_default_enabled=True,
    )
    monkeypatch.setattr(model_catalog, "_cached_response", ModelCatalogResponse(providers=[], models=[row]))

    def sent(extra: dict[str, object]) -> object:
        """Return the effort a built Codex CLI route sends for ``extra``."""
        config = ModelConfig(name=slug, token_source="subscription", cli_provider="codex", extra=extra)
        lm = build_language_model(apply_model_reasoning_config(config))
        return (lm.kwargs.get("extra_body") or {}).get(cli_dispatch.CLI_EFFORT_KEY)

    assert sent({}) is None
    assert sent({"reasoning_effort": "max"}) == "max"
    for effort in (None, "max"):
        lm = code._build_agent_lm(f"subscription/codex/{slug}", effort)
        assert (lm.kwargs.get("extra_body") or {}).get(cli_dispatch.CLI_EFFORT_KEY) == effort


@pytest.mark.usefixtures("local")
def test_subscription_model_answers_through_dspy() -> None:
    """Answer a real DSPy call from the fake CLI through the loopback route."""
    lm = build_language_model(
        ModelConfig(name=SUBSCRIPTION_ID, token_source="subscription", cli_provider="claude"), disable_cache=True
    )
    assert lm("ping") == ["pong"]
    assert lm.usage_totals.calls == 1
    assert lm.usage_totals.reported_cost_usd == 0.0
    assert lm.usage_totals.model_ms == 1326


# Haiku 4.5's public price per token, so the gateway reports a non-zero list price.
LIST_PRICES = (Decimal("0.000001"), Decimal("0.000005"))


def _priced_subscription_lm(monkeypatch: pytest.MonkeyPatch) -> MeteredLM:
    """Build a subscription LM whose calls carry the model's list price in ``usage.cost``.

    Args:
        monkeypatch: Gives the gateway a list price without a network read.

    Returns:
        The subscription LM, routed through the loopback CLI gateway.
    """
    monkeypatch.setattr(cli_dispatch, "_list_prices", lambda model: LIST_PRICES)
    return build_language_model(model_router.route_agent_model(SUBSCRIPTION_ID), disable_cache=True)


@pytest.mark.usefixtures("local")
def test_non_streamed_subscription_turn_bills_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Price a plain subscription call at 0 cents though its usage carries a list price."""
    lm = _priced_subscription_lm(monkeypatch)
    assert lm("ping") == ["pong"]
    assert lm.usage_totals.input_tokens > 0
    assert lm.usage_totals.reported_cost_usd == 0.0
    assert estimate_run_cents([lm]) == 0


@pytest.mark.usefixtures("local")
def test_streamed_subscription_turn_bills_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Price a streamed subscription call at 0 cents.

    LiteLLM rebuilds a streamed call's usage from the chunks: it keeps
    ``cost`` (the list price) and drops the gateway's ``token_source`` mark.
    Agent, interview, tagger and serve turns all stream.
    """
    answer = "[[ ## answer ## ]]\npong\n\n[[ ## completed ## ]]"
    fake_cli(tmp_path, "claude", printing({**CLAUDE_SUCCESS, "result": answer}))
    lm = _priced_subscription_lm(monkeypatch)
    seen: list[object] = []
    update_history = MeteredLM.update_history

    def spy(self: MeteredLM, entry: object) -> None:
        """Record each call's usage block, then fold it as usual."""
        seen.append(entry.get("usage") if isinstance(entry, dict) else None)
        update_history(self, entry)

    monkeypatch.setattr(MeteredLM, "update_history", spy)

    class Answer(dspy.Signature):
        """Answer the question."""

        question: str = dspy.InputField()
        answer: str = dspy.OutputField()

    program = dspy.streamify(
        dspy.Predict(Answer),
        stream_listeners=[dspy.streaming.StreamListener(signature_field_name="answer")],
        async_streaming=True,
    )

    async def run() -> list[object]:
        """Drain the stream.

        Returns:
            Every streamed item.
        """
        with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
            return [item async for item in program(question="ping")]

    items = asyncio.run(run())
    assert isinstance(items[-1], dspy.Prediction)
    assert items[-1].answer == "pong"
    assert len(seen) == 1
    usage = seen[0]
    assert isinstance(usage, dict)
    assert usage.get("cost")
    assert usage.get("token_source") is None
    assert lm.usage_totals.input_tokens > 0
    assert lm.usage_totals.reported_cost_usd == 0.0
    assert estimate_run_cents([lm]) == 0


def test_subscription_lm_bills_nothing_without_a_usage_mark() -> None:
    """Fold a subscription LM's call at zero cost; a managed LM keeps paying the reported cost."""
    entry = {"model": MODEL, "usage": {"prompt_tokens": 2000, "completion_tokens": 50, "cost": 0.25}}
    subscription = MeteredLM(model=f"litellm_proxy/{MODEL}")
    subscription.subscription = True
    managed = MeteredLM(model=f"litellm_proxy/{MODEL}")
    subscription.update_history(entry)
    managed.update_history(entry)
    assert (subscription.usage_totals.reported_cost_usd, subscription.usage_totals.reported_input_tokens) == (0.0, 2000)
    assert estimate_run_cents([subscription]) == 0
    assert managed.usage_totals.reported_cost_usd == 0.25
    assert estimate_run_cents([managed]) > 0


def test_only_a_subscription_config_marks_the_lm() -> None:
    """Mark only subscription configs; managed and BYOK LMs keep billing."""
    assert build_language_model(ModelConfig(name="openrouter/openai/gpt-4o-mini")).subscription is False
    byok = ModelConfig(name="openai/gpt-4o-mini", token_source="byok", extra={"api_key": "sk-test"})
    assert build_language_model(byok).subscription is False


def test_subscription_config_outside_local_mode_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse a subscription model when local mode is off."""
    monkeypatch.setattr(config.settings, "local_mode", False)
    with pytest.raises(ServiceError):
        build_language_model(ModelConfig(name=MODEL, token_source="subscription", cli_provider="claude"))


def test_subscription_usage_bills_nothing() -> None:
    """Report no billed cost for a subscription call, though its list price is kept."""
    totals = LmUsageTotals()
    totals.add_split({"cost": 0.25, "token_source": "subscription", "model_ms": 40}, (10, 2))
    totals.add_split({"cost": 0.5}, (10, 2))
    assert totals.reported_cost_usd == 0.5
    assert totals.model_ms == 40


def test_attempt_record_prefers_model_time() -> None:
    """Use the usage block's ``model_ms`` as the attempt time when it is present."""
    usage = {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4, "cost": 0.001, "model_ms": 250}
    body = json.dumps({"usage": usage}).encode()
    assert attempt_record("task", 200, 9.0, body)["ms"] == 250.0
    without = json.dumps({"usage": {k: v for k, v in usage.items() if k != "model_ms"}}).encode()
    assert attempt_record("task", 200, 9.0, without)["ms"] == 9000.0


@pytest.mark.usefixtures("local")
def test_router_accepts_subscription_ids_in_local_mode() -> None:
    """Keep a subscription id in local mode and build its subscription config."""
    assert model_router.route_menu_model(SUBSCRIPTION_ID) == SUBSCRIPTION_ID
    agent = model_router.route_agent_model(SUBSCRIPTION_ID)
    assert (agent.name, agent.token_source, agent.cli_provider) == (MODEL, "subscription", "claude")
    with pytest.raises(DomainError) as caught:
        model_router.route_menu_model("subscription/codex/anthropic/claude-haiku-4.5")
    assert (caught.value.status_code, caught.value.code) == (422, "billing.subscription_unmapped_model")


TERRA_SUBSCRIPTION_ID = "subscription/codex/openai/gpt-5.6-terra"


@pytest.mark.usefixtures("local")
@pytest.mark.parametrize(
    "route", [model_router.route_menu_model, model_router.route_interview_model], ids=["menu", "interview"]
)
def test_local_mode_keeps_a_subscription_pick_that_matches_the_removed_pattern(route: object) -> None:
    """Keep a subscription pick whose slug also matches the removed-model pattern.

    Args:
        route: The composer router under test.
    """
    assert route(TERRA_SUBSCRIPTION_ID) == TERRA_SUBSCRIPTION_ID


def test_hosted_interview_still_replaces_a_removed_pick(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the interview default for a removed pick in hosted mode, as before.

    Args:
        monkeypatch: Keeps local mode off and gives an interview default.
    """
    monkeypatch.setattr(model_router.settings, "local_mode", False)
    monkeypatch.setattr(model_router, "interview_model_id", lambda: MANAGED_DEFAULT)
    assert model_router.route_interview_model(TERRA_SUBSCRIPTION_ID) == MANAGED_DEFAULT


MANAGED_DEFAULT = "openrouter/z-ai/glm-5.3-flash"


@pytest.mark.usefixtures("local")
@pytest.mark.parametrize("model", [None, "", "auto:intelligent"], ids=["none", "empty", "retired_sentinel"])
@pytest.mark.parametrize(
    "route",
    [model_router.route_menu_model, model_router.route_interview_model, model_router.route_agent_model],
    ids=["menu", "interview", "agent"],
)
def test_local_mode_never_falls_back_to_a_managed_model(
    monkeypatch: pytest.MonkeyPatch, route: object, model: str | None
) -> None:
    """Refuse a turn with no subscription pick instead of sending it to managed OpenRouter.

    Args:
        monkeypatch: Gives the catalog and interview a managed default.
        route: The composer router under test.
        model: A request that picks no model.
    """
    monkeypatch.setattr(model_router, "default_model_id", lambda: MANAGED_DEFAULT)
    monkeypatch.setattr(model_router, "interview_model_id", lambda: MANAGED_DEFAULT)
    with pytest.raises(DomainError) as caught:
        route(model)
    assert (caught.value.status_code, caught.value.code) == (422, "models.managed_unavailable_local")


@pytest.mark.usefixtures("local")
@pytest.mark.parametrize("model", ["openai/gpt-4o-mini", MANAGED_DEFAULT], ids=["catalog_id", "openrouter_id"])
@pytest.mark.parametrize(
    "route",
    [model_router.route_menu_model, model_router.route_interview_model, model_router.route_agent_model],
    ids=["menu", "interview", "agent"],
)
def test_local_mode_refuses_an_explicit_managed_model(
    monkeypatch: pytest.MonkeyPatch, route: object, model: str
) -> None:
    """Refuse a picked managed model in local mode, which has no managed key.

    Args:
        monkeypatch: Makes every id a known catalog model.
        route: The composer router under test.
        model: A managed catalog id the user picked.
    """
    monkeypatch.setattr(model_router, "require_known_model", lambda name: None)
    with pytest.raises(DomainError) as caught:
        route(model)
    assert (caught.value.status_code, caught.value.code) == (422, "models.managed_unavailable_local")


@pytest.mark.parametrize(
    "route", [model_router.route_menu_model, model_router.route_interview_model], ids=["menu", "interview"]
)
def test_hosted_mode_keeps_an_explicit_managed_model(monkeypatch: pytest.MonkeyPatch, route: object) -> None:
    """Keep a picked managed model for a hosted turn.

    Args:
        monkeypatch: Keeps local mode off and makes every id a known catalog model.
        route: The composer router under test.
    """
    monkeypatch.setattr(model_router.settings, "local_mode", False)
    monkeypatch.setattr(model_router, "require_known_model", lambda name: None)
    assert route("openai/gpt-4o-mini") == "openai/gpt-4o-mini"


@pytest.mark.parametrize(
    "route", [model_router.route_menu_model, model_router.route_interview_model], ids=["menu", "interview"]
)
def test_hosted_mode_still_falls_back_to_the_managed_default(monkeypatch: pytest.MonkeyPatch, route: object) -> None:
    """Keep the managed default for a hosted turn with no pick.

    Args:
        monkeypatch: Keeps local mode off and gives a managed default.
        route: The composer router under test.
    """
    monkeypatch.setattr(model_router.settings, "local_mode", False)
    monkeypatch.setattr(model_router, "default_model_id", lambda: MANAGED_DEFAULT)
    monkeypatch.setattr(model_router, "interview_model_id", lambda: MANAGED_DEFAULT)
    assert route(None) == MANAGED_DEFAULT


async def test_local_mode_labels_no_thinking_on_a_managed_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the managed thinking-summary side call in local mode.

    Args:
        monkeypatch: Turns local mode on and fails any summary model build.
    """

    def managed_lm() -> None:
        """Fail the test if a managed summary model is built."""
        raise AssertionError("local mode built the managed summary model")

    monkeypatch.setattr(generalist.settings, "local_mode", True)
    monkeypatch.setattr(generalist, "_build_summary_lm", managed_lm)
    sink: list = []
    summaries = generalist._thinking_summaries("English", asyncio.Queue(), sink)

    assert await summaries._summarize("Reading the dataset columns.") is None
    assert sink == []


def test_router_ignores_subscription_ids_outside_local_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never return a subscription config when local mode is off."""
    monkeypatch.setattr(model_router.settings, "local_mode", False)
    try:
        agent = model_router.route_agent_model(SUBSCRIPTION_ID)
    except DomainError:
        return
    assert agent.token_source != "subscription"
