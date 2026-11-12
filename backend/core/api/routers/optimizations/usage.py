"""Route for the per-run usage and cost breakdown behind the run page's Usage tab."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends

from ....billing.run_usage import aggregate, billed_cents, from_result, load_records, serialize, with_rounding
from ....constants import (
    OPTIMIZATION_TYPE_BLACKBOX,
    PAYLOAD_OVERVIEW_MODEL_NAME,
    PAYLOAD_OVERVIEW_OPTIMIZATION_TYPE,
    PAYLOAD_OVERVIEW_REFLECTION_MODEL,
    PAYLOAD_OVERVIEW_TASK_MODEL,
    PAYLOAD_OVERVIEW_TOKEN_SOURCE,
    TOKEN_SOURCE_BYOK,
)
from ...auth import AuthenticatedUser, get_authenticated_user
from ...converters import parse_overview
from .._helpers import load_job_for_user
from ..constants import TERMINAL_STATUSES

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]


def run_usage(job_store: Any, job_data: dict[str, Any]) -> dict[str, Any]:
    """Build the usage breakdown of one run.

    Args:
        job_store: Job store whose ``engine`` holds the billing tables.
        job_data: The run's job row.

    Returns:
        ``{"rows", "settling", "proposer"}``; a run without a budget is read from its own result.
    """
    overview = parse_overview(job_data)
    blackbox = overview.get(PAYLOAD_OVERVIEW_OPTIMIZATION_TYPE) == OPTIMIZATION_TYPE_BLACKBOX
    budget_id = job_data.get("execution_budget_id")
    engine = getattr(job_store, "engine", None)
    records = load_records(engine, budget_id) if budget_id and engine is not None else []
    result = job_data.get("result") if isinstance(job_data.get("result"), dict) else {}
    metadata = result.get("optimization_metadata") if isinstance(result.get("optimization_metadata"), dict) else {}
    direct = metadata.get("direct_proposer_usage")
    rows = aggregate(
        records,
        proposer=blackbox,
        direct_usage=direct if isinstance(direct, list) else (),
    )
    if not budget_id:
        rows = [
            *from_result(
                result,
                task_model=overview.get(PAYLOAD_OVERVIEW_TASK_MODEL) or overview.get(PAYLOAD_OVERVIEW_MODEL_NAME),
                reflection_model=overview.get(PAYLOAD_OVERVIEW_REFLECTION_MODEL),
                proposer=blackbox,
                byok=overview.get(PAYLOAD_OVERVIEW_TOKEN_SOURCE) == TOKEN_SOURCE_BYOK,
            ),
            *rows,
        ]
    finished = job_data.get("status") in TERMINAL_STATUSES
    settling = finished and any(row.pending_calls for row in rows)
    if finished and not settling and records:
        rows = with_rounding(rows, billed_cents(engine, budget_id))
    return {
        "rows": serialize(rows),
        "settling": settling,
        "proposer": blackbox,
    }


def register_usage_routes(router: APIRouter, *, job_store) -> None:
    """Register ``GET /optimizations/{id}/usage``.

    Args:
        router: The router to attach the route to.
        job_store: Job store the run and its billing records are read from.
    """

    @router.get(
        "/optimizations/{optimization_id}/usage",
        summary="Usage and cost breakdown of one optimization",
        tags=["agent"],
    )
    async def get_usage(
        optimization_id: str,
        current_user: AuthenticatedUserDep,
    ) -> dict:
        """Return a run's charges and model activity, pre-aggregated per role, model, stage and candidate.

        The rows add up to what the run charged. Open to anyone the run is shared with.

        Args:
            optimization_id: Optimization id.
            current_user: Authenticated caller resolved from the bearer token.

        Returns:
            ``{"rows": [...], "settling": bool, "proposer": bool}``.

        Raises:
            DomainError: 404 when the run is unknown or not shared with the caller.
        """
        loop = asyncio.get_running_loop()
        job_data = await loop.run_in_executor(None, load_job_for_user, job_store, optimization_id, current_user)
        return await loop.run_in_executor(None, run_usage, job_store, job_data)
