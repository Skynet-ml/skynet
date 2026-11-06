"""Continuous optimization plans on a successful repository run.

* ``GET  /optimizations/{id}/continuous`` — the plan this run belongs to, its
  checks, this month's usage and the next scheduled check.
* ``POST /optimizations/{id}/continuous`` — enable a plan from a successful run.
* ``POST /continuous/{plan_id}/pause`` and ``/resume`` — the customer's pause.
* ``DELETE /continuous/{plan_id}`` — remove a plan and its check history; the
  runs it started stay.
* ``PUT  /continuous/{plan_id}/schedule`` — change when the plan checks.
* ``POST /continuous/schedule-preview`` — the next check times a rule gives.
* ``POST /continuous/{plan_id}/cycles/{cycle_id}/approve`` and ``/reject`` —
  review a validated candidate. Approval makes it the incumbent later checks
  start from; it deploys nothing.

A check starts a run only when a better model came out. There is
deliberately no endpoint that reruns the same models.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...billing.budgets import BudgetError, BudgetService
from ...models.common import OptimizationStatus
from ...storage.models import ContinuousCycleModel, ContinuousPlanModel
from ...worker import continuous as engine_mod
from ...worker.continuous_schedule import ScheduleRule, drop_past_slots, next_runs
from ...worker.repo_staging import is_repo_payload
from ..auth import AuthenticatedUser, get_authenticated_user
from ..converters import job_owner
from ..errors import DomainError
from ._helpers import get_job_no_payload

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]

_HISTORY_LIMIT = 50
_UPCOMING_CHECKS = 3
_IN_FLIGHT = frozenset(
    {OptimizationStatus.pending.value, OptimizationStatus.validating.value, OptimizationStatus.running.value}
)


# Which models a plan may switch on its own when a better one comes out.
class ContinuousPermitted(BaseModel):
    task_model: bool = False
    reflection_model: bool = False


# Enables continuous optimization on a successful run.
class ContinuousEnableRequest(BaseModel):
    schedule: ScheduleRule
    permitted: ContinuousPermitted
    # Both are optional: a run reuses the source run's budget, and no monthly
    # limit means only the wallet balance gates a run (we never buy credit).
    monthly_limit_cents: int | None = Field(default=None, gt=0, le=10_000_000)
    cycle_budget_cents: int | None = Field(default=None, gt=0, le=10_000_000)

    @model_validator(mode="after")
    def _check(self) -> ContinuousEnableRequest:
        """Reject a plan that can switch no model or cannot afford one cycle.

        Returns:
            The validated request.

        Raises:
            ValueError: On an inconsistent plan.
        """
        if not (self.permitted.task_model or self.permitted.reflection_model):
            raise ValueError("turn on at least one model switch")
        if (
            self.cycle_budget_cents is not None
            and self.monthly_limit_cents is not None
            and self.cycle_budget_cents > self.monthly_limit_cents
        ):
            raise ValueError("the cycle budget exceeds the monthly limit")
        return self


# One scheduled check and the run it started, if any.
class ContinuousCycleItem(BaseModel):
    id: str
    slot: str
    month: str
    status: str
    changes: list[str]
    detail_code: str | None = None
    detail_params: dict[str, Any] = Field(default_factory=dict)
    snapshot: dict[str, Any] | None = None
    optimization_id: str | None = None
    incumbent_score: float | None = None
    candidate_score: float | None = None
    spent_cents: int | None = None
    created_at: datetime


# This month's allowance as the plan sees it.
class ContinuousUsage(BaseModel):
    month: str
    limit_cents: int | None = None
    committed_cents: int
    resets_at: datetime


# A plan with its history.
class ContinuousPlanItem(BaseModel):
    id: str
    source_optimization_id: str
    incumbent_optimization_id: str
    status: str
    pause_reason: str | None = None
    schedule: ScheduleRule
    next_check_at: datetime | None = None
    upcoming_checks: list[datetime] = Field(default_factory=list)
    permitted: ContinuousPermitted
    cycle_budget_cents: int
    usage: ContinuousUsage
    cycles: list[ContinuousCycleItem]


# The next check times a schedule rule gives, earliest first.
class SchedulePreviewResponse(BaseModel):
    next_checks: list[datetime]


# ``plan`` is null when the run has none; ``eligible`` says whether one can start here.
# ``held_out`` is false when the run kept no test cases apart, so no plan can start from it.
class ContinuousStateResponse(BaseModel):
    plan: ContinuousPlanItem | None = None
    eligible: bool
    held_out: bool = True


def _is_rerunnable(job: dict[str, Any]) -> bool:
    """Say whether a job row is a run a check can copy: a repository run with inline cases.

    A repository run qualifies only with inline cases, since a check holds out
    the same test cases to compare its candidate with the incumbent. A DSPy run
    never qualifies, because Skynet no longer starts DSPy runs (SKY-316).

    Args:
        job: A job row with its payload.

    Returns:
        ``True`` for a repository run with inline cases.
    """
    payload = job.get("payload") or {}
    return is_repo_payload(payload) and bool(payload.get("cases"))


def _source_budget_cents(job_store, job: dict[str, Any], username: str) -> int | None:
    """Read the spending budget the source run started with.

    Args:
        job_store: Store whose engine holds the budgets.
        job: The source run, with its payload.
        username: Budget owner.

    Returns:
        The run's budget in cents, or ``None`` when it had none.
    """
    stated = (job.get("payload") or {}).get("max_cost_cents")
    if stated:
        return int(stated)
    budget_id = job.get("execution_budget_id") or (job.get("payload") or {}).get("execution_budget_id")
    if not budget_id:
        return None
    try:
        return BudgetService(engine=job_store.engine).get(budget_id, username).total_cents
    except BudgetError:
        return None


def _owned_job(job_store, optimization_id: str, user: AuthenticatedUser, *, payload: bool) -> dict[str, Any]:
    """Load a run the caller owns.

    Args:
        job_store: Job store.
        optimization_id: The run.
        user: The caller.
        payload: Whether to read the stored request.

    Returns:
        The job row.

    Raises:
        DomainError: 404 when the run is missing or not the caller's.
    """
    try:
        job = (
            job_store.get_job(optimization_id, include_payload=True)
            if payload
            else get_job_no_payload(job_store, optimization_id)
        )
    except KeyError as error:
        raise DomainError("optimization.not_found", status=404, optimization_id=optimization_id) from error
    if job_owner(job) != user.username.lower():
        raise DomainError("optimization.not_found", status=404, optimization_id=optimization_id)
    return job


def _owned_plan(session: Session, plan_id: str, user: AuthenticatedUser, *, lock: bool = False) -> ContinuousPlanModel:
    """Load a plan the caller owns.

    Args:
        session: Open session.
        plan_id: The plan.
        user: The caller.
        lock: Lock the plan row before reading it, so a check that writes it meanwhile waits.

    Returns:
        The plan row.

    Raises:
        DomainError: 404 when the plan is missing or not the caller's.
    """
    plan = session.get(ContinuousPlanModel, plan_id, with_for_update=lock)
    if plan is None or plan.username.lower() != user.username.lower():
        raise DomainError("continuous.not_found", status=404)
    return plan


def _plan_item(session: Session, plan: ContinuousPlanModel) -> ContinuousPlanItem:
    """Serialize a plan with its recent checks and this month's usage.

    Args:
        session: Open session.
        plan: The plan.

    Returns:
        The response item.
    """
    now = datetime.now(UTC)
    month = engine_mod.month_key(now)
    cycles = list(
        session.scalars(
            select(ContinuousCycleModel)
            # Most checks find no better model; listing them would push real runs out of the window.
            .where(ContinuousCycleModel.plan_id == plan.id, ContinuousCycleModel.status != engine_mod.CYCLE_SKIPPED)
            .order_by(ContinuousCycleModel.created_at.desc())
            .limit(_HISTORY_LIMIT)
        )
    )
    shown = {cycle.id for cycle in cycles}
    # A candidate waiting for approval must stay reviewable even after it ages out of the recent window.
    cycles += [
        cycle
        for cycle in session.scalars(
            select(ContinuousCycleModel).where(
                ContinuousCycleModel.plan_id == plan.id, ContinuousCycleModel.status == engine_mod.CYCLE_AWAITING_REVIEW
            )
        )
        if cycle.id not in shown
    ]
    return ContinuousPlanItem(
        id=plan.id,
        source_optimization_id=plan.source_optimization_id,
        incumbent_optimization_id=plan.incumbent_optimization_id,
        status=plan.status,
        pause_reason=plan.pause_reason,
        schedule=engine_mod.plan_rule(plan),
        next_check_at=engine_mod.aware(plan.next_check_at),
        upcoming_checks=_upcoming(plan),
        permitted=ContinuousPermitted.model_validate(engine_mod.upgrade_switches(plan.permitted or {})),
        cycle_budget_cents=plan.cycle_budget_cents,
        usage=ContinuousUsage(
            month=month,
            limit_cents=plan.monthly_limit_cents,
            committed_cents=engine_mod.month_committed(session, plan, month),
            resets_at=engine_mod.next_month_start(now),
        ),
        cycles=[
            ContinuousCycleItem(
                id=cycle.id,
                slot=cycle.slot,
                month=cycle.month,
                status=cycle.status,
                changes=list(cycle.changes or []),
                detail_code=cycle.detail_code,
                detail_params=cycle.detail_params or {},
                snapshot=cycle.snapshot,
                optimization_id=cycle.optimization_id,
                incumbent_score=cycle.incumbent_score,
                candidate_score=cycle.candidate_score,
                spent_cents=cycle.spent_cents,
                created_at=engine_mod.aware(cycle.created_at),
            )
            for cycle in cycles
        ],
    )


def _upcoming(plan: ContinuousPlanModel) -> list[datetime]:
    """List the plan's next few check times; none while it is paused.

    Args:
        plan: The plan.

    Returns:
        Up to three check times, earliest first.
    """
    first = engine_mod.aware(plan.next_check_at)
    if first is None:
        return []
    later = next_runs(
        engine_mod.plan_rule(plan), first, anchor=engine_mod.plan_anchor(plan), count=_UPCOMING_CHECKS - 1
    )
    return [first, *later]


def _set_schedule(plan: ContinuousPlanModel, rule: ScheduleRule, now: datetime) -> None:
    """Store a rule and, for a plan that still checks, its first check after now.

    The rule's intervals count from now, so "every 2 weeks" starts this week.
    A plan paused for its allowance or funds still checks, so it takes the
    new rule too; a customer's or a failure pause has no next check to move.

    Args:
        plan: The plan.
        rule: The new rule.
        now: Current time.
    """
    rule = drop_past_slots(rule, now)
    plan.schedule = rule.frequency
    plan.schedule_rule = {**rule.model_dump(), "anchor": now.isoformat()}
    if plan.status == engine_mod.PLAN_ACTIVE or (
        plan.status == engine_mod.PLAN_PAUSED and plan.pause_reason in engine_mod.SYSTEM_PAUSES
    ):
        plan.next_check_at = engine_mod.next_check_after(plan, now)
    plan.updated_at = now


def _plan_for_run(session: Session, optimization_id: str) -> ContinuousPlanModel | None:
    """Find the plan a run started, is the incumbent of, or was started by.

    Args:
        session: Open session.
        optimization_id: Any run.

    Returns:
        The plan, or ``None``.
    """
    plan = session.scalar(
        select(ContinuousPlanModel).where(
            or_(
                ContinuousPlanModel.source_optimization_id == optimization_id,
                ContinuousPlanModel.incumbent_optimization_id == optimization_id,
            )
        )
    )
    if plan is not None:
        return plan
    plan_id = session.scalar(
        select(ContinuousCycleModel.plan_id).where(ContinuousCycleModel.optimization_id == optimization_id)
    )
    return session.get(ContinuousPlanModel, plan_id) if plan_id else None


def create_continuous_router(*, job_store) -> APIRouter:
    """Build the continuous-optimization router.

    Args:
        job_store: Job store whose engine holds the plans.

    Returns:
        The router.
    """
    router = APIRouter()

    def _session() -> Session:
        """Open a session on the store's engine.

        Returns:
            A new session.

        Raises:
            DomainError: 503 for a store without a database.
        """
        engine = getattr(job_store, "engine", None)
        if engine is None:
            raise DomainError("continuous.unavailable", status=503)
        return Session(engine)

    @router.get(
        "/optimizations/{optimization_id}/continuous",
        response_model=ContinuousStateResponse,
        summary="Read a run's continuous optimization plan",
    )
    def get_continuous(optimization_id: str, current_user: AuthenticatedUserDep) -> ContinuousStateResponse:
        """Return the plan a run belongs to, or whether one can start from it.

        Args:
            optimization_id: Any run the caller owns.
            current_user: The caller.

        Returns:
            The plan state.
        """
        job = _owned_job(job_store, optimization_id, current_user, payload=True)
        with _session() as session:
            plan = _plan_for_run(session, optimization_id)
            if plan is not None:
                engine_mod.settle_waiting(plan, job["status"], datetime.now(UTC), held_out=engine_mod.has_held_out(job))
            session.commit()
            held_out = engine_mod.has_held_out(job)
            eligible = (
                plan is None and job["status"] == OptimizationStatus.success.value and _is_rerunnable(job) and held_out
            )
            return ContinuousStateResponse(
                plan=_plan_item(session, plan) if plan else None, eligible=eligible, held_out=held_out
            )

    @router.post(
        "/optimizations/{optimization_id}/continuous",
        response_model=ContinuousPlanItem,
        status_code=201,
        summary="Enable continuous optimization from a successful run",
    )
    def enable_continuous(
        optimization_id: str, body: ContinuousEnableRequest, current_user: AuthenticatedUserDep
    ) -> ContinuousPlanItem:
        """Create a plan that reuses this run's setup on a schedule.

        Args:
            optimization_id: A successful repository run the caller owns.
            body: Schedule, model switches and limits.
            current_user: The caller.

        Returns:
            The new plan.

        Raises:
            DomainError: 409 when the run did not succeed, is not a repository
                run with inline cases, keeps no test cases apart, or already has a plan.
        """
        job = _owned_job(job_store, optimization_id, current_user, payload=True)
        # A run still in flight takes a waiting plan, set up from the submit wizard.
        waiting = job["status"] in _IN_FLIGHT
        if (job["status"] != OptimizationStatus.success.value and not waiting) or not _is_rerunnable(job):
            raise DomainError("continuous.not_eligible", status=409)
        if not engine_mod.has_held_out(job):
            raise DomainError("continuous.no_test_split", status=409)
        cycle_budget = body.cycle_budget_cents or _source_budget_cents(job_store, job, current_user.username)
        if cycle_budget is None:
            raise DomainError("continuous.not_eligible", status=409)
        now = datetime.now(UTC)
        plan = ContinuousPlanModel(
            username=current_user.username,
            source_optimization_id=optimization_id,
            incumbent_optimization_id=optimization_id,
            status=engine_mod.PLAN_WAITING if waiting else engine_mod.PLAN_ACTIVE,
            permitted=body.permitted.model_dump(),
            baseline_snapshot={"incumbent": optimization_id, "tried_models": []},
            monthly_limit_cents=body.monthly_limit_cents,
            cycle_budget_cents=cycle_budget,
            created_at=now,
        )
        _set_schedule(plan, body.schedule, now)
        with _session() as session:
            if _plan_for_run(session, optimization_id) is not None:
                raise DomainError("continuous.already_enabled", status=409)
            session.add(plan)
            try:
                session.commit()
            except IntegrityError as error:
                raise DomainError("continuous.already_enabled", status=409) from error
            return _plan_item(session, plan)

    @router.post(
        "/continuous/{plan_id}/pause",
        response_model=ContinuousPlanItem,
        summary="Pause a continuous plan",
    )
    def pause_continuous(plan_id: str, current_user: AuthenticatedUserDep) -> ContinuousPlanItem:
        """Stop scheduled checks until the customer resumes them.

        Args:
            plan_id: The plan.
            current_user: The caller.

        Returns:
            The paused plan.
        """
        with _session() as session:
            plan = _owned_plan(session, plan_id, current_user)
            if plan.status in (engine_mod.PLAN_WAITING, engine_mod.PLAN_ENDED):
                raise DomainError("continuous.not_eligible", status=409)
            plan.status = engine_mod.PLAN_PAUSED
            plan.pause_reason = engine_mod.PAUSE_USER
            plan.next_check_at = None
            plan.updated_at = datetime.now(UTC)
            session.commit()
            return _plan_item(session, plan)

    @router.post(
        "/continuous/{plan_id}/resume",
        response_model=ContinuousPlanItem,
        summary="Resume a paused continuous plan",
    )
    def resume_continuous(plan_id: str, current_user: AuthenticatedUserDep) -> ContinuousPlanItem:
        """Resume checks; the next one is the schedule's first time after now.

        An allowance or funds limit still applies at that check.

        Args:
            plan_id: The plan.
            current_user: The caller.

        Returns:
            The resumed plan.
        """
        with _session() as session:
            plan = _owned_plan(session, plan_id, current_user)
            if plan.status in (engine_mod.PLAN_WAITING, engine_mod.PLAN_ENDED):
                raise DomainError("continuous.not_eligible", status=409)
            now = datetime.now(UTC)
            plan.status = engine_mod.PLAN_ACTIVE
            plan.pause_reason = None
            plan.next_check_at = engine_mod.next_check_after(plan, now)
            plan.updated_at = now
            session.commit()
            return _plan_item(session, plan)

    @router.delete(
        "/continuous/{plan_id}",
        status_code=204,
        summary="Delete a continuous plan",
    )
    def delete_continuous(plan_id: str, current_user: AuthenticatedUserDep) -> None:
        """Remove a plan and its check history in any state.

        The runs its checks started stay as ordinary runs. A check claimed at
        the same moment finds no plan and records nothing.

        Args:
            plan_id: The plan.
            current_user: The caller.
        """
        with _session() as session:
            plan = _owned_plan(session, plan_id, current_user)
            for cycle in session.scalars(select(ContinuousCycleModel).where(ContinuousCycleModel.plan_id == plan.id)):
                session.delete(cycle)
            session.delete(plan)
            session.commit()

    @router.put(
        "/continuous/{plan_id}/schedule",
        response_model=ContinuousPlanItem,
        summary="Change when a continuous plan checks",
    )
    def update_schedule(plan_id: str, body: ScheduleRule, current_user: AuthenticatedUserDep) -> ContinuousPlanItem:
        """Replace the plan's repeat rule; a paused plan stays paused.

        Args:
            plan_id: The plan.
            body: The new rule.
            current_user: The caller.

        Returns:
            The updated plan.
        """
        with _session() as session:
            plan = _owned_plan(session, plan_id, current_user)
            _set_schedule(plan, body, datetime.now(UTC))
            session.commit()
            return _plan_item(session, plan)

    @router.post(
        "/continuous/schedule-preview",
        response_model=SchedulePreviewResponse,
        summary="Preview the next check times of a schedule rule",
    )
    def preview_schedule(body: ScheduleRule, current_user: AuthenticatedUserDep) -> SchedulePreviewResponse:
        """Return the next three times a rule would check, counting from now.

        Args:
            body: The rule.
            current_user: The caller; only signed-in users may preview.

        Returns:
            The next check times.
        """
        now = datetime.now(UTC)
        return SchedulePreviewResponse(next_checks=next_runs(body, now, anchor=now, count=_UPCOMING_CHECKS))

    def _review(plan_id: str, cycle_id: str, user: AuthenticatedUser, *, approve: bool) -> ContinuousPlanItem:
        """Approve or reject a candidate that waits for review.

        Args:
            plan_id: The plan.
            cycle_id: The cycle with the candidate.
            user: The caller.
            approve: ``True`` to make the candidate the incumbent.

        Returns:
            The updated plan.

        Raises:
            DomainError: 409 when the cycle has no candidate awaiting review.
        """
        with _session() as session:
            # Locked before the cycle, in the worker's lock order, so a review the worker
            # records meanwhile is not overwritten.
            plan = _owned_plan(session, plan_id, user, lock=True)
            # Locked so a concurrent approve and reject can't both see ``awaiting_review``.
            cycle = session.get(ContinuousCycleModel, cycle_id, with_for_update=True)
            if cycle is None or cycle.plan_id != plan.id or cycle.status != engine_mod.CYCLE_AWAITING_REVIEW:
                raise DomainError("continuous.not_reviewable", status=409)
            now = datetime.now(UTC)
            if approve:
                plan.incumbent_optimization_id = cycle.optimization_id
                # The new version runs on the better models, so every model gets a fresh turn against it.
                plan.baseline_snapshot = {"incumbent": cycle.optimization_id, "tried_models": []}
                cycle.status = engine_mod.CYCLE_APPROVED
                cycle.detail_code = "continuous.review_approved"
            else:
                cycle.status = engine_mod.CYCLE_REJECTED
                cycle.detail_code = "continuous.review_rejected"
            cycle.updated_at = now
            plan.updated_at = now
            session.commit()
            return _plan_item(session, plan)

    @router.post(
        "/continuous/{plan_id}/cycles/{cycle_id}/approve",
        response_model=ContinuousPlanItem,
        summary="Approve a validated candidate as the new incumbent",
    )
    def approve_cycle(plan_id: str, cycle_id: str, current_user: AuthenticatedUserDep) -> ContinuousPlanItem:
        """Make a validated candidate the version later checks start from.

        Args:
            plan_id: The plan.
            cycle_id: The cycle awaiting review.
            current_user: The caller.

        Returns:
            The updated plan.
        """
        return _review(plan_id, cycle_id, current_user, approve=True)

    @router.post(
        "/continuous/{plan_id}/cycles/{cycle_id}/reject",
        response_model=ContinuousPlanItem,
        summary="Reject a candidate and keep the incumbent",
    )
    def reject_cycle(plan_id: str, cycle_id: str, current_user: AuthenticatedUserDep) -> ContinuousPlanItem:
        """Keep the incumbent and close the candidate's review.

        Args:
            plan_id: The plan.
            cycle_id: The cycle awaiting review.
            current_user: The caller.

        Returns:
            The updated plan.
        """
        return _review(plan_id, cycle_id, current_user, approve=False)

    return router
