"""Reserve and settle shared setup/run budgets before admitting paid work.

Provider adapters supply verified request bounds and cumulative actual charges.
This service never estimates provider prices, dispatches work or calls Stripe.
Every mutation locks the account before its budget, so unrelated interactive
debits and concurrent optimizer lanes cannot consume covered funding twice.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import case, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..storage.models import (
    BillingCustomerModel,
    ExecutionBudgetModel,
    ExecutionOperationModel,
    ExecutionUsageEvidenceModel,
    WalletLedgerModel,
)
from .budget_amounts import (
    CENT_SCALE,
    MAX_CENTS,
    budget_wallet_hold,
    ceil_cents,
    cent_units,
    cents_from_units,
)
from .service import StripeBillingService, account_committed_cents, free_grant_cents, local_customer_id

_ACTIVE_STATES = ("reserved", "dispatched", "pending")
_DISPATCHED_STATES = ("dispatched", "pending")
# A context variable, not a service field: concurrent calls on one service each see only their own settles.
_SETTLED_CENTS: ContextVar[dict[str, Decimal] | None] = ContextVar("settled_cents", default=None)


@contextmanager
def settled_charges() -> Iterator[dict[str, Decimal]]:
    """Collect the charge each operation settles in this context, for the run-call record (E28).

    Yields:
        A map from operation id to its cumulative settled charge in cents;
        it fills as operations settle inside the block.
    """
    charges: dict[str, Decimal] = {}
    token = _SETTLED_CENTS.set(charges)
    try:
        yield charges
    finally:
        _SETTLED_CENTS.reset(token)


class BudgetError(ValueError):
    """Reject invalid spending authority without dispatching paid work."""


class BudgetNotFoundError(BudgetError):
    """Hide missing budgets and budgets owned by another account identically."""


class BudgetConflictError(BudgetError):
    """Reject stale revisions or an idempotency key reused for different work."""


class BudgetTotalConflictError(BudgetConflictError):
    """Return the accepted total and minimum after a rejected budget edit."""

    def __init__(self, message: str, *, current_total_cents: int, minimum_total_cents: int) -> None:
        """Describe the authoritative values the client must restore.

        Args:
            message: Internal diagnostic for logs and focused tests.
            current_total_cents: Last total accepted by the ledger.
            minimum_total_cents: Lowest total that covers settled and reserved work.
        """
        super().__init__(message)
        self.current_total_cents = current_total_cents
        self.minimum_total_cents = minimum_total_cents


class BudgetFencedError(BudgetConflictError):
    """Reject new work from an obsolete execution generation."""


class BudgetInsufficientError(BudgetError):
    """Report work that cannot fit even after outstanding holds are released."""


class AccountFundsError(BudgetInsufficientError):
    """Report work the account's balance cannot fund, whatever the spending limit allows."""


class BudgetInFlightError(BudgetError):
    """Wait for already-covered work before deciding whether funding is exhausted."""

    def __init__(self, message: str, *, scope_units: int | None = None, room_units: int | None = None) -> None:
        """Keep the limit arithmetic so a caller can tell whether waiting could help.

        Args:
            message: Internal diagnostic for logs and focused tests.
            scope_units: Coverage the refused operation needs.
            room_units: The limit's unspent units, reserved work included.
        """
        super().__init__(message)
        self.scope_units = scope_units
        self.room_units = room_units


class BudgetUnreconciledError(BudgetInFlightError):
    """Keep old-generation work covered until its actual usage is established."""


class BudgetBoundExceededError(BudgetError):
    """Block admission after preserving evidence that exceeds its admitted bound."""


class BudgetFundingLostError(BudgetError):
    """Preserve coverage when an external wallet adjustment removed required funds."""


@dataclass(frozen=True)
class BudgetSnapshot:
    """Expose exact scope spending separately from rounded wallet movements."""

    id: str
    username: str
    total_cents: int
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
    uncapped: bool = False


@dataclass(frozen=True)
class OperationSnapshot:
    """Describe one physical attempt and its current shared spending authority."""

    id: str
    budget_id: str
    operation_key: str
    attempt: int
    generation: int
    state: str
    phase: str
    cost_kind: str
    request_fingerprint: str
    max_cents: Decimal
    max_wallet_cents: Decimal
    actual_cents: Decimal
    actual_wallet_cents: Decimal
    provider_request_id: str | None
    budget: BudgetSnapshot
    dispatch_claimed: bool = False


@dataclass(frozen=True)
class ReconciliationSnapshot:
    """Expose trusted historical prices and usage only to parent reconciliation workers."""

    operation: OperationSnapshot
    price_snapshot: dict[str, Any]
    evidence: tuple[dict[str, Any], ...]


def _fingerprint(value: Any) -> str:
    """Hash canonical JSON while rejecting nonfinite or nonserializable evidence.

    Args:
        value: JSON-compatible request identity or usage evidence.

    Returns:
        SHA-256 digest of the canonical representation.
    """
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _identifier(value: str, *, maximum: int = 128) -> str:
    """Validate a bounded nonempty idempotency or attribution identifier."""
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("A nonempty bounded identifier is required.")
    return value


def _total(value: int) -> int:
    """Validate an authorized whole-cent budget."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_CENTS:
        raise ValueError("Total budget must be a positive whole number of cents.")
    return value


def usd_text(cents: int) -> str:
    """Write whole cents as dollars, the unit the wizard shows the limit in."""
    return f"${cents / 100:,.2f}"


def _admit(
    session: Session,
    wallet: BillingCustomerModel,
    budget: ExecutionBudgetModel,
    username: str,
    scope: int,
    charge: int,
) -> None:
    """Check that new coverage fits the budget's limit and the account's funds.

    Args:
        session: Transaction holding the account and budget locks.
        wallet: Locked account wallet.
        budget: Locked execution budget.
        username: Authenticated owner.
        scope: Combined scope units the new coverage adds.
        charge: Wallet units the new coverage adds.

    Raises:
        BudgetInsufficientError: When the coverage cannot fit without a budget or funding change.
        BudgetInFlightError: When settling covered work may make the coverage fit.
    """
    if not budget.uncapped:
        remaining = budget.total_cents * CENT_SCALE - budget.settled_units
        if scope > remaining:
            raise BudgetInsufficientError(
                f"The next operation needs up to {usd_text(ceil_cents(scope))}, but only "
                f"{usd_text(max(remaining, 0) // CENT_SCALE)} of the {usd_text(budget.total_cents)} limit remain."
            )
        if scope > remaining - budget.reserved_units:
            raise BudgetInFlightError(
                "Covered work must settle before this operation can fit.", scope_units=scope, room_units=remaining
            )
    held = account_committed_cents(session, username)
    wallet_balance = int(wallet.balance_cents) + int(wallet.grant_remaining or 0)
    hold_delta = (
        ceil_cents(budget.wallet_settled_units + budget.wallet_reserved_units + charge)
        - budget.billed_cents
        - budget_wallet_hold(budget)
    )
    if hold_delta > wallet_balance - held:
        after_release = ceil_cents(budget.wallet_settled_units + charge) - budget.billed_cents
        if after_release <= wallet_balance:
            raise BudgetInFlightError("Other covered work currently holds the required wallet funds.")
        raise AccountFundsError("The account cannot fund the next operation.")


class BudgetService:
    """Own budget admission, immutable usage evidence and exactly-once settlement."""

    def __init__(self, *, engine: Any) -> None:
        """Bind the service to the same SQLAlchemy engine as the account wallet.

        Args:
            engine: Application database engine, without any Stripe connection.
        """
        self._engine = engine

    @property
    def engine(self) -> Any:
        """Return the application database engine that the ledger writes to.

        Returns:
            The SQLAlchemy engine this service was bound to.
        """
        return self._engine

    @contextmanager
    def _transaction(self, session: Session | None = None) -> Iterator[Session]:
        """Keep mutations atomic, optionally joining a caller's job-creation transaction.

        Args:
            session: Caller-owned transaction; omitted for a service-owned transaction.

        Yields:
            Session holding the complete atomic operation.
        """
        if session is not None:
            yield session
            return
        with Session(self._engine) as own:
            try:
                if self._engine.dialect.name == "sqlite":
                    own.execute(text("BEGIN IMMEDIATE"))
                yield own
                own.commit()
            except BaseException:
                own.rollback()
                raise

    def _wallet(self, session: Session, username: str) -> BillingCustomerModel:
        """Lock an existing prepaid account before reading or changing commitments.

        Args:
            session: Current mutation transaction.
            username: Authenticated account whose funding is required.

        Returns:
            Locked wallet with its legacy grant resolved.
        """
        wallet = session.get(BillingCustomerModel, username, with_for_update=True)
        if wallet is None and free_grant_cents() > 0:
            # A new account's grant is only shown until a mutation creates its
            # row; create that row here so the grant can also fund a budget.
            # Two first submissions race to create it, so the loser keeps the
            # winner's row instead of failing on the primary key (SKY-87).
            insert = postgresql_insert if self._engine.dialect.name == "postgresql" else sqlite_insert
            session.execute(
                insert(BillingCustomerModel)
                .values(username=username, stripe_customer_id=local_customer_id(username), balance_cents=0)
                .on_conflict_do_nothing()
            )
            wallet = session.get(BillingCustomerModel, username, with_for_update=True)
        if wallet is None:
            raise AccountFundsError("The account has no funded wallet.")
        StripeBillingService(engine=self._engine)._resolve_grant(wallet, datetime.now(UTC))
        return wallet

    def _locked(
        self, session: Session, budget_id: str, username: str
    ) -> tuple[BillingCustomerModel, ExecutionBudgetModel]:
        """Lock the wallet and then its owned budget in a consistent order.

        Args:
            session: Current mutation transaction.
            budget_id: Spending envelope to lock.
            username: Authenticated owner required by the envelope.

        Returns:
            Locked wallet and budget.
        """
        # Ownership first, without a lock, so a caller with no wallet probing
        # someone else's budget gets not-found rather than a funding error. An
        # owner never changes, so the locked re-check below cannot disagree.
        owner = session.scalar(select(ExecutionBudgetModel.username).where(ExecutionBudgetModel.id == budget_id))
        if owner != username:
            raise BudgetNotFoundError("Budget not found.")
        wallet = self._wallet(session, username)
        budget = session.get(ExecutionBudgetModel, budget_id, with_for_update=True)
        if budget is None or budget.username != username:
            raise BudgetNotFoundError("Budget not found.")
        return wallet, budget

    def _operation(
        self, session: Session, operation_id: str, username: str
    ) -> tuple[BillingCustomerModel, ExecutionBudgetModel, ExecutionOperationModel]:
        """Resolve an attempt while retaining wallet-before-budget lock ordering.

        Args:
            session: Current mutation transaction.
            operation_id: Physical attempt being modified.
            username: Authenticated account owner.

        Returns:
            Locked wallet, budget and physical attempt.
        """
        budget_id = session.scalar(
            select(ExecutionOperationModel.budget_id)
            .join(ExecutionBudgetModel, ExecutionBudgetModel.id == ExecutionOperationModel.budget_id)
            .where(ExecutionOperationModel.id == operation_id, ExecutionBudgetModel.username == username)
        )
        if budget_id is None:
            raise BudgetNotFoundError("Operation not found.")
        wallet, budget = self._locked(session, budget_id, username)
        operation = session.get(ExecutionOperationModel, operation_id, with_for_update=True)
        assert operation is not None
        return wallet, budget, operation

    def _snapshot(self, session: Session, budget: ExecutionBudgetModel, wallet: BillingCustomerModel) -> BudgetSnapshot:
        """Read reconciled phase totals while account and budget locks are held.

        Args:
            session: Transaction owning the row locks.
            budget: Current spending envelope.
            wallet: Locked prepaid account.

        Returns:
            Consistent budget and funding amounts for the caller.
        """
        session.flush()
        totals = {
            phase: (int(scope or 0), int(charge or 0))
            for phase, scope, charge in session.execute(
                select(
                    ExecutionOperationModel.phase,
                    func.sum(ExecutionOperationModel.actual_units),
                    func.sum(ExecutionOperationModel.actual_wallet_units),
                )
                .where(ExecutionOperationModel.budget_id == budget.id)
                .group_by(ExecutionOperationModel.phase)
            )
        }
        pending = session.scalar(
            select(func.count())
            .select_from(ExecutionOperationModel)
            .where(
                ExecutionOperationModel.budget_id == budget.id,
                ExecutionOperationModel.state.in_(_DISPATCHED_STATES),
            )
        )
        setup, setup_wallet = totals.get("setup", (0, 0))
        run, run_wallet = totals.get("run", (0, 0))
        account_available = max(
            0,
            int(wallet.balance_cents)
            + int(wallet.grant_remaining or 0)
            - account_committed_cents(session, budget.username),
        )
        # Without a limit the remaining allowance is whatever the account can still fund.
        available = (
            Decimal(account_available)
            if budget.uncapped
            else cents_from_units(
                max(0, budget.total_cents * CENT_SCALE - budget.settled_units - budget.reserved_units)
            )
        )
        return BudgetSnapshot(
            id=budget.id,
            username=budget.username,
            total_cents=budget.total_cents,
            revision=budget.revision,
            generation=budget.generation,
            state=budget.state,
            job_id=budget.job_id,
            setup_spent_cents=cents_from_units(setup),
            run_spent_cents=cents_from_units(run),
            reserved_cents=cents_from_units(budget.reserved_units),
            available_cents=available,
            billed_cents=budget.billed_cents,
            wallet_setup_spent_cents=cents_from_units(setup_wallet),
            wallet_run_spent_cents=cents_from_units(run_wallet),
            wallet_reserved_cents=budget_wallet_hold(budget),
            account_available_cents=account_available,
            external_spent_cents=cents_from_units(budget.settled_units - budget.wallet_settled_units),
            pending_operations=int(pending or 0),
            blocked_reason=budget.blocked_reason,
            uncapped=bool(budget.uncapped),
        )

    def _operation_snapshot(
        self,
        session: Session,
        operation: ExecutionOperationModel,
        budget: ExecutionBudgetModel,
        wallet: BillingCustomerModel,
    ) -> OperationSnapshot:
        """Return one attempt without exposing provider credentials or request content.

        Args:
            session: Transaction owning the row locks.
            operation: Physical attempt whose state to expose.
            budget: Its spending envelope.
            wallet: Its locked prepaid account.

        Returns:
            Attempt state and the shared authoritative totals.
        """
        session.flush()
        return OperationSnapshot(
            id=operation.id,
            budget_id=budget.id,
            operation_key=operation.operation_key,
            attempt=operation.attempt,
            generation=operation.generation,
            state=operation.state,
            phase=operation.phase,
            cost_kind=operation.cost_kind,
            request_fingerprint=operation.request_fingerprint,
            max_cents=cents_from_units(operation.max_units),
            max_wallet_cents=cents_from_units(operation.max_wallet_units),
            actual_cents=cents_from_units(operation.actual_units),
            actual_wallet_cents=cents_from_units(operation.actual_wallet_units),
            provider_request_id=operation.provider_request_id,
            budget=self._snapshot(session, budget, wallet),
        )

    def create(
        self, username: str, total_cents: int, *, idempotency_key: str, uncapped: bool = False
    ) -> BudgetSnapshot:
        """Create or recover a budget envelope without holding its unspent total.

        Args:
            username: Authenticated account owner.
            total_cents: Authorized combined scope limit.
            idempotency_key: Stable creation identity across browser retries.
            uncapped: Let the run draw on the account instead of stopping at the total.

        Returns:
            Current authoritative budget; paid work still requires a reservation.
        """
        total = _total(total_cents)
        key = _identifier(idempotency_key)
        # Only an uncapped request widens the fingerprint so earlier replays keep matching.
        fingerprint = _fingerprint({"total": total, "uncapped": True} if uncapped else {"total": total})
        with self._transaction() as session:
            wallet = self._wallet(session, username)
            budget = session.scalar(
                select(ExecutionBudgetModel).where(
                    ExecutionBudgetModel.username == username, ExecutionBudgetModel.creation_key == key
                )
            )
            if budget is not None:
                if budget.creation_fingerprint != fingerprint:
                    raise BudgetConflictError("Creation key was already used with a different total.")
                return self._snapshot(session, budget, wallet)
            if wallet.balance_cents + int(wallet.grant_remaining or 0) <= account_committed_cents(session, username):
                raise AccountFundsError("The account has no available funds.")
            now = datetime.now(UTC)
            budget = ExecutionBudgetModel(
                id=str(uuid4()),
                username=username,
                creation_key=key,
                creation_fingerprint=fingerprint,
                total_cents=total,
                uncapped=uncapped,
                created_at=now,
                updated_at=now,
            )
            session.add(budget)
            return self._snapshot(session, budget, wallet)

    def find_by_creation_key(self, username: str, idempotency_key: str) -> BudgetSnapshot | None:
        """Recover existing account-owned authority by its stable creation identity.

        Args:
            username: Authenticated account owner.
            idempotency_key: Original budget creation identity.

        Returns:
            Current authoritative budget, or ``None`` when no matching creation exists.
        """
        key = _identifier(idempotency_key)
        with self._transaction() as session:
            wallet = self._wallet(session, username)
            budget = session.scalar(
                select(ExecutionBudgetModel).where(
                    ExecutionBudgetModel.username == username,
                    ExecutionBudgetModel.creation_key == key,
                )
            )
            return None if budget is None else self._snapshot(session, budget, wallet)

    def get(self, budget_id: str, username: str, *, session: Session | None = None) -> BudgetSnapshot:
        """Read current owned spending authority, including unresolved coverage.

        Args:
            budget_id: Envelope to read.
            username: Authenticated owner.
            session: Optional existing transaction for atomic setup or submission coordination.

        Returns:
            Authoritative amounts and execution state.
        """
        with self._transaction(session) as session:
            wallet, budget = self._locked(session, budget_id, username)
            return self._snapshot(session, budget, wallet)

    def get_operation(self, operation_id: str, username: str) -> OperationSnapshot:
        """Recover an existing attempt without dispatching or reserving it again.

        Args:
            operation_id: Existing physical attempt.
            username: Authenticated owner.

        Returns:
            Current operation and its shared budget.
        """
        with self._transaction() as session:
            wallet, budget, operation = self._operation(session, operation_id, username)
            return self._operation_snapshot(session, operation, budget, wallet)

    def get_reconciliation(self, operation_id: str, username: str) -> ReconciliationSnapshot:
        """Read immutable admission and receipt evidence for an owned physical attempt.

        Args:
            operation_id: Existing physical attempt.
            username: Authenticated owner or owner resolved by the trusted sweep.

        Returns:
            Current attempt and detached copies of its price and usage evidence.
        """
        with self._transaction() as session:
            wallet, budget, operation = self._operation(session, operation_id, username)
            evidence = session.scalars(
                select(ExecutionUsageEvidenceModel.evidence)
                .where(ExecutionUsageEvidenceModel.operation_id == operation_id)
                .order_by(ExecutionUsageEvidenceModel.created_at, ExecutionUsageEvidenceModel.id)
            ).all()
            return ReconciliationSnapshot(
                self._operation_snapshot(session, operation, budget, wallet),
                json.loads(json.dumps(operation.price_snapshot)),
                tuple(json.loads(json.dumps(document)) for document in evidence),
            )

    def unsettled_operations(
        self, *, provider: str, cost_kind: str, limit: int = 100, after_id: str | None = None
    ) -> list[tuple[str, str]]:
        """Page covered attempts for a trusted provider reconciliation worker.

        Args:
            provider: Provider recorded in the immutable price snapshot.
            cost_kind: Model or sandbox attribution to reconcile.
            limit: Maximum attempts returned in this bounded page.
            after_id: Previous page's final operation ID, if any.

        Returns:
            Operation IDs and their authoritative owners, ordered by ID.

        Raises:
            ValueError: When the page size is outside its bounded range.
        """
        if not 1 <= limit <= 1000:
            raise ValueError("Reconciliation page size must be between 1 and 1000.")
        with self._transaction() as session:
            statement = (
                select(ExecutionOperationModel.id, ExecutionBudgetModel.username)
                .join(ExecutionBudgetModel, ExecutionBudgetModel.id == ExecutionOperationModel.budget_id)
                .where(
                    ExecutionOperationModel.state.in_(_DISPATCHED_STATES),
                    ExecutionOperationModel.cost_kind == cost_kind,
                    ExecutionOperationModel.price_snapshot["provider"].as_string() == provider,
                )
                .order_by(ExecutionOperationModel.id)
                .limit(limit)
            )
            if after_id is not None:
                statement = statement.where(ExecutionOperationModel.id > after_id)
            return list(session.execute(statement))

    def update_total(
        self, budget_id: str, username: str, total_cents: int, *, expected_revision: int, uncapped: bool = False
    ) -> BudgetSnapshot:
        """Accept a versioned total without invalidating spent or covered amounts.

        Args:
            budget_id: Envelope being edited.
            username: Authenticated owner.
            total_cents: User's explicitly edited scope ceiling.
            expected_revision: Last accepted revision held by the caller.
            uncapped: Whether the run may keep drawing on the account past the total.

        Returns:
            Updated authority, or the same revision for an unchanged total.
        """
        total = _total(total_cents)
        with self._transaction() as session:
            wallet, budget = self._locked(session, budget_id, username)
            minimum = ceil_cents(budget.settled_units + budget.reserved_units)
            if budget.revision != expected_revision:
                raise BudgetTotalConflictError(
                    "The budget changed; refresh its current revision.",
                    current_total_cents=budget.total_cents,
                    minimum_total_cents=minimum,
                )
            if not uncapped and total < minimum:
                raise BudgetTotalConflictError(
                    f"The minimum currently supportable total is {minimum} cents.",
                    current_total_cents=budget.total_cents,
                    minimum_total_cents=minimum,
                )
            widening = total > budget.total_cents or (uncapped and not budget.uncapped)
            if widening and wallet.balance_cents + int(wallet.grant_remaining or 0) <= account_committed_cents(
                session, username
            ):
                raise AccountFundsError("The account has no available funds for additional work.")
            if total != budget.total_cents or uncapped != budget.uncapped:
                budget.total_cents = total
                budget.uncapped = uncapped
                budget.revision += 1
                budget.updated_at = datetime.now(UTC)
            return self._snapshot(session, budget, wallet)

    def attach_to_job(
        self, budget_id: str, username: str, job_id: str, *, expected_revision: int, session: Session | None = None
    ) -> BudgetSnapshot:
        """Attach one root job, optionally inside its caller-owned creation transaction.

        Args:
            budget_id: Setup envelope consumed by submission.
            username: Authenticated owner.
            job_id: Single root job receiving this allowance.
            expected_revision: Last accepted revision from preflight.
            session: Optional transaction creating and persisting the job atomically.

        Returns:
            Attached budget; identical retries preserve the existing linkage.
        """
        with self._transaction(session) as transaction:
            wallet, budget = self._locked(transaction, budget_id, username)
            if budget.job_id == job_id:
                return self._snapshot(transaction, budget, wallet)
            if budget.job_id is not None:
                raise BudgetConflictError("This setup budget already belongs to a submitted job.")
            if budget.revision != expected_revision or budget.state != "open":
                raise BudgetConflictError("The setup budget changed or is not open.")
            active_setup = transaction.scalar(
                select(ExecutionOperationModel.id)
                .where(
                    ExecutionOperationModel.budget_id == budget.id,
                    ExecutionOperationModel.phase == "setup",
                    ExecutionOperationModel.state.in_(_ACTIVE_STATES),
                )
                .limit(1)
            )
            if active_setup is not None:
                raise BudgetInFlightError("Setup operations must finish reconciliation before submission.")
            existing_job = transaction.scalar(
                select(ExecutionBudgetModel.id).where(ExecutionBudgetModel.job_id == job_id).limit(1)
            )
            if existing_job is not None:
                raise BudgetConflictError("This job already has a different budget.")
            budget.job_id = _identifier(job_id, maximum=36)
            budget.state = "attached"
            budget.revision += 1
            budget.updated_at = datetime.now(UTC)
            return self._snapshot(transaction, budget, wallet)

    def reserve(
        self,
        budget_id: str,
        username: str,
        *,
        operation_key: str,
        generation: int,
        phase: str,
        cost_kind: str,
        request_fingerprint: str,
        price_snapshot: Mapping[str, Any],
        max_cents: Decimal | str | int | float,
        max_wallet_cents: Decimal | str | int | float | None = None,
        attempt: int = 0,
        role: str | None = None,
        session: Session | None = None,
    ) -> OperationSnapshot:
        """Admit one physical attempt under its verified scope and wallet bounds.

        Args:
            budget_id: Shared setup/run envelope.
            username: Authenticated owner, never taken from untrusted usage data.
            operation_key: Logical work identity retained across request delivery retries.
            generation: Current execution epoch.
            phase: Setup or run attribution.
            cost_kind: Model, sandbox or other explicitly priced category.
            request_fingerprint: Digest of the exact resolved request and enforced limits.
            price_snapshot: Versioned prices and bound evidence supplied by the adapter.
            max_cents: Maximum combined scope charge for this physical attempt.
            max_wallet_cents: Maximum Skynet wallet charge; defaults to the scope bound.
            attempt: Physical retry number; a new billable retry needs a new attempt.
            role: Optional task, judge, proposer or runtime attribution.
            session: Optional caller transaction for atomic lifecycle publication.

        Returns:
            The reserved operation or its existing identical attempt.

        Raises:
            BudgetInsufficientError: When the required work cannot fit without a budget/funding change.
            BudgetInFlightError: When settling covered work may make the admission possible.
        """
        key = _identifier(operation_key)
        request_fingerprint = _identifier(request_fingerprint)
        cost_kind = _identifier(cost_kind, maximum=32)
        if phase not in {"setup", "run"} or attempt < 0 or generation < 0:
            raise ValueError("Invalid operation phase, attempt or generation.")
        if role is not None:
            _identifier(role, maximum=64)
        scope = cent_units(max_cents)
        charge = cent_units(max_cents if max_wallet_cents is None else max_wallet_cents)
        if charge > scope:
            raise ValueError("Wallet coverage cannot exceed the combined scope coverage.")
        prices = dict(price_snapshot)
        if not prices or not prices.get("version"):
            raise ValueError("A versioned applicable price snapshot is required.")
        admission = _fingerprint(
            {
                "request": request_fingerprint,
                "prices": prices,
                "scope": scope,
                "wallet": charge,
                "phase": phase,
                "kind": cost_kind,
                "role": role,
                "generation": generation,
            }
        )
        with self._transaction(session) as session:
            wallet, budget = self._locked(session, budget_id, username)
            if generation != budget.generation:
                raise BudgetFencedError("The execution generation is obsolete.")
            operation = session.scalar(
                select(ExecutionOperationModel).where(
                    ExecutionOperationModel.budget_id == budget_id,
                    ExecutionOperationModel.operation_key == key,
                    ExecutionOperationModel.attempt == attempt,
                )
            )
            if operation is not None:
                if operation.admission_fingerprint != admission:
                    raise BudgetConflictError("Operation key was reused for different admitted work.")
                return self._operation_snapshot(session, operation, budget, wallet)
            if budget.state not in {"open", "attached"}:
                raise BudgetConflictError("This budget is not admitting new work.")
            if (phase == "setup" and budget.job_id is not None) or (phase == "run" and budget.job_id is None):
                raise BudgetConflictError("The operation phase does not match the budget's submission state.")
            unresolved = session.scalar(
                select(ExecutionOperationModel.id)
                .where(
                    ExecutionOperationModel.budget_id == budget_id,
                    ExecutionOperationModel.generation < generation,
                    ExecutionOperationModel.state.in_(_DISPATCHED_STATES),
                )
                .limit(1)
            )
            if unresolved is not None:
                raise BudgetUnreconciledError("Previous-generation paid work must be reconciled before recovery.")
            _admit(session, wallet, budget, username, scope, charge)
            now = datetime.now(UTC)
            operation = ExecutionOperationModel(
                id=str(uuid4()),
                budget_id=budget.id,
                operation_key=key,
                attempt=attempt,
                generation=generation,
                phase=phase,
                cost_kind=cost_kind,
                role=role,
                request_fingerprint=request_fingerprint,
                admission_fingerprint=admission,
                price_snapshot=prices,
                max_units=scope,
                max_wallet_units=charge,
                created_at=now,
                updated_at=now,
            )
            session.add(operation)
            budget.reserved_units += scope
            budget.wallet_reserved_units += charge
            budget.updated_at = now
            return self._operation_snapshot(session, operation, budget, wallet)

    def mark_dispatched(
        self, operation_id: str, username: str, provider_request_id: str | None = None
    ) -> OperationSnapshot:
        """Durably mark dispatch before sending a provider request; never release on uncertainty.

        Args:
            operation_id: Reserved physical attempt.
            username: Authenticated owner.
            provider_request_id: Provider identity when known before dispatch.

        Returns:
            Attempt state with dispatch_claimed true only for its single sender.
        """
        with self._transaction() as session:
            wallet, budget, operation = self._operation(session, operation_id, username)
            if operation.generation != budget.generation and operation.state not in _DISPATCHED_STATES:
                raise BudgetFencedError("The execution generation is obsolete.")
            if operation.state not in _ACTIVE_STATES:
                raise BudgetConflictError("A finished or released operation cannot be dispatched.")
            if provider_request_id is not None:
                _identifier(provider_request_id, maximum=255)
                if operation.provider_request_id not in {None, provider_request_id}:
                    raise BudgetConflictError("The operation already has a different provider request identity.")
                operation.provider_request_id = provider_request_id
            if operation.state == "reserved":
                if budget.state == "blocked":
                    raise BudgetConflictError("This budget is quarantined pending usage reconciliation.")
                if budget.state not in {"open", "attached"}:
                    raise BudgetConflictError("This budget is not admitting new work.")
                if (operation.phase == "setup") != (budget.job_id is None):
                    raise BudgetConflictError("The operation phase does not match the budget's submission state.")
                operation.state = "dispatched"
                operation.dispatched_at = datetime.now(UTC)
                claimed = True
            else:
                claimed = False
            operation.updated_at = datetime.now(UTC)
            return replace(self._operation_snapshot(session, operation, budget, wallet), dispatch_claimed=claimed)

    def mark_pending(
        self,
        operation_id: str,
        username: str,
        *,
        evidence_key: str | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> OperationSnapshot:
        """Retain coverage after a lost response, interruption or incomplete usage report.

        Args:
            operation_id: Previously dispatched attempt.
            username: Authenticated owner.
            evidence_key: Optional immutable identity for an incomplete provider receipt.
            evidence: Raw trusted usage that cannot yet establish a charge.

        Returns:
            Pending operation with its remaining coverage intact.
        """
        if (evidence_key is None) != (evidence is None):
            raise ValueError("Pending evidence requires both an identity and a document.")
        document = dict(evidence) if evidence is not None else None
        key = _identifier(evidence_key) if evidence_key is not None else None
        fingerprint = _fingerprint({"pending_evidence": document}) if document is not None else None
        with self._transaction() as session:
            wallet, budget, operation = self._operation(session, operation_id, username)
            if operation.state not in _DISPATCHED_STATES:
                raise BudgetConflictError("Only dispatched work can await reconciliation.")
            if key is not None:
                prior = session.scalar(
                    select(ExecutionUsageEvidenceModel).where(
                        ExecutionUsageEvidenceModel.operation_id == operation_id,
                        ExecutionUsageEvidenceModel.evidence_key == key,
                    )
                )
                if prior is not None and prior.fingerprint != fingerprint:
                    raise BudgetConflictError("Pending provider evidence is immutable.")
                if prior is None:
                    session.add(
                        ExecutionUsageEvidenceModel(
                            id=str(uuid4()),
                            operation_id=operation_id,
                            evidence_key=key,
                            fingerprint=fingerprint,
                            actual_units=operation.actual_units,
                            actual_wallet_units=operation.actual_wallet_units,
                            billed_cents=0,
                            final=False,
                            issue="usage_pending",
                            evidence=document,
                            created_at=datetime.now(UTC),
                        )
                    )
            operation.state = "pending"
            operation.updated_at = datetime.now(UTC)
            return self._operation_snapshot(session, operation, budget, wallet)

    def release(self, operation_id: str, username: str) -> OperationSnapshot:
        """Release coverage only when the operation is known never to have dispatched.

        Args:
            operation_id: Unstarted reserved attempt.
            username: Authenticated owner.

        Returns:
            Released operation without a usage charge.

        Raises:
            BudgetUnreconciledError: If the attempt may already have incurred costs.
        """
        with self._transaction() as session:
            wallet, budget, operation = self._operation(session, operation_id, username)
            if operation.state == "released":
                return self._operation_snapshot(session, operation, budget, wallet)
            if operation.state != "reserved" or operation.dispatched_at is not None:
                raise BudgetUnreconciledError("Dispatched work requires actual usage reconciliation, not release.")
            budget.reserved_units -= operation.max_units
            budget.wallet_reserved_units -= operation.max_wallet_units
            operation.state = "released"
            operation.updated_at = budget.updated_at = datetime.now(UTC)
            return self._operation_snapshot(session, operation, budget, wallet)

    def reject(
        self,
        operation_id: str,
        username: str,
        *,
        evidence_key: str,
        evidence: Mapping[str, Any],
    ) -> OperationSnapshot:
        """Release a dispatched or retained hold whose request the provider refused outright.

        Args:
            operation_id: Dispatched or pending attempt with no provider identity and no settled usage.
            username: Authenticated owner.
            evidence_key: Immutable identity of the refusal.
            evidence: Raw provider refusal proving nothing was created.

        Returns:
            Released operation without a usage charge.

        Raises:
            BudgetConflictError: If the attempt was never dispatched and should be released instead.
            BudgetUnreconciledError: If the attempt received a provider identity or settled usage, so
                it may have reached the provider.
        """
        key = _identifier(evidence_key)
        document = dict(evidence)
        with self._transaction() as session:
            wallet, budget, operation = self._operation(session, operation_id, username)
            if operation.state == "released":
                return self._operation_snapshot(session, operation, budget, wallet)
            if operation.state == "reserved":
                raise BudgetConflictError("Unstarted work is released, not rejected.")
            settled = session.scalar(
                select(ExecutionUsageEvidenceModel.id)
                .where(
                    ExecutionUsageEvidenceModel.operation_id == operation_id,
                    or_(
                        ExecutionUsageEvidenceModel.issue.is_(None),
                        ExecutionUsageEvidenceModel.issue != "usage_pending",
                    ),
                )
                .limit(1)
            )
            if (
                operation.state not in _DISPATCHED_STATES
                or operation.provider_request_id is not None
                or operation.actual_units
                or operation.actual_wallet_units
                or settled is not None
            ):
                raise BudgetUnreconciledError("Work that may have reached the provider requires usage reconciliation.")
            now = datetime.now(UTC)
            session.add(
                ExecutionUsageEvidenceModel(
                    id=str(uuid4()),
                    operation_id=operation_id,
                    evidence_key=key,
                    fingerprint=_fingerprint({"refusal": document}),
                    actual_units=operation.actual_units,
                    actual_wallet_units=operation.actual_wallet_units,
                    billed_cents=0,
                    final=True,
                    issue="rejected",
                    evidence=document,
                    created_at=now,
                )
            )
            budget.reserved_units -= operation.max_units
            budget.wallet_reserved_units -= operation.max_wallet_units
            operation.state = "released"
            operation.updated_at = budget.updated_at = now
            return self._operation_snapshot(session, operation, budget, wallet)

    def extend_coverage(
        self,
        operation_id: str,
        username: str,
        *,
        evidence_key: str,
        max_cents: Decimal | str | int | float,
        max_wallet_cents: Decimal | str | int | float,
        evidence: Mapping[str, Any],
    ) -> OperationSnapshot:
        """Grow a running sandbox's coverage before the provider is asked to keep it alive longer.

        The extension passes the same admission check as a new operation, so a
        box only outlives its first slice while the budget and the account can
        pay for the next one. Coverage only grows: settlement still caps the
        charge at the operation's total bound.

        Args:
            operation_id: Dispatched sandbox attempt in the current generation.
            username: Authenticated owner.
            evidence_key: Immutable identity of this extension; replays are idempotent.
            max_cents: Combined scope coverage the operation holds after the extension.
            max_wallet_cents: Wallet coverage the operation holds after the extension.
            evidence: The extended request bound, recorded for audit.

        Returns:
            The operation with its grown coverage and the budget totals.

        Raises:
            BudgetFencedError: When the operation's execution generation is obsolete.
            BudgetConflictError: When the operation cannot grow, or the identity was reused.
            BudgetInsufficientError: When the extension cannot fit without a budget or funding change.
            BudgetInFlightError: When settling covered work may make the extension fit.
        """
        key = _identifier(evidence_key)
        scope = cent_units(max_cents)
        charge = cent_units(max_wallet_cents)
        if charge > scope:
            raise ValueError("Wallet coverage cannot exceed the combined scope coverage.")
        document = dict(evidence)
        fingerprint = _fingerprint({"extension": document, "scope": scope, "wallet": charge})
        with self._transaction() as session:
            wallet, budget, operation = self._operation(session, operation_id, username)
            prior = session.scalar(
                select(ExecutionUsageEvidenceModel).where(
                    ExecutionUsageEvidenceModel.operation_id == operation_id,
                    ExecutionUsageEvidenceModel.evidence_key == key,
                )
            )
            if prior is not None:
                if prior.fingerprint != fingerprint:
                    raise BudgetConflictError("This coverage extension already recorded a different bound.")
                return self._operation_snapshot(session, operation, budget, wallet)
            if operation.generation != budget.generation:
                raise BudgetFencedError("The execution generation is obsolete.")
            if operation.cost_kind != "sandbox" or operation.state not in _DISPATCHED_STATES:
                raise BudgetConflictError("Only a running sandbox's coverage can be extended.")
            if budget.state not in {"open", "attached"}:
                raise BudgetConflictError("This budget is not admitting new work.")
            if scope <= operation.max_units or charge < operation.max_wallet_units:
                raise BudgetConflictError("A coverage extension must grow the operation's bound.")
            _admit(
                session,
                wallet,
                budget,
                username,
                scope - operation.max_units,
                charge - operation.max_wallet_units,
            )
            now = datetime.now(UTC)
            session.add(
                ExecutionUsageEvidenceModel(
                    id=str(uuid4()),
                    operation_id=operation_id,
                    evidence_key=key,
                    fingerprint=fingerprint,
                    actual_units=operation.actual_units,
                    actual_wallet_units=operation.actual_wallet_units,
                    billed_cents=0,
                    final=False,
                    issue="usage_pending",
                    evidence=document,
                    created_at=now,
                )
            )
            budget.reserved_units += scope - operation.max_units
            budget.wallet_reserved_units += charge - operation.max_wallet_units
            operation.max_units = scope
            operation.max_wallet_units = charge
            operation.updated_at = budget.updated_at = now
            return self._operation_snapshot(session, operation, budget, wallet)

    def fence_generation(
        self, budget_id: str, username: str, *, expected_generation: int, session: Session | None = None
    ) -> BudgetSnapshot:
        """Revoke old admission and release only its provably undispatched reservations.

        Args:
            budget_id: Envelope whose old worker is being fenced.
            username: Authenticated owner.
            expected_generation: Generation currently owned by the recovery coordinator.
            session: Optional recovery transaction, retaining account-before-budget locking.

        Returns:
            New epoch, retaining all old dispatched coverage until reconciliation.
        """
        with self._transaction(session) as session:
            wallet, budget = self._locked(session, budget_id, username)
            if budget.generation != expected_generation:
                raise BudgetFencedError("The execution generation has already changed.")
            budget.generation += 1
            for operation in session.scalars(
                select(ExecutionOperationModel).where(
                    ExecutionOperationModel.budget_id == budget_id, ExecutionOperationModel.state == "reserved"
                )
            ):
                budget.reserved_units -= operation.max_units
                budget.wallet_reserved_units -= operation.max_wallet_units
                operation.state = "released"
                operation.updated_at = datetime.now(UTC)
            budget.updated_at = datetime.now(UTC)
            return self._snapshot(session, budget, wallet)

    def waive_interrupted_work(
        self,
        budget_id: str,
        username: str,
        *,
        generation: int,
        settled_after: datetime | None,
    ) -> BudgetSnapshot:
        """Stop charging the work an infrastructure interruption makes a resumed run repeat.

        The interrupted generation's usage that settled after the last saved
        checkpoint is lost, and the resumed run pays for it again. Its wallet
        charge is credited back, and its usage stops counting against the run's
        limit. The operations keep their evidence, so the provider cost stays
        auditable.

        Args:
            budget_id: Envelope of the interrupted run.
            username: Authenticated owner.
            generation: Budget generation the interruption ended.
            settled_after: Save time of the checkpoint the run resumes from, or
                ``None`` when the run starts again and loses all of that work.

        Returns:
            Budget after the waiver.
        """
        with self._transaction() as session:
            wallet, budget = self._locked(session, budget_id, username)
            settlement_key = f"restart:{budget_id}:g{generation}"
            if session.scalar(select(WalletLedgerModel.id).where(WalletLedgerModel.settlement_key == settlement_key)):
                return self._snapshot(session, budget, wallet)
            # Setup work ran before the budget joined the run, and no resume
            # repeats it, so only run work can be lost.
            lost_operations = select(ExecutionOperationModel.id).where(
                ExecutionOperationModel.budget_id == budget_id,
                ExecutionOperationModel.generation == generation,
                ExecutionOperationModel.phase == "run",
                ExecutionOperationModel.state == "settled",
            )
            if settled_after is not None:
                lost_operations = lost_operations.where(ExecutionOperationModel.updated_at > settled_after)
            lost = select(
                func.coalesce(func.sum(ExecutionOperationModel.actual_units), 0),
                func.coalesce(func.sum(ExecutionOperationModel.actual_wallet_units), 0),
            ).where(ExecutionOperationModel.id.in_(lost_operations))
            scope, charge = (int(value) for value in session.execute(lost).one())
            if not scope:
                return self._snapshot(session, budget, wallet)
            budget.settled_units -= scope
            budget.wallet_settled_units -= charge
            refund = budget.billed_cents - ceil_cents(budget.wallet_settled_units)
            budget.billed_cents -= refund
            budget.updated_at = datetime.now(UTC)
            to_grant = 0
            if refund:
                # Free-grant cents go back to the grant, so a waiver never turns
                # them into paid balance. Only the grant the lost charges drew
                # returns; the kept work stays paid as it was.
                run_rows = (WalletLedgerModel.budget_id == budget_id, WalletLedgerModel.kind == "run")
                lost_grant = select(func.coalesce(func.sum(WalletLedgerModel.from_grant_cents), 0)).where(
                    *run_rows,
                    WalletLedgerModel.delta_cents < 0,
                    WalletLedgerModel.settlement_key.in_(
                        select(ExecutionUsageEvidenceModel.id).where(
                            ExecutionUsageEvidenceModel.operation_id.in_(lost_operations)
                        )
                    ),
                )
                net_grant = select(
                    func.coalesce(
                        func.sum(
                            case((WalletLedgerModel.delta_cents < 0, WalletLedgerModel.from_grant_cents), else_=0)
                            - case((WalletLedgerModel.delta_cents > 0, WalletLedgerModel.from_grant_cents), else_=0)
                        ),
                        0,
                    )
                ).where(*run_rows)
                to_grant = max(0, min(refund, int(session.scalar(lost_grant)), int(session.scalar(net_grant))))
                wallet.grant_remaining = int(wallet.grant_remaining or 0) + to_grant
                wallet.balance_cents += refund - to_grant
                wallet.updated_at = datetime.now(UTC)
            # Written even for a sub-cent waiver: the row is what makes a repeated
            # call for this generation a no-op.
            session.add(
                WalletLedgerModel(
                    username=username,
                    delta_cents=refund,
                    kind="run",
                    description="Work repeated after a restart (not charged)",
                    budget_id=budget.id,
                    settlement_key=settlement_key,
                    optimization_id=budget.job_id,
                    from_grant_cents=to_grant,
                )
            )
            return self._snapshot(session, budget, wallet)

    def stop_admission(
        self,
        budget_id: str,
        username: str,
        *,
        reason: str,
        session: Session | None = None,
    ) -> BudgetSnapshot:
        """Close the envelope to new work while preserving all unsettled obligations.

        Args:
            budget_id: Envelope no longer allowed to admit work.
            username: Authenticated owner.
            reason: Stable terminal or administrative reason.
            session: Optional caller transaction for atomic lifecycle publication.

        Returns:
            Closed authority with pending usage and holds retained.
        """
        with self._transaction(session) as session:
            wallet, budget = self._locked(session, budget_id, username)
            if budget.state == "blocked":
                return self._snapshot(session, budget, wallet)
            budget.state = "closed"
            budget.blocked_reason = _identifier(reason)
            budget.updated_at = datetime.now(UTC)
            return self._snapshot(session, budget, wallet)

    def close_for_deleted_jobs(self, job_ids: Sequence[str], *, session: Session) -> int:
        """Close and detach the budgets of jobs being hard-deleted, keeping their money history.

        Budgets and their ledger rows are the account's financial record, so they
        outlive the job. Unstarted reservations are released so no money stays
        held; dispatched work keeps its coverage until its usage reconciles.

        Args:
            job_ids: Root and grid-child job ids being deleted in ``session``.
            session: The caller's delete transaction, so budget and job go together.

        Returns:
            Number of budgets closed.
        """
        if not job_ids:
            return 0
        owned = session.execute(
            select(ExecutionBudgetModel.id, ExecutionBudgetModel.username)
            .where(ExecutionBudgetModel.job_id.in_(list(job_ids)))
            .order_by(ExecutionBudgetModel.username, ExecutionBudgetModel.id)
        ).all()
        now = datetime.now(UTC)
        for budget_id, username in owned:
            # Wallet before budget, the same lock order as every other mutation.
            session.get(BillingCustomerModel, username, with_for_update=True)
            budget = session.get(ExecutionBudgetModel, budget_id, with_for_update=True)
            assert budget is not None
            for operation in session.scalars(
                select(ExecutionOperationModel).where(
                    ExecutionOperationModel.budget_id == budget_id, ExecutionOperationModel.state == "reserved"
                )
            ):
                budget.reserved_units -= operation.max_units
                budget.wallet_reserved_units -= operation.max_wallet_units
                operation.state = "released"
                operation.updated_at = now
            if budget.state != "blocked":
                budget.state = "closed"
                budget.blocked_reason = "job_deleted"
            budget.job_id = None
            budget.updated_at = now
        return len(owned)

    def resume_admission(self, budget_id: str, username: str, *, expected_generation: int) -> BudgetSnapshot:
        """Reopen an explicitly resumed attached job after obligations are reconciled.

        The lifecycle coordinator must first verify the same job's checkpoint
        compatibility and quote coverage. Changing funding never calls this method.

        Args:
            budget_id: Closed execution envelope bound to the resumed root job.
            username: Authenticated owner.
            expected_generation: Epoch authorized by the recovery coordinator.

        Returns:
            Attached authority ready for new per-operation admission.

        Raises:
            BudgetUnreconciledError: When prior work still owns a reservation.
            BudgetInsufficientError: When no total allowance remains.
        """
        with self._transaction() as session:
            wallet, budget = self._locked(session, budget_id, username)
            if budget.generation != expected_generation:
                raise BudgetFencedError("The execution generation is obsolete.")
            if budget.job_id is None or budget.state not in {"closed", "attached"}:
                raise BudgetConflictError("Only an attached, reconciled job can resume admission.")
            active = session.scalar(
                select(ExecutionOperationModel.id)
                .where(
                    ExecutionOperationModel.budget_id == budget_id, ExecutionOperationModel.state.in_(_ACTIVE_STATES)
                )
                .limit(1)
            )
            if active is not None:
                raise BudgetUnreconciledError("Previous paid work must reconcile before admission resumes.")
            if not budget.uncapped and budget.settled_units >= budget.total_cents * CENT_SCALE:
                raise BudgetInsufficientError("The total budget has no remaining allowance.")
            budget.state = "attached"
            budget.blocked_reason = None
            budget.updated_at = datetime.now(UTC)
            return self._snapshot(session, budget, wallet)

    def settle(
        self,
        operation_id: str,
        username: str,
        *,
        evidence_key: str,
        actual_cents: Decimal | str | int | float,
        evidence: Mapping[str, Any],
        final: bool = True,
        actual_wallet_cents: Decimal | str | int | float | None = None,
    ) -> OperationSnapshot:
        """Settle cumulative actual usage once, including partial and failed attempts.

        Args:
            operation_id: Physical attempt being reconciled.
            username: Authenticated owner.
            evidence_key: Stable provider-event identity within this attempt.
            actual_cents: Cumulative actual combined scope charge, not an increment.
            evidence: Authoritative provider/runtime usage and its attribution.
            final: Whether all usage is reconciled and remaining coverage can be released.
            actual_wallet_cents: Cumulative wallet charge, excluding externally paid BYOK usage.

        Returns:
            Updated attempt and authoritative budget snapshot.

        Raises:
            BudgetBoundExceededError: After preserving evidence and blocking the faulty priced path.
            BudgetFundingLostError: After preserving evidence when an external adjustment removed funding.
        """
        key = _identifier(evidence_key)
        scope = cent_units(actual_cents)
        charge = cent_units(actual_cents if actual_wallet_cents is None else actual_wallet_cents)
        if charge > scope:
            raise ValueError("Wallet usage cannot exceed combined scope usage.")
        document = dict(evidence)
        fingerprint = _fingerprint({"scope": scope, "wallet": charge, "final": final, "evidence": document})
        issue: str | None = None
        with self._transaction() as session:
            wallet, budget, operation = self._operation(session, operation_id, username)
            prior = session.scalar(
                select(ExecutionUsageEvidenceModel).where(
                    ExecutionUsageEvidenceModel.operation_id == operation_id,
                    ExecutionUsageEvidenceModel.evidence_key == key,
                )
            )
            if prior is not None:
                if prior.fingerprint != fingerprint:
                    raise BudgetConflictError(
                        "Usage evidence is immutable; this identity already has different evidence."
                    )
                issue = prior.issue
            else:
                if operation.state not in _DISPATCHED_STATES:
                    raise BudgetConflictError("Usage may only settle a dispatched attempt.")
                if scope < operation.actual_units or charge < operation.actual_wallet_units:
                    raise BudgetConflictError("Cumulative usage cannot decrease.")
                scope_delta = scope - operation.actual_units
                wallet_delta = charge - operation.actual_wallet_units
                billed = ceil_cents(budget.wallet_settled_units + wallet_delta) - budget.billed_cents
                if scope > operation.max_units or charge > operation.max_wallet_units:
                    issue = "bound_exceeded"
                elif billed > int(wallet.balance_cents) + int(wallet.grant_remaining or 0):
                    issue = "funding_lost"
                event_id = str(uuid4())
                session.add(
                    ExecutionUsageEvidenceModel(
                        id=event_id,
                        operation_id=operation_id,
                        evidence_key=key,
                        fingerprint=fingerprint,
                        actual_units=scope,
                        actual_wallet_units=charge,
                        billed_cents=0 if issue else billed,
                        final=final,
                        issue=issue,
                        evidence=document,
                        created_at=datetime.now(UTC),
                    )
                )
                if issue:
                    budget.state = "blocked"
                    budget.blocked_reason = issue
                    operation.state = "pending"
                else:
                    budget.settled_units += scope_delta
                    budget.wallet_settled_units += wallet_delta
                    budget.reserved_units -= scope_delta
                    budget.wallet_reserved_units -= wallet_delta
                    operation.actual_units = scope
                    operation.actual_wallet_units = charge
                    if final:
                        budget.reserved_units -= operation.max_units - scope
                        budget.wallet_reserved_units -= operation.max_wallet_units - charge
                        operation.state = "settled"
                    else:
                        operation.state = "pending"
                    if billed:
                        from_grant = min(int(wallet.grant_remaining or 0), billed)
                        wallet.grant_remaining = int(wallet.grant_remaining or 0) - from_grant
                        wallet.balance_cents -= billed - from_grant
                        budget.billed_cents += billed
                        session.add(
                            WalletLedgerModel(
                                username=username,
                                delta_cents=-billed,
                                kind="run",
                                description=f"Optimization {operation.phase}: {operation.cost_kind}",
                                budget_id=budget.id,
                                settlement_key=event_id,
                                model=document.get("model"),
                                from_grant_cents=from_grant,
                            )
                        )
                        wallet.updated_at = datetime.now(UTC)
                operation.updated_at = budget.updated_at = datetime.now(UTC)
            snapshot = self._operation_snapshot(session, operation, budget, wallet)
        charges = _SETTLED_CENTS.get()
        if charges is not None and issue is None:
            charges[operation_id] = snapshot.actual_cents
        if issue == "bound_exceeded":
            raise BudgetBoundExceededError(
                "Reported usage exceeded its verified bound; evidence and coverage were retained."
            )
        if issue == "funding_lost":
            raise BudgetFundingLostError(
                "Account funding changed after admission; usage remains pending reconciliation."
            )
        return snapshot
