"""Self-test for the benchmark world: coverage, success and error paths.

Run with ``python -m bench.selftest``. It asserts every tool in ``tools.json``
has a registered handler (and no handler is orphaned), calls every tool at least
once on a success path, exercises a representative error/lifecycle path per tool
group, and checks that every result is JSON-serialisable. It prints a one-line
summary and exits non-zero if anything failed.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any

from bench.fixtures import base_state
from bench.world import HANDLERS, World, load_handlers, tool_specs

GPT4O_MINI = "openrouter/openai/gpt-4o-mini"
CLAUDE_HAIKU = "openrouter/anthropic/claude-haiku-4.5"
CLAUDE_SONNET = "openrouter/anthropic/claude-sonnet-4.5"
GPT4O = "openrouter/openai/gpt-4o"


def _oid(n: int) -> str:
    """Return the seeded optimization id for job number ``n``."""
    return f"00000000-0000-4000-8000-{n:012d}"


MAPPING = {"inputs": {"text": "text"}, "outputs": {"label": "label"}}
SIG_CODE = "import dspy\n\nclass Classify(dspy.Signature):\n    text = dspy.InputField()\n    label = dspy.OutputField()\n"
METRIC_CODE = (
    "def metric(example, pred, trace=None):\n    return example.label == pred.label\n"
)
SCORER = {
    "kind": "python",
    "metric_code": "def score(candidate, case):\n    return 1.0\n",
}
MODEL_CFG = {"name": GPT4O_MINI}


def _success_cases() -> list[tuple[str, dict[str, Any]]]:
    """Return one succeeding ``(tool, args)`` case for every tool."""
    return [
        ("list_jobs_optimizations_get", {}),
        ("get_optimization_counts_optimizations_counts_get", {"include_shared": True}),
        ("get_job_summary_optimizations", {"optimization_id": _oid(1)}),
        ("get_job_logs_optimizations", {"optimization_id": _oid(1)}),
        ("get_test_results_optimizations", {"optimization_id": _oid(1)}),
        ("get_grid_search_result_optimizations", {"optimization_id": _oid(11)}),
        (
            "get_pair_test_results_optimizations",
            {"optimization_id": _oid(11), "pair_index": 3},
        ),
        ("cancel_job_optimizations", {"optimization_id": _oid(7)}),
        ("pause_job_optimizations", {"optimization_id": _oid(7)}),
        ("resume_job_optimizations", {"optimization_id": _oid(8)}),
        ("retry_job_optimizations", {"optimization_id": _oid(4)}),
        ("restart_job_optimizations", {"optimization_id": _oid(1)}),
        ("rename_job_optimizations", {"optimization_id": _oid(1), "name": "renamed"}),
        ("toggle_pin_job_optimizations", {"optimization_id": _oid(1)}),
        ("clone_job_optimizations", {"optimization_id": _oid(1), "count": 2}),
        ("delete_job_optimizations", {"optimization_id": _oid(4)}),
        (
            "bulk_cancel_jobs_optimizations_bulk_cancel_post",
            {"optimization_ids": [_oid(7)]},
        ),
        (
            "bulk_delete_jobs_optimizations_bulk_delete_post",
            {"optimization_ids": [_oid(4)]},
        ),
        (
            "bulk_pin_jobs_optimizations_bulk_pin_post",
            {"optimization_ids": [_oid(1)], "value": True},
        ),
        (
            "submit_job_run_post",
            {
                "module_name": "cot",
                "optimizer_name": "gepa",
                "column_mapping": MAPPING,
                "model_config": MODEL_CFG,
                "source_dataset_id": "ds_support_tickets",
                "name": "new run",
            },
        ),
        (
            "submit_grid_search_grid_search_post",
            {
                "module_name": "cot",
                "optimizer_name": "gepa",
                "column_mapping": MAPPING,
                "generation_models": [{"name": CLAUDE_HAIKU}],
                "reflection_models": [{"name": GPT4O}],
                "source_dataset_id": "ds_support_tickets",
            },
        ),
        ("get_analytics_summary_analytics_summary_get", {}),
        ("get_model_stats_analytics_models_get", {}),
        ("get_optimizer_stats_analytics_optimizers_get", {}),
        ("get_wallet_for_agent", {}),
        ("list_models_for_agent", {"query": "claude"}),
        (
            "discover_models_models_discover_post",
            {"base_url": "https://litellm.internal:4000"},
        ),
        ("get_registry_snapshot_registry_get", {}),
        ("update_user_preferences", {"advanced_mode": True}),
        ("list_datasets_for_agent", {}),
        ("list_sample_datasets_datasets_samples_get", {}),
        ("stage_sample_dataset_datasets_samples", {"sample_id": "sentiment-he"}),
        (
            "profile_datasets_profile_post",
            {
                "column_mapping": MAPPING,
                "dataset": [{"text": "a", "label": "x"}, {"text": "b", "label": "y"}],
            },
        ),
        (
            "validate_datasets_validate_post",
            {"row_count": 120, "fractions": {"train": 0.7, "val": 0.15, "test": 0.15}},
        ),
        (
            "set_column_roles_datasets_column_roles_post",
            {
                "dataset_columns": ["text", "label"],
                "column_roles": {"text": "input", "label": "output"},
            },
        ),
        ("list_tagging_sessions_for_agent", {}),
        ("request_user_dataset_datasets_request_upload_post", {"prompt": "upload"}),
        ("request_user_dataset_from_library", {"prompt": "pick"}),
        ("update_wizard_state", {"job_name": "wizard job"}),
        (
            "validate_code_validate_code_post",
            {
                "column_mapping": MAPPING,
                "signature_code": SIG_CODE,
                "metric_code": METRIC_CODE,
            },
        ),
        ("request_code_authoring", {"goal": "write a metric"}),
        ("blackbox_engines_blackbox_engines_get", {"target": "prompt"}),
        (
            "blackbox_scorer_dry_run_blackbox_scorer_dry_run_post",
            {"scorer": SCORER, "candidate": "hello"},
        ),
        (
            "submit_blackbox_run_blackbox_run_post",
            {
                "objective": "opener",
                "scorer": SCORER,
                "reflection_model_config": {"name": CLAUDE_SONNET},
                "strategy": {"mode": "single", "engine": "gepa"},
            },
        ),
        (
            "public_search_dashboard_search_post",
            {"query": "classifier", "sort": "relevance"},
        ),
    ]


def _error_cases() -> list[tuple[str, dict[str, Any], int, str]]:
    """Return ``(tool, args, expected_status, label)`` error/lifecycle cases."""
    return [
        (
            "get_job_summary_optimizations",
            {"optimization_id": "does-not-exist"},
            404,
            "unknown id",
        ),
        (
            "get_job_summary_optimizations",
            {"optimization_id": _oid(17)},
            404,
            "other user's private run",
        ),
        ("get_job_summary_optimizations", {}, 422, "missing id"),
        (
            "cancel_job_optimizations",
            {"optimization_id": _oid(1)},
            409,
            "cancel terminal job",
        ),
        (
            "resume_job_optimizations",
            {"optimization_id": _oid(1)},
            409,
            "resume non-resumable",
        ),
        (
            "retry_job_optimizations",
            {"optimization_id": _oid(1)},
            409,
            "retry a success",
        ),
        (
            "delete_job_optimizations",
            {"optimization_id": _oid(7)},
            409,
            "delete active job",
        ),
        (
            "rename_job_optimizations",
            {"optimization_id": _oid(16), "name": "x"},
            403,
            "viewer writes shared job",
        ),
        (
            "rename_job_optimizations",
            {"optimization_id": _oid(1), "name": "   "},
            422,
            "empty name",
        ),
        (
            "get_test_results_optimizations",
            {"optimization_id": _oid(9)},
            409,
            "no results on pending",
        ),
        (
            "get_grid_search_result_optimizations",
            {"optimization_id": _oid(1)},
            409,
            "not a grid search",
        ),
        (
            "get_pair_test_results_optimizations",
            {"optimization_id": _oid(11), "pair_index": 99},
            422,
            "pair index out of range",
        ),
        (
            "submit_job_run_post",
            {
                "optimizer_name": "gepa",
                "column_mapping": MAPPING,
                "model_config": MODEL_CFG,
                "source_dataset_id": "ds_support_tickets",
            },
            422,
            "missing module_name",
        ),
        (
            "submit_job_run_post",
            {
                "module_name": "nope",
                "optimizer_name": "gepa",
                "column_mapping": MAPPING,
                "model_config": MODEL_CFG,
                "source_dataset_id": "ds_support_tickets",
            },
            422,
            "unknown module",
        ),
        (
            "submit_grid_search_grid_search_post",
            {"module_name": "cot", "optimizer_name": "gepa", "column_mapping": MAPPING},
            422,
            "empty model grid",
        ),
        (
            "submit_blackbox_run_blackbox_run_post",
            {"objective": "x", "scorer": SCORER},
            422,
            "missing reflection model",
        ),
        (
            "submit_blackbox_run_blackbox_run_post",
            {
                "objective": "x",
                "scorer": SCORER,
                "reflection_model_config": {"name": CLAUDE_SONNET},
                "strategy": {"mode": "single", "engine": "autosaddler"},
            },
            422,
            "unavailable engine",
        ),
        (
            "blackbox_scorer_dry_run_blackbox_scorer_dry_run_post",
            {"scorer": {}, "candidate": "x"},
            422,
            "missing scorer body",
        ),
        ("update_user_preferences", {"nonsense_key": 1}, 422, "unknown preference"),
        ("update_wizard_state", {"nonsense_field": 1}, 422, "unknown wizard field"),
        (
            "set_column_roles_datasets_column_roles_post",
            {"dataset_columns": ["text"], "column_roles": {"missing": "input"}},
            422,
            "role on unknown column",
        ),
        (
            "stage_sample_dataset_datasets_samples",
            {"sample_id": "nope"},
            404,
            "unknown sample",
        ),
    ]


def _fresh_world(mutator: Callable[[dict[str, Any]], None] | None = None) -> World:
    """Return a freshly built world, optionally mutating the state first."""
    state = base_state()
    if mutator:
        mutator(state)
    return World(state)


def _run_success(results: dict[str, int], failures: list[str]) -> None:
    """Run every success case on a fresh world, checking JSON-serialisability."""
    for name, args in _success_cases():
        world = _fresh_world()
        try:
            result = world.call(name, args)
            json.dumps(result)
            results["success"] += 1
        except Exception as exc:
            failures.append(f"success[{name}]: {type(exc).__name__}: {exc}")


def _run_errors(results: dict[str, int], failures: list[str]) -> None:
    """Run every error case, asserting the expected HTTP-like status is raised."""
    from bench.world import ToolError

    for name, args, status, label in _error_cases():
        world = _fresh_world()
        try:
            world.call(name, args)
            failures.append(
                f"error[{name}:{label}]: expected {status} but call succeeded"
            )
        except ToolError as exc:
            if exc.status == status:
                results["error"] += 1
            else:
                failures.append(
                    f"error[{name}:{label}]: expected {status}, got {exc.status}"
                )
        except Exception as exc:
            failures.append(
                f"error[{name}:{label}]: unexpected {type(exc).__name__}: {exc}"
            )


def _run_balance_gate(results: dict[str, int], failures: list[str]) -> None:
    """Assert an empty wallet makes a submit fail with 402."""
    from bench.world import ToolError

    def empty(state: dict[str, Any]) -> None:
        state["wallet"]["paid_balance_cents"] = 0
        state["wallet"]["free_grant"]["cents_remaining"] = 0

    world = _fresh_world(empty)
    args = {
        "module_name": "cot",
        "optimizer_name": "gepa",
        "column_mapping": MAPPING,
        "model_config": MODEL_CFG,
        "source_dataset_id": "ds_support_tickets",
    }
    try:
        world.call("submit_job_run_post", args)
        failures.append("credit-gate: expected 402 but submit succeeded")
    except ToolError as exc:
        if exc.status == 402:
            results["error"] += 1
        else:
            failures.append(f"credit-gate: expected 402, got {exc.status}")


def main() -> int:
    """Run all checks and return a process exit code (0 pass, 1 fail)."""
    load_handlers()
    spec_names = {t["name"] for t in tool_specs()}
    handler_names = set(HANDLERS)
    failures: list[str] = []

    missing = spec_names - handler_names
    orphaned = handler_names - spec_names
    if missing:
        failures.append(f"tools with no handler: {sorted(missing)}")
    if orphaned:
        failures.append(f"handlers with no tool spec: {sorted(orphaned)}")

    covered = {name for name, _ in _success_cases()}
    uncovered = spec_names - covered
    if uncovered:
        failures.append(f"tools with no success case: {sorted(uncovered)}")

    results = {"success": 0, "error": 0}
    _run_success(results, failures)
    _run_errors(results, failures)
    _run_balance_gate(results, failures)

    ok = not failures
    print(
        f"selftest: {'PASS' if ok else 'FAIL'} | tools={len(spec_names)} handlers={len(handler_names)} "
        f"success_calls={results['success']} error_calls={results['error']} failures={len(failures)}"
    )
    for line in failures:
        print(f"  - {line}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
