"""Routes for submitting optimization jobs. [PUBLIC DEV API]

``POST /optimize`` — one optimization run of a DSPy program, for every DSPy
optimizer.
``POST /blackbox/run`` — one optimization of a GitHub repository.
``GET /optimizers`` — the optimizers that fit a setup, with availability.
``GET /blackbox/engines`` — the engine catalog with per-deployment availability.

Stored grid-search, DSPy and legacy black-box runs stay readable; the worker
still executes legacy payloads already queued or resumed.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, wait
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from ...billing import (
    ProviderKeyVault,
    StripeBillingService,
    byok_provider_for_litellm,
    cost_ceiling_budget,
)
from ...billing.budget_amounts import MAX_CENTS
from ...billing.budgets import BudgetError, BudgetService
from ...billing.byok_bridge import byok_upstream_routable
from ...billing.byok_upstreams import UPSTREAMS, byok_model_prefix, resolve_upstream, validate_byok_novelty
from ...billing.byok_vault import safe_connection_params
from ...billing.operation_pricing import UnpricedOperationError
from ...billing.protected_credentials import ProtectedCredentialVault, protect_execution_credentials
from ...config import settings
from ...constants import (
    COMPOSITION_SINGLE,
    OPTIMIZATION_TYPE_BLACKBOX,
    OPTIMIZATION_TYPE_RUN,
    PAYLOAD_OVERVIEW_ARTIFACT_KIND,
    PAYLOAD_OVERVIEW_COMPOSITION,
    PAYLOAD_OVERVIEW_DATA_MODE,
    PAYLOAD_OVERVIEW_DATASET_ROWS,
    PAYLOAD_OVERVIEW_DESCRIPTION,
    PAYLOAD_OVERVIEW_ESTIMATED_HIGH,
    PAYLOAD_OVERVIEW_ESTIMATED_LOW,
    PAYLOAD_OVERVIEW_IS_PRIVATE,
    PAYLOAD_OVERVIEW_MAX_COST_CENTS,
    PAYLOAD_OVERVIEW_MODEL_NAME,
    PAYLOAD_OVERVIEW_MODEL_SETTINGS,
    PAYLOAD_OVERVIEW_MODULE_NAME,
    PAYLOAD_OVERVIEW_NAME,
    PAYLOAD_OVERVIEW_OPTIMIZATION_TYPE,
    PAYLOAD_OVERVIEW_OPTIMIZER_CHOICE,
    PAYLOAD_OVERVIEW_OPTIMIZER_NAME,
    PAYLOAD_OVERVIEW_OPTIMIZER_RESOLVED,
    PAYLOAD_OVERVIEW_PAYLOAD_KIND,
    PAYLOAD_OVERVIEW_REFLECTION_MODEL,
    PAYLOAD_OVERVIEW_SEED,
    PAYLOAD_OVERVIEW_SHUFFLE,
    PAYLOAD_OVERVIEW_SPLIT_COUNTS,
    PAYLOAD_OVERVIEW_SPLIT_FRACTIONS,
    PAYLOAD_OVERVIEW_TASK_MODEL,
    PAYLOAD_OVERVIEW_TOKEN_SOURCE,
    PAYLOAD_OVERVIEW_TOKEN_SOURCES_BY_MODEL,
    PAYLOAD_OVERVIEW_USERNAME,
    TOKEN_SOURCE_BYOK,
)
from ...exceptions import ServiceError
from ...i18n_keys import I18nKey
from ...models import (
    FEEDBACK_OPTIMIZERS,
    UNIFIED_PAYLOAD_KIND,
    BlackboxEngineCatalogResponse,
    BlackboxRunRequest,
    DataMode,
    OptimizationStatus,
    OptimizationSubmissionResponse,
    OptimizerCatalogResponse,
    OptimizerChoice,
    OptimizerOption,
    OptimizerTarget,
    UnifiedData,
    UnifiedRunRequest,
    optimizer_family,
    optimizers_for,
    resolve_optimizer,
)
from ...models.blackbox import (
    BLACKBOX_ITERATION_LIMIT_ENGINES,
    BLACKBOX_MODULE_NAME,
    BLACKBOX_STRATEGY_AUTO,
    BLACKBOX_TARGET_REPO,
    BLACKBOX_TARGET_TEXT,
)
from ...models.common import ModelConfig, OptimizationType
from ...models.submissions import _OptimizationRequestBase
from ...models.unified import METRIC_SIGNATURE, RUN_ENTRY
from ...service_gateway.optimization.blackbox.registry import ENGINES
from ...service_gateway.optimization.blackbox.service import engine_catalog, validate_blackbox_payload
from ...service_gateway.optimization.unified.contract import validate_unified_payload
from ...service_gateway.optimization.unified.split import split_cases, task_fingerprint
from ...storage.preflights import PreflightStore, setup_seed
from ...storage.usage import json_byte_size
from ...worker.engine import get_worker
from ..agent_engine import fill_proposer_host_cli
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..model_billing import enforce_local_mode_sources, normalize_model_token_sources
from ..preflight_execution import BALANCE_FIELD, BUDGET_CHECK_KEY, WizardPreflightRequest, run_preflight
from ..rate_limit import enforce_submission_rate
from ..submission_idempotency import resolve_submission_replay_keys
from ._helpers import (
    enforce_job_quota,
    enforce_storage_quota,
    stable_seed,
    strip_api_key,
)
from .execution_budgets import budget_http_error

logger = logging.getLogger(__name__)

# Preflight evidence and replay keys are scoped by request family; unified
# runs are their own family so their evidence never matches a legacy payload.
UNIFIED_WORKFLOW = "unified"
# Repository runs keep the legacy black-box family, which the preflight and
# worker still read.
_BLACKBOX_WORKFLOW = "anything"
_UNIFIED_CONTRACT_FAILED = "submit.unified.contract_failed"
# The DSPy optimizer that bootstraps demos from the task model alone.
_DSPY_WITHOUT_REFLECTION = frozenset({"dspy_bootstrap_fewshot"})
# Common proxies (nginx, AWS ALB) drop a request silent for 60 seconds, and a
# budgetless submission's setup check takes minutes. The request answers 202
# well before that while the submission continues in the background.
_SUBMISSION_WAIT_SECONDS = 25.0
_SUBMISSION_RETRY_AFTER_SECONDS = 5
# A submission that answered 202 keeps its outcome for the next same-key
# retry, so a failed check is reported instead of run and paid for again.
_SUBMISSION_OUTCOME_TTL_SECONDS = 900.0
_SUBMISSION_IN_PROGRESS = "submission.in_progress"


class SubmissionInProgressError(DomainError):
    """Report that another attempt is still running this submission's setup check.

    The code stays ``budget.pending`` so a continuous cycle keeps replaying the
    same key, while the HTTP routes answer 202 for a client to retry.
    """

    def __init__(self) -> None:
        """Build the in-progress refusal."""
        super().__init__("budget.pending", status=409)


class _InFlightSubmissions:
    """Run each keyed submission once per process and hold its outcome for a retry."""

    def __init__(self) -> None:
        """Start with no submissions."""
        self._lock = threading.Lock()
        self._futures: dict[tuple[str, str], Future] = {}
        self._finished_at: dict[tuple[str, str], float] = {}

    def start(self, scope: tuple[str, str] | None, work: Callable[[], OptimizationSubmissionResponse]) -> Future:
        """Join the running submission for a scope, or start ``work`` in a background thread.

        Args:
            scope: Account and replay key, or None for a request without one.
            work: The complete submission.

        Returns:
            Future of the submission's response or error.
        """
        with self._lock:
            now = time.monotonic()
            for stale, finished in list(self._finished_at.items()):
                if now - finished > _SUBMISSION_OUTCOME_TTL_SECONDS:
                    self._discard(stale)
            if scope is not None and scope in self._futures:
                return self._futures[scope]
            future: Future = Future()
            if scope is not None:
                self._futures[scope] = future
                future.add_done_callback(lambda _done: self._mark_finished(scope))
        context = contextvars.copy_context()
        threading.Thread(target=context.run, args=(self._run, future, work), daemon=True).start()
        return future

    def forget(self, scope: tuple[str, str] | None, future: Future) -> None:
        """Drop a delivered outcome so later retries read the stored run instead.

        Args:
            scope: Scope the future was started under.
            future: The delivered submission.
        """
        with self._lock:
            if scope is not None and self._futures.get(scope) is future:
                self._discard(scope)

    def _mark_finished(self, scope: tuple[str, str]) -> None:
        """Start the outcome's retention window.

        Args:
            scope: Scope of the finished submission.
        """
        with self._lock:
            if scope in self._futures:
                self._finished_at[scope] = time.monotonic()

    def _discard(self, scope: tuple[str, str]) -> None:
        """Remove one scope; the caller holds the lock.

        Args:
            scope: Scope to remove.
        """
        self._futures.pop(scope, None)
        self._finished_at.pop(scope, None)

    @staticmethod
    def _run(future: Future, work: Callable[[], OptimizationSubmissionResponse]) -> None:
        """Settle the future with the submission's response or error.

        Args:
            future: Future the waiting requests share.
            work: The complete submission.
        """
        try:
            future.set_result(work())
        except BaseException as error:
            future.set_exception(error)


_IN_FLIGHT_SUBMISSIONS = _InFlightSubmissions()


def _submit_within_proxy_timeout(
    username: str,
    replay_key: str | None,
    work: Callable[[], OptimizationSubmissionResponse],
) -> OptimizationSubmissionResponse | JSONResponse:
    """Answer a submission before common proxy timeouts, even while its setup check runs.

    A request with the same replay key joins the running submission instead of
    starting a second one. A submission still running after the wait answers
    202, and the client retries the same request to collect the run or its error.

    Args:
        username: Authenticated submitter.
        replay_key: The caller's ``Idempotency-Key``, or the synthesized key of
            a keyless budgetless request; None when the request has neither.
        work: The complete submission.

    Returns:
        The submission response, or a 202 asking the client to retry.
    """
    scope = (username, replay_key) if replay_key else None
    future = _IN_FLIGHT_SUBMISSIONS.start(scope, work)
    wait([future], timeout=_SUBMISSION_WAIT_SECONDS)
    if not future.done():
        return _in_progress_response()
    _IN_FLIGHT_SUBMISSIONS.forget(scope, future)
    try:
        return future.result()
    except SubmissionInProgressError:
        return _in_progress_response()


def _in_progress_response() -> JSONResponse:
    """Tell the client its submission is still running and when to retry with the same key."""
    return JSONResponse(
        status_code=202,
        content={
            "status": "in_progress",
            "code": _SUBMISSION_IN_PROGRESS,
            "detail": "The setup check is still running. Send the same request again to get the run.",
        },
        headers={"Retry-After": str(_SUBMISSION_RETRY_AFTER_SECONDS)},
    )


def _ensure_api_budget(
    payload,
    job_store,
    workflow: str,
    budget_creation_key: str | None,
    user: AuthenticatedUser,
) -> None:
    """Give older API requests the same durable setup and run authority as the wizard.

    Args:
        payload: Materialized typed request, optionally lacking modern budget fields.
        job_store: Authoritative wallet and job database.
        workflow: DSPy or Anything request family.
        budget_creation_key: Stable account-scoped identity for budget creation.
        user: Authenticated submitter.
    """
    if payload.execution_budget_id is not None:
        return
    engine = getattr(job_store, "engine", None)
    if engine is None or budget_creation_key is None:
        raise DomainError("budget.invalid", status=503)
    service = BudgetService(engine=engine)
    try:
        # A retry reuses the original approved total: the balance it would be
        # recomputed from moves once the first attempt's check reserves spend.
        budget = service.find_by_creation_key(user.username, budget_creation_key)
        if budget is None and settings.local_mode:
            # Local mode charges no managed spend or platform fee, so the
            # budget only records usage and never stops the run.
            total = payload.max_cost_cents or max(1, min(settings.local_grant_cents, MAX_CENTS))
            budget = service.create(user.username, total, idempotency_key=budget_creation_key, uncapped=True)
        elif budget is None:
            spendable = _enforce_balance(job_store, user.username, payload.token_source)
            total = payload.max_cost_cents
            if total is None:
                total = cost_ceiling_budget(spendable or 0, payload.token_source)
            budget = service.create(user.username, total, idempotency_key=budget_creation_key)
    except BudgetError as error:
        raise budget_http_error(error) from error
    payload.execution_budget_id = budget.id
    payload.execution_budget_revision = budget.revision
    payload.max_cost_cents = budget.total_cents
    if payload.seed is None:
        payload.seed = setup_seed(budget.id)
    checked = run_preflight(
        WizardPreflightRequest(
            scope="execution",
            workflow=workflow,
            payload=payload.model_dump(mode="json", by_alias=True),
            execution_budget_id=budget.id,
            execution_budget_revision=budget.revision,
        ),
        user,
        job_store,
    )
    if checked.pending_reason is not None and checked.pending_reason.category == "setup_incomplete":
        # Only an attempt that still holds the claim reads as incomplete:
        # a same-key retry reached a check another request is running.
        raise SubmissionInProgressError()
    if checked.status == "failed" and any(
        check.key == BUDGET_CHECK_KEY and check.status == "failed" and check.field != BALANCE_FIELD
        for check in checked.checks
    ):
        # The limit, not the account balance, stopped the check, so name the
        # limit rather than the optimizer or the columns.
        raise DomainError("budget.below_minimum", status=409)
    if checked.status != "succeeded":
        raise DomainError(
            "budget.pending" if checked.status == "pending" else "submission.validation_failed", status=409
        )
    payload.preflight_id = checked.id
    payload.preflight_fingerprint = checked.fingerprint


def _protected_submission(payload, job_store, workflow: str) -> dict | None:
    """Require current complete setup evidence before accepting a protected submission.

    Args:
        payload: Typed public run request.
        job_store: Authoritative storage for budget and evidence ownership.
        workflow: DSPy or Anything.

    Returns:
        Canonical verified inputs for the atomic attachment transaction, or None for a legacy request.
    """
    if payload.execution_budget_id is None:
        return None
    if payload.seed is None:
        payload.seed = setup_seed(payload.execution_budget_id)
    try:
        budget = BudgetService(engine=job_store.engine).get(payload.execution_budget_id, payload.username)
        if budget.revision != payload.execution_budget_revision:
            raise DomainError("budget.conflict", status=409)
        canonical = _persistable_payload(payload, job_store)
        PreflightStore(job_store.engine).require_current(
            username=payload.username,
            budget_id=budget.id,
            identity=payload.preflight_id,
            fingerprint=payload.preflight_fingerprint,
            workflow=workflow,
            payload=canonical,
        )
    except BudgetError as error:
        raise budget_http_error(error) from error
    return canonical


def _persistable_payload(payload, job_store) -> dict:
    """Serialize a request with protected MCP credentials replaced by an opaque reference.

    Args:
        payload: Typed submission request.
        job_store: Store whose engine owns the execution credential vault.

    Returns:
        Canonical payload safe for preflight fingerprinting and job persistence.
    """
    canonical = payload.model_dump(mode="json", by_alias=True)
    if payload.execution_budget_id is None:
        return canonical
    return protect_execution_credentials(
        canonical,
        username=payload.username,
        binding_id=payload.execution_budget_id,
        vault=ProtectedCredentialVault(engine=job_store.engine),
    )


def _preflight_workflow(payload) -> str:
    """Name the request family a payload's setup evidence was recorded under.

    Args:
        payload: Typed submission request.

    Returns:
        ``"unified"``, ``"anything"`` or ``"dspy"``.
    """
    if isinstance(payload, UnifiedRunRequest):
        return UNIFIED_WORKFLOW
    return "anything" if isinstance(payload, BlackboxRunRequest) else "dspy"


def _create_submission_job(job_store, optimization_id: str, payload, key: str | None, verified: dict | None) -> None:
    """Atomically attach verified setup spend to the single accepted root job.

    Args:
        job_store: Database-backed job store.
        optimization_id: New root identity.
        payload: Typed request whose runtime budget generation is stamped after attachment.
        key: Account-scoped submission replay identity.
        verified: Exact inputs validated by the preflight authority.
    """
    if verified is None:
        job_store.create_job(optimization_id, username=payload.username, idempotency_key=key)
        return
    try:
        job = job_store.create_job(
            optimization_id,
            username=payload.username,
            idempotency_key=key,
            budget_service=BudgetService(engine=job_store.engine),
            preflight_store=PreflightStore(job_store.engine),
            execution_budget_id=payload.execution_budget_id,
            execution_budget_revision=payload.execution_budget_revision,
            preflight_id=payload.preflight_id,
            preflight_fingerprint=payload.preflight_fingerprint,
            preflight_payload=verified,
            preflight_workflow=_preflight_workflow(payload),
        )
        payload.execution_budget_generation = job["execution_budget_generation"]
    except BudgetError as error:
        raise budget_http_error(error) from error


def _execution_overview(payload) -> dict:
    """Expose the attached budget and runtime identity on a protected job overview."""
    if payload.execution_budget_id is None:
        return {}
    return {
        "execution_budget_id": payload.execution_budget_id,
        "execution_budget_generation": payload.execution_budget_generation,
        "execution_runtime": getattr(payload, "execution_runtime", None),
    }


AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]
IdempotencyKeyHeader = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        description=(
            "Optional client-supplied dedup key. When the same key is reused "
            "for the same authenticated submitter, the original submission "
            "response is returned and no new job is enqueued. A budgetless "
            "legacy request with no key receives a content-addressed replay "
            "identity; submit an explicit new key or change the request to "
            "intentionally start the same work again."
        ),
        max_length=128,
    ),
]


def _persist_and_signal_job(
    job_store,
    service,
    optimization_id: str,
    payload_dump: dict,
) -> None:
    """Persist a pending payload and optionally wake the in-process worker.

    Args:
        job_store: Shared database-backed job store.
        service: DSPy service passed to an enabled in-process worker.
        optimization_id: Identifier of the pending job row.
        payload_dump: JSON-compatible request payload to persist.
    """
    job_store.update_job(
        optimization_id,
        payload=payload_dump,
        code_version=settings.code_version,
    )
    if settings.worker_enabled:
        get_worker(job_store, service=service).enqueue_job(optimization_id)


def _existing_submission_response(job_store, optimization_id: str) -> OptimizationSubmissionResponse | None:
    """Rehydrate a previous submission's response from the persisted overview.

    Used when an ``Idempotency-Key`` header matches a prior submission so the
    retry returns the same shape it did the first time without re-enqueuing.

    Args:
        job_store: Source of the persisted job + payload overview.
        optimization_id: Identifier matched via :meth:`find_job_by_idempotency_key`.

    Returns:
        The rebuilt response, or ``None`` if the job vanished between the
        lookup and the rehydration (treated as no-dedup-hit by callers).
    """
    try:
        job = job_store.get_job(optimization_id)
    except KeyError:
        return None
    overview = job.get("payload_overview") or {}
    optimization_type = overview.get(PAYLOAD_OVERVIEW_OPTIMIZATION_TYPE) or OPTIMIZATION_TYPE_RUN
    status_value = job.get("status") or OptimizationStatus.pending.value
    try:
        status_enum = OptimizationStatus(status_value)
    except ValueError:
        status_enum = OptimizationStatus.pending
    return OptimizationSubmissionResponse(
        optimization_id=optimization_id,
        optimization_type=cast(OptimizationType, optimization_type),
        status=status_enum,
        created_at=job.get("created_at") or datetime.now(UTC),
        name=overview.get(PAYLOAD_OVERVIEW_NAME, ""),
        username=overview.get(PAYLOAD_OVERVIEW_USERNAME, ""),
        module_name=overview.get(PAYLOAD_OVERVIEW_MODULE_NAME, ""),
        optimizer_name=overview.get(PAYLOAD_OVERVIEW_OPTIMIZER_NAME, ""),
    )


# Statuses whose runs hold a live claim on the balance: queued/leased work,
# plus paused runs — resume re-enqueues those without a fresh balance gate.
_COMMITTED_JOB_STATUSES = ("pending", "validating", "running", "paused")


def _enforce_balance(job_store, username: str, token_source: str) -> int | None:
    """Block a depleted account from starting a run, and report its free balance.

    Reads the spendable balance after StripeBillingService has subtracted both
    authoritative operation holds and database-backed legacy job commitments.
    Subtracting the legacy ceilings again would reject funded submissions.
    Concurrent dispatch is serialized by the shared ledger. There is no free
    allowance — a brand-new account is gated until it tops up. Both run
    modes are gated: a managed run spends its full per-token cost, and a BYOK
    run may spend on sandbox usage even though its model calls carry no fee. The returned balance
    feeds the per-run cost ceiling (see :func:`_cap_cost_ceiling_to_balance`) so
    a run can never spend past what the account holds.

    Args:
        job_store: Job-store instance whose ORM engine backs the billing tables.
        username: Account attempting the submission.
        token_source: ``"managed"`` or ``"byok"`` — carried to the cost-ceiling cap.

    Returns:
        The account's uncommitted spendable cents, or ``None`` for a store
        with no SQL engine (legacy/in-memory).

    Raises:
        DomainError: 402 when the account has no spendable balance, or every
            remaining cent is already committed to active runs.
    """
    engine = getattr(job_store, "engine", None)
    if engine is None or not username:
        return None
    service = StripeBillingService(engine=engine)
    spendable = service.spendable_cents(username)
    if spendable <= 0:
        raise DomainError("billing.insufficient_funds", status=402)
    return spendable


def _enforce_global_daily_spend_ceiling(job_store) -> None:
    """Refuse a submission once platform-wide 24h spend hits the configured ceiling.

    A cost backstop that sits above the per-user balance gate: it caps the whole
    platform's trailing-24h run spend, so a spike in traffic (or an abusive
    fleet of funded accounts) cannot run the shared provider float dry. No-op
    when the ceiling is unset (``0``) or the store has no SQL engine. Tripping
    it logs at ERROR, which the alert log handler forwards to operators, so the
    refusal is never silent. Local mode has no shared provider float, so the
    check is off there.

    Args:
        job_store: Job-store instance whose ORM engine backs the billing tables.

    Raises:
        DomainError: 503 ``submission.capacity_reached`` when the trailing-window
            spend is at or above the ceiling. The message is deliberately generic
            so internal budget figures are not leaked to callers.
    """
    ceiling = settings.global_daily_spend_ceiling_cents
    if ceiling <= 0 or settings.local_mode:
        return
    engine = getattr(job_store, "engine", None)
    if engine is None:
        return
    service = StripeBillingService(engine=engine)
    spent = service.cents_spent_since(datetime.now(UTC) - timedelta(hours=24))
    if spent >= ceiling:
        # ERROR (not WARNING) so AlertLogHandler pages an operator: every new
        # submission is refused until spend ages out or the ceiling is raised.
        logger.error(
            "Global daily spend ceiling reached: %d cents spent in 24h (ceiling %d); refusing new submissions",
            spent,
            ceiling,
        )
        raise DomainError("submission.capacity_reached", status=503)


def _enforce_submission_admission(job_store, username: str) -> None:
    """Gate a submission on the global kill-switches and the per-user concurrency cap.

    Runs before any dataset materialization or payload validation so an
    over-cap or globally-paused submission is rejected cheaply, and after the
    idempotency short-circuit so a retry of an already-accepted run is never
    blocked. Four controls, in order: a per-user request-rate cap (the
    cross-replica limiter that stops a runaway script), a manual global pause
    (an operator emergency brake), the automatic platform-wide daily spend
    ceiling (:func:`_enforce_global_daily_spend_ceiling`), and a per-user cap on
    concurrently active runs. Each is a no-op when its setting is unset/zero.

    Args:
        job_store: Job-store instance whose ORM engine backs jobs and billing.
        username: Account attempting the submission.

    Raises:
        DomainError: 429 ``rate_limit.exceeded`` when the user exceeds the
            per-minute submission rate; 503 ``submission.capacity_reached`` when
            submissions are paused or the daily spend ceiling is reached; 429
            ``quota.concurrent_reached`` when the user is at their active-run cap.
    """
    enforce_submission_rate(username)
    if settings.submissions_paused:
        raise DomainError("submission.capacity_reached", status=503)
    _enforce_global_daily_spend_ceiling(job_store)
    has_pro_plan = getattr(job_store, "has_pro_plan", None)
    if callable(has_pro_plan) and has_pro_plan(username):
        limit = settings.pro_max_concurrent_jobs_per_user
    else:
        limit = settings.max_concurrent_jobs_per_user
    if limit <= 0:
        return
    counter = getattr(job_store, "count_jobs_by_status", None)
    if not callable(counter):
        return
    counts = counter(username=username)
    active = sum(int(counts.get(status, 0)) for status in _COMMITTED_JOB_STATUSES)
    if active >= limit:
        raise DomainError("quota.concurrent_reached", status=429, limit=limit)


def _cap_cost_ceiling_to_balance(
    payload: _OptimizationRequestBase | BlackboxRunRequest, spendable: int | None, token_source: str
) -> None:
    """Pin a run's cost ceiling to what the account's spendable balance can back.

    With model-tier gating gone, any model is runnable and the balance is the only
    thing between a user and an expensive one — so a run must not be allowed to
    spend more than the account holds. This clamps ``max_cost_cents`` to
    the balance-backed budget: a user-set cap still wins when it is tighter, but an
    absent or larger cap is lowered to the budget. For a managed run the budget is
    the spendable balance; for a BYOK run it is effectively uncapped, since model calls
    carry no Skynet fee (see :func:`cost_ceiling_budget`). The run's
    ``CostCeilingCallback`` then hard-stops it once usage crosses that budget, so an
    over-ambitious run fails mid-flight (and is never billed) instead of driving the
    balance negative. A no-op for engine-less stores, where ``spendable`` is ``None``.

    Args:
        payload: The submission whose ``max_cost_cents`` is clamped in place.
        spendable: The account's spendable cents from
            :func:`_enforce_balance`, or ``None`` to leave the cap as-is.
        token_source: ``"managed"`` or ``"byok"`` — sets the balance→budget conversion.
    """
    if spendable is None:
        return
    budget = cost_ceiling_budget(spendable, token_source)
    current = payload.max_cost_cents
    payload.max_cost_cents = budget if current is None else min(current, budget)


def _enforce_byok_connections(job_store, username: str, model_configs: list[ModelConfig]) -> dict[str, str | None]:
    """Refuse BYOK models without a verified saved provider connection.

    In BYOK mode every model authenticates with the user's own provider key,
    resolved from the encrypt-at-rest vault at run time. If the account saved no
    connection for a model's provider, the run would have nothing to authenticate
    with, so reject it at submit with a clear, translated error rather than
    letting the job fail mid-run. Managed runs are exempt (they spend the
    account balance). Models without a resolvable provider are rejected.
    Connection checks are skipped when the store exposes no SQL engine.

    Args:
        job_store: Job-store instance whose ORM engine backs the billing tables.
        username: Account attempting the submission.
        model_configs: Executable configs with normalized per-model sources.

    Returns:
        Saved provider endpoints for the verified connections.

    Raises:
        DomainError: 400 ``billing.byok_missing_connection`` listing the providers
            the account has no saved connection for, or
            ``billing.byok_model_not_served`` when a model's prefix names a
            provider its chosen connection cannot route to.
    """
    # A model id carries a LiteLLM prefix (``gemini``, ``together_ai``) but the
    # key is saved under the vault slug (``google``, ``together``); bridge the two
    # exactly as the run path does so the gate sees the same connections it will.
    providers: set[str] = set()
    routes: list[tuple[ModelConfig, str, str | None, str]] = []
    for config in model_configs:
        if config.token_source != TOKEN_SOURCE_BYOK:
            continue
        name = config.normalized_identifier()
        provider = (config.byok_provider or "").strip()
        prefix = byok_model_prefix(name, provider)
        if not provider and prefix is not None:
            provider = byok_provider_for_litellm(prefix)
        if not provider or (not config.byok_provider and provider not in UPSTREAMS):
            raise DomainError("submission.byok_provider_unsupported", status=400)
        providers.add(provider)
        routes.append((config, name, prefix, provider))
    engine = getattr(job_store, "engine", None)
    if engine is None or not username:
        return {}
    vault = ProviderKeyVault(engine=engine)
    missing = sorted(provider for provider in providers if not vault.has_verified_connection(username, provider))
    if missing:
        raise DomainError("billing.byok_missing_connection", status=400, provider=", ".join(missing))
    api_bases = {view.provider: view.api_base for view in vault.list_keys(username).keys if view.status == "verified"}
    for config, name, prefix, provider in routes:
        try:
            upstream = resolve_upstream(
                {
                    **config.model_dump(),
                    "name": name,
                    "byok_provider": provider,
                    "base_url": api_bases.get(provider),
                    "extra": safe_connection_params(config.extra),
                }
            )
        except UnpricedOperationError as error:
            raise DomainError("submission.byok_provider_unsupported", status=400) from error
        prefix = byok_model_prefix(name, config.byok_provider, custom=upstream.slug == "custom")
        if not byok_upstream_routable(prefix, provider, upstream):
            raise DomainError("billing.byok_model_not_served", status=400, model=name, provider=provider)
    return api_bases


def _admit_byok_models(
    job_store: Any, username: str, configs: list[ModelConfig], reflection: ModelConfig | None, novelty: bool
) -> dict[str, str | None]:
    """Admit model connections and novelty before setup or submission.

    Args:
        job_store: Store holding verified connections.
        username: Connection owner.
        configs: Normalized executable model configurations.
        reflection: Reflection model when the request has one.
        novelty: Whether novelty checking is enabled.

    Returns:
        Verified provider endpoint bases.

    Raises:
        DomainError: When a connection or novelty route is unsupported.
    """
    bases = _enforce_byok_connections(job_store, username, configs)
    if reflection is not None:
        validate_byok_novelty(reflection.model_dump(), novelty, api_bases=bases)
    return bases


def rows_to_cases(rows: list[dict], *, input_fields: list[str], expected_field: str | None) -> list[dict]:
    """Map table rows onto unified cases with the run's agreed case schema.

    Args:
        rows: Table rows keyed by column name.
        input_fields: Columns that form each case's input; empty means every column.
        expected_field: Column holding the expected answer, if any.

    Returns:
        One ``{"input", "expected"}`` case per row.
    """
    cases = []
    # Same shape as the wizard's inline ``rowsToCases`` (inputs-mode.ts):
    # ``expected`` keyed by the field name, which DSPy metrics read as
    # ``gold.<field>``, so a by-reference submit and its inline twin match.
    for row in rows:
        fields = input_fields or list(row)
        cases.append(
            {
                "input": {field: row.get(field) for field in fields if field != expected_field},
                "expected": {expected_field: row.get(expected_field)} if expected_field else None,
            }
        )
    return cases


def _materialize_unified_cases(payload: UnifiedRunRequest, *, job_store, username: str) -> None:
    """Inline a staged dataset into ``payload.data.cases`` as unified cases.

    Each staged row becomes one case: ``input`` keeps the row's
    ``input_fields`` (every column but ``expected_field`` when none are
    named) and ``expected`` takes ``expected_field`` when it is set. The
    staged row is left in place so a rejected submit can be retried; the TTL
    sweep reclaims it. The rebuilt data block is validated again, so a staged
    table that does not fit the data mode is rejected like an inline one.

    Args:
        payload: The unified request, mutated in place.
        job_store: Backend exposing ``get_staged_dataset``.
        username: Authenticated submitter; staging rows are scoped to one owner.

    Raises:
        DomainError: 400 ``submission.staged_dataset_not_found`` when the id
            is unknown or owned by another user, or 422
            ``submit.unified.contract_failed`` when the rows do not fit the
            data mode.
    """
    data = payload.data
    staged_id = data.staged_dataset_id
    if not staged_id:
        return
    if data.cases is None:
        rows = job_store.get_staged_dataset(staged_id, username)
        if not rows:
            raise DomainError(
                I18nKey.SUBMISSION_STAGED_DATASET_NOT_FOUND,
                status=400,
                staged_dataset_id=staged_id,
            )
        cases = rows_to_cases(rows, input_fields=data.input_fields, expected_field=data.expected_field)
    else:
        cases = [case.model_dump(mode="json") for case in data.cases]
    try:
        payload.data = UnifiedData.model_validate(
            {**data.model_dump(mode="json"), "cases": cases, "staged_dataset_id": None}
        )
    except ValidationError as error:
        raise DomainError(_UNIFIED_CONTRACT_FAILED, status=422, error=_first_error(error)) from error


def _materialize_repo_cases(payload: BlackboxRunRequest, *, job_store, username: str) -> None:
    """Inline a repository run's staged dataset into ``payload.cases``.

    Rows map to cases the same way as a unified run's, through
    ``input_fields`` and ``expected_field``. Inline cases win over a staged
    id, and the id is always cleared so the stored payload never holds it.

    Args:
        payload: The repository run request, mutated in place.
        job_store: Backend exposing ``get_staged_dataset``.
        username: Authenticated submitter; staging rows are scoped to one owner.

    Raises:
        DomainError: 400 ``submission.staged_dataset_not_found`` when the id
            is unknown or owned by another user.
    """
    staged_id = payload.staged_dataset_id
    payload.staged_dataset_id = None
    if not staged_id or payload.cases:
        return
    payload.cases = staged_repo_cases(
        staged_id,
        input_fields=payload.input_fields,
        expected_field=payload.expected_field,
        job_store=job_store,
        username=username,
    )


def staged_repo_cases(
    staged_id: str, *, input_fields: list[str], expected_field: str | None, job_store, username: str
) -> list[dict]:
    """Read one owner's staged dataset as repository run cases.

    Args:
        staged_id: The staged dataset id the caller sent.
        input_fields: Columns that form each case's input; empty means every column.
        expected_field: Column holding the expected answer, if any.
        job_store: Backend exposing ``get_staged_dataset``.
        username: Authenticated submitter; staging rows are scoped to one owner.

    Returns:
        The staged rows mapped onto cases.

    Raises:
        DomainError: 400 ``submission.staged_dataset_not_found`` when the id
            is unknown or owned by another user.
    """
    rows = job_store.get_staged_dataset(staged_id, username)
    if not rows:
        raise DomainError(
            I18nKey.SUBMISSION_STAGED_DATASET_NOT_FOUND,
            status=400,
            staged_dataset_id=staged_id,
        )
    return rows_to_cases(rows, input_fields=input_fields, expected_field=expected_field)


def _first_error(error: ValidationError) -> str:
    """Return the first readable message of a Pydantic validation error.

    Args:
        error: The validation error raised while rebuilding a request block.

    Returns:
        The first error message, without Pydantic's ``Value error,`` prefix.
    """
    errors = error.errors()
    message = str(errors[0].get("msg", "")) if errors else str(error)
    return message.removeprefix("Value error, ")


def _default_unified_seed(payload: UnifiedRunRequest) -> None:
    """Seed a unified run from its task identity when the caller sent none.

    Never seeded from the optimization id: resubmitting the same task must
    reproduce the same train/val/test split.

    Args:
        payload: Unified request with its cases already materialized.
    """
    if payload.seed is None:
        payload.seed = stable_seed(task_fingerprint(payload))


def _unified_overview(
    payload: UnifiedRunRequest,
    *,
    token_sources_by_model: dict[str, str],
) -> dict:
    """Build the stored overview of a unified run.

    The run is stored as a black-box run so every existing read path keeps
    working; the extra keys say how the wizard configured it.

    Args:
        payload: The validated unified request with cases and seed set.
        token_sources_by_model: Normalized per-model billing sources.

    Returns:
        The overview dict for :meth:`set_payload_overview`.
    """
    artifact_kind = payload.task.artifact.kind
    optimizer_resolved = resolve_optimizer(payload.optimizer.choice, artifact_kind)
    cases = payload.data.cases or []
    splits = split_cases(
        cases,
        payload.data.mode,
        payload.data.split,
        shuffle=payload.data.shuffle,
        seed=cast(int, payload.seed),
    )
    reflection = payload.models.reflection_model
    reflection_name = reflection.normalized_identifier()
    task_model = payload.models.task_model
    return {
        PAYLOAD_OVERVIEW_OPTIMIZATION_TYPE: OPTIMIZATION_TYPE_BLACKBOX,
        **_execution_overview(payload),
        PAYLOAD_OVERVIEW_COMPOSITION: COMPOSITION_SINGLE,
        PAYLOAD_OVERVIEW_NAME: payload.name,
        PAYLOAD_OVERVIEW_DESCRIPTION: payload.description,
        PAYLOAD_OVERVIEW_USERNAME: payload.username,
        PAYLOAD_OVERVIEW_MODULE_NAME: BLACKBOX_MODULE_NAME,
        # The list and detail views show the engine through optimizer_name,
        # exactly as they do for a legacy black-box run's strategy engine.
        PAYLOAD_OVERVIEW_OPTIMIZER_NAME: optimizer_resolved,
        PAYLOAD_OVERVIEW_MODEL_NAME: reflection_name,
        PAYLOAD_OVERVIEW_MODEL_SETTINGS: strip_api_key(reflection.model_dump()),
        PAYLOAD_OVERVIEW_REFLECTION_MODEL: reflection_name,
        PAYLOAD_OVERVIEW_TASK_MODEL: task_model.normalized_identifier() if task_model else None,
        PAYLOAD_OVERVIEW_DATASET_ROWS: len(cases),
        PAYLOAD_OVERVIEW_SPLIT_FRACTIONS: payload.data.split.model_dump(),
        PAYLOAD_OVERVIEW_SHUFFLE: payload.data.shuffle,
        PAYLOAD_OVERVIEW_SEED: payload.seed,
        PAYLOAD_OVERVIEW_TOKEN_SOURCE: payload.token_source,
        PAYLOAD_OVERVIEW_TOKEN_SOURCES_BY_MODEL: token_sources_by_model,
        PAYLOAD_OVERVIEW_MAX_COST_CENTS: payload.max_cost_cents,
        PAYLOAD_OVERVIEW_ESTIMATED_LOW: payload.estimated_cents_low,
        PAYLOAD_OVERVIEW_ESTIMATED_HIGH: payload.estimated_cents_high,
        PAYLOAD_OVERVIEW_IS_PRIVATE: payload.is_private,
        PAYLOAD_OVERVIEW_PAYLOAD_KIND: UNIFIED_PAYLOAD_KIND,
        PAYLOAD_OVERVIEW_ARTIFACT_KIND: artifact_kind,
        PAYLOAD_OVERVIEW_OPTIMIZER_CHOICE: payload.optimizer.choice,
        PAYLOAD_OVERVIEW_OPTIMIZER_RESOLVED: optimizer_resolved,
        PAYLOAD_OVERVIEW_DATA_MODE: payload.data.mode,
        PAYLOAD_OVERVIEW_SPLIT_COUNTS: {
            "train": splits.counts.train,
            "val": splits.counts.val,
            "test": splits.counts.test,
        },
    }


def _optimizer_option(
    option_id: OptimizerChoice,
    artifact_kind: OptimizerTarget,
    catalog: BlackboxEngineCatalogResponse,
) -> OptimizerOption:
    """Describe one optimizer choice with its availability on this deployment.

    Args:
        option_id: The optimizer choice being described.
        artifact_kind: A DSPy program, or ``"repo"``; fixes Auto's resolution.
        catalog: The black-box engine catalog for the run's target.

    Returns:
        The catalog option.
    """
    resolved = resolve_optimizer(option_id, artifact_kind)
    family = optimizer_family(resolved)
    available = True
    reason: str | None = None
    native_proposer = False
    if family == "auto":
        available = catalog.auto_available
        reason = catalog.auto_unavailable_reason
    elif family == "blackbox":
        engine = next((entry for entry in catalog.engines if entry.id == resolved), None)
        available = engine is not None and engine.available
        reason = engine.unavailable_reason if engine is not None else None
        native_proposer = ENGINES[resolved].requires_proposer if resolved in ENGINES else False
    return OptimizerOption(
        id=option_id,
        family=optimizer_family(option_id),
        available=available,
        unavailable_reason=None if available else reason,
        needs_reflection_model=resolved not in _DSPY_WITHOUT_REFLECTION,
        uses_feedback=resolved in FEEDBACK_OPTIMIZERS,
        supports_iteration_limit=resolved in BLACKBOX_ITERATION_LIMIT_ENGINES,
        native_proposer=native_proposer,
    )


def submit_unified_payload(
    payload: UnifiedRunRequest,
    *,
    service,
    job_store,
    current_user: AuthenticatedUser,
    idempotency_key: str | None,
) -> OptimizationSubmissionResponse:
    """Admit, fund, verify and enqueue one unified run for an authenticated owner.

    The ``/optimize`` route and a continuous-optimization cycle share this one
    path, so a scheduled run gets the same preflight, protected budget and
    replay protection as a wizard submission.

    Args:
        payload: The unified request, mutated in place.
        service: Optimization service handed to the in-process worker.
        job_store: Job store that persists the submission.
        current_user: Owner of the new run.
        idempotency_key: Optional dedup key; a repeat returns the original.

    Returns:
        An ``OptimizationSubmissionResponse`` for the new or replayed run.

    Raises:
        DomainError: 400 (unknown staged dataset), 402 (no balance), 409
            (quota or stale setup evidence), 422 (workspace code does not
            fit the optimizer).
    """
    payload.username = current_user.username
    # Resolved before the replay key is derived so a by-reference submit
    # and its inline twin hash to the same request.
    _materialize_unified_cases(payload, job_store=job_store, username=payload.username)

    replay_keys = resolve_submission_replay_keys(
        payload,
        username=payload.username,
        workflow=UNIFIED_WORKFLOW,
        supplied_key=idempotency_key,
    )
    normalized_key = replay_keys.job
    if normalized_key:
        existing_id = job_store.find_job_by_idempotency_key(payload.username, normalized_key)
        if existing_id:
            cached = _existing_submission_response(job_store, existing_id)
            if cached is not None:
                logger.info(
                    "Idempotent unified retry hit: returning existing %s for user=%s",
                    existing_id,
                    payload.username,
                )
                return cached

    _enforce_submission_admission(job_store, payload.username)

    _requested_source = payload.token_source
    _model_configs, _token_sources_by_model = normalize_model_token_sources(payload)
    fill_proposer_host_cli(job_store, payload.username, payload)
    enforce_local_mode_sources(payload, _requested_source, _model_configs)
    if not settings.local_mode:
        _spendable = _enforce_balance(job_store, payload.username, payload.token_source)
        if payload.execution_budget_id is None:
            _cap_cost_ceiling_to_balance(payload, _spendable, payload.token_source)
    _admit_byok_models(
        job_store,
        payload.username,
        _model_configs,
        payload.models.reflection_model,
        bool(payload.optimizer.shinka and payload.optimizer.shinka.novelty),
    )

    try:
        validate_unified_payload(payload)
    except ValueError as exc:
        raise DomainError(_UNIFIED_CONTRACT_FAILED, status=422, error=str(exc)) from exc
    # Seeded before the budget step, which would otherwise default the
    # seed from the budget id and break split reproducibility.
    _default_unified_seed(payload)

    _ensure_api_budget(
        payload,
        job_store,
        UNIFIED_WORKFLOW,
        replay_keys.budget,
        current_user,
    )
    verified = _protected_submission(payload, job_store, UNIFIED_WORKFLOW)

    optimization_id = str(uuid4())
    overview = _unified_overview(payload, token_sources_by_model=_token_sources_by_model)

    # Same single-serialization pattern as the other submit routes: the
    # dump copies every case, so it is built once after the last mutation.
    payload_dump = _persistable_payload(payload, job_store)
    enforce_job_quota(job_store, payload.username)
    enforce_storage_quota(job_store, payload.username, incoming_bytes=json_byte_size(payload_dump))

    _create_submission_job(job_store, optimization_id, payload, normalized_key, verified)
    payload_dump["execution_budget_generation"] = payload.execution_budget_generation
    overview.update(_execution_overview(payload))
    job_store.set_payload_overview(optimization_id, overview)

    _persist_and_signal_job(job_store, service, optimization_id, payload_dump)

    optimizer_name = overview[PAYLOAD_OVERVIEW_OPTIMIZER_NAME]
    logger.info(
        "Enqueued unified run %s: artifact=%s optimizer=%s mode=%s",
        optimization_id,
        payload.task.artifact.kind,
        optimizer_name,
        payload.data.mode,
    )

    return OptimizationSubmissionResponse(
        optimization_id=optimization_id,
        optimization_type=cast(OptimizationType, OPTIMIZATION_TYPE_BLACKBOX),
        status=OptimizationStatus.pending,
        created_at=datetime.now(UTC),
        name=payload.name,
        username=payload.username,
        module_name=BLACKBOX_MODULE_NAME,
        optimizer_name=optimizer_name,
    )


def submit_repo_payload(
    payload: BlackboxRunRequest,
    *,
    service,
    job_store,
    current_user: AuthenticatedUser,
    idempotency_key: str | None,
) -> OptimizationSubmissionResponse:
    """Admit, fund, verify and enqueue one repository run for an authenticated owner.

    The ``/blackbox/run`` route and the MCP ``repo_draft_submit`` tool share
    this one path, so an agent submission gets the same admission, balance,
    protected budget and replay protection as a wizard submission.

    Args:
        payload: The black-box request, mutated in place.
        service: Optimization service handed to the in-process worker.
        job_store: Job store that persists the submission.
        current_user: Owner of the new run.
        idempotency_key: Optional dedup key; a repeat returns the original.

    Returns:
        An ``OptimizationSubmissionResponse`` for the new or replayed run.

    Raises:
        DomainError: 400 (not a repository target, or the scorer does not
            load), 402 (no balance), 409 (quota or a failed setup check).
    """
    if payload.target.kind != BLACKBOX_TARGET_REPO or payload.target.repo is None:
        raise DomainError("submission.validation_failed", status=400)
    payload.username = current_user.username
    _materialize_repo_cases(payload, job_store=job_store, username=payload.username)

    replay_keys = resolve_submission_replay_keys(
        payload,
        username=payload.username,
        workflow=_BLACKBOX_WORKFLOW,
        supplied_key=idempotency_key,
    )
    normalized_key = replay_keys.job
    if normalized_key:
        existing_id = job_store.find_job_by_idempotency_key(payload.username, normalized_key)
        if existing_id:
            cached = _existing_submission_response(job_store, existing_id)
            if cached is not None:
                logger.info(
                    "Idempotent repo retry hit: returning existing %s for user=%s",
                    existing_id,
                    payload.username,
                )
                return cached

    _enforce_submission_admission(job_store, payload.username)

    _requested_source = payload.token_source
    _model_configs, _token_sources_by_model = normalize_model_token_sources(payload)
    fill_proposer_host_cli(job_store, payload.username, payload)
    enforce_local_mode_sources(payload, _requested_source, _model_configs)
    if not settings.local_mode:
        _spendable = _enforce_balance(job_store, payload.username, payload.token_source)
        if payload.execution_budget_id is None:
            _cap_cost_ceiling_to_balance(payload, _spendable, payload.token_source)
    _admit_byok_models(
        job_store,
        payload.username,
        _model_configs,
        payload.reflection_model_settings,
        bool(payload.shinka and payload.shinka.novelty),
    )

    _ensure_api_budget(
        payload,
        job_store,
        _BLACKBOX_WORKFLOW,
        replay_keys.budget,
        current_user,
    )
    verified = _protected_submission(payload, job_store, _BLACKBOX_WORKFLOW)
    try:
        if verified is None:
            validate_blackbox_payload(payload)
    except ServiceError as exc:
        # Log the detail server-side only; don't leak it to the client.
        logger.warning("Repo run validation failed: %s", exc)
        raise DomainError("submission.validation_failed", status=400) from exc

    optimization_id = str(uuid4())
    if payload.seed is None:
        payload.seed = stable_seed(optimization_id)
    optimizer_name = payload.strategy.engine or BLACKBOX_STRATEGY_AUTO
    reflection_model = payload.reflection_model_settings.normalized_identifier()

    payload_dump = _persistable_payload(payload, job_store)
    enforce_job_quota(job_store, payload.username)
    enforce_storage_quota(job_store, payload.username, incoming_bytes=json_byte_size(payload_dump))

    _create_submission_job(job_store, optimization_id, payload, normalized_key, verified)
    payload_dump["execution_budget_generation"] = payload.execution_budget_generation
    job_store.set_payload_overview(
        optimization_id,
        {
            PAYLOAD_OVERVIEW_OPTIMIZATION_TYPE: OPTIMIZATION_TYPE_BLACKBOX,
            **_execution_overview(payload),
            PAYLOAD_OVERVIEW_COMPOSITION: COMPOSITION_SINGLE,
            PAYLOAD_OVERVIEW_NAME: payload.name,
            PAYLOAD_OVERVIEW_DESCRIPTION: payload.description,
            PAYLOAD_OVERVIEW_USERNAME: payload.username,
            PAYLOAD_OVERVIEW_MODULE_NAME: BLACKBOX_MODULE_NAME,
            PAYLOAD_OVERVIEW_OPTIMIZER_NAME: optimizer_name,
            # The reflection model is the only model a black-box run
            # bills, so it doubles as the overview's primary model.
            PAYLOAD_OVERVIEW_MODEL_NAME: reflection_model,
            PAYLOAD_OVERVIEW_MODEL_SETTINGS: strip_api_key(payload.reflection_model_settings.model_dump()),
            PAYLOAD_OVERVIEW_REFLECTION_MODEL: reflection_model,
            PAYLOAD_OVERVIEW_DATASET_ROWS: len(payload.cases or []),
            PAYLOAD_OVERVIEW_SEED: payload.seed,
            PAYLOAD_OVERVIEW_TOKEN_SOURCE: payload.token_source,
            PAYLOAD_OVERVIEW_TOKEN_SOURCES_BY_MODEL: _token_sources_by_model,
            PAYLOAD_OVERVIEW_MAX_COST_CENTS: payload.max_cost_cents,
            PAYLOAD_OVERVIEW_ESTIMATED_LOW: payload.estimated_cents_low,
            PAYLOAD_OVERVIEW_ESTIMATED_HIGH: payload.estimated_cents_high,
            PAYLOAD_OVERVIEW_IS_PRIVATE: payload.is_private,
        },
    )

    _persist_and_signal_job(job_store, service, optimization_id, payload_dump)

    logger.info(
        "Enqueued repo run %s: repository=%s optimizer=%s",
        optimization_id,
        payload.target.repo.repository,
        optimizer_name,
    )

    return OptimizationSubmissionResponse(
        optimization_id=optimization_id,
        optimization_type=cast(OptimizationType, OPTIMIZATION_TYPE_BLACKBOX),
        status=OptimizationStatus.pending,
        created_at=datetime.now(UTC),
        name=payload.name,
        username=payload.username,
        module_name=BLACKBOX_MODULE_NAME,
        optimizer_name=optimizer_name,
    )


def create_submissions_router(*, service, job_store) -> APIRouter:
    """Build the submissions router.

    Args:
        service: Optimization service handed to the in-process worker.
        job_store: Job-store instance used to persist new submissions.

    Returns:
        A FastAPI ``APIRouter`` exposing ``/optimize``, ``/blackbox/run``,
        ``/optimizers`` and ``/blackbox/engines``.
    """
    router = APIRouter()

    @router.post(
        "/optimize",
        response_model=OptimizationSubmissionResponse,
        status_code=201,
        summary="Submit an optimization run",
        tags=["agent"],
        responses={
            202: {
                "description": (
                    "The setup check is still running. Retry after ``Retry-After`` seconds "
                    "with the same body and ``Idempotency-Key`` to get the run or its error."
                )
            }
        },
    )
    def submit_unified_run(
        payload: UnifiedRunRequest,
        current_user: AuthenticatedUserDep,
        idempotency_key: IdempotencyKeyHeader = None,
    ) -> OptimizationSubmissionResponse | JSONResponse:
        """Queue one optimization of a DSPy program.

        Every DSPy optimizer runs through the black-box engine layer against
        the workspace code, so the
        run is stored as a black-box run marked ``payload_kind=unified_v1``.
        The persisted owner is the authenticated caller, not whatever the
        client posted.

        Args:
            payload: The unified request body validated by FastAPI.
            current_user: Authenticated submitter resolved from the bearer token.
            idempotency_key: Optional dedup key; a repeat returns the original.

        Returns:
            An ``OptimizationSubmissionResponse`` carrying the assigned id
            and ``pending`` status, or a 202 while the setup check still runs.

        Raises:
            DomainError: 400 (unknown staged dataset), 402 (no balance), 409
                (quota or stale setup evidence), 422 (workspace code does not
                fit the optimizer).
        """
        # Taken before the submission mutates the payload, so an identical
        # keyless retry joins the same in-flight submission and its outcome.
        replay_key = resolve_submission_replay_keys(
            payload,
            username=current_user.username,
            workflow=UNIFIED_WORKFLOW,
            supplied_key=idempotency_key,
        ).job
        return _submit_within_proxy_timeout(
            current_user.username,
            replay_key,
            lambda: submit_unified_payload(
                payload,
                service=service,
                job_store=job_store,
                current_user=current_user,
                idempotency_key=idempotency_key,
            ),
        )

    @router.post(
        "/blackbox/run",
        response_model=OptimizationSubmissionResponse,
        status_code=201,
        summary="Submit a repository optimization",
    )
    def submit_repo_run(
        payload: BlackboxRunRequest,
        current_user: AuthenticatedUserDep,
        idempotency_key: IdempotencyKeyHeader = None,
    ) -> OptimizationSubmissionResponse:
        """Queue a black-box optimization of a GitHub repository against a scorer.

        Only repository targets come through here; DSPy programs submit to
        ``/optimize``. Without an execution budget the
        server creates one and runs the setup check itself, which stages the
        repository and infers a setup command when none is given. The
        persisted owner is the authenticated caller.

        Args:
            payload: The black-box request body validated by FastAPI.
            current_user: Authenticated submitter resolved from the bearer token.
            idempotency_key: Optional dedup key; a repeat returns the original.

        Returns:
            An ``OptimizationSubmissionResponse`` carrying the assigned id
            and ``pending`` status.

        Raises:
            DomainError: 400 (not a repository target, or the scorer does not
                load), 402 (no balance), 409 (quota or a failed setup check).
        """
        return submit_repo_payload(
            payload,
            service=service,
            job_store=job_store,
            current_user=current_user,
            idempotency_key=idempotency_key,
        )

    @router.get(
        "/optimizers",
        response_model=OptimizerCatalogResponse,
        summary="List the optimizers that fit a setup and their availability",
        tags=["agent"],
    )
    def optimizer_catalog(
        current_user: AuthenticatedUserDep,
        artifact_kind: OptimizerTarget,
        data_mode: DataMode,
    ) -> OptimizerCatalogResponse:
        """Describe the optimizers the wizard may offer for a DSPy program or a repository.

        DSPy optimizers are always available; the black-box engines a
        repository run takes carry this deployment's availability.

        Args:
            current_user: Authenticated caller resolved from the bearer token.
            artifact_kind: ``dspy_program``, or ``repo`` for a repository run.
            data_mode: Whether the run has one input or input/output pairs.

        Returns:
            The relevant options, with what Auto resolves to. A repository
            run has no Auto, so its ``auto_resolves_to`` is empty.
        """
        del current_user
        repo = artifact_kind == BLACKBOX_TARGET_REPO
        catalog = engine_catalog(BLACKBOX_TARGET_REPO if repo else BLACKBOX_TARGET_TEXT)
        options = [
            _optimizer_option(option, artifact_kind, catalog) for option in optimizers_for(artifact_kind, data_mode)
        ]
        auto_resolves_to = [] if repo else [resolve_optimizer("auto", artifact_kind)]
        return OptimizerCatalogResponse(
            artifact_kind=artifact_kind,
            data_mode=data_mode,
            run_entry=RUN_ENTRY,
            metric_signature=METRIC_SIGNATURE,
            options=options,
            auto_resolves_to=auto_resolves_to,
            sandbox_available=catalog.sandbox_available,
            sandbox_reason=catalog.sandbox_reason,
        )

    @router.get(
        "/blackbox/engines",
        response_model=BlackboxEngineCatalogResponse,
        summary="List black-box engines and their availability",
        tags=["agent"],
    )
    def blackbox_engines(
        current_user: AuthenticatedUserDep,
        target: Literal["text", "agent", "repo"] = "text",
    ) -> BlackboxEngineCatalogResponse:
        """Describe every engine so the wizard can show why one cannot run here.

        Availability depends on the deployment (agent sandboxes) and on the
        job's target kind, so the caller names the target it is building.

        Args:
            current_user: Authenticated caller resolved from the bearer token.
            target: The target kind the job will use.
        Returns:
            The catalog in registry order.
        """
        del current_user
        return engine_catalog(target)

    return router
