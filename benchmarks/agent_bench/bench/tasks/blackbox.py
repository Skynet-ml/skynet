"""Benchmark tasks for the ``bbserve`` category: black-box mode and user-written code.

The three tasks cover picking a black-box engine by capability, validating pasted
signature/metric code, and interpreting a failed scorer dry-run and refusing to
submit. The category id and task ids keep their old ``bbserve`` names so new
results line up with earlier ones. Every task uses only the base fixture; no
``setup`` is needed. Oracles solve through ``w.call`` and checks grade the reply,
the call log and the final state.
"""

from __future__ import annotations

from bench.checks import (
    answer_has,
    answer_has_any,
    called,
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
]
