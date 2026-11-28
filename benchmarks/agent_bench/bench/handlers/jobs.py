"""Handlers for optimization jobs: listing, reads, lifecycle, bulk ops, submit.

Job state lives in ``world.s["jobs"]`` keyed by optimization id. Lifecycle
handlers mutate a job in place; ``submit_*`` and ``clone`` create new pending
jobs. Reads project the stored job through :func:`_common.job_summary` or return
the stored result/grid blocks.
"""

from __future__ import annotations

from typing import Any

from bench.fixtures import make_job
from bench.handlers._common import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    accessible_jobs,
    current_username,
    get_job,
    is_admin,
    job_summary,
    new_job_id,
    require_balance,
)
from bench.world import ToolError, World, tool


@tool("list_jobs_optimizations_get")
def list_jobs(w: World, args: dict[str, Any]) -> dict[str, Any]:
    """Return a page of the caller's optimizations, newest first.

    Args:
        w: The world.
        args: Optional ``status``, ``username``, ``optimization_type``, ``limit``,
            ``offset`` and ``include_shared``.

    Returns:
        ``{"items": [...summaries], "total": n, "limit": ..., "offset": ...}``.
    """
    jobs = accessible_jobs(w, include_shared=bool(args.get("include_shared", False)))
    status = args.get("status")
    username = args.get("username")
    otype = args.get("optimization_type")
    if status:
        jobs = [j for j in jobs if j["status"] == status]
    if username:
        jobs = [j for j in jobs if j.get("username", "").lower() == username.lower()]
    if otype:
        jobs = [j for j in jobs if j["optimization_type"] == otype]
    limit = min(int(args.get("limit", 25)), 50)
    offset = max(int(args.get("offset", 0)), 0)
    page = jobs[offset : offset + limit]
    return {
        "items": [job_summary(w, j) for j in page],
        "total": len(jobs),
        "limit": limit,
        "offset": offset,
    }


@tool("get_optimization_counts_optimizations_counts_get")
def get_optimization_counts(w: World, args: dict[str, Any]) -> dict[str, Any]:
    """Return optimization counts grouped by status for dashboard stat cards.

    Args:
        w: The world.
        args: Optional ``username`` and ``include_shared``.

    Returns:
        A dict with ``total``, a per-status count under ``by_status``, and the
        number of shared runs under ``shared`` when ``include_shared`` is set.
    """
    include_shared = bool(args.get("include_shared", False))
    jobs = accessible_jobs(w, include_shared=include_shared)
    username = args.get("username")
    if username:
        jobs = [j for j in jobs if j.get("username", "").lower() == username.lower()]
    by_status: dict[str, int] = {}
    for job in jobs:
        by_status[job["status"]] = by_status.get(job["status"], 0) + 1
    user = current_username(w).lower()
    shared = sum(
        1 for j in jobs if not is_admin(w) and j.get("username", "").lower() != user
    )
    return {
        "total": len(jobs),
        "by_status": by_status,
        "shared": shared if include_shared else 0,
    }


@tool("get_job_summary_optimizations")
def get_job_summary(w: World, args: dict[str, Any]) -> dict[str, Any]:
    """Return the compact dashboard-card shape for one optimization."""
    job = get_job(w, args.get("optimization_id"))
    return job_summary(w, job)


@tool("get_job_logs_optimizations")
def get_job_logs(w: World, args: dict[str, Any]) -> dict[str, Any]:
    """Return log lines for an optimization in chronological order.

    Args:
        w: The world.
        args: ``optimization_id`` and optional ``limit``, ``offset``, ``level``.

    Returns:
        ``{"optimization_id", "total", "returned", "entries": [...]}``.
    """
    job = get_job(w, args.get("optimization_id"))
    entries = list(job.get("logs") or [])
    level = args.get("level")
    if level:
        entries = [e for e in entries if e["level"].upper() == level.upper()]
    total = len(entries)
    offset = max(int(args.get("offset", 0)), 0)
    limit = min(int(args.get("limit", 25)), 50)
    page = entries[offset : offset + limit]
    return {
        "optimization_id": job["optimization_id"],
        "total": total,
        "returned": len(page),
        "entries": page,
    }


@tool("get_test_results_optimizations")
def get_test_results(w: World, args: dict[str, Any]) -> dict[str, Any]:
    """Return stored per-example baseline and optimized test scores.

    Raises:
        ToolError: 409 when the optimization has no stored per-example results.
    """
    job = get_job(w, args.get("optimization_id"))
    result = job.get("result")
    if not result or not result.get("optimized_test_results"):
        raise ToolError(
            409,
            f"Optimization {job['optimization_id']} has no test results (status: {job['status']})",
        )
    return {
        "optimization_id": job["optimization_id"],
        "metric_name": result.get("metric_name", "accuracy"),
        "split_counts": result.get("split_counts"),
        "baseline_test_metric": result.get("baseline_test_metric"),
        "optimized_test_metric": result.get("optimized_test_metric"),
        "baseline_test_results": result.get("baseline_test_results", []),
        "optimized_test_results": result.get("optimized_test_results", []),
    }


@tool("get_grid_search_result_optimizations")
def get_grid_search_result(w: World, args: dict[str, Any]) -> dict[str, Any]:
    """Return all pair results for a finished grid search, including ``best_pair``.

    Raises:
        ToolError: 409 when the optimization is not a grid search or has no
            stored pair results yet.
    """
    job = get_job(w, args.get("optimization_id"))
    if job["optimization_type"] != "grid_search":
        raise ToolError(
            409, f"Optimization {job['optimization_id']} is not a grid search"
        )
    grid = job.get("grid_result")
    if not grid:
        raise ToolError(
            409,
            f"Grid search {job['optimization_id']} has no results (status: {job['status']})",
        )
    return grid


@tool("get_pair_test_results_optimizations")
def get_pair_test_results(w: World, args: dict[str, Any]) -> dict[str, Any]:
    """Return per-example results for one grid-search pair.

    Raises:
        ToolError: 409 when not a grid search, 422 when ``pair_index`` is out of
            range, 409 when the pair failed and has no results.
    """
    job = get_job(w, args.get("optimization_id"))
    if job["optimization_type"] != "grid_search":
        raise ToolError(
            409, f"Optimization {job['optimization_id']} is not a grid search"
        )
    pair = _pair(job, args.get("pair_index"))
    if pair.get("error") or not pair.get("optimized_test_results"):
        raise ToolError(409, f"Pair {pair['pair_index']} has no test results")
    return {
        "optimization_id": job["optimization_id"],
        "pair_index": pair["pair_index"],
        "generation_model": pair["generation_model"],
        "reflection_model": pair["reflection_model"],
        "baseline_test_metric": pair["baseline_test_metric"],
        "optimized_test_metric": pair["optimized_test_metric"],
        "baseline_test_results": pair.get("baseline_test_results", []),
        "optimized_test_results": pair.get("optimized_test_results", []),
    }


@tool("cancel_job_optimizations", mutates=True)
def cancel_job(w: World, args: dict[str, Any]) -> dict[str, Any]:
    """Cooperatively cancel an active optimization.

    Raises:
        ToolError: 409 when the optimization is already in a terminal state.
    """
    job = get_job(w, args.get("optimization_id"), need="write")
    if job["status"] not in ACTIVE_STATUSES:
        raise ToolError(
            409,
            f"Optimization {job['optimization_id']} is not active (status: {job['status']})",
        )
    job["status"] = "cancelled"
    job["stop_reason"] = "cancelled"
    job["message"] = "Cancelled by user"
    job["pausable"] = False
    job["resumable"] = True
    job["completed_at"] = w.s["now"]
    return job_summary(w, job)


@tool("pause_job_optimizations", mutates=True)
def pause_job(w: World, args: dict[str, Any]) -> dict[str, Any]:
    """Suspend a running optimization at its last checkpoint so it can be resumed.

    Raises:
        ToolError: 409 when the optimization is not running.
    """
    job = get_job(w, args.get("optimization_id"), need="write")
    if job["status"] != "running":
        raise ToolError(
            409,
            f"Optimization {job['optimization_id']} is not running (status: {job['status']})",
        )
    job["status"] = "paused"
    job["stop_reason"] = "paused"
    job["pausable"] = False
    job["resumable"] = True
    return job_summary(w, job)


@tool("resume_job_optimizations", mutates=True)
def resume_job(w: World, args: dict[str, Any]) -> dict[str, Any]:
    """Resume a paused or budget-stopped optimization from its saved checkpoint.

    Raises:
        ToolError: 409 when the optimization is not resumable from a checkpoint.
    """
    job = get_job(w, args.get("optimization_id"), need="write")
    if job["status"] not in {"paused", "stopped"} or not job.get("resumable"):
        raise ToolError(
            409,
            f"Optimization {job['optimization_id']} cannot be resumed (status: {job['status']})",
        )
    job["status"] = "running"
    job["stop_reason"] = None
    job["message"] = None
    job["pausable"] = True
    job["resumable"] = False
    job["completed_at"] = None
    return job_summary(w, job)


@tool("retry_job_optimizations", mutates=True)
def retry_job(w: World, args: dict[str, Any]) -> dict[str, Any]:
    """Re-run a failed or cancelled optimization using the original payload.

    Raises:
        ToolError: 409 when the optimization is not failed or cancelled.
    """
    job = get_job(w, args.get("optimization_id"), need="write")
    if job["status"] not in {"failed", "cancelled"}:
        raise ToolError(
            409,
            f"Optimization {job['optimization_id']} cannot be retried (status: {job['status']})",
        )
    _reset_to_pending(w, job)
    return job_summary(w, job)


@tool("restart_job_optimizations", mutates=True)
def restart_job(w: World, args: dict[str, Any]) -> dict[str, Any]:
    """Reset a terminal optimization in place and re-run it from scratch.

    Raises:
        ToolError: 409 when the optimization is still active.
    """
    job = get_job(w, args.get("optimization_id"), need="write")
    if job["status"] not in TERMINAL_STATUSES:
        raise ToolError(
            409,
            f"Optimization {job['optimization_id']} is still active (status: {job['status']})",
        )
    _reset_to_pending(w, job)
    return job_summary(w, job)


@tool("rename_job_optimizations", mutates=True)
def rename_job(w: World, args: dict[str, Any]) -> dict[str, Any]:
    """Update the display name for an optimization.

    Raises:
        ToolError: 422 when the new name is empty or too long.
    """
    job = get_job(w, args.get("optimization_id"), need="write")
    name = args.get("name")
    if not name or not name.strip():
        raise ToolError(422, "name must not be empty")
    if len(name) > 200:
        raise ToolError(422, "name must be at most 200 characters")
    job["name"] = name
    return job_summary(w, job)


@tool("toggle_pin_job_optimizations", mutates=True)
def toggle_pin_job(w: World, args: dict[str, Any]) -> dict[str, Any]:
    """Flip the pinned flag for an optimization."""
    job = get_job(w, args.get("optimization_id"), need="write")
    job["pinned"] = not job.get("pinned", False)
    return job_summary(w, job)


@tool("clone_job_optimizations", mutates=True)
def clone_job(w: World, args: dict[str, Any]) -> dict[str, Any]:
    """Clone a finished or active optimization into ``count`` fresh pending runs.

    Args:
        w: The world.
        args: ``optimization_id``, optional ``count`` (1-5) and ``name_prefix``.

    Returns:
        ``{"clones": [...summaries]}`` for the newly created runs.
    """
    source = get_job(w, args.get("optimization_id"))
    count = int(args.get("count", 1))
    if not 1 <= count <= 5:
        raise ToolError(422, "count must be between 1 and 5")
    prefix = args.get("name_prefix") or "עותק של"
    clones = []
    for _ in range(count):
        oid = new_job_id(w)
        clone = dict(source)
        clone.update(
            optimization_id=oid,
            name=f"{prefix} {source.get('name') or source['optimization_id']}",
            username=current_username(w),
            grants={},
            pinned=False,
            status="pending",
            stop_reason=None,
            message=None,
            created_at=w.s["now"],
            started_at=None,
            completed_at=None,
            elapsed=None,
            elapsed_seconds=None,
            estimated_remaining=None,
            resumable=False,
            pausable=False,
            latest_metrics={},
            baseline_test_metric=None,
            optimized_test_metric=None,
            metric_improvement=None,
            result=None,
            grid_result=None,
            logs=[
                {
                    "timestamp": w.s["now"],
                    "level": "INFO",
                    "logger": "worker",
                    "message": f"Cloned from {source['optimization_id']}",
                    "pair_index": None,
                }
            ],
        )
        w.s["jobs"][oid] = clone
        clones.append(job_summary(w, clone))
    return {"clones": clones}


@tool("delete_job_optimizations", mutates=True)
def delete_job(w: World, args: dict[str, Any]) -> dict[str, Any]:
    """Hard-delete a terminal optimization and all its data (not recoverable).

    Raises:
        ToolError: 409 when the optimization is still active.
    """
    job = get_job(w, args.get("optimization_id"), need="write")
    if job["status"] not in TERMINAL_STATUSES:
        raise ToolError(
            409,
            f"Optimization {job['optimization_id']} is still active (status: {job['status']})",
        )
    del w.s["jobs"][job["optimization_id"]]
    return {"optimization_id": job["optimization_id"], "deleted": True}


@tool("bulk_cancel_jobs_optimizations_bulk_cancel_post", mutates=True)
def bulk_cancel(w: World, args: dict[str, Any]) -> dict[str, Any]:
    """Cancel a batch of non-terminal optimizations and report per-id outcomes."""
    return _bulk(w, args.get("optimization_ids") or [], _try_cancel)


@tool("bulk_delete_jobs_optimizations_bulk_delete_post", mutates=True)
def bulk_delete(w: World, args: dict[str, Any]) -> dict[str, Any]:
    """Delete a batch of terminal optimizations and report per-id outcomes."""
    return _bulk(w, args.get("optimization_ids") or [], _try_delete)


@tool("bulk_pin_jobs_optimizations_bulk_pin_post", mutates=True)
def bulk_pin(w: World, args: dict[str, Any]) -> dict[str, Any]:
    """Pin or unpin up to 100 optimizations in a single call.

    Raises:
        ToolError: 422 when no ids are given or more than 100 are supplied.
    """
    ids = args.get("optimization_ids") or []
    if not 1 <= len(ids) <= 100:
        raise ToolError(422, "optimization_ids must contain between 1 and 100 ids")
    value = bool(args.get("value"))

    def op(job: dict[str, Any]) -> None:
        job["pinned"] = value

    return _bulk(w, ids, lambda world, oid: _try_write(world, oid, op))


@tool("submit_job_run_post", mutates=True)
def submit_job_run(w: World, args: dict[str, Any]) -> dict[str, Any]:
    """Queue a single optimization run.

    Args:
        w: The world.
        args: Requires ``module_name``, ``optimizer_name``, ``column_mapping`` and
            ``model_config``, plus a dataset source (``dataset`` /
            ``staged_dataset_id`` / ``source_dataset_id``).

    Returns:
        The summary of the newly created pending run.

    Raises:
        ToolError: 422 for missing/unknown args, 402 when credits are empty.
    """
    _require(args, "module_name", "optimizer_name", "column_mapping", "model_config")
    _require_module(w, args["module_name"])
    _require_dataset(args)
    require_balance(w)
    model = (
        args["model_config"].get("name")
        if isinstance(args.get("model_config"), dict)
        else None
    )
    job = _new_job(
        w,
        args,
        optimization_type="run",
        module_name=args["module_name"],
        optimizer_name=args["optimizer_name"],
        column_mapping=args["column_mapping"],
        model_name=model,
        model_settings=args.get("model_config"),
        reflection_model_name=(args.get("reflection_model_config") or {}).get("name"),
    )
    return job_summary(w, job)


@tool("submit_grid_search_grid_search_post", mutates=True)
def submit_grid_search(w: World, args: dict[str, Any]) -> dict[str, Any]:
    """Queue a sweep over ``(generation_model, reflection_model)`` pairs.

    Raises:
        ToolError: 422 for missing/unknown args or an empty model grid, 402 when
            credits are empty.
    """
    _require(args, "module_name", "optimizer_name", "column_mapping")
    _require_module(w, args["module_name"])
    gen = args.get("generation_models") or []
    refl = args.get("reflection_models") or []
    use_all_gen = args.get("use_all_available_generation_models")
    use_all_refl = args.get("use_all_available_reflection_models")
    if not (gen or use_all_gen) or not (refl or use_all_refl):
        raise ToolError(
            422,
            "provide generation_models and reflection_models (or the use_all_available_* flags)",
        )
    require_balance(w)
    job = _new_job(
        w,
        args,
        optimization_type="grid_search",
        module_name=args["module_name"],
        optimizer_name=args["optimizer_name"],
        column_mapping=args["column_mapping"],
        model_name=None,
        model_settings=None,
        generation_models=gen,
        reflection_models=refl,
        total_pairs=(len(gen) or None) and (len(refl) or None) and len(gen) * len(refl),
    )
    return job_summary(w, job)


def _pair(job: dict[str, Any], pair_index: Any) -> dict[str, Any]:
    """Return the pair result at ``pair_index`` from a grid job, or raise 422."""
    pairs = (job.get("grid_result") or {}).get("pair_results") or []
    if not isinstance(pair_index, int) or not 0 <= pair_index < len(pairs):
        raise ToolError(
            422, f"pair_index {pair_index} is out of range (0-{len(pairs) - 1})"
        )
    return pairs[pair_index]


def _reset_to_pending(w: World, job: dict[str, Any]) -> None:
    """Reset a job's runtime fields back to a fresh pending state in place."""
    job.update(
        status="pending",
        stop_reason=None,
        message=None,
        completed_at=None,
        started_at=None,
        elapsed=None,
        elapsed_seconds=None,
        estimated_remaining=None,
        resumable=False,
        pausable=False,
        latest_metrics={},
        baseline_test_metric=None,
        optimized_test_metric=None,
        metric_improvement=None,
        result=None,
        grid_result=None,
        logs=[
            {
                "timestamp": w.s["now"],
                "level": "INFO",
                "logger": "worker",
                "message": "Re-queued",
                "pair_index": None,
            }
        ],
    )


def _new_job(w: World, args: dict[str, Any], **fields: Any) -> dict[str, Any]:
    """Create and store a new pending job seeded from a submit payload."""
    oid = new_job_id(w)
    job = make_job(
        optimization_id=oid,
        name=args.get("name") or "untitled run",
        description=args.get("description"),
        username=current_username(w),
        status="pending",
        created_at=w.s["now"],
        seed=args.get("seed", 42),
        shuffle=bool(args.get("shuffle", True)),
        split_fractions=args.get("split_fractions")
        or {"train": 0.7, "val": 0.15, "test": 0.15},
        source_dataset_id=args.get("source_dataset_id"),
        payload=dict(args),
        logs=[
            {
                "timestamp": w.s["now"],
                "level": "INFO",
                "logger": "worker",
                "message": "Run queued",
                "pair_index": None,
            }
        ],
        **fields,
    )
    w.s["jobs"][oid] = job
    return job


def _require(args: dict[str, Any], *keys: str) -> None:
    """Raise 422 when any required key is missing from ``args``."""
    missing = [k for k in keys if args.get(k) in (None, "", {}, [])]
    if missing:
        raise ToolError(422, f"missing required field(s): {', '.join(missing)}")


def _require_module(w: World, module_name: str) -> None:
    """Raise 422 when ``module_name`` is not a registered module."""
    if module_name not in w.s["registry"]["modules"]:
        raise ToolError(
            422,
            f"unknown module '{module_name}' (available: {', '.join(w.s['registry']['modules'])})",
        )


def _require_dataset(args: dict[str, Any]) -> None:
    """Raise 422 when no dataset source was supplied."""
    if not (
        args.get("dataset")
        or args.get("staged_dataset_id")
        or args.get("source_dataset_id")
    ):
        raise ToolError(
            422, "provide a dataset, staged_dataset_id, or source_dataset_id"
        )


def _bulk(w: World, ids: list[str], op: Any) -> dict[str, Any]:
    """Apply ``op`` to each id, collecting per-id success/error outcomes."""
    results = []
    succeeded = 0
    for oid in ids:
        try:
            op(w, oid)
            results.append({"optimization_id": oid, "ok": True})
            succeeded += 1
        except ToolError as exc:
            results.append(
                {
                    "optimization_id": oid,
                    "ok": False,
                    "error": exc.detail,
                    "status": exc.status,
                }
            )
    return {
        "requested": len(ids),
        "succeeded": succeeded,
        "failed": len(ids) - succeeded,
        "results": results,
    }


def _try_cancel(w: World, oid: str) -> None:
    """Cancel one job for a bulk operation."""
    job = get_job(w, oid, need="write")
    if job["status"] not in ACTIVE_STATUSES:
        raise ToolError(409, f"not active (status: {job['status']})")
    job.update(
        status="cancelled",
        stop_reason="cancelled",
        message="Cancelled by user",
        pausable=False,
        resumable=True,
        completed_at=w.s["now"],
    )


def _try_delete(w: World, oid: str) -> None:
    """Delete one terminal job for a bulk operation."""
    job = get_job(w, oid, need="write")
    if job["status"] not in TERMINAL_STATUSES:
        raise ToolError(409, f"still active (status: {job['status']})")
    del w.s["jobs"][oid]


def _try_write(w: World, oid: str, op: Any) -> None:
    """Resolve one job with write access and apply ``op`` for a bulk operation."""
    op(get_job(w, oid, need="write"))
