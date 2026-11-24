"""Reserve Novita sandbox compute and settle it from the measured runtime (SKY-935).

Novita publishes no API that reports the usage or the cost of one sandbox,
so a box settles at the published per-second rate times the runtime that
the provider measured, from the create call to the confirmed kill. The hold
covers the whole funded lifetime before the create, and the settled amount
never passes it. A hold is released only when no box can exist, and an
outcome that the provider cannot prove stays pending for reconciliation.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from ..sandbox.capabilities import SandboxSpecError
from .budget_amounts import cent_units
from .budgets import BudgetConflictError, OperationSnapshot
from .operation_pricing import ChargePolicy, OperationQuote, UnpricedOperationError, exact_nonnegative, operation_quote
from .runtime import BudgetRuntime, OperationCompletedError, UsagePendingError
from .usage_tags import box_tag
from .vercel_usage import vercel_charge_policy

logger = logging.getLogger(__name__)

# Published usage rates, read on 2026-11-24 from
# https://docs.novita.ai/guides/sandbox-pricing (novita.ai/docs/guides/sandbox-pricing
# redirects there). Novita bills CPU and memory per second while a box runs;
# the 20 GB ephemeral disk of a running box is free.
_PRICING_URL = "https://docs.novita.ai/guides/sandbox-pricing"
_PRICE_VERSION = "novita-public-2026-11-24"
_CPU_USD_PER_SECOND = Decimal("0.0000098")
_MEMORY_GIB_USD_PER_SECOND = Decimal("0.0000032")
# The same page prices memory from 512 MiB to 8192 MiB and lists CPU up to
# 8 cores. The SDK create docs cap a box timeout at 24 hours (Pro plan); the
# long-running mode that lifts the cap is never used.
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


def quote_novita_sandbox(request: Mapping[str, Any]) -> OperationQuote:
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
            raise UnpricedOperationError("Novita requires a bounded lifetime, CPU count and memory size.")
    seconds = math.ceil(lifetime_ms / 1000)
    maximum = _usd(seconds, vcpus, memory_mb, _CPU_USD_PER_SECOND, _MEMORY_GIB_USD_PER_SECOND)
    return operation_quote(
        request,
        maximum,
        ChargePolicy("sandbox"),
        {
            "provider": "novita",
            "price_version": _PRICE_VERSION,
            "source": _PRICING_URL,
            "cpu_second_usd": str(_CPU_USD_PER_SECOND),
            "memory_gib_second_usd": str(_MEMORY_GIB_USD_PER_SECOND),
            "request": dict(request),
        },
    )


class NovitaUsageReservation:
    """Own the budget coverage of one Novita box and its measured runtime."""

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
            request: The parameters that :func:`quote_novita_sandbox` prices.
            operation_key: Stable logical identity of the box.
            reserved: A pool lease slice already reserved for this box.

        Raises:
            BudgetConflictError: When the lease slice was reserved for another request.
            UnpricedOperationError: When the request has no bounded lifetime or size.
            OperationCompletedError: When the box operation already settled.
            UsagePendingError: When another caller already dispatched the box.
        """
        # The quote checks every field first, so a missing one is unpriced, not a KeyError.
        self.quote = quote_novita_sandbox(request)
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
        self.box_name: str | None = None
        self.sandbox_id: str | None = None
        self.box_vcpus: int | None = None
        self.box_memory_mb: int | None = None
        self.started_at: float | None = None
        self.stopped_at: float | None = None
        self.create_refused = False
        self.box_seen = False
        self.stop_confirmed = False

    def start(self, at: float, name: str) -> None:
        """Record the create call that may open the box.

        Args:
            at: Monotonic time just before the create call.
            name: The box tag in the box metadata. When no box id comes back,
                it is the only link from the hold to the box.
        """
        self.started_at = at
        self.box_name = name

    def refused(self) -> None:
        """Record that Novita refused the create with an answer that opens no box."""
        self.create_refused = True

    def confirm(self, info: Any) -> None:
        """Bind the created box, keep its real size and check that it fits its funding.

        The size is kept also when it is too large, so the settle prices the
        box that ran; the clamp then keeps the charge inside the hold.

        Args:
            info: The SDK ``SandboxInfo`` of the created box.

        Raises:
            SandboxSpecError: When the box is larger than its funding. The
                caller kills the box and settles.
        """
        self.sandbox_id = info.sandbox_id
        self.box_seen = True
        self.runtime.service.mark_dispatched(self.operation.id, self.runtime.username, info.sandbox_id)
        self.box_vcpus = info.cpu_count
        self.box_memory_mb = info.memory_mb
        if info.cpu_count > self.vcpus or info.memory_mb > self.memory_mb:
            raise SandboxSpecError("The Novita template is larger than the funded CPU and memory size.")

    def extend(self, lifetime_ms: int, *, deadline: float) -> None:
        """Refuse an extension: a Novita box gets its whole funded lifetime at create.

        Args:
            lifetime_ms: Total lifetime once extended.
            deadline: Unused; the protocol passes it.

        Raises:
            UnpricedOperationError: Always.
        """
        raise UnpricedOperationError("A Novita box is funded for its whole lifetime at create and never extends.")

    def stopped(self, at: float, *, confirmed: bool, box_seen: bool) -> None:
        """Record the end of the box.

        Args:
            at: Monotonic time after the kill call.
            confirmed: True when every kill call answered and no unseen box
                can still run.
            box_seen: True when the provider held a box handle or the sweep
                killed a tagged box.
        """
        self.stopped_at = at
        self.stop_confirmed = confirmed
        self.box_seen = self.box_seen or box_seen

    def billed_seconds(self) -> int:
        """Return the running seconds that the box is charged for.

        Returns:
            0 when no create call went out, and otherwise the measured time,
            rounded up. The settle caps it at the hold.
        """
        if self.started_at is None or self.stopped_at is None:
            return 0
        return math.ceil(max(0.0, self.stopped_at - self.started_at))

    def no_box(self) -> bool:
        """Tell whether no box can exist for this hold.

        Returns:
            True when the create was never sent, or when Novita refused it and
            the confirmed sweep found no box.
        """
        if self.started_at is None:
            return True
        return self.create_refused and self.stop_confirmed and not self.box_seen and self.sandbox_id is None

    def settle(self) -> None:
        """Release, settle or keep pending the hold of the stopped box, once.

        Raises:
            UsagePendingError: When the box may still run, or the recorded
                price snapshot is unusable. The hold stays pending.
            BudgetError: When the ledger refuses the settlement.
        """
        try:
            current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
            if current.state in {"settled", "released"}:
                self.runtime.unpin(self.operation.id)
                return
            if self.no_box():
                self._release()
                return
            if not self.stop_confirmed:
                raise UsagePendingError("The Novita box may still run, so its usage stays pending.")
            snapshot = self.quote.price_snapshot
            try:
                cpu_rate = exact_nonnegative(snapshot["cpu_second_usd"])
                memory_rate = exact_nonnegative(snapshot["memory_gib_second_usd"])
            except (KeyError, UnpricedOperationError) as error:
                raise UsagePendingError("The Novita admission has no usable price snapshot.") from error
            seconds = self.billed_seconds()
            # An unconfirmed box has no known size, so it pays the funded size.
            vcpus = self.box_vcpus if self.box_vcpus is not None else self.vcpus
            memory_mb = self.box_memory_mb if self.box_memory_mb is not None else self.memory_mb
            usd = _usd(seconds, vcpus, memory_mb, cpu_rate, memory_rate)
            charge = vercel_charge_policy(snapshot).convert(usd)
            total = min(charge.total, current.max_cents)
            wallet = min(charge.wallet, current.max_wallet_cents, total)
            clamped = charge.total > current.max_cents
            if clamped:
                # Kill latency, or a box larger than its funding, can pass the
                # hold; Skynet pays the difference, never the user.
                logger.warning(
                    "Novita sandbox %s ran past its hold: %s cents clamped to %s.",
                    self.sandbox_id or self.box_name,
                    charge.total,
                    current.max_cents,
                )
            self.runtime.service.settle(
                self.operation.id,
                self.runtime.username,
                evidence_key=f"novita-stop:{self.operation.id}",
                actual_cents=total,
                actual_wallet_cents=wallet,
                evidence={
                    "provider": "novita",
                    "source": _PRICING_URL,
                    "sandbox_id": self.sandbox_id,
                    "box_name": self.box_name,
                    "billed_seconds": seconds,
                    "vcpus": vcpus,
                    "memory_mb": memory_mb,
                    "provider_usd": str(usd),
                    "clamped": clamped,
                    "unclamped_cents": str(charge.total),
                },
            )
            self.runtime.unpin(self.operation.id)
        except BaseException:
            self.pending()
            raise

    def _release(self) -> None:
        """Return the whole hold, because no box can exist for it."""
        reason = "Novita refused the create call" if self.started_at is not None else "the create call was never sent"
        try:
            self.runtime.service.reject(
                self.operation.id,
                self.runtime.username,
                evidence_key=f"novita-unsent:{self.operation.id}",
                evidence={"provider": "novita", "reason": reason, "box_name": self.box_name},
            )
        finally:
            # No box can exist, so the pin has no use even when the reject fails.
            self.runtime.unpin(self.operation.id)

    def pending(self) -> None:
        """Keep the coverage pending for manual reconciliation, once."""
        current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
        # The ledger refuses new evidence under the same key, and a retried
        # settle can come here again, so an already pending hold stays as is.
        if current.state == "dispatched":
            self.runtime.service.mark_pending(
                self.operation.id,
                self.runtime.username,
                evidence_key=f"novita-pending:{self.operation.id}",
                evidence={
                    "provider": "novita",
                    "sandbox_id": self.sandbox_id,
                    "box_name": self.box_name,
                    "billed_seconds": self.billed_seconds(),
                    "create_refused": self.create_refused,
                    "box_seen": self.box_seen,
                    "stop_confirmed": self.stop_confirmed,
                },
            )


class NovitaUsage:
    """Meter Novita sandboxes through the provider-neutral usage protocol."""

    name = "novita"

    def quote(self, request: Mapping[str, Any]) -> OperationQuote:
        """Price one box before any provider resource exists.

        Args:
            request: Final create parameters.

        Returns:
            Coverage for the box's whole funded lifetime.
        """
        return quote_novita_sandbox(request)

    def reserve(
        self,
        runtime: BudgetRuntime,
        request: Mapping[str, Any],
        *,
        operation_key: str,
        reserved: OperationSnapshot | None = None,
    ) -> NovitaUsageReservation:
        """Reserve and claim one box, or claim a lease slice already reserved for it.

        Args:
            runtime: Authenticated budget authority.
            request: Final create parameters.
            operation_key: Stable logical identity of the box.
            reserved: Optional pool lease slice for this box.

        Returns:
            The claimed reservation that the box confirms and settles.
        """
        return NovitaUsageReservation(runtime, request, operation_key=operation_key, reserved=reserved)

    def confirm(self, reservation: NovitaUsageReservation, session: Any) -> None:
        """Check that the created box fits its funded size.

        Args:
            reservation: The box's claimed reservation.
            session: The SDK ``SandboxInfo`` of the box.
        """
        reservation.confirm(session)

    def extend(self, reservation: NovitaUsageReservation, lifetime_ms: int, *, deadline: float) -> None:
        """Refuse an extension; the hold covers the whole lifetime from the start.

        Args:
            reservation: The box's claimed reservation.
            lifetime_ms: Total lifetime after the extension.
            deadline: ``time.monotonic()`` instant after which waiting stops.
        """
        reservation.extend(lifetime_ms, deadline=deadline)

    def settle(self, reservation: NovitaUsageReservation) -> None:
        """Release, settle or keep pending the stopped box's hold.

        Args:
            reservation: The box's claimed reservation.
        """
        reservation.settle()


USAGE = NovitaUsage()
