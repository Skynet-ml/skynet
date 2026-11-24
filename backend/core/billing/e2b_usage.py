"""Reserve E2B sandbox compute and settle it from the measured runtime (SKY-544).

E2B publishes no API that reports the usage or the cost of one sandbox: its
console shows team totals only, and ``GET /sandboxes/{id}/metrics`` returns
resource samples, not billed time. So a box settles at the published
per-second rate times the runtime that the provider measured, from the
create call to the confirmed kill. The settled amount never passes the
reservation.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from .budget_amounts import cent_units
from .budgets import BudgetConflictError, OperationSnapshot
from .operation_pricing import ChargePolicy, OperationQuote, UnpricedOperationError, exact_nonnegative, operation_quote
from .runtime import BudgetRuntime, OperationCompletedError, UsagePendingError
from .usage_tags import box_tag
from .vercel_usage import vercel_charge_policy

# Published usage rates, read on 2026-11-24 from https://e2b.dev/pricing and
# https://docs.e2b.dev/faq/calculate-sandbox-price. E2B bills per second while
# a box runs, with no per-box fee; disk is included.
_PRICING_URL = "https://e2b.dev/pricing"
_PRICE_VERSION = "e2b-public-2026-11-24"
_CPU_USD_PER_SECOND = Decimal("0.000014")
_MEMORY_GIB_USD_PER_SECOND = Decimal("0.0000045")
# E2B boxes have 1 to 8 vCPUs and 1 to 8 GiB of memory, and live at most
# 24 hours on the Pro plan (same source).
MAX_VCPUS = 8
MAX_MEMORY_MB = 8 * 1024
_LIFETIME_CEILING_MS = 24 * 3_600_000
_MIB_PER_GIB = Decimal(1024)


def _usd(seconds: int, vcpus: int, memory_mb: int, cpu_rate: Decimal, memory_rate: Decimal) -> Decimal:
    """Price whole running seconds of one box.

    Args:
        seconds: Billed running time.
        vcpus: CPU count of the box.
        memory_mb: Memory of the box in MiB.
        cpu_rate: USD per vCPU-second.
        memory_rate: USD per GiB-second.

    Returns:
        The cost in USD.
    """
    return Decimal(seconds) * (vcpus * cpu_rate + Decimal(memory_mb) / _MIB_PER_GIB * memory_rate)


def quote_e2b_sandbox(request: Mapping[str, Any]) -> OperationQuote:
    """Price one box for its whole funded lifetime before it exists.

    Args:
        request: ``lifetime_ms``, ``vcpus`` and ``memory_mb`` of the box, and
            optionally its ``template``.

    Returns:
        Coverage for the box running at the funded size for the funded lifetime.

    Raises:
        UnpricedOperationError: When the lifetime or the size is not bounded.
    """
    lifetime_ms = request.get("lifetime_ms")
    vcpus = request.get("vcpus")
    memory_mb = request.get("memory_mb")
    for value, ceiling in ((lifetime_ms, _LIFETIME_CEILING_MS), (vcpus, MAX_VCPUS), (memory_mb, MAX_MEMORY_MB)):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= ceiling:
            raise UnpricedOperationError("E2B requires a bounded lifetime, CPU count and memory size.")
    seconds = math.ceil(lifetime_ms / 1000)
    maximum = _usd(seconds, vcpus, memory_mb, _CPU_USD_PER_SECOND, _MEMORY_GIB_USD_PER_SECOND)
    return operation_quote(
        request,
        maximum,
        ChargePolicy("sandbox"),
        {
            "provider": "e2b",
            "price_version": _PRICE_VERSION,
            "source": _PRICING_URL,
            "cpu_second_usd": str(_CPU_USD_PER_SECOND),
            "memory_gib_second_usd": str(_MEMORY_GIB_USD_PER_SECOND),
            "request": dict(request),
        },
    )


class E2BUsageReservation:
    """Own the budget coverage of one E2B box and its measured runtime."""

    def __init__(
        self,
        runtime: BudgetRuntime,
        request: Mapping[str, Any],
        *,
        operation_key: str,
        reserved: OperationSnapshot | None = None,
    ) -> None:
        """Reserve and claim one box before any network call.

        Args:
            runtime: Authenticated budget authority, outside the guest.
            request: The parameters that :func:`quote_e2b_sandbox` prices.
            operation_key: Stable logical identity of the box.
            reserved: A pool lease slice already reserved for this box.

        Raises:
            BudgetConflictError: When the lease slice was reserved for another request.
            UnpricedOperationError: When the request has no bounded lifetime or size.
            OperationCompletedError: When the box operation already settled.
            UsagePendingError: When another caller already dispatched the box.
        """
        # The quote checks every field first, so a missing one is unpriced, not a KeyError.
        self.quote = quote_e2b_sandbox(request)
        self.runtime = runtime
        self.request = dict(request)
        self.vcpus = request["vcpus"]
        self.memory_mb = request["memory_mb"]
        self.lifetime_ms = request["lifetime_ms"]
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
            raise OperationCompletedError("This sandbox operation already completed; retrieve its saved result.")
        runtime.pin(self.operation.id, cent_units(self.operation.max_cents), box=self.box)
        try:
            claimed = runtime.service.mark_dispatched(self.operation.id, runtime.username)
        except BaseException:
            # A refused claim means no box exists for this operation, so a kept
            # pin would count coverage this runtime no longer holds.
            runtime.unpin(self.operation.id)
            raise
        if not claimed.dispatch_claimed:
            raise UsagePendingError("This sandbox was already dispatched; reconcile it by hand.")
        self.sandbox_id: str | None = None
        self.box_vcpus: int | None = None
        self.box_memory_mb: int | None = None
        self.started_at: float | None = None
        self.stopped_at: float | None = None
        self.stop_confirmed = False

    def start(self, at: float) -> None:
        """Record the monotonic instant of the create call that may open the box.

        Args:
            at: Monotonic time just before the create call.
        """
        self.started_at = at

    def confirm(self, info: Any) -> None:
        """Bind the created box, keep its real size and check that it fits its funding.

        The settle prices the real size, which can be smaller than the funded one.

        Args:
            info: The SDK ``SandboxInfo`` of the created box.

        Raises:
            UsagePendingError: When the box is larger than its funding.
        """
        self.sandbox_id = info.sandbox_id
        self.runtime.service.mark_dispatched(self.operation.id, self.runtime.username, info.sandbox_id)
        if info.cpu_count > self.vcpus or info.memory_mb > self.memory_mb:
            raise UsagePendingError("The E2B template is larger than the funded CPU and memory size.")
        self.box_vcpus = info.cpu_count
        self.box_memory_mb = info.memory_mb

    def extend(self, lifetime_ms: int, *, deadline: float) -> None:
        """Fund the box up to ``lifetime_ms`` before E2B is asked to keep it alive that long.

        Args:
            lifetime_ms: Total lifetime once extended.
            deadline: ``time.monotonic()`` instant after which waiting for in-flight work stops.

        Raises:
            ValueError: When the lifetime does not grow.
            UnpricedOperationError: When the extended lifetime cannot be bounded.
            UsagePendingError: When covered work did not settle before ``deadline``.
            BudgetReached: When the budget cannot pay for the extension.
        """
        if lifetime_ms <= self.lifetime_ms:
            raise ValueError("An extension must lengthen the funded lifetime.")
        quote = quote_e2b_sandbox({**self.request, "lifetime_ms": lifetime_ms})
        self.operation = self.runtime.extend(
            self.operation.id,
            quote,
            evidence_key=f"e2b-extend:{lifetime_ms}",
            evidence={"provider": "e2b", "lifetime_ms": lifetime_ms, "request": quote.request_fingerprint},
            deadline=deadline,
        )
        self.runtime.pin(self.operation.id, cent_units(self.operation.max_cents), box=self.box)
        self.lifetime_ms = lifetime_ms

    def stopped(self, at: float, *, confirmed: bool) -> None:
        """Record the end of the box.

        Args:
            at: Monotonic time after the kill call.
            confirmed: True when every kill call answered, so no box can still run.
        """
        self.stopped_at = at
        self.stop_confirmed = confirmed

    def billed_seconds(self) -> int:
        """Return the running seconds that the box is charged for.

        Returns:
            0 when no create call went out, the whole funded lifetime when the
            kill is not confirmed (the box can run until its E2B timeout), and
            otherwise the measured time, rounded up and capped at the funded lifetime.
        """
        funded = math.ceil(self.lifetime_ms / 1000)
        if self.started_at is None:
            return 0
        if self.stopped_at is None or not self.stop_confirmed:
            return funded
        return min(funded, math.ceil(max(0.0, self.stopped_at - self.started_at)))

    def settle(self) -> None:
        """Settle the stopped box at its measured runtime, inside the reservation.

        Raises:
            UsagePendingError: When the recorded price snapshot is unusable.
            BudgetError: When the ledger refuses the settlement.
        """
        try:
            current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
            if current.state == "settled":
                self.runtime.unpin(self.operation.id)
                return
            snapshot = self.quote.price_snapshot
            try:
                cpu_rate = exact_nonnegative(snapshot["cpu_second_usd"])
                memory_rate = exact_nonnegative(snapshot["memory_gib_second_usd"])
            except (KeyError, UnpricedOperationError) as error:
                raise UsagePendingError("The E2B admission has no usable price snapshot.") from error
            seconds = self.billed_seconds()
            # An unconfirmed box has no known size, so it pays the funded size.
            vcpus = self.box_vcpus if self.box_vcpus is not None else self.vcpus
            memory_mb = self.box_memory_mb if self.box_memory_mb is not None else self.memory_mb
            usd = _usd(seconds, vcpus, memory_mb, cpu_rate, memory_rate)
            charge = vercel_charge_policy(snapshot).convert(usd)
            # Kill latency or a create that started before the box can make
            # the measured time pass the funded lifetime; the charge must stay
            # inside the reservation.
            total = min(charge.total, current.max_cents)
            wallet = min(charge.wallet, current.max_wallet_cents, total)
            self.runtime.service.settle(
                self.operation.id,
                self.runtime.username,
                evidence_key=f"e2b-stop:{self.operation.id}",
                actual_cents=total,
                actual_wallet_cents=wallet,
                evidence={
                    "provider": "e2b",
                    "source": _PRICING_URL,
                    "sandbox_id": self.sandbox_id,
                    "billed_seconds": seconds,
                    "vcpus": vcpus,
                    "memory_mb": memory_mb,
                    "stop_confirmed": self.stop_confirmed,
                    "provider_usd": str(usd),
                },
            )
            self.runtime.unpin(self.operation.id)
        except BaseException:
            self._pending()
            raise

    def _pending(self) -> None:
        """Keep the coverage pending for manual reconciliation after a failed settle."""
        current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
        if current.state in {"dispatched", "pending"}:
            evidence = {
                "provider": "e2b",
                "sandbox_id": self.sandbox_id,
                "billed_seconds": self.billed_seconds(),
                "stop_confirmed": self.stop_confirmed,
            }
            self.runtime.service.mark_pending(
                self.operation.id,
                self.runtime.username,
                evidence_key=f"e2b-pending:{self.operation.id}",
                evidence=evidence,
            )


class E2BUsage:
    """Meter E2B sandboxes through the provider-neutral usage protocol."""

    name = "e2b"

    def quote(self, request: Mapping[str, Any]) -> OperationQuote:
        """Price one box before any provider resource exists.

        Args:
            request: Final create parameters.

        Returns:
            Coverage for the box's whole funded lifetime.
        """
        return quote_e2b_sandbox(request)

    def reserve(
        self,
        runtime: BudgetRuntime,
        request: Mapping[str, Any],
        *,
        operation_key: str,
        reserved: OperationSnapshot | None = None,
    ) -> E2BUsageReservation:
        """Reserve and claim one box, or claim a lease slice already reserved for it.

        Args:
            runtime: Authenticated budget authority.
            request: Final create parameters.
            operation_key: Stable logical identity of the box.
            reserved: Optional pool lease slice for this box.

        Returns:
            The claimed reservation that the box confirms, extends and settles.
        """
        return E2BUsageReservation(runtime, request, operation_key=operation_key, reserved=reserved)

    def confirm(self, reservation: E2BUsageReservation, session: Any) -> None:
        """Check that the created box fits its funded size.

        Args:
            reservation: The box's claimed reservation.
            session: The SDK ``SandboxInfo`` of the box.
        """
        reservation.confirm(session)

    def extend(self, reservation: E2BUsageReservation, lifetime_ms: int, *, deadline: float) -> None:
        """Fund a longer box lifetime before E2B grants it.

        Args:
            reservation: The box's claimed reservation.
            lifetime_ms: Total lifetime after the extension.
            deadline: ``time.monotonic()`` instant after which waiting stops.
        """
        reservation.extend(lifetime_ms, deadline=deadline)

    def settle(self, reservation: E2BUsageReservation) -> None:
        """Settle the stopped box at its measured runtime.

        Args:
            reservation: The box's claimed reservation.
        """
        reservation.settle()


USAGE = E2BUsage()
