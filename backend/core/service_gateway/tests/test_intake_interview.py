"""Unit tests for the onboarding intake interview engine (parsing, validation, caps)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from dspy.utils.dummies import DummyLM

from core.service_gateway.agents import intake_interview
from core.service_gateway.agents.intake_interview import (
    _PHASE_BRIEFS,
    AGENDA,
    LLM_PHASES,
    MAX_PHASE_QUESTIONS,
    IntakeInterviewTurnSig,
    answered_phases,
    intake_inputs,
    parse_intake_prediction,
    validate_profile_patch,
)

_SLUGS = ["openai", "anthropic"]


def _parse(pred: SimpleNamespace | None, asked: int = 0) -> dict:
    """Parse a prediction against the test catalog."""
    return parse_intake_prediction(pred, asked, provider_slugs=_SLUGS)


def _pred(**fields: str) -> SimpleNamespace:
    """Build a stand-in prediction with the given output fields."""
    return SimpleNamespace(**fields)


def test_signature_streams_message_then_done_first() -> None:
    """``message`` and ``done`` lead the outputs so the shared driver can stream and hint early."""
    outputs = list(IntakeInterviewTurnSig.output_fields)
    assert outputs[:2] == ["message", "done"]
    assert set(outputs) == {"message", "done", "options_json", "profile_patch_json", "skip_rest"}


def test_parse_question_turn() -> None:
    """A question turn carries its options and a validated patch, and the phase stays open."""
    turn = _parse(
        _pred(
            message="Who pays for the model calls?",
            done="false",
            options_json=json.dumps(
                [
                    {"label": "Platform credits", "description": "Nothing to set up"},
                    {"label": "My own key", "description": "Billed to your provider"},
                ]
            ),
            profile_patch_json=json.dumps(
                {
                    "goal": "route tickets right",
                    "source": "repo",
                    "models": ["gpt-4o-mini"],
                    "budget_usd": "$5",
                    "privacy": "zdr",
                    "level": "guided",
                }
            ),
            skip_rest="false",
        )
    )
    assert turn["message"] == "Who pays for the model calls?"
    assert turn["phase_done"] is False
    assert [o["label"] for o in turn["options"]] == ["Platform credits", "My own key"]
    # The setup is settings only: a volunteered goal, source, model or budget is dropped.
    assert turn["profile_patch"] == {"privacy": "zdr", "level": "guided"}
    assert turn["skip_phases"] == ["privacy"]
    assert turn["skip_rest"] is False
    assert set(turn) == {
        "message",
        "options",
        "phase_done",
        "profile_patch",
        "skip_phases",
        "skip_rest",
        "model",
    }


def test_parse_forces_phase_done_at_question_cap() -> None:
    """At the per-phase question cap the phase finishes even when the model keeps asking."""
    turn = _parse(_pred(message="One more?", done="false", options_json='[{"label": "x"}]'), asked=MAX_PHASE_QUESTIONS)
    assert turn["phase_done"] is True
    assert turn["options"] == []


def test_parse_skip_rest_finishes_the_phase() -> None:
    """A skip request ends the phase and drops the options."""
    turn = _parse(_pred(message="Sure.", done="false", options_json='[{"label": "x"}]', skip_rest="true"))
    assert turn["skip_rest"] is True
    assert turn["phase_done"] is True
    assert turn["options"] == []


def test_parse_unparseable_final_turn_answers_nothing() -> None:
    """A finished phase without a parseable patch sets nothing and skips nothing."""
    turn = _parse(_pred(message="Got it.", done="true", profile_patch_json="not json"), asked=1)
    assert turn["phase_done"] is True
    assert turn["profile_patch"] == {}
    assert turn["skip_phases"] == []


def test_parse_tolerates_missing_prediction() -> None:
    """A stream that produced nothing yields a safe, empty turn."""
    turn = _parse(None)
    assert turn["message"] == ""
    assert turn["profile_patch"] == {}
    assert turn["options"] == []
    assert turn["skip_phases"] == []


def test_validate_patch_coerces_enums_and_drops_invalid_keys() -> None:
    """Enum aliases are canonicalised; unknown values, keys, models, providers and run details are dropped."""
    patch = validate_profile_patch(
        {
            "source": "GitHub",
            "source_url": "https://github.com/acme/bot",
            "models": ["openai/gpt-4o-mini"],
            "billing": "BYOK",
            "byok_provider": "OpenAI",
            "budget_usd": -3,
            "privacy": "deny",
            "trust": "auto-safe",
            "code_assist": "Auto",
            "split_mode": "manual",
            "level": "familiar",
            "goal": "",
            "mystery": "x",
        },
        provider_slugs=_SLUGS,
    )
    assert patch == {
        "billing": "byok",
        "byok_provider": "openai",
        "privacy": "no_training",
        "trust": "auto_safe",
        "level": "standard",
    }


def test_validate_patch_rejects_bad_providers_and_budgets() -> None:
    """Unknown providers never pass, and a budget is not a setup setting."""
    patch = validate_profile_patch(
        {
            "byok_provider": "acme",
            "budget_usd": True,
        },
        provider_slugs=_SLUGS,
    )
    assert patch == {}
    assert validate_profile_patch({"budget_usd": 20}, provider_slugs=_SLUGS) == {}
    assert validate_profile_patch("[]", provider_slugs=_SLUGS) == {}


def test_platform_billing_drops_byok_provider() -> None:
    """A platform-billed patch carries no BYOK provider."""
    patch = validate_profile_patch({"billing": "platform", "byok_provider": "openai"}, provider_slugs=_SLUGS)
    assert patch == {"billing": "platform"}


def test_answered_phases() -> None:
    """Phases skip only when fully answered; level is never a phase."""
    assert answered_phases({"billing": "byok"}) == []
    assert answered_phases({"billing": "byok", "byok_provider": "openai"}) == ["billing"]
    assert answered_phases(
        {
            "privacy": "zdr",
            "trust": "ask",
            "level": "expert",
        }
    ) == ["privacy", "trust"]


def test_every_agenda_phase_runs_on_the_model() -> None:
    """The model runs the whole agenda, which is settings only, and every phase has a brief."""
    assert LLM_PHASES == AGENDA
    assert AGENDA == ("billing", "privacy", "trust")
    assert set(_PHASE_BRIEFS) == set(AGENDA)
    # A default option appears only where the default is not already a concrete answer.
    for phase in ("privacy", "trust"):
        assert "end the options with 'Use the default'" in _PHASE_BRIEFS[phase]
    for phase in ("billing",):
        assert "never" in _PHASE_BRIEFS[phase]
        assert "end the options with 'Use the default'" not in _PHASE_BRIEFS[phase]


def test_intake_inputs() -> None:
    """The prompt gets the provider slugs, the validated profile and the per-phase question note."""
    inputs = intake_inputs(
        phase="billing",
        turns=[{"role": "assistant", "content": "Q?"}, {"role": "user", "content": "A"}],
        profile={"trust": "ask"},
        provider_slugs=["openai", "anthropic"],
        locale="en",
    )
    assert inputs["phase"] == "billing"
    assert inputs["phase_brief"] == _PHASE_BRIEFS["billing"]
    assert json.loads(inputs["byok_providers"]) == ["anthropic", "openai"]
    assert inputs["reply_language"] == "English"
    assert json.loads(inputs["profile_json"]) == {"trust": "ask"}
    transcript = json.loads(inputs["transcript_json"])
    assert transcript[-1]["role"] == "system"
    assert f"1 of at most {MAX_PHASE_QUESTIONS}" in transcript[-1]["content"]
    assert (
        json.loads(
            intake_inputs(phase="trust", turns=[], profile={}, provider_slugs=[], locale="he")["transcript_json"]
        )
        == []
    )


def test_intake_turn_stream_end_to_end_with_dummy_lm(monkeypatch) -> None:
    """A full turn runs over the shared driver and ends in a validated ``interview_done``."""
    answer = {
        "message": "Who pays for the model calls?",
        "done": "false",
        "options_json": json.dumps([{"label": "Platform credits", "description": "Nothing to set up"}]),
        "profile_patch_json": json.dumps({"goal": "route tickets", "trust": "ask"}),
        "skip_rest": "false",
    }
    built: list[dict] = []

    def fake_lm(model, effort, max_tokens, model_config=None):
        """Record the LM knobs and return a canned responder."""
        built.append({"model": model, "effort": effort, "max_tokens": max_tokens})
        return DummyLM([answer])

    monkeypatch.setattr(intake_interview, "_build_agent_lm", fake_lm)

    async def collect() -> list[dict]:
        """Drain the stream."""
        return [
            event
            async for event in intake_interview.intake_turn_stream(
                phase="billing",
                turns=[{"role": "user", "content": "ask me before every change"}],
                profile={"level": "bogus"},
                provider_slugs=_SLUGS,
                locale="en",
                model="openai/gpt-4o-mini",
                reasoning_effort="low",
            )
        ]

    events = asyncio.run(collect())
    done = events[-1]
    assert done["event"] == "interview_done"
    assert built == [{"model": "openai/gpt-4o-mini", "effort": "low", "max_tokens": intake_interview.INTAKE_MAX_TOKENS}]
    assert done["data"]["message"] == "Who pays for the model calls?"
    assert done["data"]["phase_done"] is False
    assert done["data"]["options"] == [{"label": "Platform credits", "description": "Nothing to set up"}]
    # The volunteered trust answer is kept; a goal is not on the agenda and is dropped.
    assert done["data"]["profile_patch"] == {"trust": "ask"}
    assert done["data"]["skip_phases"] == ["trust"]
    assert done["data"]["model"] == "openai/gpt-4o-mini"
    assert "served_model" in done["data"]
