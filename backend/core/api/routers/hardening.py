"""The caller's metric-hardening jobs: start, read, review exploits, decide fixes, retry.

These routes only keep job state. The worker (SKY-360) runs the attack loop
from ``core.service_gateway.optimization.blackbox.hardening`` and writes each
step back to the same row. The hardening page polls GET while a job is in
preflight or running.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...billing.budgets import BudgetError, BudgetService
from ...config import settings
from ...local.cli_catalog import CLI_MODELS, parse_subscription_model_id
from ...models.blackbox import BlackboxScorer
from ...models.hardening import (
    ACTIVE_STATUSES,
    CreateHardeningJobRequest,
    HardeningFixDecisionRequest,
    HardeningJob,
    HardeningJobModel,
    HardeningVerdictRequest,
)
from ...service_gateway.optimization.blackbox.hardening import (
    ExploitNotFoundError,
    FixChecksFailedError,
    HardeningStateError,
    decide_fix,
    new_job,
    record_verdict,
    retry,
)
from ...worker.hardening_runner import finish_hardening_budget
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..model_catalog import require_known_model
from ._helpers import strip_api_key
from .execution_budgets import budget_http_error

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]


def _load(session: Session, username: str, job_id: str, *, lock: bool) -> HardeningJobModel:
    """Load one of the caller's jobs.

    Args:
        session: The open session.
        username: The caller.
        job_id: The job.
        lock: Whether to lock the row for a write.

    Returns:
        The row.

    Raises:
        DomainError: 404 when the caller has no such job.
    """
    query = select(HardeningJobModel).where(HardeningJobModel.id == job_id, HardeningJobModel.username == username)
    if lock:
        query = query.with_for_update()
    row = session.scalars(query).first()
    if row is None:
        raise DomainError("hardening.job_not_found", status=404)
    return row


def _require_model(model: str, *, role: str | None = None) -> None:
    """Reject a model that the job cannot run. Never swap it for another one.

    Args:
        model: The model id from the request.
        role: The role the model fills; ``judge`` also accepts the classifier-only models.

    Raises:
        DomainError: 422 when the id is not a catalog model, or names a
            subscription model its CLI cannot run.
    """
    subscription = parse_subscription_model_id(model) if settings.local_mode else None
    if subscription is not None:
        cli, slug = subscription
        if slug not in CLI_MODELS[cli]:
            raise DomainError("billing.subscription_unmapped_model", status=422, provider=cli)
        return
    require_known_model(model, role=role)


def _store(row: HardeningJobModel, job: HardeningJob) -> None:
    """Write a job into its row.

    Args:
        row: The row, changed in place.
        job: The job.
    """
    row.job = job.model_dump(mode="json")
    row.status = job.status
    row.updated_at = datetime.now(UTC)


def _stored_scorer(scorer: BlackboxScorer | None) -> dict[str, Any] | None:
    """Return the scorer as the job row keeps it, without any key or endpoint.

    The row and GET return the scorer as stored, so a key or a custom endpoint
    must never reach them. The worker resolves a BYOK key from the vault at
    run time, as the run submit does.

    Args:
        scorer: The scorer the create request sent.

    Returns:
        The scorer to store, or None.
    """
    if scorer is None:
        return None
    stored = scorer.model_copy(update={"secret": None}).model_dump(mode="json")
    model = stored.get("model")
    if isinstance(model, dict):
        # ``extra`` is a free dict, so only the shared scrubber catches every
        # credential shape (headers, provider-named keys, tokens).
        model = strip_api_key(model)
        model["base_url"] = None
        extra = model.get("extra") or {}
        for field in ("api_base", "base_url", "endpoint", "url"):
            extra.pop(field, None)
        stored["model"] = model
    return stored


def create_hardening_router(*, job_store) -> APIRouter:
    """Build the caller-scoped hardening router.

    Args:
        job_store: Job-store instance whose ORM engine keeps the jobs.

    Returns:
        A router with the hardening job operations.
    """
    router = APIRouter()

    def _change(username: str, job_id: str, step: Callable[[HardeningJob], HardeningJob]) -> HardeningJob:
        """Apply one user step to a locked job and store the result.

        Args:
            username: The caller.
            job_id: The job.
            step: The state change.

        Returns:
            The changed job.

        Raises:
            DomainError: 404 for an unknown job or exploit, 409 when the job
                cannot take the step now or another job of the draft is active.
        """
        with Session(job_store.engine) as session:
            row = _load(session, username, job_id, lock=True)
            try:
                job = step(HardeningJob.model_validate(row.job))
            except ExploitNotFoundError as exc:
                raise DomainError("hardening.exploit_not_found", status=404) from exc
            except FixChecksFailedError as exc:
                raise DomainError("hardening.fix_checks_failed", status=409) from exc
            except HardeningStateError as exc:
                raise DomainError("hardening.wrong_state", status=409) from exc
            _store(row, job)
            # A clean stop or a rejected fix ends the job here, not in the
            # worker, so its budget hold closes here (SKY-369).
            if (
                job.status not in ACTIVE_STATUSES
                and row.budget_operation_id is not None
                and finish_hardening_budget(
                    BudgetService(engine=job_store.engine), job, username, row.budget_operation_id
                )
            ):
                row.budget_operation_id = None
            try:
                session.commit()
            except IntegrityError as exc:
                # A retry makes the job active again; another job may already
                # be active on the same draft, and the partial unique index refuses it.
                raise DomainError("hardening.job_active", status=409) from exc
            return job

    @router.post(
        "/hardening/jobs",
        response_model=HardeningJob,
        status_code=201,
        operation_id="create_hardening_job",
        summary="Start a metric-hardening job for a setup draft",
        tags=["agent"],
    )
    def create_hardening_job(body: CreateHardeningJobRequest, user: AuthenticatedUserDep) -> HardeningJob:
        """Create a job that waits for its preflight.

        Args:
            body: The draft, rounds, round cap, models, view, repository target and budget.
            user: Authenticated caller.

        Returns:
            The new job.

        Raises:
            DomainError: 422 for an unknown model, 404 when the caller owns no
                such budget, 409 when the draft already has an unfinished job.
        """
        _require_model(body.attacker_model)
        _require_model(body.judge_model, role="judge")
        if body.binding_id is not None:
            # The same owner check a run submit makes on its execution budget.
            try:
                BudgetService(engine=job_store.engine).get(body.binding_id, user.username)
            except BudgetError as error:
                raise budget_http_error(error) from error
        with Session(job_store.engine) as session:
            active = session.scalars(
                select(HardeningJobModel.id)
                .where(
                    HardeningJobModel.username == user.username,
                    HardeningJobModel.draft_id == body.draft_id,
                    HardeningJobModel.status.in_(ACTIVE_STATUSES),
                )
                .limit(1)
            ).first()
            if active is not None:
                raise DomainError("hardening.job_active", status=409)
            job = new_job(
                attacker_model=body.attacker_model,
                judge_model=body.judge_model,
                place="local" if settings.local_mode else "cloud",
                rounds=body.rounds,
                round_cap_usd=body.round_cap_usd,
                view=body.view,
            ).model_copy(
                update={
                    "target": None if body.target is None else body.target.model_dump(mode="json"),
                    "binding_id": body.binding_id,
                    "scorer": _stored_scorer(body.scorer),
                }
            )
            row = HardeningJobModel(id=job.id, username=user.username, draft_id=body.draft_id)
            _store(row, job)
            row.created_at = row.updated_at
            session.add(row)
            try:
                session.commit()
            except IntegrityError as exc:
                # Two creates for one draft can both pass the SELECT above; the
                # partial unique index keeps only one of them.
                raise DomainError("hardening.job_active", status=409) from exc
            return job

    @router.get(
        "/hardening/jobs/{job_id}",
        response_model=HardeningJob,
        operation_id="get_hardening_job",
        summary="Read one metric-hardening job",
        tags=["agent"],
    )
    def get_hardening_job(job_id: str, user: AuthenticatedUserDep) -> HardeningJob:
        """Return one of the caller's jobs.

        Args:
            job_id: The job.
            user: Authenticated caller.

        Returns:
            The job.
        """
        with Session(job_store.engine) as session:
            return HardeningJob.model_validate(_load(session, user.username, job_id, lock=False).job)

    @router.get(
        "/hardening/jobs",
        response_model=HardeningJob | None,
        operation_id="get_latest_hardening_job",
        summary="Read the newest metric-hardening job of a setup draft",
        tags=["agent"],
    )
    def get_latest_hardening_job(
        user: AuthenticatedUserDep,
        draft_id: Annotated[str, Query(min_length=1, max_length=255, description="The setup draft.")],
    ) -> HardeningJob | None:
        """Return the caller's newest job for a draft, or null when there is none.

        Args:
            user: Authenticated caller.
            draft_id: The setup draft.

        Returns:
            The newest job, or None.
        """
        with Session(job_store.engine) as session:
            row = session.scalars(
                select(HardeningJobModel)
                .where(HardeningJobModel.username == user.username, HardeningJobModel.draft_id == draft_id)
                .order_by(HardeningJobModel.created_at.desc(), HardeningJobModel.id.desc())
                .limit(1)
            ).first()
            return None if row is None else HardeningJob.model_validate(row.job)

    @router.post(
        "/hardening/jobs/{job_id}/exploits/{exploit_id}/verdict",
        response_model=HardeningJob,
        operation_id="set_hardening_exploit_verdict",
        summary="Confirm or reject one exploit of a hardening round",
        tags=["agent"],
    )
    def set_hardening_exploit_verdict(
        job_id: str, exploit_id: str, body: HardeningVerdictRequest, user: AuthenticatedUserDep
    ) -> HardeningJob:
        """Store the caller's verdict on one exploit.

        Args:
            job_id: The job.
            exploit_id: The exploit of the current round.
            body: Whether the caller confirms it.
            user: Authenticated caller.

        Returns:
            The job after the verdict.
        """
        return _change(user.username, job_id, lambda job: record_verdict(job, exploit_id, confirmed=body.confirmed))

    @router.post(
        "/hardening/jobs/{job_id}/fix",
        response_model=HardeningJob,
        operation_id="decide_hardening_fix",
        summary="Approve or reject the drafted metric fix",
        tags=["agent"],
    )
    def decide_hardening_fix(
        job_id: str, body: HardeningFixDecisionRequest, user: AuthenticatedUserDep
    ) -> HardeningJob:
        """Store the caller's decision on the drafted fix.

        Args:
            job_id: The job.
            body: Whether the caller approves the fix.
            user: Authenticated caller.

        Returns:
            The job after the decision.
        """
        return _change(user.username, job_id, lambda job: decide_fix(job, approve=body.approve))

    @router.post(
        "/hardening/jobs/{job_id}/retry",
        response_model=HardeningJob,
        operation_id="retry_hardening_job",
        summary="Retry a failed, stale or rejected hardening job",
        tags=["agent"],
    )
    def retry_hardening_job(job_id: str, user: AuthenticatedUserDep) -> HardeningJob:
        """Put a failed, stale or rejected job back to work.

        Args:
            job_id: The job.
            user: Authenticated caller.

        Returns:
            The job, ready for the worker.
        """
        return _change(user.username, job_id, retry)

    return router
