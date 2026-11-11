"""Continuous optimization: scheduled checks that try a better model when one comes out.

A plan is enabled from one successful repository run. On each scheduled
check the plan looks for a better reflection model, starting from the current
version's own model. A better model is rated higher and costs no more for
input or output. When there is none, the check is recorded as skipped and
nothing is spent. When there is one, the check starts exactly one run through
the same admission, preflight and protected budget path as ``/blackbox/run``:
the current version's best patch on the same pinned commit, with its cases,
scorer and split seed, so it holds out the same test cases and its test score
compares with the current version's saved test score. A candidate that beats
the current version waits for the customer's approval; nothing is ever
deployed automatically.

Skynet no longer starts DSPy runs, so a plan whose current version is a DSPy
run skips its check and ends with the reason ``dspy_retired``.

Each check spends against a monthly allowance and the existing wallet only.
A new month resets the allowance, keeps every recorded cycle and its spend,
leaves in-flight budgets untouched, never buys credit, and never lifts a
customer's own pause.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..api.auth import AuthenticatedUser
from ..api.errors import DomainError
from ..api.model_catalog import CatalogModel, _reasoning_match_key, get_catalog_cached
from ..api.observability import advisory_lock
from ..api.routers.billing import _byok_catalog_for_user
from ..api.routers.submissions import _BLACKBOX_WORKFLOW, submit_repo_payload
from ..api.submission_idempotency import resolve_submission_replay_keys
from ..billing import ProviderKeyVault, byok_provider_for_litellm, provider_slug_for_model
from ..billing.budget_amounts import budget_wallet_hold
from ..billing.budgets import BudgetError, BudgetService
from ..billing.operation_pricing import json_fingerprint
from ..billing.protected_credentials import scrub_execution_credentials
from ..billing.service import StripeBillingService
from ..constants import PAYLOAD_OVERVIEW_NAME
from ..models import BlackboxRunRequest
from ..models.common import OptimizationStatus
from ..service_gateway.optimization.blackbox.service import legacy_case_splits
from ..storage.models import ContinuousCycleModel, ContinuousPlanModel, ExecutionBudgetModel
from .continuous_schedule import ScheduleRule, legacy_rule, next_runs
from .repo_staging import is_repo_payload

logger = logging.getLogger(__name__)

CYCLE_LEASE = timedelta(minutes=30)
# A minute keeps a check within a minute of the time the owner picked.
SWEEP_INTERVAL_SECONDS = 60.0
# Distinct from every other sweeper's advisory lock key in core.api.observability.
CONTINUOUS_SWEEP_LOCK_KEY = 7_412_093_551

PLAN_ACTIVE = "active"
PLAN_PAUSED = "paused"
# A plan set up in the submit wizard waits for its first run, and starts once that run succeeds.
PLAN_WAITING = "waiting"
PAUSE_USER = "user"
PAUSE_ALLOWANCE = "allowance"
PAUSE_FUNDS = "funds"
# Checks kept failing; only the customer resumes, so a broken source stops costing checks.
PAUSE_FAILING = "failing"
# An ended plan whose first run kept no test cases apart, so no check could ever replace it.
END_NO_TEST_SPLIT = "no_test_split"
# An ended plan whose current version is a DSPy run; Skynet no longer starts DSPy runs (SKY-316).
END_DSPY_RETIRED = "dspy_retired"
SYSTEM_PAUSES = frozenset({PAUSE_ALLOWANCE, PAUSE_FUNDS})
# A waiting plan whose first run did not succeed; kept so the owner sees why it never started.
PLAN_ENDED = "ended"
# This many failed checks in a row pause the plan.
FAILURE_STREAK = 3
# A check that lost its worker this many times is closed as failed instead of retried again.
MAX_CHECK_ATTEMPTS = 3
# A candidate must beat the incumbent by this much on a 0..1 score, so scoring noise cannot win.
WIN_MARGIN = 0.01

CYCLE_CHECKING = "checking"
CYCLE_SKIPPED = "skipped"
CYCLE_FAILED = "failed"
CYCLE_PAUSED = "paused"
CYCLE_OPTIMIZING = "optimizing"
CYCLE_AWAITING_REVIEW = "awaiting_review"
CYCLE_REGRESSED = "regressed"
CYCLE_APPROVED = "approved"
CYCLE_REJECTED = "rejected"

_TERMINAL_JOB = {
    OptimizationStatus.success.value,
    OptimizationStatus.failed.value,
    OptimizationStatus.cancelled.value,
    OptimizationStatus.stopped.value,
}


def aware(moment: datetime | None) -> datetime | None:
    """Treat a naive database timestamp as UTC.

    Args:
        moment: A timestamp as read back from the database.

    Returns:
        The same instant with a UTC timezone, or ``None``.
    """
    if moment is None or moment.tzinfo is not None:
        return moment
    return moment.replace(tzinfo=UTC)


def month_key(moment: datetime) -> str:
    """Name the monthly allowance a moment falls in.

    Args:
        moment: Any timestamp.

    Returns:
        ``YYYY-MM`` in UTC.
    """
    return moment.astimezone(UTC).strftime("%Y-%m")


def next_month_start(moment: datetime) -> datetime:
    """Return when the allowance after ``moment``'s month begins.

    Args:
        moment: Any timestamp.

    Returns:
        Midnight UTC on the first day of the next month.
    """
    moment = moment.astimezone(UTC)
    year, month = (moment.year + 1, 1) if moment.month == 12 else (moment.year, moment.month + 1)
    return datetime(year, month, 1, tzinfo=UTC)


def _comparable(model: CatalogModel) -> bool:
    """Say whether a catalog model has the rating and both prices a comparison needs.

    Args:
        model: A catalog model.

    Returns:
        ``True`` when it is rated and priced for input and output.
    """
    return (
        model.intelligence_index is not None
        and model.input_cost_per_token is not None
        and model.output_cost_per_token is not None
    )


def better_model(
    job_store: Any, username: str, current: dict[str, Any], skip: frozenset[str] = frozenset()
) -> dict[str, Any] | None:
    """Find the model a plan should try next for one role: rated higher and priced no higher.

    A higher rating at an equal or lower price for both input and output has
    no downside, so a plan may try it on its own. Only models the run can pay
    for the way it pays now are considered: the hosted catalog for a managed
    run, the owner's verified connections for a BYOK run. A model on a saved
    custom endpoint is never switched, and never chosen as a replacement.

    Args:
        job_store: Store whose engine holds the provider connections.
        username: Plan owner.
        current: The model block the run uses now for this role.
        skip: Model names the plan already tried for this role, so the next best gets its turn.

    Returns:
        ``{"name", "byok_provider"}`` for the highest-rated such model, or ``None``.
    """
    if current.get("token_source") == "byok":
        vault = ProviderKeyVault(engine=job_store.engine)
        # Submission clears a model's own base_url, so the saved connection is the only record of a custom endpoint.
        custom = {view.provider for view in vault.list_keys(username).keys if view.api_base}
        prefix = provider_slug_for_model(str(current.get("name") or ""))
        # Submission reads an unnamed provider from the model prefix, so this check does too.
        provider = current.get("byok_provider") or (byok_provider_for_litellm(prefix) if prefix else None)
        if provider in custom:
            return None
        # The BYOK registry carries no ratings, so each model borrows the
        # rating of its hosted twin (same id, or the same model under another
        # provider's spelling).
        ratings: dict[str, float] = {}
        for hosted in get_catalog_cached().models:
            if hosted.intelligence_index is not None:
                ratings.setdefault(hosted.value, hosted.intelligence_index)
                ratings.setdefault(_reasoning_match_key(hosted.value), hosted.intelligence_index)
        models = [
            model
            if model.intelligence_index is not None
            else model.model_copy(
                update={"intelligence_index": ratings.get(model.value, ratings.get(_reasoning_match_key(model.value)))}
            )
            for model in _byok_catalog_for_user(vault, username).models
            if model.byok_provider not in custom
        ]
    else:
        models = [model for model in get_catalog_cached().models if model.available]
    now = next((model for model in models if model.value == current.get("name")), None)
    if now is None or not _comparable(now):
        # Without a rating and both prices there is nothing to compare, so nothing counts as better.
        return None
    better = [
        model
        for model in models
        if _comparable(model)
        and model.value not in skip
        and (model.intelligence_index or 0.0) > (now.intelligence_index or 0.0)
        and (model.input_cost_per_token or 0.0) <= (now.input_cost_per_token or 0.0)
        and (model.output_cost_per_token or 0.0) <= (now.output_cost_per_token or 0.0)
    ]
    if not better:
        return None
    best = max(better, key=lambda model: (model.intelligence_index or 0.0, -(model.input_cost_per_token or 0.0)))
    return {"name": best.value, "byok_provider": best.byok_provider}


ModelPicker = Callable[[Any, str, dict[str, Any], frozenset[str]], dict[str, Any] | None]

MODEL_SWITCHES = ("task_model", "reflection_model")
# The switches plans stored before SKY-100; ``models`` covered both roles.
_OLD_SWITCHES = frozenset({"data", "models", "optimizers"})


def upgrade_switches(permitted: dict[str, Any]) -> dict[str, Any]:
    """Read a plan's stored switches, mapping the old single ``models`` switch to both roles.

    Args:
        permitted: The stored ``permitted`` mapping.

    Returns:
        The same mapping when it already names a role, else both roles set to the old ``models`` switch.
    """
    if any(role in permitted for role in MODEL_SWITCHES) or not _OLD_SWITCHES & permitted.keys():
        return permitted
    allowed = bool(permitted.get("models"))
    return dict.fromkeys(MODEL_SWITCHES, allowed)


def tried_models(plan: ContinuousPlanModel) -> list[str]:
    """List the ``role:model`` pairs the plan already compared against its current version.

    The list belongs to one version: once another version is approved, its
    models are the new starting point, so every model gets a fresh turn.

    Args:
        plan: The plan.

    Returns:
        The tried pairs, oldest first.
    """
    seen = plan.baseline_snapshot or {}
    if seen.get("incumbent") != plan.incumbent_optimization_id:
        return []
    return list(seen.get("tried_models") or [])


def find_better_models(
    job_store: Any, plan: ContinuousPlanModel, models: dict[str, Any], pick_model: ModelPicker
) -> dict[str, dict[str, Any]]:
    """Find a better, untried model for every role the plan's switches turn on.

    Args:
        job_store: Job store handed to the model finder.
        plan: The plan.
        models: The current version's ``models`` block.
        pick_model: Finds a better model for one role.

    Returns:
        ``{role: {"name", "byok_provider"}}`` for each role that has one.
    """
    permitted = upgrade_switches(plan.permitted or {})
    tried = tried_models(plan)
    found = {}
    swapped = dict(models)
    for role in MODEL_SWITCHES:
        current = models.get(role)
        if not permitted.get(role) or not current:
            continue
        skip = frozenset(item.split(":", 1)[1] for item in tried if item.startswith(f"{role}:"))
        while (better := pick_model(job_store, plan.username, current, skip)) is not None:
            candidate = {**swapped, role: {**current, "name": better["name"]}}
            if not _mixed_billing(candidate):
                found[role] = better
                swapped = candidate
                break
            # Submission refuses one model id under two billing sources, so the next best gets its turn.
            skip |= {better["name"]}
    return found


def _mixed_billing(models: dict[str, Any]) -> bool:
    """Say whether one model id appears in two roles under different billing sources.

    Args:
        models: A run's ``models`` block.

    Returns:
        ``True`` when submission would refuse the block.
    """
    blocks = [
        models.get("task_model"),
        models.get("reflection_model"),
        *(models.get("extra_reflection_models") or []),
        models.get("metric_model"),
    ]
    sources: dict[str, Any] = {}
    for block in blocks:
        if not block:
            continue
        name = str(block.get("name") or "").strip("/")
        if sources.setdefault(name, block.get("token_source")) != block.get("token_source"):
            return True
    return False


# A repository request names its models at the top level; these map them onto the plan's roles.
_REPO_MODEL_FIELDS = {"reflection_model": "reflection_model_config"}


def run_models(payload: dict[str, Any]) -> dict[str, Any]:
    """Read a stored request's models as a unified ``models`` block.

    A repository run has no task model: the code under optimization picks its
    own models, so only the reflection model can be switched.

    Args:
        payload: A stored unified or repository request.

    Returns:
        The model blocks by role, with the other models kept for the billing check.
    """
    if not is_repo_payload(payload):
        return payload.get("models") or {}
    models: dict[str, Any] = {role: payload.get(field) for role, field in _REPO_MODEL_FIELDS.items()}
    models["extra_reflection_models"] = payload.get("extra_reflection_model_configs") or []
    models["metric_model"] = (payload.get("scorer") or {}).get("model")
    return models


def _request_copy(stored: dict[str, Any]) -> dict[str, Any]:
    """Drop the fields of a stored request that belong to that run alone: its funding and deadline.

    Args:
        stored: A stored unified request, with credentials scrubbed.

    Returns:
        The fields a new run may reuse.
    """
    return {
        key: value
        for key, value in stored.items()
        if not key.startswith("_budget")
        and not key.startswith("execution_budget_")
        and key
        not in {
            "preflight_id",
            "preflight_fingerprint",
            "estimated_cents_low",
            "estimated_cents_high",
            # A deadline override shortens only the run that asked for it (SKY-572).
            "deadline_seconds",
        }
    }


def held_out_fingerprint(payload: dict[str, Any] | None) -> str | None:
    """Fingerprint the exact test cases a stored run held out, split the way the run engine splits.

    Two test scores compare only when this matches: equal counts are not
    enough, the cases must be the very same.

    A repository request also needs the same pinned commit and scorer code,
    since either one changes what a test score measures.

    Args:
        payload: A stored request.

    Returns:
        The fingerprint, or ``None`` for a DSPy request, or a repository request
        with no pinned commit, inline cases or test cases.
    """
    if payload and is_repo_payload(payload):
        return _repo_held_out_fingerprint(payload)
    return None


def _repo_held_out_fingerprint(payload: dict[str, Any]) -> str | None:
    """Fingerprint a repository request's commit, scorer code and held-out test cases.

    Args:
        payload: A stored repository request.

    Returns:
        The fingerprint, or ``None`` without a pinned commit, inline cases or test cases.
    """
    repo = payload["target"]["repo"]
    cases = payload.get("cases")
    if not repo.get("commit") or not cases:
        return None
    test = legacy_case_splits(list(cases), payload.get("seed")).test
    if not test:
        return None
    scorer = payload.get("scorer") or {}
    return json_fingerprint(
        {
            "repository": repo.get("repository"),
            "commit": repo["commit"],
            "scorer": {key: scorer.get(key) for key in ("kind", "metric_code", "install_command", "dependency_lock")},
            "test": sorted(json_fingerprint(case) for case in test),
        }
    )


def has_held_out(job: dict[str, Any]) -> bool:
    """Say whether a run scores its versions on test cases that no optimizer saw.

    A plan can replace its current version only on such a run, so a run without
    them would spend money on checks that can never win. A finished run answers
    from the split it recorded; a run still in flight from its request.

    Args:
        job: A run with its payload and, once finished, its result.

    Returns:
        ``True`` when the run has held-out test cases.
    """
    split = (job.get("result") or {}).get("split_counts")
    if isinstance(split, dict):
        return bool(split.get("test")) and split.get("held_out") is True
    payload = job.get("payload") or {}
    if not is_repo_payload(payload):
        return False
    cases = payload.get("cases")
    return bool(cases) and bool(legacy_case_splits(list(cases), payload.get("seed")).test)


def _incumbent(job_store: Any, optimization_id: str) -> dict[str, Any]:
    """Load the incumbent run with its payload and result.

    Args:
        job_store: Job store.
        optimization_id: The incumbent run.

    Returns:
        The job record.
    """
    return job_store.get_job(optimization_id, include_payload=True)


def month_committed(session: Session, plan: ContinuousPlanModel, month: str) -> int:
    """Sum what a plan's cycles spent or hold in one monthly allowance.

    A cycle still optimizing counts at its full budget total, because its
    reservation can still be drawn on. A finished cycle counts what its budget
    billed plus any hold it still keeps for usage awaiting reconciliation.

    Args:
        session: Open database session.
        plan: The plan.
        month: ``YYYY-MM`` allowance.

    Returns:
        Committed cents.
    """
    cycles = session.scalars(
        select(ContinuousCycleModel).where(ContinuousCycleModel.plan_id == plan.id, ContinuousCycleModel.month == month)
    ).all()
    budget_ids = [cycle.execution_budget_id for cycle in cycles if cycle.execution_budget_id]
    budgets = {
        budget.id: budget
        for budget in session.scalars(select(ExecutionBudgetModel).where(ExecutionBudgetModel.id.in_(budget_ids)))
    }
    total = 0
    for cycle in cycles:
        budget = budgets.get(cycle.execution_budget_id)
        if cycle.status == CYCLE_OPTIMIZING:
            total += plan.cycle_budget_cents
        elif budget is not None:
            # ``spent_cents`` is sampled once when the run ends; reconciliation can bill more later.
            total += budget.billed_cents + budget_wallet_hold(budget)
        elif cycle.spent_cents:
            total += cycle.spent_cents
    return total


def _billed_cents(job_store: Any, budget_id: str | None, username: str) -> int | None:
    """Read what a cycle's protected budget billed the wallet.

    Args:
        job_store: Store whose engine holds the budgets.
        budget_id: The cycle's budget, if one was created.
        username: Budget owner.

    Returns:
        Billed cents, or ``None`` without a budget.
    """
    if budget_id is None:
        return None
    try:
        return BudgetService(engine=job_store.engine).get(budget_id, username).billed_cents
    except BudgetError:
        return None


def _cycle_payload(
    plan: ContinuousPlanModel,
    incumbent: dict[str, Any],
    new_models: dict[str, dict[str, Any]],
    sequence: int,
) -> BlackboxRunRequest:
    """Copy the incumbent's repository request with the better models swapped in.

    Args:
        plan: The plan.
        incumbent: The incumbent repository run record.
        new_models: ``{role: {"name", "byok_provider"}}`` to swap in.
        sequence: Cycle number shown in the run name.

    Returns:
        A new, unfunded repository request.
    """
    copy = _request_copy(scrub_execution_credentials(incumbent["payload"]))
    return _repo_cycle_payload(plan, incumbent, copy, new_models, sequence)


def _repo_cycle_payload(
    plan: ContinuousPlanModel,
    incumbent: dict[str, Any],
    copy: dict[str, Any],
    new_models: dict[str, dict[str, Any]],
    sequence: int,
) -> BlackboxRunRequest:
    """Build a repository check's request: the incumbent's run, started from its best patch.

    The pinned commit, cases, scorer and split seed stay the incumbent's own,
    so the new run holds out the same test cases on the same tree. Its seed
    patch is the incumbent's best version, so the check improves on the
    current version instead of starting again from the bare commit.

    Args:
        plan: The plan.
        incumbent: The incumbent run record.
        copy: The incumbent's request, scrubbed and stripped of its funding.
        new_models: ``{role: {"name", "byok_provider"}}`` to swap in.
        sequence: Cycle number shown in the run name.

    Returns:
        A new, unfunded repository request.
    """
    # The cases travel inline, so a staged upload that later expires cannot change them.
    copy["staged_dataset_id"] = None
    best = (incumbent.get("result") or {}).get("best_candidate")
    if isinstance(best, str):
        copy["seed_candidate"] = best
    for role, better in new_models.items():
        field = _REPO_MODEL_FIELDS[role]
        copy[field] = _swapped(copy[field], better)
    repo = (copy.get("target") or {}).get("repo")
    if isinstance(repo, dict):
        # A check must not move ``skynet/best`` before the owner approves it,
        # and a check with no gain would reset it to the bare commit. Checks
        # publish a draft pull request instead, as on any other repository.
        repo["created_by_skynet"] = False
    _name_and_fund(copy, plan, incumbent, sequence)
    return BlackboxRunRequest.model_validate(copy)


def _swapped(block: dict[str, Any], better: dict[str, Any]) -> dict[str, Any]:
    """Swap a better model into one role's model block.

    Args:
        block: The role's current model block.
        better: ``{"name", "byok_provider"}`` of the better model.

    Returns:
        The new block, billed the same way.
    """
    swapped = {**block, "name": better["name"]}
    if swapped.get("token_source") == "byok":
        swapped["byok_provider"] = better.get("byok_provider")
    return swapped


def _name_and_fund(copy: dict[str, Any], plan: ContinuousPlanModel, incumbent: dict[str, Any], sequence: int) -> None:
    """Name a check's run after the incumbent and cap it at the plan's cycle budget.

    Args:
        copy: The new request, mutated in place.
        plan: The plan.
        incumbent: The incumbent run record.
        sequence: Cycle number shown in the run name.
    """
    base_name = (incumbent.get("payload_overview") or {}).get(PAYLOAD_OVERVIEW_NAME) or copy.get("name") or ""
    copy["name"] = f"{base_name} · #{sequence}".strip(" ·")
    copy["max_cost_cents"] = plan.cycle_budget_cents


def _finish_cycle(session: Session, cycle: ContinuousCycleModel, status: str, code: str | None, **params: Any) -> None:
    """Record a cycle's outcome and release its lease.

    Args:
        session: Open session that owns ``cycle``.
        cycle: The cycle.
        status: New cycle status.
        code: I18n key explaining the outcome.
        **params: Substitution params for ``code``.
    """
    cycle.status = status
    cycle.detail_code = code
    cycle.detail_params = params
    cycle.lease_until = None
    cycle.updated_at = datetime.now(UTC)


def _pause(plan: ContinuousPlanModel, reason: str) -> None:
    """Pause a plan for a recorded reason.

    Args:
        plan: The plan.
        reason: ``user``, ``allowance``, ``funds`` or ``failing``.
    """
    if plan.pause_reason == PAUSE_USER and reason != PAUSE_USER:
        # Only the customer lifts their own pause, so a funds or allowance pause never replaces it.
        return
    plan.status = PLAN_PAUSED
    plan.pause_reason = reason
    if reason == PAUSE_FAILING:
        # Unlike a funds or allowance pause, nothing a later check sees can lift this one.
        plan.next_check_at = None
    plan.updated_at = datetime.now(UTC)


_STREAK_NEUTRAL = frozenset({"continuous.skip_busy", "continuous.skip_review_pending", "continuous.skip_paused"})


def _fail_cycle(
    session: Session, plan: ContinuousPlanModel, cycle: ContinuousCycleModel, code: str, **params: Any
) -> None:
    """Record a failed cycle and pause the plan after a streak of failures.

    Checks skipped only because another check, a run or a review was in the
    way say nothing about the source, so they neither count nor break a streak.

    Args:
        session: Open session that owns ``plan`` and ``cycle``.
        plan: The cycle's plan.
        cycle: The cycle.
        code: I18n key explaining the failure.
        **params: Substitution params for ``code``.
    """
    _finish_cycle(session, cycle, CYCLE_FAILED, code, **params)
    session.flush()
    recent = session.scalars(
        select(ContinuousCycleModel)
        .where(ContinuousCycleModel.plan_id == plan.id, ContinuousCycleModel.status != CYCLE_CHECKING)
        .order_by(ContinuousCycleModel.created_at.desc(), ContinuousCycleModel.slot.desc())
        .limit(FAILURE_STREAK * 4)
    ).all()
    counted = [item for item in recent if item.detail_code not in _STREAK_NEUTRAL][:FAILURE_STREAK]
    if len(counted) == FAILURE_STREAK and all(item.status == CYCLE_FAILED for item in counted):
        _pause(plan, PAUSE_FAILING)


def _run_key(cycle_id: str) -> str:
    """Return the idempotency key of a check's run.

    Args:
        cycle_id: The check.

    Returns:
        The key.
    """
    return f"continuous:{cycle_id}"


def _holds_lease(cycle: ContinuousCycleModel | None, lease: datetime | None) -> bool:
    """Say whether a worker still owns a check it claimed under ``lease``.

    Args:
        cycle: The check as stored now.
        lease: The lease the worker read when it took the check.

    Returns:
        ``True`` while no other worker took the check over and it has no outcome yet.
    """
    return cycle is not None and cycle.status == CYCLE_CHECKING and aware(cycle.lease_until) == aware(lease)


def _record_started(session: Session, cycle: ContinuousCycleModel, optimization_id: str, budget_id: str | None) -> None:
    """Mark a check as optimizing on the run it started.

    Args:
        session: Open session that owns ``cycle``.
        cycle: The cycle.
        optimization_id: The started run.
        budget_id: The run's protected budget.
    """
    cycle.optimization_id = optimization_id
    cycle.execution_budget_id = budget_id
    _finish_cycle(session, cycle, CYCLE_OPTIMIZING, "continuous.run_started", changes=",".join(cycle.changes or []))


def _link_accepted(job_store: Any, session: Session, username: str, cycle: ContinuousCycleModel) -> bool:
    """Link the run a check already submitted before its worker died.

    The run is already paid for and uses the models recorded then, so it is
    linked as it is instead of searching models or checking funds again. When
    no run was accepted, a preflight may still have charged the check's
    budget, so the check keeps that budget and the monthly allowance counts it.

    Args:
        job_store: Job store.
        session: Open session that owns ``cycle``; a second connection would wait on its write lock.
        username: Plan owner.
        cycle: The cycle.

    Returns:
        ``True`` when the check had a run and is now optimizing.
    """
    accepted = job_store.find_job_by_idempotency_key(username, _run_key(cycle.id))
    if accepted is None:
        budget_key = (cycle.snapshot or {}).get("budget_key")
        if budget_key and cycle.execution_budget_id is None:
            cycle.execution_budget_id = session.scalar(
                select(ExecutionBudgetModel.id).where(
                    ExecutionBudgetModel.username == username, ExecutionBudgetModel.creation_key == budget_key
                )
            )
        return False
    job = job_store.get_job(accepted, include_payload=False)
    _record_started(session, cycle, accepted, job.get("execution_budget_id"))
    return True


def process_cycle(
    job_store: Any,
    service: Any,
    cycle_id: str,
    *,
    now: datetime | None = None,
    pick_model: ModelPicker = better_model,
) -> None:
    """Run one claimed check to its outcome: skip, pause, fail or start a run.

    Safe to repeat. The run is submitted under an idempotency key derived from
    the cycle, so a retried check replays the same run and the same budget
    instead of creating new ones.

    Args:
        job_store: Job store.
        service: Optimization service handed to the in-process worker.
        cycle_id: The claimed cycle.
        now: Clock override for tests.
        pick_model: Finds a better model for one role.
    """
    now = now or datetime.now(UTC)
    engine = job_store.engine
    with Session(engine) as session, session.begin():
        cycle = session.get(ContinuousCycleModel, cycle_id)
        if cycle is None or cycle.status != CYCLE_CHECKING:
            return
        lease = cycle.lease_until
        # Locked so a customer's pause committed meanwhile is not overwritten by this check's outcome.
        plan = session.get(ContinuousPlanModel, cycle.plan_id, with_for_update=True)
        if plan is None:
            return
        if _link_accepted(job_store, session, plan.username, cycle):
            return
        if plan.status == PLAN_PAUSED and plan.pause_reason in (PAUSE_USER, PAUSE_FAILING):
            _finish_cycle(session, cycle, CYCLE_SKIPPED, "continuous.skip_paused")
            return
        busy = session.scalar(
            select(ContinuousCycleModel.status).where(
                ContinuousCycleModel.plan_id == plan.id,
                ContinuousCycleModel.id != cycle.id,
                ContinuousCycleModel.status.in_((CYCLE_OPTIMIZING, CYCLE_AWAITING_REVIEW))
                | ((ContinuousCycleModel.status == CYCLE_CHECKING) & (ContinuousCycleModel.slot < cycle.slot)),
            )
        )
        if busy is not None:
            code = "continuous.skip_review_pending" if busy == CYCLE_AWAITING_REVIEW else "continuous.skip_busy"
            _finish_cycle(session, cycle, CYCLE_SKIPPED, code)
            return
        try:
            incumbent = _incumbent(job_store, plan.incumbent_optimization_id)
        except KeyError:
            # Uncaught, the cycle would stay ``checking`` and hold back every later check.
            _fail_cycle(session, plan, cycle, "continuous.run_rejected", reason="the incumbent run was deleted")
            return
        if not is_repo_payload(incumbent["payload"]):
            # A DSPy plan can never start a check again, so it ends instead of skipping forever.
            _finish_cycle(session, cycle, CYCLE_SKIPPED, "optimization.dspy_run_retired")
            _end_plan(plan, END_DSPY_RETIRED, now)
            return
        previous = cycle.snapshot or {}
        current_models = run_models(incumbent["payload"])
        if "new_models" in previous:
            # A retry replays the first attempt's run, so its idempotency key meets the same request.
            new_models = previous["new_models"]
        else:
            new_models = find_better_models(job_store, plan, current_models, pick_model)
        if missing := sorted(role for role in new_models if not (current_models.get(role) or {}).get("name")):
            # Only a retry can name a role the incumbent lacks; uncaught, it would stay ``checking``.
            _fail_cycle(
                session, plan, cycle, "continuous.run_rejected", reason=f"the incumbent has no {', '.join(missing)}"
            )
            return
        old_models = previous.get("old_models") or {role: current_models[role]["name"] for role in new_models}
        cycle.changes = sorted(new_models)
        gate = None
        # A retry after ``budget.pending`` already holds this cycle's budget; gating it again
        # would demand a second full cycle budget of headroom and pause a check that is covered.
        if cycle.execution_budget_id is None:
            if (
                plan.monthly_limit_cents is not None
                and plan.monthly_limit_cents - month_committed(session, plan, cycle.month) < plan.cycle_budget_cents
            ):
                gate = PAUSE_ALLOWANCE
            elif StripeBillingService(engine=engine).spendable_cents(plan.username) < plan.cycle_budget_cents:
                gate = PAUSE_FUNDS
        if gate is None and plan.status == PLAN_PAUSED and plan.pause_reason in SYSTEM_PAUSES:
            plan.status, plan.pause_reason = PLAN_ACTIVE, None
        if not new_models:
            _finish_cycle(session, cycle, CYCLE_SKIPPED, "continuous.skip_unchanged")
            return
        if held_out_fingerprint(incumbent["payload"]) is None:
            # Without the incumbent's own test cases the new run's score could never be compared,
            # so it would be paid for and never judged.
            _fail_cycle(session, plan, cycle, "continuous.no_test_split")
            return
        if gate is not None:
            _pause(plan, gate)
            _finish_cycle(session, cycle, CYCLE_PAUSED, f"continuous.paused_{gate}")
            return
        started = session.scalars(
            select(ContinuousCycleModel.id).where(
                ContinuousCycleModel.plan_id == plan.id, ContinuousCycleModel.optimization_id.is_not(None)
            )
        ).all()
        try:
            payload = _cycle_payload(plan, incumbent, new_models, len(started) + 1)
        except ValidationError as error:
            # A model the run contract rejects fails this cycle instead of wedging the plan.
            reason = "; ".join(str(item.get("msg")) for item in error.errors())
            _fail_cycle(session, plan, cycle, "continuous.run_rejected", reason=reason)
            return
        plan_username = plan.username
        cycle.snapshot = {
            "compared_against": plan.incumbent_optimization_id,
            "new_models": new_models,
            "old_models": old_models,
            # Kept before submitting, so a crash recovery finds a budget the preflight already charged.
            "budget_key": resolve_submission_replay_keys(
                payload, username=plan_username, workflow=_workflow(payload), supplied_key=_run_key(cycle_id)
            ).budget,
        }
    _submit_run(job_store, service, cycle_id, plan_username, payload, lease)


def _workflow(payload: BlackboxRunRequest) -> str:
    """Name the submission workflow a check's request goes through.

    Args:
        payload: The check's request.

    Returns:
        The workflow its replay and budget keys are derived under.
    """
    return _BLACKBOX_WORKFLOW


def _submit_run(
    job_store: Any,
    service: Any,
    cycle_id: str,
    username: str,
    payload: BlackboxRunRequest,
    lease: datetime | None,
) -> None:
    """Submit a check's run, then record whether it started.

    A run refused for a pending budget leaves the check claimed, so a later
    sweep replays the same key and budget. A worker whose lease another worker
    took over stops before it submits or writes, because that worker owns the
    check now and may already have recorded its outcome.

    Args:
        job_store: Job store.
        service: Optimization service handed to the in-process worker.
        cycle_id: The check.
        username: Plan owner.
        payload: The run's request.
        lease: The lease this worker read when it took the check.
    """
    engine = job_store.engine
    with Session(engine) as session:
        if not _holds_lease(session.get(ContinuousCycleModel, cycle_id), lease):
            logger.warning("Continuous cycle %s lost its lease before it submitted", cycle_id)
            return
    refused: DomainError | None = None
    try:
        response = submit_repo_payload(
            payload,
            service=service,
            job_store=job_store,
            current_user=AuthenticatedUser(username, "user", ()),
            idempotency_key=_run_key(cycle_id),
        )
    except DomainError as error:
        refused = error
    if refused is None:
        optimization_id = response.optimization_id
        budget_id = job_store.get_job(optimization_id, include_payload=False).get("execution_budget_id")
    else:
        with Session(engine) as session:
            cycle = session.get(ContinuousCycleModel, cycle_id)
            budget_key = (cycle.snapshot or {}).get("budget_key") if cycle else None
        budget = BudgetService(engine=engine).find_by_creation_key(username, budget_key) if budget_key else None
        budget_id = budget.id if budget else None
        spent = _billed_cents(job_store, budget_id, username)
    with Session(engine) as session, session.begin():
        cycle = session.get(ContinuousCycleModel, cycle_id, with_for_update=True)
        if not _holds_lease(cycle, lease):
            # The owner deleted the plan, or another worker took the check over; this late write
            # must not reopen an outcome that worker recorded.
            return
        if refused is None:
            _record_started(session, cycle, optimization_id, budget_id)
            logger.info("Continuous cycle %s started run %s", cycle_id, optimization_id)
            return
        plan = session.get(ContinuousPlanModel, cycle.plan_id, with_for_update=True)
        # A refused preflight can bill the budget, and the monthly allowance must count it.
        cycle.execution_budget_id = budget_id
        cycle.spent_cents = spent
        if refused.code == "budget.pending":
            # Setup usage is still being confirmed. The cycle keeps its lease, so a
            # later sweep replays this same key and budget instead of a new check
            # paying for setup again. This wait is not a crash, so it does
            # not count toward MAX_CHECK_ATTEMPTS.
            cycle.detail_params = {**(cycle.detail_params or {}), "attempts": 0}
            return
        if refused.code in {"billing.insufficient_funds", "budget.insufficient"}:
            _pause(plan, PAUSE_FUNDS)
            _finish_cycle(session, cycle, CYCLE_PAUSED, "continuous.paused_funds")
        else:
            _fail_cycle(session, plan, cycle, "continuous.run_rejected", reason=str(refused.detail))


def _mark_tried(plan: ContinuousPlanModel, incumbent_id: str | None, new_models: dict[str, Any]) -> None:
    """Record that a check compared its models against the current version.

    Args:
        plan: The plan.
        incumbent_id: The version the check compared against.
        new_models: The models the check's run used, by role.
    """
    tried = tried_models(plan) if incumbent_id == plan.incumbent_optimization_id else []
    for role, model in new_models.items():
        if f"{role}:{model['name']}" not in tried:
            tried.append(f"{role}:{model['name']}")
    plan.baseline_snapshot = {"incumbent": incumbent_id, "tried_models": tried}


def review_cycle(job_store: Any, cycle_id: str) -> None:
    """Judge a finished cycle run against the incumbent's saved test score.

    The scores compare only when both runs held out the very same test cases.
    A candidate is kept for review when its run succeeded on held-out test
    cases and scored at least ``WIN_MARGIN`` higher. Anything else leaves the
    incumbent in place. Only a real comparison marks the run's models as
    tried, so a run that failed (a provider outage) or could not be compared
    tries the same models again on the next check.

    Args:
        job_store: Job store.
        cycle_id: A cycle in ``optimizing``.
    """
    with Session(job_store.engine) as session, session.begin():
        cycle = session.get(ContinuousCycleModel, cycle_id)
        if cycle is None or cycle.status != CYCLE_OPTIMIZING or cycle.optimization_id is None:
            return
        plan = session.get(ContinuousPlanModel, cycle.plan_id, with_for_update=True)
        try:
            job = job_store.get_job(cycle.optimization_id, include_payload=True)
        except KeyError:
            _fail_cycle(session, plan, cycle, "continuous.run_failed", reason="missing")
            return
        if job["status"] not in _TERMINAL_JOB:
            return
        cycle.spent_cents = _billed_cents(job_store, cycle.execution_budget_id, plan.username)
        plan.updated_at = datetime.now(UTC)
        if job["status"] != OptimizationStatus.success.value:
            _fail_cycle(session, plan, cycle, "continuous.run_failed", reason=job["status"])
            return
        snapshot = cycle.snapshot or {}
        incumbent_id = snapshot.get("compared_against") or plan.incumbent_optimization_id
        try:
            incumbent = job_store.get_job(incumbent_id, include_payload=True)
        except KeyError:
            incumbent = None
        result = job.get("result") or {}
        best = result.get("best_score")
        current = (incumbent.get("result") or {}).get("best_score") if incumbent else None
        cycle.incumbent_score, cycle.candidate_score = current, best
        fingerprint = held_out_fingerprint(job.get("payload"))
        if (
            best is None
            or current is None
            or fingerprint is None
            or fingerprint != held_out_fingerprint(incumbent.get("payload"))
        ):
            # A failure, so a streak of runs that can never be compared pauses the plan instead of paying forever.
            _fail_cycle(session, plan, cycle, "continuous.review_unscored")
            return
        _mark_tried(plan, incumbent_id, snapshot.get("new_models") or {})
        split = result.get("split_counts") or {}
        if not (split.get("test") and split.get("held_out")):
            # Without separate test cases the split that picked the candidate would also judge it.
            _finish_cycle(session, cycle, CYCLE_REGRESSED, "continuous.review_not_held_out")
        elif best <= current:
            _finish_cycle(session, cycle, CYCLE_REGRESSED, "continuous.review_regressed")
        # Rounded, so a gain of exactly the margin (0.56 to 0.57) is not lost to float error.
        elif round(best - current, 9) < WIN_MARGIN:
            _finish_cycle(session, cycle, CYCLE_REGRESSED, "continuous.review_within_margin")
        else:
            _finish_cycle(session, cycle, CYCLE_AWAITING_REVIEW, "continuous.review_ready")


def plan_rule(plan: ContinuousPlanModel) -> ScheduleRule:
    """Read a plan's schedule rule, including plans saved as ``daily``/``weekly``.

    Args:
        plan: The plan.

    Returns:
        The plan's rule.
    """
    return ScheduleRule.model_validate(plan.schedule_rule) if plan.schedule_rule else legacy_rule(plan.schedule)


def plan_anchor(plan: ContinuousPlanModel) -> datetime | None:
    """Return when the plan's intervals start counting: its last schedule change, else its creation.

    Args:
        plan: The plan.

    Returns:
        The anchor time, or ``None`` for an unsaved plan with neither.
    """
    stored = (plan.schedule_rule or {}).get("anchor")
    if isinstance(stored, str):
        return aware(datetime.fromisoformat(stored))
    return aware(plan.created_at)


def next_check_after(plan: ContinuousPlanModel, after: datetime) -> datetime:
    """Return the plan's first scheduled check strictly after ``after``.

    Args:
        plan: The plan.
        after: Reference time.

    Returns:
        The next check time in UTC.
    """
    return next_runs(plan_rule(plan), after, anchor=plan_anchor(plan) or after)[0]


def _end_plan(plan: ContinuousPlanModel, reason: str, now: datetime) -> None:
    """End a plan for good, with the reason the owner sees.

    Args:
        plan: The plan, mutated in place.
        reason: The end reason, kept in ``pause_reason``.
        now: Current time.
    """
    plan.status = PLAN_ENDED
    plan.pause_reason = reason
    plan.next_check_at = None
    plan.updated_at = now


def settle_waiting(plan: ContinuousPlanModel, job_status: str | None, now: datetime, *, held_out: bool = True) -> None:
    """Start a waiting plan once its first run succeeded, or end it once that run ended otherwise.

    An ended plan is kept, with the run's outcome as its reason, so the owner
    sees why the schedule never started; they delete it from the run page.

    Args:
        plan: A plan, in any state.
        job_status: Status of the plan's source run, or ``None`` when the run is gone.
        now: Current time.
        held_out: Whether the source run kept test cases apart; without them the plan ends.
    """
    if plan.status != PLAN_WAITING:
        return
    if job_status == OptimizationStatus.success.value and not held_out:
        plan.status = PLAN_ENDED
        plan.pause_reason = END_NO_TEST_SPLIT
        plan.next_check_at = None
        plan.updated_at = now
    elif job_status == OptimizationStatus.success.value:
        plan.status = PLAN_ACTIVE
        plan.next_check_at = next_check_after(plan, now)
        plan.updated_at = now
    elif job_status is None or job_status in _TERMINAL_JOB:
        plan.status = PLAN_ENDED
        plan.pause_reason = job_status or "missing"
        plan.next_check_at = None
        plan.updated_at = now


def _settle_waiting_plans(job_store: Any, now: datetime) -> None:
    """Start or end every waiting plan whose first run finished.

    Args:
        job_store: Job store.
        now: Current time.
    """
    with Session(job_store.engine) as session:
        for plan in session.scalars(select(ContinuousPlanModel).where(ContinuousPlanModel.status == PLAN_WAITING)):
            try:
                job = job_store.get_job(plan.source_optimization_id, include_payload=True)
            except KeyError:
                settle_waiting(plan, None, now)
                continue
            settle_waiting(plan, job["status"], now, held_out=has_held_out(job))
        session.commit()


def _claim_due(engine: Any, now: datetime) -> list[str]:
    """Claim every due check exactly once and return the new cycle ids.

    The plan's ``next_check_at`` is advanced with a compare-and-set and the
    cycle is inserted under the unique ``(plan_id, slot)`` pair in the same
    transaction, so a second worker or an overlapping sweep claims nothing.
    The next check is the rule's first time after now, so checks missed while
    the service was down collapse into this one.

    Args:
        engine: Database engine.
        now: Current time.

    Returns:
        Ids of the cycles this call claimed.
    """
    query = select(ContinuousPlanModel).where(
        ContinuousPlanModel.next_check_at.is_not(None),
        ContinuousPlanModel.next_check_at <= now,
    )
    with Session(engine) as session:
        due = session.scalars(query).all()
        plans = [(plan.id, plan.next_check_at, next_check_after(plan, now)) for plan in due]
    claimed = []
    for plan_id, slot_at, following in plans:
        slot = aware(slot_at)
        try:
            with Session(engine) as session, session.begin():
                won = session.execute(
                    update(ContinuousPlanModel)
                    .where(ContinuousPlanModel.id == plan_id, ContinuousPlanModel.next_check_at == slot_at)
                    .values(next_check_at=following)
                ).rowcount
                if not won:
                    continue
                cycle = ContinuousCycleModel(
                    plan_id=plan_id,
                    slot=slot.isoformat(),
                    month=month_key(now),
                    status=CYCLE_CHECKING,
                    lease_until=now + CYCLE_LEASE,
                )
                session.add(cycle)
                session.flush()
                claimed.append(cycle.id)
        except IntegrityError:
            continue
    return claimed


def _reclaim_stale(job_store: Any, now: datetime) -> list[str]:
    """Take over checks whose worker died before recording an outcome.

    A check that already lost its worker ``MAX_CHECK_ATTEMPTS`` times is
    closed as failed, so one check that always crashes cannot hold back the
    plan's later checks forever. Before it closes, it still links a run its
    last worker submitted, so a paid run is never orphaned, and it keeps any
    budget a preflight charged, so the allowance counts it.

    Args:
        job_store: Job store.
        now: Current time.

    Returns:
        Ids of the cycles this call took over.
    """
    engine = job_store.engine
    with Session(engine) as session:
        stale = session.execute(
            select(ContinuousCycleModel.id, ContinuousCycleModel.lease_until).where(
                ContinuousCycleModel.status == CYCLE_CHECKING,
                ContinuousCycleModel.lease_until <= now,
            )
        ).all()
    taken = []
    for cycle_id, lease in stale:
        with Session(engine) as session, session.begin():
            won = session.execute(
                update(ContinuousCycleModel)
                .where(ContinuousCycleModel.id == cycle_id, ContinuousCycleModel.lease_until == lease)
                .values(lease_until=now + CYCLE_LEASE)
            ).rowcount
            if not won:
                continue
            cycle = session.get(ContinuousCycleModel, cycle_id)
            attempts = int((cycle.detail_params or {}).get("attempts", 1)) + 1
            if attempts > MAX_CHECK_ATTEMPTS:
                plan = session.get(ContinuousPlanModel, cycle.plan_id, with_for_update=True)
                if plan is None:
                    _finish_cycle(session, cycle, CYCLE_FAILED, "continuous.check_stuck", attempts=MAX_CHECK_ATTEMPTS)
                elif not _link_accepted(job_store, session, plan.username, cycle):
                    _fail_cycle(session, plan, cycle, "continuous.check_stuck", attempts=MAX_CHECK_ATTEMPTS)
                continue
            cycle.detail_params = {**(cycle.detail_params or {}), "attempts": attempts}
        taken.append(cycle_id)
    return taken


def run_due_checks(
    job_store: Any,
    service: Any,
    *,
    now: datetime | None = None,
    pick_model: ModelPicker = better_model,
) -> list[str]:
    """Review finished cycle runs, then run every due or abandoned check once.

    Args:
        job_store: Job store.
        service: Optimization service handed to the in-process worker.
        now: Clock override for tests.
        pick_model: Finds a better model for one role.

    Returns:
        Ids of the cycles this pass checked.
    """
    now = now or datetime.now(UTC)
    engine = job_store.engine
    with Session(engine) as session:
        optimizing = session.scalars(
            select(ContinuousCycleModel.id).where(ContinuousCycleModel.status == CYCLE_OPTIMIZING)
        ).all()
    for cycle_id in optimizing:
        review_cycle(job_store, cycle_id)
    _settle_waiting_plans(job_store, now)
    checked = _reclaim_stale(job_store, now) + _claim_due(engine, now)
    for cycle_id in checked:
        try:
            process_cycle(job_store, service, cycle_id, now=now, pick_model=pick_model)
        except Exception:
            # The lease expires and a later sweep retries; the idempotency key
            # keeps that retry from starting or funding a second run.
            logger.exception("Continuous cycle %s failed", cycle_id)
    return checked


class ContinuousSweeper:
    """Run :func:`run_due_checks` on a timer on one replica at a time."""

    def __init__(self, job_store: Any, service: Any, interval_seconds: float = SWEEP_INTERVAL_SECONDS) -> None:
        """Bind the sweeper to its store and service.

        Args:
            job_store: Job store.
            service: Optimization service handed to the in-process worker.
            interval_seconds: Seconds between sweeps.
        """
        self._job_store = job_store
        self._service = service
        self._interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the background sweep loop."""
        self._thread = threading.Thread(target=self._run, name="continuous-sweeper", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the background sweep loop."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def sweep_once(self) -> None:
        """Run one pass when this replica holds the sweep lock."""
        try:
            with advisory_lock(self._job_store.engine, CONTINUOUS_SWEEP_LOCK_KEY) as leader:
                if leader:
                    run_due_checks(self._job_store, self._service)
        except Exception:
            logger.warning("Continuous sweep failed", exc_info=True)

    def _run(self) -> None:
        """Sweep until stopped."""
        while not self._stop_event.wait(self._interval_seconds):
            self.sweep_once()


def start_continuous_sweeper(job_store: Any, service: Any) -> ContinuousSweeper | None:
    """Start the continuous-optimization sweeper when the store has a database.

    Args:
        job_store: Job store.
        service: Optimization service handed to the in-process worker.

    Returns:
        The started sweeper, or ``None`` for a store without an engine.
    """
    if getattr(job_store, "engine", None) is None:
        return None
    sweeper = ContinuousSweeper(job_store, service)
    sweeper.start()
    return sweeper
