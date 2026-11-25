"""Reserve Runta runtime compute and settle it from measured wall time.

Runta has no API that reports the cost or the usage of one runtime, so a
box settles at its measured wall time times the published rates. The
provider sends equal requests and limits for vCPU and memory and a fixed
disk size, so the priced size is the billed size. The clock runs from the
create call to the confirmed delete. A deleted runtime costs nothing, so the
clock is never shorter than the time that Runta bills.

The hold funds the requested lifetime plus a margin. The provider deletes
the box at the lifetime, and also sets Runta's own session limit
(``auto_shutdown_after_secs``) and retention (``auto_delete_after_secs``), so
a box that a lost worker never deletes still ends inside the funded time.
The settled time never exceeds the funded time. A box whose measured time is
longer settles at the funded time, logs a warning and flags ``over_funded``.

A box whose delete was not confirmed has no known end, so its hold stays
pending for reconciliation and never settles at a guess.

Runta's pricing page lists no egress charge, so the quote and the settle
carry none. A reservation refuses an extension.
"""

from __future__ import annotations

import logging
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
# module only for history, and moving it is out of scope for SKY-938.
from .vercel_usage import vercel_charge_policy

logger = logging.getLogger(__name__)

# Read from https://runta.com/pricing on 2026-11-24. A running runtime bills
# vCPU, memory and disk; a shut-down runtime bills disk only; a deleted one
# bills nothing. The page lists no egress charge, no billing increment and no
# maximum lifetime, so the clock rounds up to whole seconds.
_PRICING_URL = "https://runta.com/pricing"
_PRICE_VERSION = "runta-public-2026-11-24"
_VCPU_HOUR_USD = Decimal("0.0504")
_MEMORY_GIB_HOUR_USD = Decimal("0.0162")
_DISK_GIB_HOUR_USD = Decimal("0.000108")
_SECONDS_PER_HOUR = Decimal(3600)
_MIB_PER_GIB = Decimal(1024)
# Harbor 0.25.0 sends at least 16 GiB of disk; the provider does the same.
MIN_DISK_GIB = 16
# Runta publishes no maximum lifetime, so this is Skynet's own cap.
LIFETIME_CEILING_MS = 24 * 60 * 60 * 1000
# The provider waits at most this long for a new runtime to run.
BOOT_LIMIT_SEC = 120
# Runta deletes a shut-down runtime after this retention.
RETENTION_SEC = 60
# Runta's session limit counts from the running state, so the boot comes on
# top of the lifetime, then the retention after the shutdown. The rest is
# slack for the delete call.
_LIFETIME_MARGIN_MS = (BOOT_LIMIT_SEC + RETENTION_SEC + 120) * 1000


def _seconds(duration_ms: int) -> int:
    """Round a duration up to whole seconds.

    Args:
        duration_ms: The duration in milliseconds.

    Returns:
        Whole seconds, at least one.
    """
    return max(1, math.ceil(duration_ms / 1000))


def _usd(snapshot: Mapping[str, Any], duration_ms: int) -> Decimal:
    """Price one runtime of the admitted size for a duration.

    Args:
        snapshot: The admitted price snapshot with its rates and request.
        duration_ms: The billed duration.

    Returns:
        The compute cost for the duration.

    Raises:
        UsagePendingError: When the snapshot has no usable rates.
    """
    try:
        if snapshot.get("provider") != "runta":
            raise ValueError("Not a Runta price snapshot")
        vcpu_rate = exact_nonnegative(snapshot["vcpu_hour_usd"])
        memory_rate = exact_nonnegative(snapshot["memory_gib_hour_usd"])
        disk_rate = exact_nonnegative(snapshot["disk_gib_hour_usd"])
        request = snapshot["request"]
        vcpus = Decimal(int(request["vcpus"]))
        memory_gib = Decimal(int(request["memory_mib"])) / _MIB_PER_GIB
        disk_gib = Decimal(int(request["disk_gib"]))
    except (KeyError, TypeError, ValueError) as error:
        raise UsagePendingError("The Runta runtime admission has no usable price snapshot.") from error
    hourly = vcpus * vcpu_rate + memory_gib * memory_rate + disk_gib * disk_rate
    return Decimal(_seconds(duration_ms)) * hourly / _SECONDS_PER_HOUR


def _positive_int(value: Any) -> bool:
    """Tell whether a value is a positive integer and not a bool.

    Args:
        value: The value to check.

    Returns:
        True for a positive ``int``.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def quote_runta_runtime(request: Mapping[str, Any]) -> OperationQuote:
    """Price a Runta runtime for its whole funded lifetime before it exists.

    Args:
        request: Final create parameters with ``lifetime_ms``, ``vcpus``,
            ``memory_mib`` and ``disk_gib``.

    Returns:
        Coverage for the lifetime plus the boot, retention and delete margin.

    Raises:
        UnpricedOperationError: When the lifetime or the size is unbounded or invalid.
    """
    lifetime_ms = request.get("lifetime_ms")
    if (
        not _positive_int(lifetime_ms)
        or lifetime_ms > LIFETIME_CEILING_MS
        or not _positive_int(request.get("vcpus"))
        or not _positive_int(request.get("memory_mib"))
        or not _positive_int(request.get("disk_gib"))
        or request["disk_gib"] < MIN_DISK_GIB
    ):
        raise UnpricedOperationError("Runta requires a bounded lifetime, whole vCPUs, memory and disk.")
    evidence = {
        "provider": "runta",
        "price_version": _PRICE_VERSION,
        "source": _PRICING_URL,
        "vcpu_hour_usd": str(_VCPU_HOUR_USD),
        "memory_gib_hour_usd": str(_MEMORY_GIB_HOUR_USD),
        "disk_gib_hour_usd": str(_DISK_GIB_HOUR_USD),
        "request": dict(request),
    }
    return operation_quote(
        request, _usd(evidence, lifetime_ms + _LIFETIME_MARGIN_MS), ChargePolicy("sandbox"), evidence
    )


class RuntaUsageReservation:
    """Own the budget hold of one Runta runtime from reservation to settlement."""

    def __init__(
        self,
        runtime: BudgetRuntime,
        request: Mapping[str, Any],
        *,
        operation_key: str,
        reserved: OperationSnapshot | None = None,
    ) -> None:
        """Reserve and claim one runtime before any Runta call.

        Args:
            runtime: Authenticated budget authority retained outside the guest.
            request: Final create parameters that :func:`quote_runta_runtime` prices.
            operation_key: Stable logical identity of the box.
            reserved: A pool lease slice already reserved for this box.

        Raises:
            BudgetConflictError: When the lease slice was reserved for a different request.
            OperationCompletedError: When the operation already settled.
            UsagePendingError: When another caller already dispatched the box.
        """
        self.runtime = runtime
        self.request = dict(request)
        self.quote = quote_runta_runtime(request)
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
            raise BudgetConflictError("The lease slice was reserved for a different runtime request.")
        else:
            self.operation = reserved
        if self.operation.state == "settled":
            raise OperationCompletedError("This runtime operation already completed; retrieve its saved result.")
        runtime.pin(self.operation.id, cent_units(self.operation.max_cents), box=self.box)
        try:
            claimed = runtime.service.mark_dispatched(self.operation.id, runtime.username)
        except BaseException:
            # A refused claim means no runtime exists for this operation.
            runtime.unpin(self.operation.id)
            raise
        if not claimed.dispatch_claimed:
            raise UsagePendingError("This runtime was already dispatched; reconcile it by hand.")
        self.lifetime_ms: int = self.request["lifetime_ms"] + _LIFETIME_MARGIN_MS
        self.name: str | None = None
        self.runtime_id: str | None = None
        self.sent_at: float | None = None
        self.stopped_at: float | None = None
        self._sent_wall: float | None = None
        # True once a create returned a handle, so a runtime surely existed.
        self._returned = False
        # Duplicate runtimes with this box's name that did not delete.
        self.strays: list[str] = []

    @property
    def returned(self) -> bool:
        """Return True once a create returned a handle, so a runtime surely existed."""
        return self._returned

    def sent(self, name: str) -> None:
        """Mark the create call as sent and start the clock.

        Args:
            name: The unique display name of the runtime, the evidence for a lookup.
        """
        self.name = name
        self.sent_at = time.monotonic()
        self._sent_wall = time.time()

    def confirm_created(self, handle: Any) -> None:
        """Bind the created runtime to the operation and record its start.

        The start goes into the pending evidence at once, so a worker that
        dies before the settle leaves enough to reconcile the hold.

        Args:
            handle: The Runta runtime handle.

        Raises:
            UsagePendingError: When Runta returned no runtime id.
        """
        self._returned = True
        runtime_id = getattr(handle, "id", None)
        if not isinstance(runtime_id, str) or not runtime_id:
            raise UsagePendingError("The Runta runtime returned no runtime id.")
        self.runtime.service.mark_dispatched(self.operation.id, self.runtime.username, runtime_id)
        # Set only after the ledger accepted the id, so the provider sees a
        # failed bind and tries the confirm again before the delete.
        self.runtime_id = runtime_id
        self.pending()

    def keep_stray(self, runtime_id: str) -> None:
        """Record a duplicate runtime that did not delete, so the hold never settles.

        Args:
            runtime_id: The Runta id of the duplicate runtime.
        """
        self.strays.append(runtime_id)
        self.pending()

    def stopped(self) -> None:
        """Stop the clock once Runta confirmed the delete."""
        self.stopped_at = time.monotonic()

    def extend(self, lifetime_ms: int, *, deadline: float) -> None:
        """Refuse an extension, because the provider deletes the box at its funded lifetime.

        Args:
            lifetime_ms: Total lifetime after the extension.
            deadline: ``time.monotonic()`` instant after which waiting stops.

        Raises:
            UnpricedOperationError: Always.
        """
        raise UnpricedOperationError("A Runta runtime cannot extend its funded lifetime.")

    def _evidence(self) -> dict[str, Any]:
        """Return the provider evidence of this box.

        Returns:
            The runtime id and name, the wall-clock start and the measured and funded times.
        """
        measured = None
        if self.sent_at is not None and self.stopped_at is not None:
            measured = max(0, math.ceil((self.stopped_at - self.sent_at) * 1000))
        evidence: dict[str, Any] = {
            "provider": "runta",
            "runtime_id": self.runtime_id,
            "name": self.name,
            "sent_at": self._sent_wall,
            "measured_ms": measured,
            "funded_ms": self.lifetime_ms,
        }
        if self.strays:
            evidence["stray_runtime_ids"] = list(self.strays)
        return evidence

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

    def fail(self, absent: bool = False) -> None:
        """Release the hold when no runtime can exist, otherwise retain it.

        No runtime exists when the create was never sent. A create that was
        sent and failed may still have made one, so its hold is released only
        when no create returned a runtime, Runta refused the create, and the
        name lookup found no runtime.

        Args:
            absent: True when Runta refused the create and the lookup found no runtime.
        """
        current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
        if current.state in {"settled", "released"}:
            self.runtime.unpin(self.operation.id)
            return
        if self.sent_at is None or (absent and self.runtime_id is None and not self._returned):
            if self.sent_at is None:
                key = f"runta-unsent:{self.operation.id}"
                reason = "The create call was never sent."
            else:
                key = f"runta-absent:{self.operation.id}"
                reason = "Runta refused the create and no runtime has its name."
            try:
                self.runtime.service.reject(
                    self.operation.id,
                    self.runtime.username,
                    evidence_key=key,
                    evidence={"provider": "runta", "reason": reason, "name": self.name, "sent_at": self._sent_wall},
                )
                self.runtime.unpin(self.operation.id)
                return
            except BudgetError:
                pass
        self.pending()

    def settle(self) -> None:
        """Settle the deleted box at its measured wall time, clamped to the funded lifetime.

        Raises:
            UsagePendingError: When the delete was not confirmed, or a duplicate
                runtime did not delete, so the end is unknown.
            BudgetError: When the charge breaks the reserved bounds.
        """
        try:
            current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
            if current.state == "settled":
                self.runtime.unpin(self.operation.id)
                return
            if self.sent_at is None or self.stopped_at is None:
                raise UsagePendingError("The Runta runtime has no confirmed delete to settle.")
            if self.strays:
                # A duplicate can still run, and its time is not in this charge.
                raise UsagePendingError("A duplicate Runta runtime did not delete, so the hold stays pending.")
            evidence = self._evidence()
            measured = evidence["measured_ms"]
            billed_ms = min(measured, self.lifetime_ms)
            over_funded = measured > self.lifetime_ms
            if over_funded:
                logger.warning(
                    "Runta runtime %s ran %d ms, more than its funded %d ms; it settles at the funded time.",
                    self.runtime_id or self.name or self.operation.id,
                    measured,
                    self.lifetime_ms,
                )
            usd = _usd(self.quote.price_snapshot, billed_ms)
            charge = vercel_charge_policy(self.quote.price_snapshot).convert(usd)
            self.runtime.service.settle(
                self.operation.id,
                self.runtime.username,
                evidence_key=f"runta-stop:{self.runtime_id or self.operation.id}",
                actual_cents=charge.total,
                actual_wallet_cents=charge.wallet,
                evidence={
                    **evidence,
                    "billed_ms": billed_ms,
                    "over_funded": over_funded,
                    "source": _PRICING_URL,
                    "provider_usd": str(usd),
                },
            )
            self.runtime.unpin(self.operation.id)
        except BaseException:
            # A failed pending call must not replace the first error.
            try:
                self.pending()
            except Exception as pending_error:
                logger.warning("Runta could not mark the hold pending: %s", type(pending_error).__name__)
            raise


class RuntaUsage:
    """Meter Runta runtimes through the provider-neutral usage protocol."""

    name = "runta"

    def quote(self, request: Mapping[str, Any]) -> OperationQuote:
        """Price one runtime before any provider resource exists.

        Args:
            request: Final create parameters.

        Returns:
            Coverage for the runtime's whole funded lifetime.
        """
        return quote_runta_runtime(request)

    def reserve(
        self,
        runtime: BudgetRuntime,
        request: Mapping[str, Any],
        *,
        operation_key: str,
        reserved: OperationSnapshot | None = None,
    ) -> RuntaUsageReservation:
        """Reserve and claim one runtime, or claim a lease slice already reserved for it.

        Args:
            runtime: Authenticated budget authority.
            request: Final create parameters.
            operation_key: Stable logical identity of the box.
            reserved: Optional pool lease slice for this box.

        Returns:
            The claimed reservation that the box confirms and settles.
        """
        return RuntaUsageReservation(runtime, request, operation_key=operation_key, reserved=reserved)

    def confirm(self, reservation: RuntaUsageReservation, session: Any) -> None:
        """Bind the created runtime to its reservation.

        Args:
            reservation: The box's claimed reservation.
            session: The Runta runtime handle.
        """
        reservation.confirm_created(session)

    def extend(self, reservation: RuntaUsageReservation, lifetime_ms: int, *, deadline: float) -> None:
        """Refuse an extension, because the provider deletes the box at its funded lifetime.

        Args:
            reservation: The box's claimed reservation.
            lifetime_ms: Total lifetime after the extension.
            deadline: ``time.monotonic()`` instant after which waiting stops.

        Raises:
            UnpricedOperationError: Always.
        """
        reservation.extend(lifetime_ms, deadline=deadline)

    def settle(self, reservation: RuntaUsageReservation) -> None:
        """Settle the deleted runtime at its measured cost.

        Args:
            reservation: The box's claimed reservation.
        """
        reservation.settle()


USAGE = RuntaUsage()
