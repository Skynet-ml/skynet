"""Private helpers shared between the optimizations route modules."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Any

from ....billing.budgets import BudgetService
from ....constants import PAYLOAD_OVERVIEW_NAME
from ...auth import AuthenticatedUser
from ...converters import parse_overview
from ...sharing_access import ShareRole
from .._helpers import filter_ids_at_least, get_job_no_payload
from .schemas import BulkMetadataRequest, BulkMetadataResponse, BulkMetadataSkipped

logger = logging.getLogger(__name__)

DASHBOARD_POLL_SECONDS = 3
JOB_STREAM_POLL_SECONDS = 2


async def stream_dashboard_snapshots(
    job_store,
    *,
    owner_filter: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield live dashboard snapshots as ``{event, data}`` dicts.

    Polls every pending/validating/running optimization every
    ``DASHBOARD_POLL_SECONDS`` seconds and yields one default (``message``)
    event per tick. When no optimizations are active, emits a terminal
    ``idle`` event and closes.

    Args:
        job_store: Job-store used to enumerate active optimizations.
        owner_filter: When set, restrict the stream to jobs owned by this
            username; ``None`` means show every active optimization
            (admin-scoped behaviour).

    Yields:
        ``{"event": "message"|"idle", "data": ...}`` dicts.
    """
    loop = asyncio.get_running_loop()

    def _fetch(status: str) -> list[dict]:
        """List up to 100 jobs in the given status from ``job_store``.

        Args:
            status: The job-store status bucket to enumerate.

        Returns:
            Raw ``job_store`` rows for the requested status (capped at 100).
        """
        return job_store.list_jobs(status=status, limit=100, username=owner_filter)

    while True:
        active_rows: list[dict] = []
        for s in ("pending", "validating", "running"):
            rows = await loop.run_in_executor(None, _fetch, s)
            active_rows.extend(rows)

        summaries = []
        for row in active_rows:
            overview = parse_overview(row)
            summaries.append(
                {
                    "optimization_id": row["optimization_id"],
                    "status": row.get("status", "pending"),
                    "name": overview.get(PAYLOAD_OVERVIEW_NAME),
                    "latest_metrics": row.get("latest_metrics", {}),
                    # list_jobs already folds these in via two aggregate queries
                    # (see RemoteDBJobStore._rows_with_counts), so the previous
                    # per-row COUNT round trips (2 per active job, every tick)
                    # were pure redundancy.
                    "log_count": row.get("log_count", 0),
                    "progress_count": row.get("progress_count", 0),
                }
            )

        yield {"event": "message", "data": {"active_jobs": summaries, "active_count": len(summaries)}}

        if len(summaries) == 0:
            yield {"event": "idle", "data": {"active_count": 0}}
            return

        await asyncio.sleep(DASHBOARD_POLL_SECONDS)


def execution_budget_view(job_store: Any, job_data: dict[str, Any]) -> dict[str, Any] | None:
    """Build the caller-safe execution budget snapshot for one job.

    Reads the live budget when the job has one and the store is database
    backed, and falls back to the copy frozen into the terminal evidence.

    Args:
        job_store: Job store, whose ``engine`` backs the budget service.
        job_data: Job fields holding ``execution_budget_id``, ``username``
            and ``terminal_evidence``.

    Returns:
        The budget fields without the owner and account balance, or ``None``
        when the job has no budget.
    """
    execution_budget = (job_data.get("terminal_evidence") or {}).get("execution_budget")
    if job_data.get("execution_budget_id") is not None and getattr(job_store, "engine", None) is not None:
        execution_budget = asdict(
            BudgetService(engine=job_store.engine).get(job_data["execution_budget_id"], job_data["username"])
        )
        execution_budget.pop("username", None)
        execution_budget.pop("account_available_cents", None)
    return execution_budget


async def stream_job_updates(job_store, optimization_id: str) -> AsyncIterator[dict[str, Any]]:
    """Yield live status updates for a single optimization.

    Polls the job every ``JOB_STREAM_POLL_SECONDS`` seconds and yields a
    default (``message``) snapshot event per tick. Emits a terminal ``done``
    event once the job reaches a terminal status, or an ``error`` event if
    the job vanishes mid-stream.

    Args:
        job_store: Job-store used to read the job snapshot.
        optimization_id: Optimization id to follow.

    Yields:
        ``{"event": "message"|"done"|"error", "data": ...}`` dicts.
    """
    loop = asyncio.get_running_loop()
    terminal = {"success", "failed", "cancelled", "stopped"}
    while True:
        # Narrow projection: the loop polls every few seconds and only needs
        # status/message/latest_metrics, never the multi-MB payload that
        # get_job materializes. get_job_status_fields raises KeyError (not None)
        # when the row is gone, so the not-found path is an except, not a guard.
        try:
            raw = await loop.run_in_executor(None, job_store.get_job_status_fields, optimization_id)
        except KeyError:
            yield {"event": "error", "data": {"error": "Optimization not found"}}
            return

        status = raw.get("status", "pending")
        log_count, progress_count, execution_budget = await asyncio.gather(
            loop.run_in_executor(None, job_store.get_log_count, optimization_id),
            loop.run_in_executor(None, job_store.get_progress_count, optimization_id),
            loop.run_in_executor(None, execution_budget_view, job_store, raw),
        )
        yield {
            "event": "message",
            "data": {
                "optimization_id": optimization_id,
                "status": status,
                "message": raw.get("message"),
                "latest_metrics": raw.get("latest_metrics", {}),
                "recovery": raw.get("recovery"),
                "terminal_evidence": raw.get("terminal_evidence"),
                "stop_reason": raw.get("stop_reason"),
                "result_availability": raw.get("result_availability"),
                "execution_budget": execution_budget,
                "log_count": log_count,
                "progress_count": progress_count,
            },
        }

        if status in terminal:
            yield {"event": "done", "data": {"status": status}}
            return

        await asyncio.sleep(JOB_STREAM_POLL_SECONDS)


def bulk_set_flag(
    job_store,
    req: BulkMetadataRequest,
    *,
    flag: str,
    user: AuthenticatedUser,
    minimum: ShareRole = ShareRole.editor,
) -> BulkMetadataResponse:
    """Shared bulk-metadata update used by ``/bulk-pin``.

    Walks ``req.optimization_ids``, setting ``overview[flag]`` to ``req.value``
    on each row the caller may edit. Duplicate ids are collapsed; unknown ids —
    and ids the caller's share role can't reach ``minimum`` on — are surfaced in
    ``skipped`` with ``reason="not_found"`` rather than raising. Members can
    pin/flag a shared run when their grant is at least ``minimum`` (editor).

    Args:
        job_store: Job-store the flag is written to.
        req: Bulk-metadata request body.
        flag: Overview key to toggle (e.g. ``"pinned"``).
        user: Authenticated caller.
        minimum: Lowest share role permitted to edit each row (pin is editor+).

    Returns:
        A ``BulkMetadataResponse`` listing successful and skipped ids.
    """
    updated: list[str] = []
    skipped: list[BulkMetadataSkipped] = []
    seen: set[str] = set()
    ordered: list[str] = []
    for oid in req.optimization_ids:
        if oid in seen:
            continue
        seen.add(oid)
        ordered.append(oid)
    allowed, denied = filter_ids_at_least(job_store, ordered, user, minimum)
    skipped.extend(BulkMetadataSkipped(optimization_id=oid, reason="not_found") for oid in denied)
    for oid in allowed:
        try:
            job_data = get_job_no_payload(job_store, oid)
        except KeyError:
            skipped.append(BulkMetadataSkipped(optimization_id=oid, reason="not_found"))
            continue
        overview = parse_overview(job_data)
        overview[flag] = bool(req.value)
        job_store.set_payload_overview(oid, overview)
        updated.append(oid)
    return BulkMetadataResponse(updated=updated, skipped=skipped)
