"""Expose account-owned spending authority shared by wizard setup and its run."""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import AliasChoices, BaseModel, Field

from ...billing.budget_amounts import CENT_SCALE, MAX_CENTS, ceil_cents
from ...billing.budgets import (
    CHUNK_CENTS,
    BudgetConflictError,
    BudgetError,
    BudgetInFlightError,
    BudgetInsufficientError,
    BudgetNotFoundError,
    BudgetService,
    BudgetSnapshot,
    BudgetTotalConflictError,
)
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from .setup_billing import bills_setup_from_wallet

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]


class CreateExecutionBudgetRequest(BaseModel):
    total_cents: int = Field(
        validation_alias=AliasChoices("total_cents", "total_credits"), ge=1, le=MAX_CENTS, strict=True
    )
    uncapped: bool = Field(default=False, strict=True)


class UpdateExecutionBudgetRequest(CreateExecutionBudgetRequest):
    expected_revision: int = Field(ge=0, strict=True)


class ExecutionBudgetResponse(BaseModel):
    id: str
    total_cents: int
    uncapped: bool
    revision: int
    generation: int
    state: str
    job_id: str | None
    setup_spent_cents: Decimal
    run_spent_cents: Decimal
    reserved_cents: Decimal
    available_cents: Decimal
    billed_cents: int
    wallet_setup_spent_cents: Decimal
    wallet_run_spent_cents: Decimal
    wallet_reserved_cents: int
    account_available_cents: int
    external_spent_cents: Decimal
    pending_operations: int
    blocked_reason: str | None


def budget_response(snapshot: BudgetSnapshot) -> ExecutionBudgetResponse:
    """Serialize exact cent amounts without exposing an account identifier.

    Args:
        snapshot: Authoritative state read under the ledger's transaction locks.

    Returns:
        Response whose fractional amounts are decimal strings in JSON.
    """
    # The budget card reads run_spent_cents as the spend against the limit, so a
    # continuous-plan budget, whose limit also counts setup, reports both (SKY-925).
    return ExecutionBudgetResponse.model_validate({**asdict(snapshot), "run_spent_cents": snapshot.limit_spent_cents})


def budget_http_error(error: BudgetError) -> HTTPException:
    """Map admission failures to stable client states without authorizing work.

    Args:
        error: Expected ledger ownership, funding, or concurrency failure.

    Returns:
        An HTTP error with a machine-readable code and actionable message.
    """
    if isinstance(error, BudgetNotFoundError):
        status, code = 404, "budget.not_found"
    elif isinstance(error, BudgetInsufficientError):
        status, code = 402, "budget.insufficient"
    elif isinstance(error, BudgetInFlightError):
        status, code = 409, "budget.pending"
    elif isinstance(error, BudgetTotalConflictError):
        return DomainError(
            "budget.conflict",
            status=409,
            current_total_cents=error.current_total_cents,
            minimum_total_cents=error.minimum_total_cents,
        )
    elif isinstance(error, BudgetConflictError):
        status, code = 409, "budget.conflict"
    else:
        status, code = 422, "budget.invalid"
    return DomainError(code, status=status)


def require_one_lease(total_cents: int, *, from_balance: bool = False, spent_units: int = 0) -> None:
    """Refuse a run limit whose unspent room cannot buy the run's first budget lease.

    A run takes its budget in leases of :data:`CHUNK_CENTS`, so a smaller
    limit ends the run before its first paid step (SKY-914). Setup checks
    bill only the wallet, so they leave the whole limit to the run (SKY-925).
    A continuous-plan check still spends from the limit, so its lease counts
    only the room the check left (SKY-923).

    Args:
        total_cents: The run's approved spending limit.
        from_balance: Whether the account balance, not the user's own
            limit, sets this limit.
        spent_units: Settled spending already charged to this limit, in
            budget units.

    Raises:
        DomainError: 402 ``budget.insufficient`` when a balance-set limit is
            below one lease, or 409 ``budget.below_minimum`` when the user's
            own limit is.
    """
    if total_cents * CENT_SCALE - spent_units >= CHUNK_CENTS * CENT_SCALE:
        return
    # A higher limit cannot help when the balance sets it, so the refusal names the balance.
    if from_balance:
        raise DomainError("budget.insufficient", status=402)
    raise DomainError(
        "budget.below_minimum", status=409, minimum_total_cents=ceil_cents(spent_units + CHUNK_CENTS * CENT_SCALE)
    )


def require_funded_lease(
    total_cents: int, spendable_cents: int | None, *, spent_units: int = 0, from_balance: bool = False
) -> None:
    """Refuse a capped run whose balance or limit cannot buy the run's first budget lease.

    Args:
        total_cents: The run's approved spending limit.
        spendable_cents: The account's spendable balance, or ``None`` when no
            balance applies.
        spent_units: Settled spending already charged to this limit, in
            budget units.
        from_balance: Whether the account balance, not the user's own
            limit, sets this limit.

    Raises:
        DomainError: 402 ``budget.insufficient`` when the balance, or a limit
            the balance sets, is below one lease; or 409
            ``budget.below_minimum`` when the user's own limit's room is.
    """
    # Every run's first lease holds one full lease of the balance, also for BYOK. A higher limit cannot help a
    # balance below that, so the balance is checked first (SKY-923).
    if spendable_cents is not None:
        require_one_lease(spendable_cents, from_balance=True)
    require_one_lease(total_cents, from_balance=from_balance, spent_units=spent_units)


def create_execution_budgets_router(*, job_store: Any) -> APIRouter:
    """Build authenticated budget routes against the shared account database.

    Args:
        job_store: Job storage exposing the SQLAlchemy wallet engine.

    Returns:
        Router for creating, restoring, and editing a draft's spending limit.
    """
    router = APIRouter(prefix="/execution-budgets")

    def ledger() -> BudgetService:
        """Require authoritative storage when a budget endpoint is actually invoked."""
        engine = getattr(job_store, "engine", None)
        if engine is None:
            raise DomainError("budget.invalid", status=503)
        return BudgetService(engine=engine)

    @router.post("", response_model=ExecutionBudgetResponse)
    def create_budget(
        request: CreateExecutionBudgetRequest,
        user: AuthenticatedUserDep,
        idempotency_key: IdempotencyKey,
    ) -> ExecutionBudgetResponse:
        """Create or replay one account-owned draft budget.

        Args:
            request: User-selected total, shared across setup and execution.
            user: Authenticated budget owner.
            idempotency_key: Stable identity reused after network uncertainty.

        Returns:
            The new or previously created authoritative budget.
        """
        try:
            return budget_response(
                ledger().create(
                    user.username,
                    request.total_cents,
                    idempotency_key=idempotency_key,
                    uncapped=request.uncapped,
                    setup_in_total=not bills_setup_from_wallet(job_store.engine, user.username),
                )
            )
        except BudgetError as error:
            raise budget_http_error(error) from error

    @router.get("/{budget_id}", response_model=ExecutionBudgetResponse)
    def get_budget(budget_id: str, user: AuthenticatedUserDep) -> ExecutionBudgetResponse:
        """Refresh an owned budget without resetting settled spend or holds.

        Args:
            budget_id: Stable budget saved with the wizard draft.
            user: Authenticated budget owner.

        Returns:
            Current settled and reserved totals, in cents.
        """
        try:
            return budget_response(ledger().get(budget_id, user.username))
        except BudgetError as error:
            raise budget_http_error(error) from error

    @router.patch("/{budget_id}", response_model=ExecutionBudgetResponse)
    def update_budget(
        budget_id: str, request: UpdateExecutionBudgetRequest, user: AuthenticatedUserDep
    ) -> ExecutionBudgetResponse:
        """Change the approved total only against the revision the user reviewed.

        Args:
            budget_id: Account-owned setup or run spending authority.
            request: New total and optimistic concurrency revision.
            user: Authenticated budget owner.

        Returns:
            Updated total while preserving previous usage and active holds.
        """
        try:
            return budget_response(
                ledger().update_total(
                    budget_id,
                    user.username,
                    request.total_cents,
                    expected_revision=request.expected_revision,
                    uncapped=request.uncapped,
                )
            )
        except BudgetError as error:
            raise budget_http_error(error) from error

    return router
