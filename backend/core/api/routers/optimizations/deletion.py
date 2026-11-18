"""Hard-delete routes: single and bulk. [MIXED]

Public dev surface (in ``_SCALAR_PUBLIC_PATHS``):
- ``DELETE /optimizations/{id}`` — delete a single optimization.

Internal (dashboard plumbing, hidden from public docs):
- ``POST /optimizations/bulk-delete`` — multi-select bulk delete.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends

from ....i18n_en import t_en
from ....models import (
    BulkDeleteRequest,
    BulkDeleteResponse,
    BulkDeleteSkipped,
    JobDeleteResponse,
)
from ....storage.tree_purge import TreePurgeError
from ...auth import AuthenticatedUser, get_authenticated_user
from ...converters import status_to_job_status
from ...errors import DomainError
from ...sharing_access import ShareRole
from .._helpers import filter_ids_at_least, require_role_at_least
from ..constants import TERMINAL_STATUSES

logger = logging.getLogger(__name__)

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]


def register_deletion_routes(router: APIRouter, *, job_store) -> None:
    """Register hard-delete routes on ``router``.

    Args:
        router: The router to attach the delete routes to.
        job_store: Job-store the routes read from / mutate.
    """

    @router.delete(
        "/optimizations/{optimization_id}",
        response_model=JobDeleteResponse,
        status_code=200,
        summary="Permanently delete an optimization and all its data",
        tags=["agent"],
    )
    def delete_job(optimization_id: str, current_user: AuthenticatedUserDep) -> JobDeleteResponse:
        """Hard-delete an optimization and all its data (not recoverable).

        Only terminal optimizations can be deleted — cancel first if still
        active.

        Args:
            optimization_id: Optimization id to delete.
            current_user: Authenticated caller resolved from the bearer token.

        Returns:
            A ``JobDeleteResponse`` confirming the delete.

        Raises:
            DomainError: 404 if unknown or inaccessible, 403 if the caller is
                not an ``owner`` (admins and the creator qualify), 409 if still
                active, 503 if the run's stored tree data cannot be deleted now
                (the run stays, so a retry can delete it).
        """
        job_data, _role = require_role_at_least(job_store, optimization_id, current_user, ShareRole.owner)

        status = status_to_job_status(job_data.get("status", "pending"))
        if status not in TERMINAL_STATUSES:
            raise DomainError(
                "optimization.cannot_delete",
                status=409,
                params={"status": status.value},
            )

        try:
            job_store.delete_job(optimization_id)
        except TreePurgeError as exc:
            logger.exception("Optimization %s not deleted: its stored tree data was not deleted", optimization_id)
            raise DomainError("optimization.deletion_storage_unavailable", status=503) from exc
        logger.info("Optimization %s deleted", optimization_id)
        return JobDeleteResponse(optimization_id=optimization_id, deleted=True)

    @router.post(
        "/optimizations/bulk-delete",
        response_model=BulkDeleteResponse,
        status_code=200,
        summary="Delete many optimizations in a single request",
        tags=["agent"],
        # A POST that deletes: the verb understates the effect, so author the
        # destructive hint here (the MCP mount honours it over the method
        # default). See ``_annotations_for_route`` in ``core/api/mcp_mount.py``.
        openapi_extra={"x-mcp-annotations": {"destructiveHint": True}},
    )
    def bulk_delete_jobs(body: BulkDeleteRequest, current_user: AuthenticatedUserDep) -> BulkDeleteResponse:
        """Delete a batch of terminal optimizations and report per-id outcomes.

        Duplicate ids are deduplicated. Non-terminal, missing, or ids the
        caller is not an ``owner`` of are returned under ``skipped``; a bulk
        database failure reports every requested id as skipped rather than
        raising 500.

        Args:
            body: The bulk-delete request body.
            current_user: Authenticated caller resolved from the bearer token.

        Returns:
            A ``BulkDeleteResponse`` listing successful and skipped ids.
        """
        deleted: list[str] = []
        skipped: list[BulkDeleteSkipped] = []
        seen: set[str] = set()
        ordered_unique: list[str] = []
        for optimization_id in body.optimization_ids:
            if optimization_id in seen:
                continue
            seen.add(optimization_id)
            ordered_unique.append(optimization_id)

        if not ordered_unique:
            return BulkDeleteResponse(deleted=deleted, skipped=skipped)

        allowed, denied = filter_ids_at_least(job_store, ordered_unique, current_user, ShareRole.owner)
        skipped.extend(
            BulkDeleteSkipped(optimization_id=optimization_id, reason="not_found") for optimization_id in denied
        )

        if not allowed:
            return BulkDeleteResponse(deleted=deleted, skipped=skipped)

        status_by_id = job_store.get_jobs_status_by_ids(allowed)

        deletable: list[str] = []
        for optimization_id in allowed:
            raw_status = status_by_id.get(optimization_id)
            if raw_status is None:
                skipped.append(BulkDeleteSkipped(optimization_id=optimization_id, reason="not_found"))
                continue
            status = status_to_job_status(raw_status)
            if status not in TERMINAL_STATUSES:
                skipped.append(BulkDeleteSkipped(optimization_id=optimization_id, reason=status.value))
                continue
            deletable.append(optimization_id)

        if deletable:
            try:
                job_store.delete_jobs(deletable)
            except TreePurgeError:
                # The purge error names shard hosts and SQL, so the reason is the fixed message.
                logger.exception(
                    "Bulk delete of %d ids not done: their stored tree data was not deleted", len(deletable)
                )
                reason = t_en("optimization.deletion_storage_unavailable")
                skipped.extend(
                    BulkDeleteSkipped(optimization_id=optimization_id, reason=reason) for optimization_id in deletable
                )
            except Exception as exc:
                # Batch DB failure is reported as per-id skip reason instead of 500.
                logger.exception("Bulk delete failed for %d ids", len(deletable))
                skipped.extend(
                    BulkDeleteSkipped(
                        optimization_id=optimization_id,
                        reason=f"error: {exc}",
                    )
                    for optimization_id in deletable
                )
            else:
                deleted.extend(deletable)

        logger.info(
            "Bulk delete: %d deleted, %d skipped (requested %d)",
            len(deleted),
            len(skipped),
            len(body.optimization_ids),
        )
        return BulkDeleteResponse(deleted=deleted, skipped=skipped)
