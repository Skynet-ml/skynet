"""LLM engine for the first-login onboarding intake interview. [INTERNAL]

The intake is settings only: a fixed agenda of account settings, and the
model runs every phase of it (the client asks only the interface language
itself, before the interview, because that answer reloads the page). Each
model turn asks at most one short question, and also extracts any answer the
user volunteered for a later phase into a validated ``profile_patch`` so the
client can skip those phases. Nothing about the user's first run (what to
improve, where the data lives) is asked, nor model choice or the wizard's
defaults: a new account takes the app's existing defaults.

The turn streams over the Signature & Metric interview's machinery
(:func:`~.code_interview.relay_interview_turn`): same events, same retries,
same parse salvage. Only the signature and the terminal payload differ.
Nothing here touches the database; the transcript and the profile so far are
client-owned and re-sent on every turn.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

import dspy

from ...api.model_catalog import agent_model_id
from ...config import settings
from ...models import ModelConfig
from .answer_options import normalize_options
from .code import _build_agent_lm, _reply_language
from .code_interview import _parse_json, relay_interview_turn
from .conduct import with_conduct

AGENDA = ("billing", "privacy", "trust")
LLM_PHASES = AGENDA
MAX_PHASE_QUESTIONS = 2
MAX_REQUEST_TURNS = 8
INTAKE_MAX_TOKENS = 4000
"""Completion budget per turn, reasoning included; a turn is a sentence or two plus small JSON."""

BILLING = ("platform", "byok")
PRIVACY = ("allow", "no_training", "zdr")
# Mirrors ``TrustMode`` in ``agents.generalist``; importing it would pull the
# whole generalist agent into this module.
TRUST_MODES = ("ask", "auto_safe", "yolo")
LEVELS = ("guided", "standard", "expert")

# The privacy setting is stored as ``deny`` (see ``billing.data_policy``); the
# intake speaks ``no_training``, so accept the stored spelling too.
_PRIVACY_ALIASES = {"deny": "no_training"}
_LEVEL_ALIASES = {"new": "guided", "familiar": "standard"}

_PHASE_BRIEFS = {
    "billing": (
        "Learn who pays for the model calls: platform credits (billing 'platform', "
        "nothing to set up) or the user's own provider key (billing 'byok'). When "
        "they bring a key, learn which provider it is from (byok_provider, one of "
        "``byok_providers``) before finishing. Offer exactly two options, platform "
        "credits first; never add 'Use the default': platform credits is the default."
    ),
    "privacy": (
        "Learn whether model providers may use the user's data: allow (providers may "
        "train on it), no_training (they may not train on it), or zdr (zero "
        "retention: nothing is stored). It applies to every model call made for the "
        "user. The default keeps the account's current setting; end the options with "
        "'Use the default' naming it, and set no privacy key when they pick it."
    ),
    "trust": (
        "Learn how freely the assistant may change the wizard for the user: ask "
        "(asks before every change), auto_safe (makes safe changes, asks for the "
        "rest) or yolo (makes every change without asking). The default keeps the "
        "current setting; end the options with 'Use the default' naming it, and set "
        "no trust key when they pick it."
    ),
}


class IntakeInterviewTurnSig(dspy.Signature):
    __doc__ = with_conduct("""Run one phase of a new user's onboarding interview, one short question at a time.

    You are a sharp engineer onboarding someone to a prompt- and
    program-optimization platform. The onboarding sets up account settings
    only, as a fixed agenda of phases; you run only ``phase`` (``phase_brief``
    says what it must learn). ``profile_json`` holds what is already known —
    never ask about it again. Ask only about the phase's setting: never about
    what the user wants to optimize, where their code or data lives, which
    models to use, or their level.
    Ask ONE short question at a time: ``message`` is one or two short
    sentences. If the transcript already answers the phase, or the
    question-count note says the limit is reached, set ``done`` to true and
    write a one-sentence acknowledgement instead of a question. If the user
    says to skip, to just use the defaults, or to get going, set
    ``skip_rest`` to true and ``done`` to true.

    For every question offer 2-4 options in ``options_json``, each a short
    pickable answer (<= 6 words) with a one-line description, the
    recommended option first. Options are concrete answers — never "other",
    "something else" or anything meaning "I'll type it": the user always has
    a free-text box. Every option must mean something different: never offer
    two that lead to the same setting, and never one the question does not
    ask about. Add "Use the default" (in ``reply_language``) as the LAST
    option only when the brief asks for it, i.e. when the default is not
    already one of the concrete options; name the default in its
    description, and when the user picks it the phase is done (patch only
    the default the brief names as a value). Options are [] once ``done``.

    In ``profile_patch_json`` extract every setting the user's own words
    settle so far, for ANY phase of the agenda, omitting keys you do not
    know: billing (platform | byok), byok_provider (one of
    ``byok_providers``), privacy (allow | no_training | zdr), trust (ask | auto_safe | yolo),
    level. Never ask the level and
    set it only when the user's own words clearly show it: 'guided' when
    they say they are new to machine learning, 'expert' when they name
    optimizers, data splits, metrics, or DSPy/GEPA settings; otherwise omit
    it.
    """)

    phase: str = dspy.InputField(desc="The agenda phase this turn runs, one of ``agenda``.")
    phase_brief: str = dspy.InputField(desc="What this phase must learn, and its default if any.")
    agenda: list[str] = dspy.InputField(desc="Every phase of the onboarding, in order.")
    profile_json: str = dspy.InputField(desc="JSON object of settings already known; never ask about these.")
    byok_providers: str = dspy.InputField(
        desc="JSON array of provider slugs; byok_provider in the patch must be one of these."
    )
    transcript_json: str = dspy.InputField(desc="JSON array of this phase's prior {role, content} turns.")
    reply_language: str = dspy.InputField(desc="Language the message and options are written in.")
    message: str = dspy.OutputField(desc="The next question, or a one-sentence acknowledgement when done.")
    # ``done`` sits right after ``message`` so it streams before the slower
    # options/patch fields; the shared driver turns it into ``turn_hint``.
    done: str = dspy.OutputField(desc="'true' when this phase is finished, else 'false'.")
    options_json: str = dspy.OutputField(
        desc=(
            'JSON array of 2-4 answer options, each {"label": <short answer>, '
            '"description": <one line>}, recommended first; [] when done.'
        )
    )
    profile_patch_json: str = dspy.OutputField(desc="JSON object of the profile keys the user's answers settle.")
    skip_rest: str = dspy.OutputField(desc="'true' when the user asked to skip ahead or use the defaults.")


def _truthy(value: Any) -> bool:
    """Read a model's boolean-ish text field.

    Args:
        value: Raw field value.

    Returns:
        Whether the value spells true.
    """
    return str(value or "").strip().lower() in {"true", "yes", "1"}


def _enum(value: Any, allowed: Iterable[str], aliases: dict[str, str] | None = None) -> str | None:
    """Coerce ``value`` onto an enum, tolerating case, spaces and known aliases.

    Args:
        value: Raw value.
        allowed: Accepted spellings.
        aliases: Extra spellings mapped onto accepted ones.

    Returns:
        The accepted value, or ``None``.
    """
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower().replace("-", "_").replace(" ", "_")
    cleaned = (aliases or {}).get(cleaned, cleaned)
    return cleaned if cleaned in allowed else None


def validate_profile_patch(raw: Any, *, provider_slugs: Iterable[str]) -> dict[str, Any]:
    """Validate and coerce a model-written profile patch, dropping every invalid key.

    Args:
        raw: The parsed ``profile_patch_json`` (or a client profile).
        provider_slugs: Provider slugs ``byok_provider`` may name.

    Returns:
        Only the keys that validated, in their canonical form.
    """
    if not isinstance(raw, dict):
        return {}
    slugs = {s.strip().lower() for s in provider_slugs if s}
    checks: dict[str, Any] = {
        "billing": lambda v: _enum(v, BILLING),
        "byok_provider": lambda v: v.strip().lower() if isinstance(v, str) and v.strip().lower() in slugs else None,
        "privacy": lambda v: _enum(v, PRIVACY, _PRIVACY_ALIASES),
        "trust": lambda v: _enum(v, TRUST_MODES),
        "level": lambda v: _enum(v, LEVELS, _LEVEL_ALIASES),
    }
    patch: dict[str, Any] = {}
    for key, check in checks.items():
        if key in raw:
            value = check(raw[key])
            if value is not None:
                patch[key] = value
    if patch.get("billing") == "platform":
        patch.pop("byok_provider", None)
    return patch


def answered_phases(patch: dict[str, Any]) -> list[str]:
    """List the agenda phases a validated patch fully answers.

    ``level`` is never listed: it is not a phase, the model always infers it.

    Args:
        patch: Output of :func:`validate_profile_patch`.

    Returns:
        Phase ids in agenda order.
    """
    answered = {
        "billing": patch.get("billing") == "platform" or bool(patch.get("byok_provider")),
        "privacy": "privacy" in patch,
        "trust": "trust" in patch,
    }
    return [phase for phase in AGENDA if answered.get(phase)]


def _transcript_json(turns: list[dict[str, str]]) -> str:
    """Encode this phase's transcript with the per-phase question-count note.

    Args:
        turns: This phase's prior ``{role, content}`` turns, oldest first.

    Returns:
        The JSON-encoded transcript.
    """
    asked = sum(1 for t in turns if t.get("role") == "assistant")
    noted = list(turns)
    if asked:
        noted.append(
            {
                "role": "system",
                "content": (
                    f"Questions asked in this phase: {asked} of at most {MAX_PHASE_QUESTIONS}."
                    " If the limit is reached you MUST finish now."
                ),
            }
        )
    return json.dumps(noted, ensure_ascii=False)


def intake_inputs(
    *,
    phase: str,
    turns: list[dict[str, str]],
    profile: dict[str, Any],
    provider_slugs: Iterable[str],
    locale: str | None,
) -> dict[str, Any]:
    """Assemble the ``IntakeInterviewTurnSig`` inputs for one turn.

    Args:
        phase: One of :data:`AGENDA`.
        turns: This phase's prior turns.
        profile: The validated profile so far.
        provider_slugs: Provider slugs ``byok_provider`` may name.
        locale: UI locale code; replies are written in that language.

    Returns:
        Keyword inputs for the predictor.
    """
    return {
        "phase": phase,
        "phase_brief": _PHASE_BRIEFS[phase],
        "agenda": list(AGENDA),
        "profile_json": json.dumps(profile, ensure_ascii=False),
        "byok_providers": json.dumps(sorted(provider_slugs), ensure_ascii=False),
        "transcript_json": _transcript_json(turns),
        "reply_language": _reply_language(locale),
    }


def parse_intake_prediction(
    pred: Any,
    asked: int,
    *,
    provider_slugs: Iterable[str],
) -> dict[str, Any]:
    """Turn a raw ``IntakeInterviewTurnSig`` prediction into the ``interview_done`` payload.

    Args:
        pred: The prediction, or ``None`` when the stream produced nothing.
        asked: Assistant questions asked in this phase before this turn.
        provider_slugs: Provider slugs ``byok_provider`` may name.

    Returns:
        ``{"message", "options", "phase_done", "profile_patch", "skip_phases",
        "skip_rest", "model"}``.
    """
    skip_rest = _truthy(getattr(pred, "skip_rest", ""))
    phase_done = _truthy(getattr(pred, "done", "")) or skip_rest or asked >= MAX_PHASE_QUESTIONS
    patch = validate_profile_patch(
        _parse_json(getattr(pred, "profile_patch_json", "{}"), {}), provider_slugs=provider_slugs
    )
    options = [] if phase_done else normalize_options(_parse_json(getattr(pred, "options_json", "[]"), []))
    return {
        "message": str(getattr(pred, "message", "") or "").strip(),
        "options": options,
        "phase_done": phase_done,
        "profile_patch": patch,
        "skip_phases": answered_phases(patch),
        "skip_rest": skip_rest,
        "model": agent_model_id(settings.code_agent_model),
    }


async def intake_turn_stream(
    *,
    phase: str,
    turns: list[dict[str, str]],
    profile: dict[str, Any],
    provider_slugs: list[str],
    locale: str | None,
    model: str | None,
    reasoning_effort: str | None = None,
    usage_sink: list | None = None,
    model_config: ModelConfig | None = None,
) -> Any:
    """Run one intake turn, streaming the Signature & Metric interview's events.

    Yields ``reasoning_patch``, ``message_patch``, ``message_end``,
    ``turn_hint`` (``{"final"}`` = phase done), ``message_reset`` on a
    retry, and a terminal ``interview_done`` (see
    :func:`parse_intake_prediction`, plus ``served_model``).

    Args:
        phase: One of :data:`AGENDA`.
        turns: This phase's prior turns, oldest first.
        profile: The client's profile so far (validated again here).
        provider_slugs: Provider slugs ``byok_provider`` may name.
        locale: UI locale code.
        model: LiteLLM id conducting the turn; ``None`` runs the server default.
        reasoning_effort: Effort level for ``model``; ``None`` keeps its default.
        usage_sink: Optional list the built LM is appended to, for metering.
        model_config: Optional resolved config for ``model`` (the Mac app's
            BYOK route).
    """
    asked = sum(1 for t in turns if t.get("role") == "assistant")
    lm = _build_agent_lm(model, reasoning_effort, max_tokens=INTAKE_MAX_TOKENS, model_config=model_config)
    if usage_sink is not None:
        usage_sink.append(lm)
    known = validate_profile_patch(profile, provider_slugs=provider_slugs)
    inputs = intake_inputs(phase=phase, turns=turns, profile=known, provider_slugs=provider_slugs, locale=locale)

    def parse(pred: Any, count: int) -> dict[str, Any]:
        """Bind the turn's context onto :func:`parse_intake_prediction`.

        Args:
            pred: The final prediction.
            count: Assistant questions asked before this turn.

        Returns:
            The ``interview_done`` payload.
        """
        return parse_intake_prediction(pred, count, provider_slugs=provider_slugs)

    async for event in relay_interview_turn(
        predict=dspy.Predict(IntakeInterviewTurnSig), lm=lm, inputs=inputs, asked=asked, model=model, parse=parse
    ):
        yield event
