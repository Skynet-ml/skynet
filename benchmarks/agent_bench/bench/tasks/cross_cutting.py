"""Benchmark tasks for the ``cross`` category: skills the other categories miss.

The five category modules cover run setup, run insight, run lifecycle, black-box
serving and account robustness. This module fills the remaining gaps so the
benchmark is not blind to them: resolving a pronoun from earlier turns, a black-box
scorer check that *passes* and is followed by a launch, handing code writing to
the authoring card, and filtering the model catalog by capability.
"""

from __future__ import annotations

from bench.checks import (
    answer_has_any,
    answer_is_hebrew,
    called,
    custom,
    no_mutations,
    only_mutated,
    state_check,
)
from bench.fixtures import CLAUDE_SONNET
from bench.task import Run, Task
from bench.world import World

HEBREW_RUN = "ניתוח רגש בעברית"
J3 = "00000000-0000-4000-8000-000000000003"

_SCORER = (
    "def score(candidate, case):\n"
    '    """Reward short taglines that mention the product name."""\n'
    "    text = candidate.strip()\n"
    "    if not text:\n"
    "        return 0.0\n"
    '    bonus = 0.5 if "skynet" in text.lower() else 0.0\n'
    "    return bonus + max(0.0, 0.5 - len(text) / 400.0)\n"
)
_SEED_PROMPT = "Write a one-line tagline for Skynet, a code-optimization platform."

_DRY_RUN = "blackbox_scorer_dry_run_blackbox_scorer_dry_run_post"
_SUBMIT_BB = "submit_blackbox_run_blackbox_run_post"

_AUTHORING_WIZARD = {
    "step": "code",
    "optimization_type": "run",
    "name": "ticket urgency",
    "staged_dataset_id": "ds_support_tickets",
    "signature_code": "",
    "metric_code": "",
}


def _oracle_followup(w: World) -> str:
    """Pin the run the earlier turn was about, then clone it once."""
    w.call("toggle_pin_job_optimizations", {"optimization_id": J3})
    clone = w.call("clone_job_optimizations", {"optimization_id": J3})["clones"][0]
    return f"Pinned '{HEBREW_RUN}' and made a copy of it: '{clone['name']}' is queued as a new pending run."


def _pinned_and_cloned(state: dict) -> bool:
    """Return whether only the Hebrew run got pinned and exactly one copy of it exists."""
    jobs = state["jobs"]
    pinned = {oid for oid, job in jobs.items() if job.get("pinned")}
    copies = [job for oid, job in jobs.items() if not oid.startswith("00000000")]
    return (
        pinned == {"00000000-0000-4000-8000-000000000001", J3}
        and len(copies) == 1
        and HEBREW_RUN in copies[0]["name"]
        and copies[0]["status"] == "pending"
    )


def _oracle_blackbox_launch(w: World) -> str:
    """Dry-run the pasted scorer, then launch the run it validates."""
    scorer = {"kind": "python", "metric_code": _SCORER}
    check = w.call(_DRY_RUN, {"scorer": scorer, "candidate": _SEED_PROMPT})
    if not check["valid"]:
        return f"The scorer is not valid: {check['errors']}"
    run = w.call(
        _SUBMIT_BB,
        {
            "name": "tagline-opt",
            "objective": "Short tagline that mentions the product name.",
            "recipe": "prompt",
            "seed_candidate": _SEED_PROMPT,
            "scorer": scorer,
            "strategy": {"mode": "single", "engine": "gepa"},
            "reflection_model_config": {"name": CLAUDE_SONNET},
        },
    )
    return (
        f"The scorer is valid (function '{check['function_name']}'), so I launched 'tagline-opt' on the GEPA "
        f"engine with claude-sonnet-4.5 reflecting. It is {run['status']}."
    )


def _blackbox_launched(state: dict) -> bool:
    """Return whether exactly one new black-box run matches the requested name, engine and model."""
    new = [job for oid, job in state["jobs"].items() if not oid.startswith("00000000")]
    if len(new) != 1:
        return False
    job = new[0]
    scorer = (job.get("payload") or {}).get("scorer") or {}
    return (
        job["optimization_type"] == "blackbox"
        and job["name"].strip().lower() == "tagline-opt"
        and job["optimizer_name"] == "gepa"
        and job["reflection_model_name"] == CLAUDE_SONNET
        and "def score" in (scorer.get("metric_code") or "")
    )


def _dry_run_before_submit(r: Run) -> bool:
    """Return whether a successful dry run came before the first submit attempt."""
    names = [c["tool"] for c in r.calls if c["ok"] or c["tool"] == _SUBMIT_BB]
    return _DRY_RUN in names and (_SUBMIT_BB not in names or names.index(_DRY_RUN) < names.index(_SUBMIT_BB))


def _oracle_authoring(w: World) -> str:
    """Open the code-authoring card with the user's goal."""
    w.call("request_code_authoring", {"goal": "Classify support tickets by urgency."})
    return "I opened the code helper with your goal - it will draft the signature and metric for ticket urgency."


def _authoring_card_open(state: dict) -> bool:
    """Return whether a code-authoring card is on screen."""
    return any(card["kind"] == "code_authoring" for card in state["ui_cards"])


def _oracle_long_context(w: World) -> str:
    """List the available models whose context reaches one million tokens."""
    models = w.call("list_models_for_agent", {})["models"]
    names = [m["name"].split("/")[-1] for m in models if m["available"] and m["max_input_tokens"] >= 1_000_000]
    return "המודלים הזמינים עם חלון הקשר של מיליון טוקנים לפחות: " + ", ".join(names) + "."


def _llama_not_offered(r: Run) -> bool:
    """Return whether the unavailable Llama model is left out or flagged as unusable."""
    answer = r.answer.lower()
    if "llama" not in answer:
        return True
    return any(flag in answer for flag in ("לא זמין", "אינו זמין", "אינה זמינה", "unavailable", "not available", "מפתח", "key"))


TASKS: list[Task] = [
    Task(
        id="cross-followup-pin-and-copy",
        category="cross",
        difficulty="medium",
        prompt="Nice. Pin it, and make me one copy of it so I can try a tweak.",
        history=[
            ("user", "How did my Hebrew sentiment run do?"),
            (
                "assistant",
                f"'{HEBREW_RUN}' finished successfully on claude-haiku-4.5 and reached 0.75 on the test set.",
            ),
        ],
        checks=[
            state_check("only the Hebrew run got pinned and exactly one pending copy of it exists", _pinned_and_cloned),
            answer_has_any("pinned", "pin", name="answer confirms the pin"),
            answer_has_any("copy", "clone", "duplicate", "עותק", name="answer confirms the copy"),
            only_mutated(
                "toggle_pin_job_optimizations",
                "bulk_pin_jobs_optimizations_bulk_pin_post",
                "clone_job_optimizations",
                name="only pinned and cloned",
            ),
        ],
        oracle=_oracle_followup,
        skills=["multi-turn", "reference-resolution", "pin", "clone"],
        notes="'it' refers to the run named in the previous turn; the agent has to resolve that name to an id first.",
    ),
    Task(
        id="cross-blackbox-check-then-launch",
        category="cross",
        difficulty="hard",
        prompt=(
            "I want to optimize this prompt in black-box mode:\n\n"
            f"{_SEED_PROMPT}\n\n"
            "Here is my scorer:\n\n"
            f"```python\n{_SCORER}```\n\n"
            "Check that the scorer is valid first. If it is, launch the run as 'tagline-opt' on the GEPA engine "
            "with Claude Sonnet as the reflection model."
        ),
        checks=[
            custom("dry-ran the scorer before submitting", _dry_run_before_submit),
            state_check("one new 'tagline-opt' black-box run on gepa with claude-sonnet-4.5", _blackbox_launched),
            answer_has_any("launched", "submitted", "queued", "started", "pending", name="answer confirms the launch"),
            only_mutated(_SUBMIT_BB, name="only submitted the black-box run"),
        ],
        oracle=_oracle_blackbox_launch,
        skills=["blackbox", "validate-then-act", "model-resolution"],
        notes="The positive twin of bbserve-scorer-dryrun-refuse: the scorer is valid, so stopping after the check is wrong.",
    ),
    Task(
        id="cross-open-code-authoring",
        category="cross",
        difficulty="easy",
        prompt=(
            "I have no idea how to write the signature and metric code. Can you open the code helper for me? "
            "The goal is to classify support tickets by urgency."
        ),
        wizard_state=_AUTHORING_WIZARD,
        checks=[
            called("request_code_authoring", goal=lambda g: "urgen" in str(g).lower(), name="opened the helper with the urgency goal"),
            state_check("a code-authoring card is on screen", _authoring_card_open),
            no_mutations(),
        ],
        oracle=_oracle_authoring,
        skills=["wizard", "ui-card", "delegation"],
        notes="The user asked for the helper card, not for code pasted in chat; the goal must be passed through.",
    ),
    Task(
        id="cross-models-long-context-he",
        category="cross",
        difficulty="medium",
        prompt="אילו מהמודלים שזמינים לי תומכים בחלון הקשר של מיליון טוקנים לפחות?",
        checks=[
            answer_has_any("claude-sonnet-4.5", "sonnet 4.5", "sonnet-4.5", name="lists claude-sonnet-4.5"),
            answer_has_any("gemini-2.5-pro", "gemini 2.5 pro", name="lists gemini-2.5-pro"),
            answer_has_any("gemini-2.5-flash", "gemini 2.5 flash", name="lists gemini-2.5-flash"),
            custom("does not offer the unavailable llama-4-scout", _llama_not_offered),
            answer_is_hebrew(),
            no_mutations(),
        ],
        oracle=_oracle_long_context,
        skills=["model-catalog", "filtering", "hebrew"],
        notes="llama-4-scout has the largest context but is unavailable (no provider key), so it must not be offered.",
    ),
]
