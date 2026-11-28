"""Hard-delete routes: single and bulk. [MIXED]

Public dev surface (in ``_SCALAR_PUBLIC_PATHS``):
- ``DELETE /optimizations/{id}`` — delete a single optimization.

Internal (dashboard plumbing, hidden from public docs):
- ``POST /optimizations/bulk-delete`` — multi-select bulk delete.

On the desktop local stack, both routes also delete the kept folder pack of a
deleted local folder run when no stored run uses its commit (SKY-1005).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends

from ....config import settings
from ....i18n_en import t_en
from ....models import (
    BulkDeleteRequest,
    BulkDeleteResponse,
    BulkDeleteSkipped,
    JobDeleteResponse,
)
from ....storage.tree_purge import TreePurgeError
from ....worker.local_folder import LocalFolderError, forget_snapshot, is_local
from ...auth import AuthenticatedUser, get_authenticated_user
from ...converters import status_to_job_status
from ...errors import DomainError
from ...sharing_access import ShareRole
from .._helpers import filter_ids_at_least, require_role_at_least
from ..constants import TERMINAL_STATUSES

logger = logging.getLogger(__name__)

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]


def _local_repo(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the repository block of a local folder run.

    Args:
        payload: A run's payload, or only its ``target`` member.

    Returns:
        The ``target.repo`` block when it names a local folder, else ``None``.
    """
    target = payload.get("target") if isinstance(payload, dict) else None
    repo = target.get("repo") if isinstance(target, dict) else None
    return repo if is_local(repo) else None


def _pinned(repo: dict[str, Any]) -> str | None:
    """Return the commit a local folder run stores, if it stores one.

    Args:
        repo: The run's ``target.repo`` block.

    Returns:
        The commit, or ``None`` before the run pins it.
    """
    commit = repo.get("commit")
    return commit if isinstance(commit, str) and commit else None


def _local_commits(job_store, optimization_ids: list[str]) -> set[str]:
    """Read the commits of the local folder runs among the runs to delete.

    Args:
        job_store: Job-store the runs are read from.
        optimization_ids: The runs to delete.

    Returns:
        Their pinned commits; empty away from the desktop local stack, or when
        the read fails, so a pack is kept rather than a delete refused.
    """
    if not settings.local_mode:
        return set()
    try:
        payloads = job_store.payload_targets(optimization_ids)
    except Exception:
        logger.exception("Kept folder packs not checked: the runs to delete could not be read")
        return set()
    return {commit for payload in payloads.values() if (repo := _local_repo(payload)) and (commit := _pinned(repo))}


def _forget_unused_packs(job_store, commits: set[str]) -> None:
    """Delete the kept folder packs that no stored run uses after a delete.

    The run delete already succeeded, so a failure here only keeps a pack.

    Args:
        job_store: Job-store the remaining runs are read from.
        commits: The commits of the deleted local folder runs.
    """
    if not commits:
        return
    try:
        # One read sees every run at once; pages could skip a row that a parallel delete moves.
        limit = job_store.count_jobs() + 1
        while len(rows := job_store.list_jobs(limit=limit)) >= limit:
            limit *= 2
        statuses = {row["optimization_id"]: row.get("status") for row in rows}
        in_use: set[str] = set()
        for optimization_id, payload in job_store.payload_targets(list(statuses)).items():
            repo = _local_repo(payload)
            if repo is None:
                continue
            if commit := _pinned(repo):
                in_use.add(commit)
            elif status_to_job_status(statuses.get(optimization_id) or "pending") not in TERMINAL_STATUSES:
                # A starting run keeps its pack before it stores the commit, so any pack may be its pack.
                logger.info("Kept folder packs kept: run %s has not stored its commit yet", optimization_id)
                return
    except Exception:
        logger.exception("Kept folder packs kept: the stored runs could not be read")
        return
    for commit in commits - in_use:
        try:
            forget_snapshot(commit)
        except LocalFolderError:
            logger.exception("Kept folder pack %s not deleted", commit)


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

        commits = _local_commits(job_store, [optimization_id])
        try:
            job_store.delete_job(optimization_id)
        except TreePurgeError as exc:
            logger.exception("Optimization %s not deleted: its stored tree data was not deleted", optimization_id)
            raise DomainError("optimization.deletion_storage_unavailable", status=503) from exc
        logger.info("Optimization %s deleted", optimization_id)
        _forget_unused_packs(job_store, commits)
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
            commits = _local_commits(job_store, deletable)
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
                _forget_unused_packs(job_store, commits)

        logger.info(
            "Bulk delete: %d deleted, %d skipped (requested %d)",
            len(deleted),
            len(skipped),
            len(body.optimization_ids),
        )
        return BulkDeleteResponse(deleted=deleted, skipped=skipped)
