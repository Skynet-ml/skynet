"""Convert stored DSPy runs (``unified_v1`` jobs) into repository runs.

SKY-316 makes Skynet repo-only. A stored unified run holds its run code,
metric and cases in the job payload. This script turns one such job into a
starter repository and a ``BlackboxRunRequest``-shaped dict that optimizes
it, and plans how continuous plans move to the converted runs.

Decisions this script encodes:

* Few-shot demos (``__dspy_state__``) do not carry over. Only predictor
  instructions do; the report says when demos were dropped.
* DSPy optimizer choices become ``auto``; repository engines stay.
* The old task model becomes ``scorer.model``, which the program calls
  through ``APP_MODEL`` / ``APP_API_KEY`` and the ``skynet.llm`` helper.
* The old reflection models stay the optimization models.
* Nothing is destructive. The dry run (default) only reads the database and
  writes files under ``--out``. ``--apply`` creates GitHub repositories, but
  submits no run and changes no database row: it prints the plan-move SQL
  for a person to review and run.

Run from ``backend/``::

    .venv/bin/python -m scripts.convert_dspy_runs_to_repos --out /tmp/convert --username alice
    .venv/bin/python -m scripts.convert_dspy_runs_to_repos --out /tmp/convert --job-id <id> \
        --apply --confirm-environment staging
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

from core.connectors.github_create_repo import (
    CreatedRepository,
    RepoCreateError,
    create_repository_with_files,
    existing_repository,
    push_initial_commit,
)
from core.connectors.github_repo import RepoFetchError, github_token
from core.models.blackbox import (
    BLACKBOX_ENGINE_SHINKA_EVOLVE,
    BLACKBOX_ITERATION_LIMIT_ENGINES,
    BLACKBOX_REPO_ENGINES,
)

MODEL_FIELDS = ("name", "token_source", "byok_provider", "cli_provider", "base_url", "temperature", "max_tokens")
SECRET_WORDS = ("key", "token", "secret", "password", "auth", "credential", "header", "route", "cookie")

# Literal copies, not imports from ``core.models.unified``: SKY-316 removes
# the unified run path, and this script must keep reading the stored rows.
UNIFIED_PAYLOAD_KIND = "unified_v1"
BLACKBOX_OPTIMIZATION_TYPE = "blackbox"
LEGACY_OPTIMIZATION_TYPES = ("run", "grid_search")
DSPY_OPTIMIZER_PREFIX = "dspy_"
DSPY_STATE_KEY = "__dspy_state__"
ARTIFACT_KIND_DSPY_PROGRAM = "dspy_program"
PLAN_ENDED = "ended"
END_DSPY_RETIRED = "dspy_retired"

PROGRAM_DIR = "program"
DSPY_REQUIREMENT = "dspy==3.3.1"
# Metric helpers only the unified harness offered; a repo scorer has no
# observation of the run, so a metric that reads them cannot be converted.
UNSUPPORTED_METRIC_HELPERS = frozenset({"observation", "service_cost", "reward"})
RESERVED_PROGRAM_FILES = frozenset({"__init__.py", "pipeline.py", "prompts.json"})
MAX_INTENT_CHARS = 4_000
MAX_SETUP_COMMAND_CHARS = 300
PLACEHOLDER_REPOSITORY = "pending-owner/pending-repo"
NEW_RUN_PLACEHOLDER = "<new run id for {job_id}>"
DATABASE_URL_DEFAULT = "postgresql://postgres@localhost:5432/skynet"

PROGRAM_INIT_TEMPLATE = '''"""Program entry point converted from a Skynet DSPy run.

``run(input)`` loads ``pipeline.py`` (the original run code, which defines
``run(artifact, input)``), applies the instructions in ``prompts.json`` and
returns the program's output for one case. Optimizers edit this folder.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

try:
    import dspy
except ImportError:
    dspy = None
try:
    from skynet import llm as skynet_llm
except ImportError:
    skynet_llm = None

ARTIFACT_KIND = __ARTIFACT_KIND__
PROGRAM_NAME = "program"
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    # The run code imports its helper files by bare module name.
    sys.path.insert(0, str(HERE))


def _load_pipeline():
    """Execute pipeline.py as a module and return it."""
    spec = importlib.util.spec_from_file_location("converted_pipeline", HERE / "pipeline.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _find_program(module):
    """Return the one module-level dspy.Module, or the one named program."""
    found = {}
    for name, value in vars(module).items():
        if isinstance(value, dspy.Module) and not name.startswith("__"):
            found.setdefault(id(value), value)
    if len(found) == 1:
        return next(iter(found.values()))
    named = getattr(module, PROGRAM_NAME, None)
    if isinstance(named, dspy.Module):
        return named
    raise NameError("pipeline.py must build one dspy.Module at module level, or name it program.")


def _apply_instructions(program, prompts):
    """Set each predictor's instruction from prompts.json."""
    for name, predictor in program.named_predictors():
        text = prompts if isinstance(prompts, str) else (prompts or {}).get(name)
        if text is not None:
            predictor.signature = predictor.signature.with_instructions(str(text))


PIPELINE = _load_pipeline()
PROMPTS = json.loads((HERE / "prompts.json").read_text(encoding="utf-8"))
LM = None
if ARTIFACT_KIND == "dspy_program" and dspy is not None:
    _apply_instructions(_find_program(PIPELINE), PROMPTS)
    if skynet_llm is not None:

        class SkynetLM(dspy.BaseLM):
            """A DSPy LM whose requests go through the skynet.llm helper (APP_MODEL)."""

            def forward(self, prompt=None, messages=None, **kwargs):
                """Complete one request and return an OpenAI-shaped response."""
                text = skynet_llm(messages=messages or [{"role": "user", "content": str(prompt)}])
                message = type("Message", (), {"content": text})()
                choice = type("Choice", (), {"message": message})()
                return type("Response", (), {"choices": [choice], "usage": {}, "model": self.model})()

        LM = SkynetLM(model=os.environ.get("APP_MODEL") or "skynet", cache=False)


def run(input):
    """Return the program's output for one case input."""
    if LM is None:
        return PIPELINE.run(PROMPTS, input)
    with dspy.context(lm=LM):
        return PIPELINE.run(PROMPTS, input)
'''

SCORER_CODE = '''"""Score one case: run the converted program, then the original metric.

Skynet calls ``score(repo_path, case)`` in a fresh process for each case.
``metric.py`` is the run's original metric, ``score(input, output,
expected)`` returning ``(score, side_info)``; this adapter turns that into
the repository scorer's ``{"score", "feedback", "scores"}`` shape.
"""

import importlib
import importlib.util
import inspect
import sys
from pathlib import Path


def _metric_module(root):
    """Load metric.py from the checkout."""
    spec = importlib.util.spec_from_file_location("converted_metric", root / "metric.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _takes_expected(fn):
    """Return whether the metric accepts a third positional argument."""
    kinds = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    params = inspect.signature(fn).parameters.values()
    if any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params):
        return True
    return sum(1 for p in params if p.kind in kinds) >= 3


def _feedback(value, fallback):
    """Return non-empty feedback text."""
    return value if isinstance(value, str) and value.strip() else fallback


def score(repo_path, case):
    """Run the program on one case and score its output with the original metric."""
    root = Path(repo_path).resolve()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    program = importlib.import_module("program")
    metric = _metric_module(root)
    inputs = dict(case.get("input") or {})
    expected = case.get("expected")
    output = program.run(inputs)
    fn = metric.score
    raw = fn(inputs, output, expected) if _takes_expected(fn) else fn(inputs, output)
    if not isinstance(raw, (tuple, list)) or len(raw) != 2:
        raise TypeError("metric.score must return (score, side_info).")
    value, side_info = raw
    side_info = side_info if isinstance(side_info, dict) else {"feedback": side_info}
    feedback = _feedback(side_info.get("feedback"), "The metric gave no feedback.")
    result = {"score": float(value), "feedback": feedback}
    named = side_info.get("scores")
    if isinstance(named, dict) and named:
        result["scores"] = {
            str(name): {"score": float(number), "feedback": f"{name}: {float(number):g}"}
            for name, number in named.items()
        }
    return result
'''

SERVE_CODE = '''"""Serve the converted program: ``serve(input)`` returns JSON-safe output."""

from program import run


def serve(input):
    """Return the program's output for one request input."""
    output = run(input)
    return output.toDict() if hasattr(output, "toDict") else output
'''


class ConversionSkippedError(Exception):
    """A job that cannot become a repository run; the message says why."""


def _toml_string(value: str) -> str:
    """Quote text as a TOML basic string.

    Args:
        value: Any text.

    Returns:
        A TOML string literal. JSON escapes are valid TOML basic-string escapes.
    """
    return json.dumps(value, ensure_ascii=True)


def _as_dict(value: Any) -> dict[str, Any]:
    """Read a stored JSON column that may arrive as text.

    Args:
        value: A dict, JSON text or ``None``.

    Returns:
        The mapping, or an empty dict.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def _unsupported_helpers(metric_code: str) -> list[str]:
    """Find unified-only helpers the metric imports from ``skynet``.

    Args:
        metric_code: The stored metric source.

    Returns:
        The sorted helper names; empty when the code does not parse.
    """
    try:
        tree = ast.parse(metric_code)
    except SyntaxError:
        return []
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "skynet":
            found.update(alias.name for alias in node.names if alias.name in UNSUPPORTED_METRIC_HELPERS)
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "skynet"
            and node.attr in UNSUPPORTED_METRIC_HELPERS
        ):
            found.add(node.attr)
    return sorted(found)


def map_strategy(choice: str | None) -> tuple[dict[str, Any], str | None]:
    """Map an old optimizer choice to a repository strategy.

    Args:
        choice: The unified ``optimizer.choice``.

    Returns:
        The strategy dict and a warning when the choice changed.
    """
    value = (choice or "auto").strip()
    if value == "auto":
        return {"mode": "auto"}, None
    if value.startswith(DSPY_OPTIMIZER_PREFIX):
        return {"mode": "auto"}, f"The DSPy optimizer '{value}' has no repository version; the run uses Auto."
    if value in BLACKBOX_REPO_ENGINES:
        return {"mode": "single", "engine": value}, None
    return {"mode": "auto"}, f"The optimizer '{value}' is unknown; the run uses Auto."


def _starting_prompts(payload: dict[str, Any], result: dict[str, Any]) -> tuple[Any, bool]:
    """Pick the instructions the starter program begins from.

    Args:
        payload: The stored unified payload.
        result: The stored job result.

    Returns:
        The best (or starting) candidate without DSPy state, and whether
        demos were dropped.
    """
    candidate = result.get("best_candidate")
    if candidate in (None, "", {}):
        artifact = (payload.get("task") or {}).get("artifact") or {}
        candidate = artifact.get("starting_version")
    if isinstance(candidate, dict):
        dropped = bool(candidate.get(DSPY_STATE_KEY))
        return {k: v for k, v in candidate.items() if k != DSPY_STATE_KEY}, dropped
    return (candidate if isinstance(candidate, str) else ""), False


def _optimize_toml(objective: str, setup_command: str | None) -> str:
    """Write the repository's ``optimize.toml``.

    Args:
        objective: The run's objective, already cut to the intent limit.
        setup_command: The old install command, when it fits.

    Returns:
        The file text.
    """
    lines = [
        "version = 1",
        "",
        "[paths]",
        f'editable = ["{PROGRAM_DIR}"]',
        'protected = ["scorer.py", "metric.py", "serve.py", "data", "optimize.toml"]',
        "",
    ]
    if setup_command:
        lines += ["[setup]", f"command = {_toml_string(setup_command)}", ""]
    lines += ["[instructions]"]
    if objective:
        lines.append(f"intent = {_toml_string(objective)}")
    lines.append(f"invariants = [{_toml_string('program/__init__.py keeps defining run(input).')}]")
    return "\n".join(lines) + "\n"


def _readme(name: str, objective: str, background: str, job_id: str, demos_dropped: bool) -> str:
    """Write the starter repository's README.

    Args:
        name: The run name.
        objective: The run's objective.
        background: The run's background text.
        job_id: The source job.
        demos_dropped: Whether the source carried few-shot demos.

    Returns:
        The file text.
    """
    parts = [
        f"# {name}",
        "",
        f"Converted by Skynet from the DSPy run `{job_id}`.",
        "",
        "- `program/`: the program. Optimizers edit only this folder.",
        "- `scorer.py`: runs the program on a case and scores it with `metric.py`.",
        "- `serve.py`: `serve(input)` returns the program's output.",
        "- `data/cases.jsonl`: the run's cases.",
        "",
    ]
    if demos_dropped:
        parts += ["The source run had few-shot demos. They did not carry over; only instructions did.", ""]
    if objective:
        parts += ["## Objective", "", objective, ""]
    if background:
        parts += ["## Background", "", background, ""]
    return "\n".join(parts)


def _model(value: Any) -> dict[str, Any] | None:
    """Copy one stored model config without its credentials.

    Args:
        value: A stored ``ModelConfig`` dict or ``None``.

    Returns:
        A copy with only the public ``ModelConfig`` fields, or ``None`` when
        the model has no name.
    """
    if not isinstance(value, dict) or not value.get("name"):
        return None
    # request.json is written to disk and later submitted, so stored keys,
    # tokens and scoped routes must not travel with it.
    model = {field: value[field] for field in MODEL_FIELDS if value.get(field) is not None}
    extra = {
        key: item
        for key, item in (value.get("extra") or {}).items()
        if not any(word in key.lower() for word in SECRET_WORDS)
    }
    if extra:
        model["extra"] = extra
    return model


def convert_job(job: dict[str, Any]) -> dict[str, Any]:
    """Turn one stored unified job into starter repository files and a run request.

    Args:
        job: ``{"optimization_id", "username", "optimization_type", "status",
            "payload", "result"}`` as stored in ``jobs``.

    Returns:
        ``{"job_id", "username", "files", "request", "warnings"}``. ``files``
        maps repository paths to text. ``request`` is shaped like
        ``BlackboxRunRequest`` with a placeholder repository until ``--apply``
        creates one.

    Raises:
        ConversionSkippedError: When the job cannot be converted; the message says why.
    """
    job_id = str(job.get("optimization_id") or "")
    payload = _as_dict(job.get("payload"))
    result = _as_dict(job.get("result"))
    kind = job.get("optimization_type")
    if kind in LEGACY_OPTIMIZATION_TYPES or payload.get("optimization_type") == "run":
        raise ConversionSkippedError(f"A legacy '{kind}' job has no run code or metric to convert.")
    if kind != BLACKBOX_OPTIMIZATION_TYPE or payload.get("payload_kind") != UNIFIED_PAYLOAD_KIND:
        raise ConversionSkippedError("The job is not a unified_v1 run.")
    workspace = payload.get("workspace") or {}
    run_code = workspace.get("run_code") or ""
    metric_code = workspace.get("metric_code") or ""
    if not run_code.strip():
        raise ConversionSkippedError("The job has no run code.")
    if not metric_code.strip():
        raise ConversionSkippedError("The job has no metric code.")
    data = payload.get("data") or {}
    cases = [
        {"input": dict(case.get("input") or {}), "expected": case.get("expected")}
        for case in data.get("cases") or []
        if isinstance(case, dict)
    ]
    if not cases:
        reason = "a staged dataset" if data.get("staged_dataset_id") else "no cases"
        raise ConversionSkippedError(f"The job uses {reason}; a repository run needs inline cases.")
    helpers = _unsupported_helpers(metric_code)
    if helpers:
        raise ConversionSkippedError(f"The metric uses {', '.join(helpers)} from skynet; repository scorers lack them.")
    extra_files = workspace.get("files") or {}
    clashes = sorted(path for path in extra_files if str(path) in RESERVED_PROGRAM_FILES)
    if clashes:
        raise ConversionSkippedError(f"Workspace files clash with the starter program: {', '.join(clashes)}.")

    warnings: list[str] = []
    task = payload.get("task") or {}
    artifact_kind = str((task.get("artifact") or {}).get("kind") or "text")
    objective = str(task.get("objective") or "")
    background = str(task.get("background") or "")
    name = str(payload.get("name") or f"Converted run {job_id[:8]}")
    prompts, demos_dropped = _starting_prompts(payload, result)
    if demos_dropped:
        warnings.append("Few-shot demos (__dspy_state__) were dropped; only instructions carried over.")
    intent = objective
    if len(intent) > MAX_INTENT_CHARS:
        intent = intent[:MAX_INTENT_CHARS]
        warnings.append("The objective was cut to 4,000 characters in optimize.toml; the request keeps all of it.")
    setup_command = (workspace.get("install_command") or "").strip() or None
    if setup_command and len(setup_command) > MAX_SETUP_COMMAND_CHARS:
        warnings.append("The install command is too long for optimize.toml; the run infers a setup command.")
        setup_command = None

    files: dict[str, str] = {
        "optimize.toml": _optimize_toml(intent, setup_command),
        "README.md": _readme(name, objective, background, job_id, demos_dropped),
        "requirements.txt": DSPY_REQUIREMENT + "\n",
        f"{PROGRAM_DIR}/__init__.py": PROGRAM_INIT_TEMPLATE.replace("__ARTIFACT_KIND__", repr(artifact_kind)),
        f"{PROGRAM_DIR}/pipeline.py": run_code,
        f"{PROGRAM_DIR}/prompts.json": json.dumps(prompts, indent=2, ensure_ascii=False) + "\n",
        "metric.py": metric_code,
        "scorer.py": SCORER_CODE,
        "serve.py": SERVE_CODE,
        "data/cases.jsonl": "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases),
    }
    for path, content in sorted(extra_files.items()):
        files[f"{PROGRAM_DIR}/{path}"] = str(content)

    models = payload.get("models") or {}
    task_model = _model(models.get("task_model"))
    if task_model is None:
        warnings.append("The run has no task model; set scorer.model before submitting.")
    metric_model = _model(models.get("metric_model"))
    if metric_model and task_model and metric_model.get("name") != task_model.get("name"):
        warnings.append(
            f"The metric's llm() used {metric_model['name']}; in a repository run it uses the task model "
            f"{task_model['name']}. Scores can drift."
        )
    reflection = _model(models.get("reflection_model"))
    if reflection is None:
        raise ConversionSkippedError("The job has no reflection model.")

    optimizer = payload.get("optimizer") or {}
    strategy, strategy_warning = map_strategy(optimizer.get("choice"))
    if strategy_warning:
        warnings.append(strategy_warning)
    budget = dict(optimizer.get("budget") or {})
    if budget.get("max_iterations") is not None and (
        strategy["mode"] != "single" or strategy.get("engine") not in BLACKBOX_ITERATION_LIMIT_ENGINES
    ):
        budget.pop("max_iterations")
        warnings.append("The iteration limit was dropped; the chosen strategy does not support it.")
    extra_models = [m for m in (_model(v) for v in models.get("extra_reflection_models") or []) if m]
    if extra_models and strategy["mode"] == "single" and strategy.get("engine") != BLACKBOX_ENGINE_SHINKA_EVOLVE:
        extra_models = []
        warnings.append("Extra optimization models were dropped; only ShinkaEvolve and Auto use them.")
    warnings.append("Repository runs split cases with their own seeded shuffle; the test cases can differ.")

    scorer: dict[str, Any] = {
        "kind": "python",
        "metric_code": SCORER_CODE,
        "timeout_seconds": float(workspace.get("metric_timeout_seconds") or 60.0),
    }
    if task_model is not None:
        scorer["model"] = task_model
    request: dict[str, Any] = {
        "name": name,
        "description": payload.get("description"),
        "username": job.get("username") or payload.get("username"),
        "objective": objective or None,
        "background": background or None,
        "recipe": "repo",
        "scorer": scorer,
        "cases": cases,
        "seed": payload.get("seed"),
        "budget": budget,
        "strategy": strategy,
        "target": {
            "kind": "repo",
            "repo": {
                "provider": "github",
                "repository": PLACEHOLDER_REPOSITORY,
                "editable_paths": [PROGRAM_DIR],
            },
        },
        "reflection_model_config": reflection,
        "extra_reflection_model_configs": extra_models,
        "token_source": payload.get("token_source") or "managed",
        "is_private": bool(payload.get("is_private")),
        "economy_mode": bool(payload.get("economy_mode")),
    }
    if optimizer.get("proposer"):
        request["proposer"] = dict(optimizer["proposer"])
    if optimizer.get("shinka"):
        request["shinka"] = dict(optimizer["shinka"])
    return {
        "job_id": job_id,
        "username": request["username"],
        "files": files,
        "request": request,
        "warnings": warnings,
    }


def plan_moves(plans: Iterable[dict[str, Any]], converted: dict[str, str]) -> list[dict[str, Any]]:
    """Plan how each continuous plan moves to its converted run.

    A plan follows its incumbent: the converted incumbent becomes both the
    new source and the new incumbent, and the baseline restarts with no tried
    models. Ended plans stay as they are, except plans the worker ended only
    because their run was a DSPy run: those move and start again.

    Args:
        plans: ``continuous_plans`` rows with ``id``, ``source_optimization_id``,
            ``incumbent_optimization_id``, ``status`` and ``pause_reason``.
        converted: Old job id to the new run id (or its placeholder).

    Returns:
        One entry per plan: ``{"plan_id", "action": "move"|"skip", "reason",
        "old_source", "old_incumbent", "from_job", "new_run", "restart"}``.
    """
    moves: list[dict[str, Any]] = []
    claimed: set[str] = set()
    for plan in plans:
        source = str(plan.get("source_optimization_id") or "")
        incumbent = str(plan.get("incumbent_optimization_id") or "") or source
        entry: dict[str, Any] = {
            "plan_id": str(plan.get("id")),
            "action": "skip",
            "reason": None,
            "old_source": source,
            "old_incumbent": incumbent,
            "from_job": None,
            "new_run": None,
            "restart": False,
        }
        moves.append(entry)
        retired = plan.get("status") == PLAN_ENDED and plan.get("pause_reason") == END_DSPY_RETIRED
        if plan.get("status") == PLAN_ENDED and not retired:
            entry["reason"] = "The plan ended."
            continue
        if incumbent not in converted:
            entry["reason"] = f"The incumbent job {incumbent} was not converted; convert it first."
            continue
        new_run = converted[incumbent]
        # continuous_plans.source_optimization_id is unique, so two plans
        # cannot both move onto one converted run.
        if new_run in claimed:
            entry["reason"] = f"Another plan already moves to the conversion of {incumbent}."
            continue
        claimed.add(new_run)
        entry.update(action="move", from_job=incumbent, new_run=new_run, restart=retired)
    return moves


def _sql_literal(value: str) -> str:
    """Quote text as a SQL string literal.

    Args:
        value: Any text.

    Returns:
        The quoted literal.
    """
    return "'" + value.replace("'", "''") + "'"


def plan_move_sql(moves: Sequence[dict[str, Any]]) -> str:
    """Render the reviewed SQL for the planned moves.

    Args:
        moves: Output of :func:`plan_moves`.

    Returns:
        One guarded ``UPDATE`` per move inside a transaction, or a comment
        when nothing moves.
    """
    rows = [move for move in moves if move["action"] == "move"]
    if not rows:
        return "-- No continuous plan moves.\n"
    lines = ["-- Review, replace every placeholder with the submitted run id, then run.", "BEGIN;"]
    for move in rows:
        snapshot = json.dumps({"incumbent": move["new_run"], "tried_models": []})
        # The worker ends a DSPy plan at its first check after the cut-over;
        # the converted run is a repository run, so that plan can check again.
        restart = " status = 'active', pause_reason = NULL, next_check_at = now()," if move.get("restart") else ""
        lines.append(
            "UPDATE continuous_plans"
            f" SET{restart} source_optimization_id = {_sql_literal(move['new_run'])},"
            f" incumbent_optimization_id = {_sql_literal(move['new_run'])},"
            f" baseline_snapshot = {_sql_literal(snapshot)}"
            f" WHERE id = {_sql_literal(move['plan_id'])}"
            f" AND source_optimization_id = {_sql_literal(move['old_source'])};"
        )
    lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


def repo_name(conversion: dict[str, Any]) -> str:
    """Build the GitHub repository name for a conversion.

    Args:
        conversion: Output of :func:`convert_job`.

    Returns:
        ``skynet-<name slug>-<job id prefix>``, at most 100 characters.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", str(conversion["request"].get("name") or "").lower()).strip("-")
    return f"skynet-{slug[:60] or 'run'}-{conversion['job_id'][:8]}".strip("-")


def load_jobs(engine: Any, usernames: Sequence[str], job_ids: Sequence[str]) -> list[dict[str, Any]]:
    """Read candidate jobs in a read-only transaction.

    Args:
        engine: SQLAlchemy engine.
        usernames: Owners to include; empty means all.
        job_ids: Jobs to include; empty means all.

    Returns:
        Unified jobs and legacy DSPy jobs, oldest first.
    """
    clauses = ["(payload->>'payload_kind' = :kind OR optimization_type IN ('run', 'grid_search'))"]
    params: dict[str, Any] = {"kind": UNIFIED_PAYLOAD_KIND}
    if usernames:
        clauses.append("username = ANY(:usernames)")
        params["usernames"] = list(usernames)
    if job_ids:
        clauses.append("optimization_id = ANY(:job_ids)")
        params["job_ids"] = list(job_ids)
    query = (
        "SELECT optimization_id, username, optimization_type, status, payload, result FROM jobs"
        f" WHERE {' AND '.join(clauses)} ORDER BY created_at"
    )
    with engine.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        rows = conn.execute(text(query), params).mappings().all()
    return [dict(row) for row in rows]


def load_plans(engine: Any, job_ids: Sequence[str]) -> list[dict[str, Any]]:
    """Read the continuous plans that start from or hold one of the jobs.

    Args:
        engine: SQLAlchemy engine.
        job_ids: Job ids to match.

    Returns:
        Plan rows.
    """
    if not job_ids:
        return []
    query = (
        "SELECT id, username, source_optimization_id, incumbent_optimization_id, status, pause_reason"
        " FROM continuous_plans"
        " WHERE source_optimization_id = ANY(:ids) OR incumbent_optimization_id = ANY(:ids)"
    )
    with engine.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        rows = conn.execute(text(query), {"ids": list(job_ids)}).mappings().all()
    return [dict(row) for row in rows]


def write_conversion(out: Path, conversion: dict[str, Any], report: dict[str, Any]) -> Path:
    """Write one conversion's repository files, request and report.

    Args:
        out: The ``--out`` folder.
        conversion: Output of :func:`convert_job`.
        report: The job's report.

    Returns:
        The job's folder.
    """
    folder = out / conversion["job_id"]
    for path, content in conversion["files"].items():
        target = folder / "repo" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    (folder / "request.json").write_text(json.dumps(conversion["request"], indent=2) + "\n", encoding="utf-8")
    (folder / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return folder


def reuse_repository(token: str, name: str, files: dict[str, str], *, message: str) -> CreatedRepository | None:
    """Reuse a repository that an earlier ``--apply`` created for the same job.

    The name holds the job id prefix, so a name conflict on a rerun means the
    first run created the repository. :func:`push_initial_commit` accepts the
    repository only when its default branch is empty or holds exactly ``files``.

    Args:
        token: GitHub token.
        name: Repository name from :func:`repo_name`.
        files: The converted repository files.
        message: First commit message, used only when the branch is empty.

    Returns:
        The repository and its pinned commit, or ``None`` when no repository has that name.

    Raises:
        RepoCreateError: When the repository holds other content.
    """
    found = existing_repository(token, name)
    if found is None:
        return None
    full_name, branch = found
    commit = push_initial_commit(full_name, branch, files, message, token)
    return CreatedRepository(full_name=full_name, default_branch=branch, commit=commit)


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser.

    Returns:
        The parser.
    """
    parser = argparse.ArgumentParser(description="Convert stored DSPy runs into repository runs.")
    parser.add_argument("--out", required=True, type=Path, help="Folder for the repo files, requests and reports.")
    parser.add_argument("--username", action="append", default=[], help="Only this owner's jobs (repeatable).")
    parser.add_argument("--job-id", action="append", default=[], help="Only this job (repeatable).")
    parser.add_argument("--apply", action="store_true", help="Create GitHub repositories and print plan-move SQL.")
    parser.add_argument(
        "--confirm-environment", help="Required with --apply: must equal APP_ENVIRONMENT of this database."
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    engine_factory: Callable[[], Any] | None = None,
    job_loader: Callable[[Any, Sequence[str], Sequence[str]], list[dict[str, Any]]] = load_jobs,
    plan_loader: Callable[[Any, Sequence[str]], list[dict[str, Any]]] = load_plans,
    token_for: Callable[[Any, str], str] = github_token,
    create_repo: Callable[..., Any] = create_repository_with_files,
    reuse_repo: Callable[..., Any] = reuse_repository,
) -> int:
    """Run the conversion CLI.

    Args:
        argv: Command-line arguments; ``None`` reads ``sys.argv``.
        engine_factory: Builds the database engine; defaults to ``DATABASE_URL``.
        job_loader: Reads candidate jobs.
        plan_loader: Reads continuous plans.
        token_for: Returns a user's GitHub token.
        create_repo: Creates a repository with files.
        reuse_repo: Returns the repository an earlier run created, or ``None``.

    Returns:
        The process exit code.
    """
    args = build_parser().parse_args(argv)
    if args.apply:
        environment = os.environ.get("APP_ENVIRONMENT", "").strip()
        if not args.confirm_environment:
            print("error: --apply needs --confirm-environment <name>.", file=sys.stderr)
            return 2
        if args.confirm_environment != environment:
            print(
                f"error: --confirm-environment {args.confirm_environment!r} does not match "
                f"APP_ENVIRONMENT {environment!r}.",
                file=sys.stderr,
            )
            return 2
    factory = engine_factory or (lambda: create_engine(os.environ.get("DATABASE_URL", DATABASE_URL_DEFAULT)))
    engine = factory()
    jobs = job_loader(engine, args.username, args.job_id)
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {"converted": [], "skipped": [], "repositories": [], "plan_moves": []}
    converted: dict[str, str] = {}
    for job in jobs:
        job_id = str(job.get("optimization_id"))
        try:
            conversion = convert_job(job)
        except ConversionSkippedError as reason:
            summary["skipped"].append({"job_id": job_id, "username": job.get("username"), "reason": str(reason)})
            continue
        report: dict[str, Any] = {
            "job_id": job_id,
            "username": conversion["username"],
            "status": job.get("status"),
            "warnings": conversion["warnings"],
            "repository": None,
        }
        new_run = NEW_RUN_PLACEHOLDER.format(job_id=job_id)
        if args.apply:
            name = repo_name(conversion)
            message = "Convert Skynet DSPy run to a repository"
            try:
                token = token_for(engine, str(conversion["username"]))
                try:
                    created = create_repo(
                        token,
                        name,
                        conversion["files"],
                        private=True,
                        description=f"Converted from Skynet run {job_id}",
                        message=message,
                    )
                except RepoCreateError:
                    created = reuse_repo(token, name, conversion["files"], message=message)
                    if created is None:
                        raise
            except RepoFetchError:
                report["warnings"].append("The owner has not linked GitHub; no repository was created.")
            except RepoCreateError as error:
                report["warnings"].append(f"Repository creation failed: {error}")
            else:
                repo = conversion["request"]["target"]["repo"]
                # Skynet created this repository, so runs push candidate branches to it, not a draft PR.
                repo.update(
                    repository=created.full_name,
                    branch=created.default_branch,
                    commit=created.commit,
                    created_by_skynet=True,
                )
                report["repository"] = created.full_name
                summary["repositories"].append({"job_id": job_id, "repository": created.full_name})
        write_conversion(out, conversion, report)
        converted[job_id] = new_run
        summary["converted"].append({"job_id": job_id, "username": conversion["username"]})
    moves = plan_moves(plan_loader(engine, list(converted)), converted)
    summary["plan_moves"] = moves
    sql = plan_move_sql(moves)
    (out / "plan_moves.sql").write_text(sql, encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(
        f"converted={len(summary['converted'])} skipped={len(summary['skipped'])} "
        f"repositories={len(summary['repositories'])} plan_moves="
        f"{sum(1 for m in moves if m['action'] == 'move')} out={out}"
    )
    if args.apply:
        print(sql, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
