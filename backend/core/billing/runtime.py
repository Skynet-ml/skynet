"""Admit each physical paid operation before dispatch and retain uncertain usage."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Generic, TypeVar

from .budget_amounts import CENT_SCALE, ceil_cents
from .budgets import (
    BudgetConflictError,
    BudgetFencedError,
    BudgetInFlightError,
    BudgetInsufficientError,
    BudgetService,
    BudgetUnreconciledError,
    OperationSnapshot,
    usd_text,
)
from .operation_pricing import ChargePolicy, OperationQuote, json_fingerprint
from .signals import BudgetStopLatch

T = TypeVar("T")

_OPERATION_KEY_LIMIT = 128


def generation_operation_key(operation_key: str, generation: int) -> str:
    """Scope a logical operation key to one execution generation.

    A recovered run repeats work such as opening its scorer box under the same
    logical key; the ledger's uniqueness spans every generation, so an unscoped
    key collides with the interrupted attempt's record. Generation 0 keeps the
    bare key so existing records and prefix lookups stay valid.

    Args:
        operation_key: Logical identity retained across delivery retries.
        generation: Execution epoch the operation is admitted under.

    Returns:
        The key recorded for this generation, within the ledger's length bound.
    """
    if generation <= 0:
        return operation_key
    suffix = f"#g{generation}"
    if len(operation_key) + len(suffix) <= _OPERATION_KEY_LIMIT:
        return operation_key + suffix
    return json_fingerprint([operation_key])[: _OPERATION_KEY_LIMIT - len(suffix)] + suffix


class UsagePendingError(RuntimeError):
    """Prevent replay while a dispatched operation's bill remains unconfirmed."""


class ProviderFailedError(UsagePendingError):
    """Report a response the provider failed after dispatch, while its coverage stays held.

    The attempt itself is over, so the caller may retry with a fresh attempt; only
    this attempt's bill is still unconfirmed.
    """


class OperationCompletedError(RuntimeError):
    """Require the caller to retrieve its durable result instead of dispatching again."""


@dataclass(frozen=True)
class PaidResult(Generic[T]):
    """Carry trusted provider usage separately from an optimizer's output."""

    value: T
    provider_usd: Decimal | None
    evidence: Mapping[str, Any]
    provider_request_id: str | None = None
    final: bool = True
    # A complete provider error that names no generation and reports no usage; its
    # coverage returns immediately because no receipt will ever confirm a charge.
    refused: bool = False
    # The provider's own message for a response it failed after dispatch.
    failure: str | None = None


class BudgetRuntime:
    """Bind trusted dispatch to one owner, phase, and fenced budget generation."""

    def __init__(
        self,
        service: BudgetService,
        *,
        username: str,
        budget_id: str,
        generation: int,
        phase: str,
        stop: BudgetStopLatch | None = None,
        wait_timeout: float = 600,
    ) -> None:
        """Bind the spending context without passing database credentials to guest code.

        Args:
            service: Authoritative ledger in the trusted API or worker parent.
            username: Authenticated owner, never a guest-supplied account name.
            budget_id: The same budget used by setup and the submitted root job.
            generation: Epoch authorizing new work; replaced workers are fenced.
            phase: Setup or run attribution.
            stop: Optional shared stop latch for all concurrent optimizer lanes.
            wait_timeout: Maximum wait for covered work before reporting uncertainty.
        """
        self.service = service
        self.username = username
        self.budget_id = budget_id
        self.generation = generation
        self.phase = phase
        self.stop = stop or BudgetStopLatch()
        self.wait_timeout = wait_timeout
        self._changed = threading.Condition()
        self._pinned: dict[str, int] = {}

    def pin(self, operation_id: str, units: int) -> None:
        """Record sandbox coverage that stays held until the work that opened it ends.

        Args:
            operation_id: The sandbox's admitted operation.
            units: Its current coverage in cent units.
        """
        with self._changed:
            self._pinned[operation_id] = units

    def unpin(self, operation_id: str) -> None:
        """Forget a sandbox's coverage once it settled or was released.

        Args:
            operation_id: The sandbox's admitted operation.
        """
        with self._changed:
            self._pinned.pop(operation_id, None)
            self._changed.notify_all()

    def _wait_cannot_help(self, error: BudgetInFlightError) -> BudgetInsufficientError | None:
        """Refuse a setup operation that only this check's own open sandboxes keep out.

        A setup check runs its samples one at a time inside sandboxes it opened
        itself, and those stay open while it waits, so their coverage can never
        make room. A run's lanes do release each other's coverage, so only the
        setup phase gives up early.

        Args:
            error: The admission refusal, with its limit arithmetic when it has one.

        Returns:
            The refusal to raise instead of waiting, or None when waiting may help.
        """
        if self.phase != "setup" or error.scope_units is None or error.room_units is None:
            return None
        with self._changed:
            pinned = sum(self._pinned.values())
        if error.scope_units <= error.room_units - pinned:
            return None
        return BudgetInsufficientError(
            f"The next operation needs up to {usd_text(ceil_cents(error.scope_units))}, but this check's "
            f"sandboxes hold {usd_text(ceil_cents(pinned))} of the "
            f"{usd_text(max(error.room_units, 0) // CENT_SCALE)} that remain."
        )

    def check_admission(self) -> None:
        """Fence unpaid control calls against the same cancellation and recovery authority.

        Raises:
            BudgetReached: When the run stopped at its approved limit.
            BudgetError: When this owner or execution generation no longer admits work.
        """
        self.stop.check()
        budget = self.service.get(self.budget_id, self.username)
        if budget.generation != self.generation:
            raise BudgetFencedError("The execution generation is obsolete.")
        if budget.blocked_reason == "budget_reached":
            raise self.stop.trip("The approved budget has been reached.")
        if budget.state not in {"open", "attached"}:
            raise BudgetConflictError("This budget is not admitting new work.")
        if (self.phase == "setup") != (budget.job_id is None):
            raise BudgetConflictError("The operation phase does not match the budget's submission state.")

    def reserve(
        self,
        quote: OperationQuote,
        *,
        operation_key: str,
        cost_kind: str,
        role: str | None = None,
        attempt: int = 0,
    ) -> OperationSnapshot:
        """Wait for in-flight coverage before declaring genuine budget exhaustion.

        Args:
            quote: Verified final request, resource bounds, and recorded prices.
            operation_key: Stable logical operation identity.
            cost_kind: Model or sandbox cost attribution.
            role: Task, judge, optimization, or runtime role.
            attempt: Physical retry number with independent usage coverage.

        Returns:
            The admitted physical attempt or its existing idempotent record.

        Raises:
            UsagePendingError: When old or unconfirmed work prevents safe admission.
            BudgetReached: When no covered work can release sufficient allowance.
        """
        deadline = time.monotonic() + self.wait_timeout
        while True:
            self.stop.check()
            try:
                return self.service.reserve(
                    self.budget_id,
                    self.username,
                    operation_key=generation_operation_key(operation_key, self.generation),
                    generation=self.generation,
                    phase=self.phase,
                    cost_kind=cost_kind,
                    request_fingerprint=quote.request_fingerprint,
                    price_snapshot=quote.price_snapshot,
                    max_cents=quote.maximum.total,
                    max_wallet_cents=quote.maximum.wallet,
                    attempt=attempt,
                    role=role,
                )
            except BudgetUnreconciledError as error:
                raise UsagePendingError(
                    "Previous work is awaiting confirmed usage; its coverage is retained."
                ) from error
            except BudgetInFlightError as error:
                refusal = self._wait_cannot_help(error)
                if refusal is not None:
                    raise refusal from error
                if time.monotonic() >= deadline:
                    raise UsagePendingError(
                        "Covered work has not settled; no overlapping work was admitted."
                    ) from error
                with self._changed:
                    self._changed.wait(timeout=min(1.0, max(0.0, deadline - time.monotonic())))
            except BudgetInsufficientError as error:
                if self.phase == "setup":
                    raise
                self.service.stop_admission(self.budget_id, self.username, reason="budget_reached")
                raise self.stop.trip(str(error)) from error

    def extend(
        self,
        operation_id: str,
        quote: OperationQuote,
        *,
        evidence_key: str,
        evidence: Mapping[str, Any],
        deadline: float,
    ) -> OperationSnapshot:
        """Grow a running sandbox's coverage, waiting for in-flight work until ``deadline``.

        Args:
            operation_id: Dispatched sandbox attempt.
            quote: The sandbox's bound over its extended lifetime.
            evidence_key: Immutable identity of this extension.
            evidence: Audit record of the extended bound.
            deadline: ``time.monotonic()`` instant after which waiting stops.

        Returns:
            The operation with its grown coverage.

        Raises:
            UsagePendingError: When covered work did not settle before ``deadline``.
            BudgetReached: When the budget cannot pay for the extension.
        """
        while True:
            self.stop.check()
            try:
                return self.service.extend_coverage(
                    operation_id,
                    self.username,
                    evidence_key=evidence_key,
                    max_cents=quote.maximum.total,
                    max_wallet_cents=quote.maximum.wallet,
                    evidence=evidence,
                )
            except BudgetInFlightError as error:
                refusal = self._wait_cannot_help(error)
                if refusal is not None:
                    raise refusal from error
                if time.monotonic() >= deadline:
                    raise UsagePendingError("Covered work has not settled; the sandbox was not extended.") from error
                with self._changed:
                    self._changed.wait(timeout=min(1.0, max(0.0, deadline - time.monotonic())))
            except BudgetInsufficientError as error:
                if self.phase == "setup":
                    raise
                self.service.stop_admission(self.budget_id, self.username, reason="budget_reached")
                raise self.stop.trip(str(error)) from error

    def execute(
        self,
        quote: OperationQuote,
        policy: ChargePolicy,
        dispatch: Callable[[], PaidResult[T]],
        *,
        operation_key: str,
        cost_kind: str,
        role: str | None = None,
        attempt: int = 0,
    ) -> T:
        """Dispatch once under coverage and settle actual usage on every outcome.

        Args:
            quote: Bound tied to the exact request the adapter will dispatch.
            policy: The same conversion policy recorded in the quote.
            dispatch: Trusted single-attempt transport; it must not perform hidden retries.
            operation_key: Logical identity retained across delivery retries.
            cost_kind: Model or sandbox category.
            role: Optional model role attribution.
            attempt: Independent physical retry number.

        Returns:
            Provider result after evidence has been durably recorded.

        Raises:
            UsagePendingError: When dispatch may have incurred cost without final evidence.
            ProviderFailedError: When the provider failed the response; a fresh attempt may follow.
            OperationCompletedError: When a completed attempt is replayed without its saved result.
        """
        if quote.price_snapshot.get("policy") != policy.snapshot():
            raise ValueError("Settlement policy must match the admitted quote.")
        operation = self.reserve(
            quote,
            operation_key=operation_key,
            cost_kind=cost_kind,
            role=role,
            attempt=attempt,
        )
        if operation.state == "settled":
            raise OperationCompletedError("The operation already completed; retrieve its saved result.")
        claimed = self.service.mark_dispatched(operation.id, self.username)
        if not claimed.dispatch_claimed:
            raise UsagePendingError("The attempt was already dispatched; do not repeat it.")
        zero_fee_byok = policy.kind == "byok_model" and policy.byok_fee_fraction == 0
        try:
            result = dispatch()
            if result.provider_request_id:
                self.service.mark_dispatched(operation.id, self.username, result.provider_request_id)
            if result.provider_usd is None:
                if result.refused:
                    self.service.reject(
                        operation.id,
                        self.username,
                        evidence_key=json_fingerprint(dict(result.evidence)),
                        evidence=dict(result.evidence),
                    )
                    return result.value
                if not zero_fee_byok:
                    self.service.mark_pending(
                        operation.id,
                        self.username,
                        evidence_key=json_fingerprint(dict(result.evidence)),
                        evidence=dict(result.evidence),
                    )
                    if result.failure is not None:
                        raise ProviderFailedError(result.failure)
                    raise UsagePendingError("Provider usage is not yet confirmed; the reservation remains active.")
            charge = policy.convert(Decimal(0) if zero_fee_byok else result.provider_usd)
            self.service.settle(
                operation.id,
                self.username,
                evidence_key=json_fingerprint(dict(result.evidence)),
                actual_cents=charge.total,
                actual_wallet_cents=charge.wallet,
                evidence=dict(result.evidence),
                final=zero_fee_byok or result.final,
            )
            if zero_fee_byok and result.failure is not None:
                raise ProviderFailedError(result.failure)
            if not result.final and not zero_fee_byok:
                raise UsagePendingError("Provider usage is incomplete; remaining coverage is retained.")
            return result.value
        except BaseException:
            current = self.service.get_operation(operation.id, self.username)
            if current.state in {"dispatched", "pending"}:
                if zero_fee_byok:
                    evidence = {"provider_failure": True}
                    self.service.settle(
                        operation.id,
                        self.username,
                        evidence_key=json_fingerprint(evidence),
                        actual_cents=Decimal(0),
                        actual_wallet_cents=Decimal(0),
                        evidence=evidence,
                        final=True,
                    )
                else:
                    self.service.mark_pending(operation.id, self.username)
            raise
        finally:
            with self._changed:
                self._changed.notify_all()
