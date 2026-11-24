"""Reserve Modal sandbox compute and settle it from measured runtime.

Modal has no API that reports the cost of one sandbox. Its billing report
(``modal.billing.workspace_billing_report``) gives cost per App and per
hour or day, and every Skynet box shares one App, so it cannot attribute a
cost to a box. A box therefore settles at its measured runtime times the
published rates. The provider sends equal CPU and memory requests and
limits, and Modal bills ``max(request, usage)``, so the request is the
exact billed size. The runtime runs from the create call to the confirmed
terminate, which is never shorter than the time Modal bills.

Modal neither caps nor reports the egress of one sandbox, so no hold can
bound it. A protected Modal box therefore has no network at all: the
provider refuses every policy that opens egress, and the quote and the
settlement carry no egress charge.

Modal cannot make the timeout of a running sandbox longer, so a reservation
refuses an extension.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
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

# The conversion is the generic sandbox charge policy; it lives in the Vercel
# module only for history, and moving it is out of scope for SKY-546.
from .vercel_usage import vercel_charge_policy

# Read from https://modal.com/pricing on 2026-11-24 ("Modal Sandbox pricing",
# non-preemptible). These sandbox rates already include Modal's 3x
# non-preemptible multiplier. A core is a physical core (2 vCPU). The page
# lists no creation fee. Region selection costs 1.15-1.75x, but the provider
# never selects a region.
_PRICING_URL = "https://modal.com/pricing"
_PRICE_VERSION = "modal-sandbox-public-2026-11-24"
_CPU_CORE_SECOND_USD = Decimal("0.00003942")
_MEMORY_GIB_SECOND_USD = Decimal("0.00000667")
_MIN_CPU_CORES = Decimal("0.125")
_MIN_MEMORY_MIB = 128
_MIB_PER_GIB = Decimal(1024)
# Modal ends every sandbox after 24 hours.
_LIFETIME_CEILING_MS = 24 * 60 * 60 * 1000
# Modal can bill the boot before the timeout starts and the terminate after
# it ends, so the hold funds this much more than the timeout that Modal gets.
_LIFETIME_MARGIN_MS = 60_000


def _seconds(duration_ms: int) -> int:
    """Round a duration up to Modal's per-second billing.

    Args:
        duration_ms: The duration in milliseconds.

    Returns:
        Whole seconds, at least one.
    """
    return max(1, math.ceil(duration_ms / 1000))


def _usd(snapshot: Mapping[str, Any], duration_ms: int) -> Decimal:
    """Price one box of the admitted shape for a duration.

    Args:
        snapshot: The admitted price snapshot with its rates and request.
        duration_ms: The billed duration.

    Returns:
        The compute cost for the duration.

    Raises:
        UsagePendingError: When the snapshot has no usable rates.
    """
    try:
        if snapshot.get("provider") != "modal":
            raise ValueError("Not a Modal price snapshot")
        cpu_rate = exact_nonnegative(snapshot["cpu_core_second_usd"])
        memory_rate = exact_nonnegative(snapshot["memory_gib_second_usd"])
        cores = max(exact_nonnegative(snapshot["request"]["cpu_cores"]), _MIN_CPU_CORES)
        memory_gib = Decimal(max(snapshot["request"]["memory_mib"], _MIN_MEMORY_MIB)) / _MIB_PER_GIB
    except (KeyError, TypeError, ValueError) as error:
        raise UsagePendingError("The Modal sandbox admission has no usable price snapshot.") from error
    return Decimal(_seconds(duration_ms)) * (cores * cpu_rate + memory_gib * memory_rate)


def quote_modal_sandbox(request: Mapping[str, Any]) -> OperationQuote:
    """Price a Modal sandbox for its whole lifetime before it exists.

    Args:
        request: Final create parameters with ``lifetime_ms`` (the timeout
            that Modal gets), ``cpu_cores``, ``memory_mib`` and ``gpu``.

    Returns:
        Coverage for the lifetime plus the boot and terminate margin, at the requested size.

    Raises:
        UnpricedOperationError: When the lifetime or size is unbounded, or the box asks for a GPU.
    """
    lifetime_ms = request.get("lifetime_ms")
    cores = request.get("cpu_cores")
    memory_mib = request.get("memory_mib")
    if (
        isinstance(lifetime_ms, bool)
        or not isinstance(lifetime_ms, int)
        or not 0 < lifetime_ms <= _LIFETIME_CEILING_MS
        or isinstance(cores, bool)
        or not isinstance(cores, int | float)
        or not cores > 0
        or isinstance(memory_mib, bool)
        or not isinstance(memory_mib, int)
        or memory_mib <= 0
    ):
        raise UnpricedOperationError("Modal requires a bounded lifetime, CPU and memory.")
    # The sandbox page names no sandbox GPU rate, so a protected box takes no GPU.
    if request.get("gpu"):
        raise UnpricedOperationError("Protected Modal sandboxes cannot use a GPU.")
    evidence = {
        "provider": "modal",
        "price_version": _PRICE_VERSION,
        "source": _PRICING_URL,
        "cpu_core_second_usd": str(_CPU_CORE_SECOND_USD),
        "memory_gib_second_usd": str(_MEMORY_GIB_SECOND_USD),
        "request": dict(request),
    }
    return operation_quote(
        request, _usd(evidence, lifetime_ms + _LIFETIME_MARGIN_MS), ChargePolicy("sandbox"), evidence
    )


class ModalUsageReservation:
    """Own the budget hold of one Modal sandbox from reservation to settlement."""

    def __init__(
        self,
        runtime: BudgetRuntime,
        request: Mapping[str, Any],
        *,
        operation_key: str,
        reserved: OperationSnapshot | None = None,
    ) -> None:
        """Reserve and claim one sandbox before any Modal call.

        Args:
            runtime: Authenticated budget authority retained outside the guest.
            request: Final create parameters that :func:`quote_modal_sandbox` prices.
            operation_key: Stable logical identity of the box.
            reserved: A pool lease slice already reserved for this box.

        Raises:
            BudgetConflictError: When the lease slice was reserved for a different request.
            OperationCompletedError: When the operation already settled.
            UsagePendingError: When another caller already dispatched the box.
        """
        self.runtime = runtime
        self.request = dict(request)
        self.quote = quote_modal_sandbox(request)
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
            # A refused claim means no sandbox exists for this operation.
            runtime.unpin(self.operation.id)
            raise
        if not claimed.dispatch_claimed:
            raise UsagePendingError("This sandbox was already dispatched; reconcile it by hand.")
        # The funded lifetime: the Modal timeout plus the boot and terminate margin.
        self.lifetime_ms: int = self.request["lifetime_ms"] + _LIFETIME_MARGIN_MS
        self.sandbox_id: str | None = None
        self.sent_at: float | None = None
        self.stopped_at: float | None = None
        self._sent_wall: float | None = None

    def sent(self) -> None:
        """Start the runtime clock just before the create call goes to Modal."""
        self.sent_at = time.monotonic()
        self._sent_wall = time.time()

    def confirm_created(self, sandbox: Any) -> None:
        """Bind the created sandbox to the operation and record its start.

        The start time goes into the pending evidence at once, so a worker
        that dies before the settle leaves enough to reconcile the hold.

        Args:
            sandbox: The Modal sandbox handle.

        Raises:
            UsagePendingError: When Modal returned no sandbox id.
        """
        sandbox_id = getattr(sandbox, "object_id", None)
        if not isinstance(sandbox_id, str) or not sandbox_id:
            raise UsagePendingError("The Modal sandbox returned no sandbox id.")
        self.sandbox_id = sandbox_id
        self.runtime.service.mark_dispatched(self.operation.id, self.runtime.username, sandbox_id)
        self.pending()

    def stopped(self) -> None:
        """Stop the runtime clock once Modal confirmed the sandbox ended."""
        self.stopped_at = time.monotonic()

    def extend(self, lifetime_ms: int, *, deadline: float) -> None:
        """Refuse an extension, because Modal cannot lengthen a running sandbox's timeout.

        Args:
            lifetime_ms: Total lifetime after the extension.
            deadline: ``time.monotonic()`` instant after which waiting stops.

        Raises:
            UnpricedOperationError: Always.
        """
        raise UnpricedOperationError("Modal cannot lengthen the timeout of a running sandbox.")

    def _evidence(self) -> dict[str, Any]:
        """Return the provider evidence of this box.

        Returns:
            The sandbox id, the wall-clock start and the measured and funded runtimes.
        """
        measured = None
        if self.sent_at is not None and self.stopped_at is not None:
            measured = max(0, math.ceil((self.stopped_at - self.sent_at) * 1000))
        return {
            "provider": "modal",
            "sandbox_id": self.sandbox_id,
            "sent_at": self._sent_wall,
            "measured_ms": measured,
            "funded_ms": self.lifetime_ms,
        }

    def pending(self) -> None:
        """Retain the hold when the box's existence or end is uncertain."""
        current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
        if current.state in {"dispatched", "pending"}:
            evidence = self._evidence()
            self.runtime.service.mark_pending(
                self.operation.id,
                self.runtime.username,
                evidence_key=json_fingerprint(evidence),
                evidence=evidence,
            )

    def fail(self, refusal: BaseException | None = None) -> None:
        """Release the hold when no sandbox can exist, otherwise retain it.

        No sandbox exists when the create was never sent, or when Modal
        refused the create itself. A create that was sent and failed in any
        other way may still have made one, so its hold stays pending.

        Args:
            refusal: The create error that proves no sandbox exists, or ``None``.
        """
        if self.sent_at is None or (refusal is not None and self.sandbox_id is None):
            if self.sent_at is None:
                key = f"modal-unsent:{self.operation.id}"
                evidence = {"provider": "modal", "reason": "The create call was never sent."}
            else:
                key = f"modal-refusal:{self.operation.id}"
                evidence = {
                    "provider": "modal",
                    "error": type(refusal).__name__,
                    "message": str(refusal)[:1000],
                    "sent_at": self._sent_wall,
                }
            try:
                self.runtime.service.reject(
                    self.operation.id, self.runtime.username, evidence_key=key, evidence=evidence
                )
                self.runtime.unpin(self.operation.id)
                return
            except BudgetError:
                pass
        self.pending()

    def settle(self) -> None:
        """Settle the box at its measured runtime, or at its whole lifetime when its end is unknown.

        Modal ends a sandbox at its timeout, so the funded lifetime (the
        timeout plus the boot and terminate margin) bounds the bill of a box
        whose terminate was not confirmed. The billed time
        never exceeds the funded lifetime, so the charge stays inside the hold.

        Raises:
            UsagePendingError: When the box has no sandbox id.
            BudgetError: When the charge breaks the reserved bounds.
        """
        try:
            current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
            if current.state == "settled":
                self.runtime.unpin(self.operation.id)
                return
            if self.sandbox_id is None:
                raise UsagePendingError("The Modal sandbox has no sandbox id to settle.")
            evidence = self._evidence()
            measured = evidence["measured_ms"]
            billed_ms = self.lifetime_ms if measured is None else min(measured, self.lifetime_ms)
            usd = _usd(self.quote.price_snapshot, billed_ms)
            charge = vercel_charge_policy(self.quote.price_snapshot).convert(usd)
            self.runtime.service.settle(
                self.operation.id,
                self.runtime.username,
                evidence_key=f"modal-stop:{self.sandbox_id}",
                actual_cents=charge.total,
                actual_wallet_cents=charge.wallet,
                evidence={**evidence, "billed_ms": billed_ms, "source": _PRICING_URL, "provider_usd": str(usd)},
            )
            self.runtime.unpin(self.operation.id)
        except BaseException:
            self.pending()
            raise


class ModalUsage:
    """Meter Modal sandboxes through the provider-neutral usage protocol."""

    name = "modal"

    def quote(self, request: Mapping[str, Any]) -> OperationQuote:
        """Price one sandbox before any provider resource exists.

        Args:
            request: Final create parameters.

        Returns:
            Coverage for the sandbox's whole funded lifetime.
        """
        return quote_modal_sandbox(request)

    def reserve(
        self,
        runtime: BudgetRuntime,
        request: Mapping[str, Any],
        *,
        operation_key: str,
        reserved: OperationSnapshot | None = None,
    ) -> ModalUsageReservation:
        """Reserve and claim one sandbox, or claim a lease slice already reserved for it.

        Args:
            runtime: Authenticated budget authority.
            request: Final create parameters.
            operation_key: Stable logical identity of the box.
            reserved: Optional pool lease slice for this box.

        Returns:
            The claimed reservation that the box confirms, extends and settles.
        """
        return ModalUsageReservation(runtime, request, operation_key=operation_key, reserved=reserved)

    def confirm(self, reservation: ModalUsageReservation, session: Any) -> None:
        """Bind the created sandbox to its reservation.

        Args:
            reservation: The box's claimed reservation.
            session: The Modal sandbox handle.
        """
        reservation.confirm_created(session)

    def extend(self, reservation: ModalUsageReservation, lifetime_ms: int, *, deadline: float) -> None:
        """Refuse an extension, because Modal cannot lengthen a running sandbox's timeout.

        Args:
            reservation: The box's claimed reservation.
            lifetime_ms: Total lifetime after the extension.
            deadline: ``time.monotonic()`` instant after which waiting stops.

        Raises:
            UnpricedOperationError: Always.
        """
        reservation.extend(lifetime_ms, deadline=deadline)

    def settle(self, reservation: ModalUsageReservation) -> None:
        """Settle the stopped sandbox at its measured cost.

        Args:
            reservation: The box's claimed reservation.
        """
        reservation.settle()


USAGE = ModalUsage()
