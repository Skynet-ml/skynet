"""Reserve Prime Intellect sandbox compute and settle it from the sandbox's own record.

Prime publishes no per-sandbox usage or cost API. Its wallet API lists
billing rows, but it does not document that a row names one sandbox or when
the row is final. So a box settles at its measured lifetime, read from
Prime's ``createdAt`` and ``terminatedAt`` timestamps, at the published
rates. The settlement never goes above the reserved quote.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from .budget_amounts import cent_units
from .budgets import BudgetConflictError, BudgetError, OperationSnapshot
from .operation_pricing import (
    ChargePolicy,
    OperationQuote,
    UnpricedOperationError,
    exact_nonnegative,
    json_fingerprint,
    operation_quote,
)
from .runtime import BudgetRuntime, OperationCompletedError, UsagePendingError
from .usage_tags import box_tag
from .vercel_usage import vercel_charge_policy

# Published launch rates, read on 2026-11-24 from
# https://docs.primeintellect.ai/sandboxes/overview ("billed while running").
# Prime says these rates hold through 2026-12-22; check them again then.
_PRICING_URL = "https://docs.primeintellect.ai/sandboxes/overview"
_PRICE_VERSION = "prime-sandbox-launch-2026-11-24"
_CPU_HOUR_USD = Decimal("0.02")
_MEMORY_GIB_HOUR_USD = Decimal("0.0125")
_DISK_GIB_HOUR_USD = Decimal("0.0002")
_SECONDS_PER_HOUR = Decimal(3600)
_ENDED_STATUSES = frozenset({"ERROR", "TERMINATED", "TIMEOUT"})
_SIZE_FIELDS = ("cpu_cores", "memory_gb", "disk_size_gb")


def _positive(value: Any, name: str) -> Decimal:
    """Read one positive size from a create request.

    Args:
        value: The request value.
        name: The field name, for the error.

    Returns:
        The exact value.

    Raises:
        UnpricedOperationError: When the value is missing or not positive.
    """
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal) or not value > 0:
        raise UnpricedOperationError(f"A protected Prime sandbox needs a positive {name}.")
    return Decimal(str(value))


def _hourly_usd(request: Mapping[str, Any], rates: Mapping[str, Any]) -> Decimal:
    """Price one hour of a sandbox of this size.

    Args:
        request: The create request with ``cpu_cores``, ``memory_gb`` and ``disk_size_gb``.
        rates: The CPU, memory and disk rates, as strings or decimals.

    Returns:
        The USD cost of one running hour.
    """
    return (
        _positive(request.get("cpu_cores"), "cpu_cores") * exact_nonnegative(rates["cpu_hour_usd"])
        + _positive(request.get("memory_gb"), "memory_gb") * exact_nonnegative(rates["memory_gib_hour_usd"])
        + _positive(request.get("disk_size_gb"), "disk_size_gb") * exact_nonnegative(rates["disk_gib_hour_usd"])
    )


def _rates() -> dict[str, str]:
    """Return the current published rates for a price snapshot.

    Returns:
        The rates as strings.
    """
    return {
        "cpu_hour_usd": str(_CPU_HOUR_USD),
        "memory_gib_hour_usd": str(_MEMORY_GIB_HOUR_USD),
        "disk_gib_hour_usd": str(_DISK_GIB_HOUR_USD),
    }


def quote_prime_sandbox(request: Mapping[str, Any]) -> OperationQuote:
    """Price a Prime sandbox for its whole timeout before it exists.

    Prime ends a sandbox at ``timeout_minutes``, so the timeout times the
    hourly rate is the most the sandbox can run.

    Args:
        request: Final create parameters with ``cpu_cores``, ``memory_gb``,
            ``disk_size_gb``, ``timeout_minutes`` and ``gpu_count``.

    Returns:
        Coverage for the sandbox's whole timeout.

    Raises:
        UnpricedOperationError: When a size or the timeout is missing, or the request asks for a GPU.
    """
    minutes = request.get("timeout_minutes")
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
        raise UnpricedOperationError("A protected Prime sandbox needs a positive whole timeout_minutes.")
    if request.get("gpu_count") != 0:
        raise UnpricedOperationError("Protected Prime runs have no GPU price.")
    maximum = _hourly_usd(request, _rates()) * Decimal(minutes * 60) / _SECONDS_PER_HOUR
    return operation_quote(
        request,
        maximum,
        ChargePolicy("sandbox"),
        {
            "provider": "prime",
            "price_version": _PRICE_VERSION,
            "source": _PRICING_URL,
            "rates": _rates(),
            "basis": "createdAt to terminatedAt, per second, capped at timeout_minutes",
            "request": dict(request),
        },
    )


def _timestamp(value: Any, name: str) -> datetime:
    """Parse one ISO timestamp from a sandbox record.

    Args:
        value: The ISO 8601 text.
        name: The field name, for the error.

    Returns:
        The aware datetime.

    Raises:
        UsagePendingError: When the timestamp is missing or has no time zone.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise UsagePendingError(f"The Prime sandbox record has no valid {name}.")
    return parsed


def prime_actual_usd(record: Mapping[str, Any], *, sandbox_id: str, price_snapshot: Mapping[str, Any]) -> Decimal:
    """Price an ended sandbox from its measured lifetime at the admitted rates.

    The time counts from ``created_at``, not ``started_at``, so the
    provisioning time is charged too: Prime does not say whether it bills
    it, and undercharging is the worse error. The quote caps the result.

    Args:
        record: The sandbox record that Prime returned after the delete.
        sandbox_id: The one admitted sandbox.
        price_snapshot: The admitted quote's price snapshot.

    Returns:
        The USD cost of the measured seconds, capped at the admitted timeout.

    Raises:
        UsagePendingError: When the sandbox has not ended or the record is not usable.
    """
    if record.get("id") != sandbox_id or record.get("status") not in _ENDED_STATUSES:
        raise UsagePendingError("Prime has not confirmed that the admitted sandbox ended.")
    created = _timestamp(record.get("created_at"), "created_at")
    ended = _timestamp(record.get("terminated_at"), "terminated_at")
    if ended < created:
        raise UsagePendingError("The Prime sandbox record has inconsistent timestamps.")
    try:
        request = price_snapshot["request"]
        rates = price_snapshot["rates"]
        if price_snapshot.get("provider") != "prime":
            raise ValueError("Unsupported historical billing policy")
        seconds = min(math.ceil((ended - created).total_seconds()), request["timeout_minutes"] * 60)
        return _hourly_usd(request, rates) * Decimal(seconds) / _SECONDS_PER_HOUR
    except (KeyError, TypeError, ValueError) as error:
        raise UsagePendingError("The Prime sandbox admission has no usable price snapshot.") from error


class PrimeUsageReservation:
    """Own the budget hold of one Prime sandbox from before its create to its settlement."""

    def __init__(
        self,
        runtime: BudgetRuntime,
        request: Mapping[str, Any],
        *,
        operation_key: str,
        reserved: OperationSnapshot | None = None,
    ) -> None:
        """Reserve and claim one sandbox before any create call.

        Args:
            runtime: Authenticated budget authority kept outside the guest.
            request: Final create parameters that ``quote_prime_sandbox`` prices.
            operation_key: Stable logical identity of the sandbox.
            reserved: A pool lease slice already reserved for this sandbox.

        Raises:
            BudgetConflictError: When the lease slice was reserved for another request.
            OperationCompletedError: When this operation already settled.
            UsagePendingError: When another caller already dispatched this operation.
        """
        self.runtime = runtime
        self.request = dict(request)
        self.quote = quote_prime_sandbox(request)
        self.box = box_tag(operation_key)
        if reserved is None:
            self.operation = runtime.reserve(
                self.quote, operation_key=operation_key, cost_kind="sandbox", role="runtime"
            )
        elif (
            reserved.request_fingerprint != self.quote.request_fingerprint
            or reserved.cost_kind != "sandbox"
            or reserved.budget_id != runtime.budget_id
        ):
            raise BudgetConflictError("The lease slice was reserved for a different sandbox request.")
        else:
            self.operation = reserved
        if self.operation.state == "settled":
            raise OperationCompletedError("This sandbox operation already completed.")
        runtime.pin(self.operation.id, cent_units(self.operation.max_cents), box=self.box)
        try:
            claimed = runtime.service.mark_dispatched(self.operation.id, runtime.username)
        except BaseException:
            # A refused claim means no sandbox exists for this operation, so a
            # kept pin would count coverage this runtime no longer holds.
            runtime.unpin(self.operation.id)
            raise
        if not claimed.dispatch_claimed:
            raise UsagePendingError("This sandbox was already dispatched; reconcile it by hand.")
        self.sandbox_id: str | None = None
        self.record: dict[str, Any] = {}

    def confirm_created(self, sandbox: Mapping[str, Any]) -> None:
        """Bind the created sandbox and check that it fits its funded size.

        Args:
            sandbox: The sandbox record that the create call returned.

        Raises:
            UsagePendingError: When the sandbox is larger or lives longer than funded.
        """
        if self.sandbox_id not in {None, sandbox.get("id")} or not isinstance(sandbox.get("id"), str):
            raise UsagePendingError("Prime returned a sandbox outside the admitted operation.")
        self.sandbox_id = sandbox["id"]
        self.runtime.service.mark_dispatched(self.operation.id, self.runtime.username, self.sandbox_id)
        try:
            fits = all(_positive(sandbox.get(name), name) <= Decimal(str(self.request[name])) for name in _SIZE_FIELDS)
        except UnpricedOperationError:
            fits = False
        if (
            not fits
            or sandbox.get("gpu_count") != 0
            or sandbox.get("timeout_minutes") != self.request["timeout_minutes"]
        ):
            raise UsagePendingError("Prime did not confirm the admitted CPU, memory, disk and timeout.")

    def extend(self, timeout_minutes: int, *, deadline: float) -> None:
        """Fund a longer timeout before Prime is asked for it.

        Args:
            timeout_minutes: The sandbox's whole timeout after the extension.
            deadline: ``time.monotonic()`` instant after which waiting for in-flight work stops.

        Raises:
            ValueError: When the timeout does not grow.
            UsagePendingError: When covered work did not settle before ``deadline``.
            BudgetReached: When the budget cannot pay for the extension.
        """
        if timeout_minutes <= self.request["timeout_minutes"]:
            raise ValueError("An extension must lengthen the funded timeout.")
        quote = quote_prime_sandbox({**self.request, "timeout_minutes": timeout_minutes})
        extended = self.runtime.extend(
            self.operation.id,
            quote,
            evidence_key=f"prime-extend:{timeout_minutes}",
            evidence={"provider": "prime", "timeout_minutes": timeout_minutes, "request": quote.request_fingerprint},
            deadline=deadline,
        )
        self.runtime.pin(self.operation.id, cent_units(extended.max_cents), box=self.box)
        self.request["timeout_minutes"] = timeout_minutes
        self.quote = quote

    def pending(self, record: Mapping[str, Any] | None = None) -> None:
        """Keep the hold when the sandbox or its usage is uncertain.

        Args:
            record: The last sandbox record Prime returned, or ``None``.
        """
        current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
        if current.state in {"dispatched", "pending"}:
            evidence = {"provider": "prime", "sandbox_id": self.sandbox_id, "sandbox": dict(record or self.record)}
            self.runtime.service.mark_pending(
                self.operation.id,
                self.runtime.username,
                evidence_key=json_fingerprint(evidence),
                evidence=evidence,
            )

    def release(self, reason: str) -> None:
        """Return the hold of a sandbox that Prime never created.

        Use it only when the create call was never sent, or Prime refused
        it with a 4xx answer. If the release fails, the hold stays pending.

        Args:
            reason: Why no sandbox exists, for the audit record.
        """
        if self.sandbox_id is None:
            try:
                self.runtime.service.reject(
                    self.operation.id,
                    self.runtime.username,
                    evidence_key=f"prime-refusal:{self.operation.id}",
                    evidence={"provider": "prime", "reason": reason[:1000]},
                )
                self.runtime.unpin(self.operation.id)
                return
            except BudgetError:
                pass
        self.pending()

    def settle(self, record: Mapping[str, Any] | None = None) -> None:
        """Settle an ended sandbox at its measured cost, never above the reservation.

        Args:
            record: The sandbox record that Prime returned after the delete;
                ``None`` uses the record kept from an earlier call.

        Raises:
            UsagePendingError: When the end or the usage is uncertain, or the
                ledger write failed; the hold stays pending.
            BudgetError: When the ledger refused the settlement.
        """
        if record is not None:
            self.record = dict(record)
        record = self.record
        try:
            current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
            if current.state == "settled":
                self.runtime.unpin(self.operation.id)
                return
            if self.sandbox_id is None:
                raise UsagePendingError("The Prime create call returned no sandbox identity.")
            usd = prime_actual_usd(record, sandbox_id=self.sandbox_id, price_snapshot=self.quote.price_snapshot)
            charge = vercel_charge_policy(self.quote.price_snapshot).convert(usd)
            # The rounding to cents can pass the quote by a fraction of a cent.
            total = min(charge.total, current.max_cents)
            wallet = min(charge.wallet, current.max_wallet_cents, total)
            self.runtime.service.settle(
                self.operation.id,
                self.runtime.username,
                evidence_key=f"prime-stop:{self.sandbox_id}",
                actual_cents=total,
                actual_wallet_cents=wallet,
                evidence={
                    "provider": "prime",
                    "source": _PRICING_URL,
                    "sandbox": dict(record),
                    "provider_usd": str(usd),
                },
            )
            self.runtime.unpin(self.operation.id)
        except BaseException as error:
            self.pending(record)
            if isinstance(error, UsagePendingError | BudgetError) or not isinstance(error, Exception):
                raise
            # Any other error text can hold database details.
            raise UsagePendingError("Sandbox usage is not yet settled; the reservation remains active.") from error


class PrimeUsage:
    """Meter Prime sandboxes through the provider-neutral usage protocol."""

    name = "prime"

    def quote(self, request: Mapping[str, Any]) -> OperationQuote:
        """Price one sandbox before any provider resource exists.

        Args:
            request: Final create parameters.

        Returns:
            Coverage for the sandbox's whole timeout.
        """
        return quote_prime_sandbox(request)

    def reserve(
        self,
        runtime: BudgetRuntime,
        request: Mapping[str, Any],
        *,
        operation_key: str,
        reserved: OperationSnapshot | None = None,
    ) -> PrimeUsageReservation:
        """Reserve and claim one sandbox, or claim a lease slice already reserved for it.

        Args:
            runtime: Authenticated budget authority.
            request: Final create parameters.
            operation_key: Stable logical identity of the box.
            reserved: Optional pool lease slice for this box.

        Returns:
            The claimed reservation that the box confirms, extends and settles.
        """
        return PrimeUsageReservation(runtime, request, operation_key=operation_key, reserved=reserved)

    def confirm(self, reservation: PrimeUsageReservation, session: Any) -> None:
        """Check the created sandbox against its funded size.

        Args:
            reservation: The box's claimed reservation.
            session: The sandbox record that the create call returned.
        """
        reservation.confirm_created(session)

    def extend(self, reservation: PrimeUsageReservation, lifetime_ms: int, *, deadline: float) -> None:
        """Fund a longer timeout before the provider grants it.

        Args:
            reservation: The box's claimed reservation.
            lifetime_ms: Total lifetime after the extension; Prime counts whole minutes.
            deadline: ``time.monotonic()`` instant after which waiting stops.
        """
        reservation.extend(math.ceil(lifetime_ms / 60_000), deadline=deadline)

    def settle(self, reservation: PrimeUsageReservation) -> None:
        """Settle the stopped sandbox from the last record the provider kept on it.

        Args:
            reservation: The box's claimed reservation.
        """
        reservation.settle()


USAGE = PrimeUsage()
