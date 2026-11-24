"""Reserve Modal sandbox compute and settle it from measured runtime.

Modal has no API that reports the cost of one sandbox. Its billing report
(``modal.billing.workspace_billing_report``) gives cost per App and per
hour or day, and every Skynet box shares one App, so it cannot attribute a
cost to a box. A box therefore settles at its measured runtime times the
published rates. The provider sends equal CPU and memory requests and
limits, and Modal bills ``max(request, usage)``, so the request is the
exact billed size. The runtime runs from the create call to the confirmed
terminate, which is never shorter than the time Modal bills.
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
from .vercel_usage import SANDBOX_NETWORK_BYTES_CAP, vercel_charge_policy

# Read from https://modal.com/pricing on 2026-11-24 ("Modal Sandbox pricing",
# non-preemptible). These sandbox rates already include Modal's 3x
# non-preemptible multiplier. A core is a physical core (2 vCPU). The page
# lists no creation fee. Region selection costs 1.15-1.75x, but the provider
# never selects a region.
_PRICING_URL = "https://modal.com/pricing"
_PRICE_VERSION = "modal-sandbox-public-2026-11-24"
_CPU_CORE_SECOND_USD = Decimal("0.00003942")
_MEMORY_GIB_SECOND_USD = Decimal("0.00000667")
_NETWORK_EGRESS_GIB_USD = Decimal("0.04")
_MIN_CPU_CORES = Decimal("0.125")
_MIN_MEMORY_MIB = 128
_MIB_PER_GIB = Decimal(1024)
_BYTES_PER_GIB = Decimal(1024**3)
# Modal ends every sandbox after 24 hours.
_LIFETIME_CEILING_MS = 24 * 60 * 60 * 1000


def _seconds(duration_ms: int) -> int:
    """Round a duration up to Modal's per-second billing.

    Args:
        duration_ms: The duration in milliseconds.

    Returns:
        Whole seconds, at least one.
    """
    return max(1, math.ceil(duration_ms / 1000))


def _usd(snapshot: Mapping[str, Any], duration_ms: int, *, network: bool) -> Decimal:
    """Price one box of the admitted shape for a duration.

    Args:
        snapshot: The admitted price snapshot with its rates and request.
        duration_ms: The billed duration.
        network: True when the box ever had open egress.

    Returns:
        Compute for the duration, plus the whole egress cap when ``network`` is True.

    Raises:
        UsagePendingError: When the snapshot has no usable rates.
    """
    try:
        if snapshot.get("provider") != "modal":
            raise ValueError("Not a Modal price snapshot")
        cpu_rate = exact_nonnegative(snapshot["cpu_core_second_usd"])
        memory_rate = exact_nonnegative(snapshot["memory_gib_second_usd"])
        egress_rate = exact_nonnegative(snapshot["network_egress_gib_usd"])
        cores = max(exact_nonnegative(snapshot["request"]["cpu_cores"]), _MIN_CPU_CORES)
        memory_gib = Decimal(max(snapshot["request"]["memory_mib"], _MIN_MEMORY_MIB)) / _MIB_PER_GIB
        cap = snapshot["request"]["network_bytes_cap"]
    except (KeyError, TypeError, ValueError) as error:
        raise UsagePendingError("The Modal sandbox admission has no usable price snapshot.") from error
    compute = Decimal(_seconds(duration_ms)) * (cores * cpu_rate + memory_gib * memory_rate)
    return compute + (Decimal(cap) / _BYTES_PER_GIB * egress_rate if network else Decimal(0))


def quote_modal_sandbox(request: Mapping[str, Any]) -> OperationQuote:
    """Price a Modal sandbox for its whole lifetime before it exists.

    Args:
        request: Final create parameters with ``lifetime_ms``, ``cpu_cores``,
            ``memory_mib``, ``gpu`` and ``network_bytes_cap``.

    Returns:
        Coverage for the lifetime at the requested size plus the egress cap.

    Raises:
        UnpricedOperationError: When the lifetime or size is unbounded, or the box asks for a GPU.
    """
    lifetime_ms = request.get("lifetime_ms")
    cores = request.get("cpu_cores")
    memory_mib = request.get("memory_mib")
    cap = request.get("network_bytes_cap")
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
        or isinstance(cap, bool)
        or not isinstance(cap, int)
        or not 0 <= cap <= SANDBOX_NETWORK_BYTES_CAP
    ):
        raise UnpricedOperationError("Modal requires a bounded lifetime, CPU, memory and egress cap.")
    # The sandbox page names no sandbox GPU rate, so a protected box takes no GPU.
    if request.get("gpu"):
        raise UnpricedOperationError("Protected Modal sandboxes cannot use a GPU.")
    evidence = {
        "provider": "modal",
        "price_version": _PRICE_VERSION,
        "source": _PRICING_URL,
        "cpu_core_second_usd": str(_CPU_CORE_SECOND_USD),
        "memory_gib_second_usd": str(_MEMORY_GIB_SECOND_USD),
        "network_egress_gib_usd": str(_NETWORK_EGRESS_GIB_USD),
        "request": dict(request),
    }
    return operation_quote(request, _usd(evidence, lifetime_ms, network=True), ChargePolicy("sandbox"), evidence)


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
        self.lifetime_ms: int = self.request["lifetime_ms"]
        self.sandbox_id: str | None = None
        # The quote funds the egress cap, but a box that never opened egress pays none.
        self.network_used = False
        self.sent_at: float | None = None
        self.stopped_at: float | None = None
        self._sent_wall: float | None = None

    def sent(self) -> None:
        """Start the runtime clock just before the create call goes to Modal."""
        self.sent_at = time.monotonic()
        self._sent_wall = time.time()

    def confirm_created(self, sandbox: Any) -> None:
        """Bind the created sandbox to the operation.

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

    def opened_network(self) -> None:
        """Record that the box has open egress, so it pays its egress cap."""
        self.network_used = True

    def stopped(self) -> None:
        """Stop the runtime clock once Modal confirmed the sandbox ended."""
        self.stopped_at = time.monotonic()

    def extend(self, lifetime_ms: int, *, deadline: float) -> None:
        """Fund a longer lifetime before Modal is asked for it.

        Args:
            lifetime_ms: Total lifetime after the extension.
            deadline: ``time.monotonic()`` instant after which waiting stops.

        Raises:
            ValueError: When the lifetime does not grow.
        """
        if lifetime_ms <= self.lifetime_ms:
            raise ValueError("An extension must lengthen the funded lifetime.")
        quote = quote_modal_sandbox({**self.request, "lifetime_ms": lifetime_ms})
        extended = self.runtime.extend(
            self.operation.id,
            quote,
            evidence_key=f"modal-extend:{lifetime_ms}",
            evidence={"provider": "modal", "lifetime_ms": lifetime_ms, "request": quote.request_fingerprint},
            deadline=deadline,
        )
        self.runtime.pin(self.operation.id, cent_units(extended.max_cents), box=self.box)
        self.request["lifetime_ms"] = lifetime_ms
        self.lifetime_ms = lifetime_ms
        self.quote = quote

    def _evidence(self) -> dict[str, Any]:
        """Return the provider evidence of this box.

        Returns:
            The sandbox id, the measured runtime and the egress state.
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
            "network_used": self.network_used,
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

    def fail(self) -> None:
        """Release the hold when no create reached Modal, otherwise retain it.

        A create that was sent but returned no sandbox may still have made
        one, so its hold stays pending for reconciliation.
        """
        if self.sent_at is None:
            try:
                self.runtime.service.reject(
                    self.operation.id,
                    self.runtime.username,
                    evidence_key=f"modal-unsent:{self.operation.id}",
                    evidence={"provider": "modal", "reason": "The create call was never sent."},
                )
                self.runtime.unpin(self.operation.id)
                return
            except BudgetError:
                pass
        self.pending()

    def settle(self) -> None:
        """Settle the box at its measured runtime, or at its whole lifetime when its end is unknown.

        Modal ends a sandbox at its timeout, so the funded lifetime bounds
        the bill of a box whose terminate was not confirmed. The billed time
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
            usd = _usd(self.quote.price_snapshot, billed_ms, network=self.network_used)
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
        """Fund a longer lifetime before the provider grants it.

        Args:
            reservation: The box's claimed reservation.
            lifetime_ms: Total lifetime after the extension.
            deadline: ``time.monotonic()`` instant after which waiting stops.
        """
        reservation.extend(lifetime_ms, deadline=deadline)

    def settle(self, reservation: ModalUsageReservation) -> None:
        """Settle the stopped sandbox at its measured cost.

        Args:
            reservation: The box's claimed reservation.
        """
        reservation.settle()


USAGE = ModalUsage()
