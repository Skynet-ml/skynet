"""Benchmark tasks for the ``bbserve`` category: black-box mode, user-written
code, and using a finished run.

The six tasks cover picking a black-box engine by capability, reading how a
finished program is called, opening an inference session for the right grid
pair, validating pasted signature/metric code, interpreting a failed scorer
dry-run and refusing to submit, and refusing to serve an unservable run. Every
task uses only the base fixture; no ``setup`` is needed. Oracles solve through
``w.call`` and checks grade the reply, the call log and the final state.
"""

from __future__ import annotations

from bench.checks import (
    answer_has,
    answer_has_any,
    answer_is_hebrew,
    called,
    custom,
    dig,
    no_mutations,
    not_called,
)
from bench.task import Task
from bench.world import World

_T4_SIGNATURE = (
    "import dspy\n\n\n"
    "class ClassifyTicket(dspy.Signature):\n"
    '    """Classify a support ticket by topic."""\n\n'
    "    text: str = dspy.InputField()\n"
    "    label: str = dspy.OutputField()\n"
)

# Missing the colon at the end of the def line -> a syntax error the AST parse catches.
_T4_METRIC = (
    "def accuracy(example, prediction, trace=None)\n"
    "    return prediction.label == example.label\n"
)

# A lambda assigned to a name defines no top-level function, which the engine requires.
_T5_SCORER = "score = lambda candidate, case: len(candidate) / 200.0\n"


def _job_id_by_name(w: World, name: str) -> str | None:
    """Return the caller's optimization id whose name equals ``name``.

    Args:
        w: The world.
        name: The exact job name to match.

    Returns:
        The optimization id, or None when no accessible job matches.
    """
    items = w.call("list_jobs_optimizations_get", {"limit": 50})["items"]
    for item in items:
        if item["name"] == name:
            return item["optimization_id"]
    return None


def _oracle_pick_engine(w: World) -> str:
    """Solve the checkpoint-engine pick: only GEPA supports checkpoint recovery."""
    catalog = w.call("blackbox_engines_blackbox_engines_get", {"target": "prompt"})
    resumable = [e["id"] for e in catalog["engines"] if e.get("checkpoint_recovery_supported")]
    return (
        "For a run you might need to resume from a checkpoint, pick "
        f"{', '.join(resumable).upper()}: it is the only black-box engine that supports "
        "checkpoint recovery. Best-of-N, AutoResearch and Meta-Harness cannot restore from a "
        "checkpoint, and AutoSaddler is disabled on this deployment."
    )


def _oracle_serve_fields(w: World) -> str:
    """Solve the serve-info question about the Hebrew sentiment run's fields and model."""
    oid = _job_id_by_name(w, "ניתוח רגש בעברית")
    info = w.call("serve_info_serve", {"optimization_id": oid})
    inputs = ", ".join(info["input_fields"])
    outputs = ", ".join(info["output_fields"])
    model = info["model_name"].split("/")[-1]
    return (
        f"התוכנית מקבלת שדה קלט אחד בשם {inputs} (הפלט הוא {outputs}), "
        f"והיא רצה על המודל {model}."
    )


def _oracle_serve_winning_pair(w: World) -> str:
    """Solve the grid best-pair question and open an inference session for that pair."""
    oid = _job_id_by_name(w, "grid: model bake-off")
    grid = w.call("get_grid_search_result_optimizations", {"optimization_id": oid})
    best = grid["best_pair"]
    w.call(
        "request_user_pair_inference",
        {"optimization_id": oid, "pair_index": best["pair_index"], "prompt": "try the winning pair"},
    )
    gen = best["generation_model"].split("/")[-1]
    refl = best["reflection_model"].split("/")[-1]
    return (
        f"The winning pairing is {gen} (generation) + {refl} (reflection) at "
        f"{best['optimized_test_metric']:.2f} test accuracy. I've opened an inference session "
        "for that pair so you can try it."
    )


def _oracle_validate_code(w: World) -> str:
    """Solve the code-validation task: signature is fine, the metric has a syntax error."""
    w.call(
        "validate_code_validate_code_post",
        {
            "signature_code": _T4_SIGNATURE,
            "metric_code": _T4_METRIC,
            "column_mapping": {"inputs": {"text": "text"}, "outputs": {"label": "label"}},
        },
    )
    return (
        "Your signature is valid (input field `text`, output field `label`). The metric has a "
        "syntax error: you are missing the colon at the end of the `def accuracy(...)` line. Add "
        "the `:` and it will parse. I did not submit anything."
    )


def _oracle_scorer_refuse(w: World) -> str:
    """Solve the scorer dry-run task: the scorer is invalid, so do not submit the run."""
    w.call(
        "blackbox_scorer_dry_run_blackbox_scorer_dry_run_post",
        {"scorer": {"kind": "python", "metric_code": _T5_SCORER}, "candidate": "Ship faster with less busywork."},
    )
    return (
        "I ran the dry run on your scorer and it is not valid: it defines no top-level function. "
        "You assigned a lambda to `score`, but the engine needs a named function such as "
        "`def score(candidate, case): ...`. I did not start the run; fix the scorer and I will launch it."
    )


def _oracle_refuse_serve(w: World) -> str:
    """Solve the refuse-to-serve task: the run failed, so there is no program to serve."""
    oid = _job_id_by_name(w, "blackbox: scorer crash")
    summary = w.call("get_job_summary_optimizations", {"optimization_id": oid})
    reason = summary.get("message") or "its scorer crashed"
    return (
        f"That run failed ({reason}), so it produced no optimized program to serve. There is "
        "nothing to run inference on. You would need to fix the scorer and re-run it first."
    )


def _only_pair3_cards(state: dict) -> bool:
    """Return True when every pair-inference card in the state targets pair index 3."""
    cards = dig(state, "ui_cards") or []
    return all(c.get("pair_index") == 3 for c in cards if c.get("kind") == "pair_inference")


TASKS: list[Task] = [
    Task(
        id="bbserve-pick-engine",
        category="bbserve",
        difficulty="easy",
        prompt=(
            "I'm about to launch a long black-box optimization run and I'm worried it might "
            "get interrupted before it finishes. If that happens I want to resume from the last "
            "checkpoint rather than restart from scratch. Which single black-box engine should I "
            "choose for that?"
        ),
        checks=[
            called("blackbox_engines_blackbox_engines_get", name="consulted the engine catalog"),
            answer_has("gepa", name="recommends GEPA"),
            no_mutations(),
        ],
        oracle=_oracle_pick_engine,
        skills=["blackbox", "engine-selection", "capability-reasoning"],
        notes="Only GEPA supports checkpoint recovery; a wrong agent picks another engine or invents support.",
    ),
    Task(
        id="bbserve-serve-fields",
        category="bbserve",
        difficulty="easy",
        prompt=(
            "אני רוצה להתחיל להשתמש בתוכנית שאופטמה בריצה 'ניתוח רגש בעברית'. "
            "אילו שדות קלט היא מקבלת ועל איזה מודל היא רצה?"
        ),
        checks=[
            answer_is_hebrew(),
            answer_has("text", name="names the input field text"),
            answer_has_any("claude-haiku-4.5", "haiku", name="names the model claude-haiku-4.5"),
            no_mutations(),
        ],
        oracle=_oracle_serve_fields,
        skills=["serve", "read-program", "hebrew"],
        notes="serve_info gives input field 'text' and model claude-haiku-4.5; a wrong agent reports the wrong field or model.",
    ),
    Task(
        id="bbserve-serve-winning-pair",
        category="bbserve",
        difficulty="medium",
        prompt=(
            "In my 'grid: model bake-off' grid search, which model pairing came out on top? "
            "Please open an inference session for that winning pairing so I can try it out."
        ),
        checks=[
            called(
                "request_user_pair_inference",
                optimization_id="00000000-0000-4000-8000-000000000011",
                pair_index=3,
                name="opened an inference card for the winning pair (index 3)",
            ),
            answer_has("haiku", name="names the winning generation model (claude-haiku-4.5)"),
            answer_has("gpt-4o", name="names the winning reflection model (gpt-4o)"),
            custom("did not open a card for any other pair", lambda r: _only_pair3_cards(r.state)),
        ],
        oracle=_oracle_serve_winning_pair,
        skills=["grid-search", "serve", "precise-targeting"],
        notes="Best pair is index 3 (claude-haiku-4.5 + gpt-4o); a wrong agent opens a look-alike pair like index 4 (haiku + sonnet).",
    ),
    Task(
        id="bbserve-validate-pasted-code",
        category="bbserve",
        difficulty="medium",
        prompt=(
            "Before I submit this DSPy run, can you double-check my signature and metric code?\n\n"
            "Signature:\n"
            f"{_T4_SIGNATURE}\n"
            "Metric:\n"
            f"{_T4_METRIC}"
        ),
        checks=[
            answer_has_any("syntax", "colon", "missing", name="identifies the syntax error"),
            answer_has_any("metric", name="points at the metric"),
            answer_has_any("signature", name="addresses the signature"),
            no_mutations(),
            not_called(
                "submit_job_run_post",
                "submit_blackbox_run_blackbox_run_post",
                name="did not submit a run",
            ),
        ],
        oracle=_oracle_validate_code,
        skills=["user-code", "validation", "error-explanation"],
        notes="Signature is valid; the metric is missing its colon. A wrong agent says both are fine, or submits anyway.",
    ),
    Task(
        id="bbserve-scorer-dryrun-refuse",
        category="bbserve",
        difficulty="hard",
        prompt=(
            "Here's the scorer I want for a black-box optimization run:\n\n"
            f"{_T5_SCORER}\n"
            "Can you run a quick check on it, and if it's good, go ahead and start the run? "
            "My objective is to write a punchy product tagline."
        ),
        checks=[
            called(
                "blackbox_scorer_dry_run_blackbox_scorer_dry_run_post",
                name="ran the scorer dry run",
            ),
            answer_has_any("function", "lambda", name="explains the scorer defines no function"),
            answer_has_any("scorer", name="refers to the scorer"),
            not_called(
                "submit_blackbox_run_blackbox_run_post",
                name="did not submit the run",
            ),
            no_mutations(),
        ],
        oracle=_oracle_scorer_refuse,
        skills=["blackbox", "scorer-dry-run", "refuse-on-failure"],
        notes="Dry run is AST-only: the lambda fails as 'no top-level function'. A wrong agent submits anyway (credits are available).",
    ),
    Task(
        id="bbserve-refuse-serve-failed",
        category="bbserve",
        difficulty="hard",
        prompt=(
            "Can you open an inference session for my 'blackbox: scorer crash' run? "
            "I'd like to start using its optimized prompt."
        ),
        checks=[
            answer_has_any(
                "failed", "fail", "crash", "unsuccessful", "did not finish", "didn't finish",
                name="states the run failed",
            ),
            answer_has_any(
                "scorer", "zerodivision", "division", "crash", "error", "no optimized program",
                "nothing to serve", "no program",
                name="explains there is nothing to serve",
            ),
            not_called(
                "request_user_inference",
                "request_user_pair_inference",
                name="did not open an inference card",
            ),
        ],
        oracle=_oracle_refuse_serve,
        skills=["serve", "honest-refusal", "run-state"],
        notes="request_user_inference would happily open a card for the failed run; the agent must refuse and explain instead.",
    ),
]
