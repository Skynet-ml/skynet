"""Reserve Blaxel sandbox compute and settle it from measured runtime.

Blaxel reports usage in its console and billing API only after a delay, and
it has no API that gives the cost of one sandbox at the moment it stops
(SKY-936). A box therefore settles at its measured runtime times the
published rate. Blaxel bills active time per GB of RAM, and the box's CPU
comes from its memory size, so the memory size is the whole billed size. A
box in standby costs nothing, so the measured wall time at the active rate
is never less than the bill.

The clock runs from the create call to the confirmed delete. The provider
sets the box's Blaxel ``ttl`` to the funded lifetime, so Blaxel deletes the
box at that time even when Skynet cannot. The hold funds the ``ttl`` plus a
margin for the boot and the background delete. The settled time never
exceeds the funded lifetime. A box whose measured time is longer settles at
the funded time, logs a warning and flags ``over_funded`` in its evidence.

The pricing page lists internet egress as included, so the quote and the
settlement carry no egress charge. Snapshot and image storage are billed per
GB-month and are far below the active rate, and the provider takes no
snapshot, so the quote carries no storage charge.

Blaxel can change the ``ttl`` of a running box, but the provider does not do
it, so a reservation refuses an extension.
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
# module only for history.
from .vercel_usage import vercel_charge_policy

logger = logging.getLogger(__name__)

# Read from https://blaxel.ai/pricing on 2026-11-24: "$0.0000115 / GB RAM ·
# second" of active time; standby time is free and internet egress is
# included. The page states no billing increment, so the time rounds up to
# whole seconds. Blaxel sizes memory in MB with 1024 MB to the GB (its 8 GB
# tier is 8192 MB).
_PRICING_URL = "https://blaxel.ai/pricing"
_PRICE_VERSION = "blaxel-sandbox-public-2026-11-24"
_GB_RAM_SECOND_USD = Decimal("0.0000115")
_MB_PER_GB = Decimal(1024)
# The Blaxel SDK (0.4.28) and API refuse a ttl below 5 minutes with HTTP 400.
_MIN_LIFETIME_MS = 5 * 60 * 1000
# SKY-936: a Blaxel ttl is at most 30 days.
_LIFETIME_CEILING_MS = 30 * 24 * 60 * 60 * 1000
# Blaxel can run the boot before its ttl clock starts and finishes a delete in
# the background, so the hold funds this much more than the ttl.
_LIFETIME_MARGIN_MS = 60_000


def _seconds(duration_ms: int) -> int:
    """Round a duration up to whole seconds.

    Args:
        duration_ms: The duration in milliseconds.

    Returns:
        Whole seconds, at least one.
    """
    return max(1, math.ceil(duration_ms / 1000))


def _usd(snapshot: Mapping[str, Any], duration_ms: int) -> Decimal:
    """Price one box of the admitted size for a duration.

    Args:
        snapshot: The admitted price snapshot with its rate and request.
        duration_ms: The billed duration.

    Returns:
        The compute cost for the duration.

    Raises:
        UsagePendingError: When the snapshot has no usable rate.
    """
    try:
        if snapshot.get("provider") != "blaxel":
            raise ValueError("Not a Blaxel price snapshot")
        rate = exact_nonnegative(snapshot["gb_ram_second_usd"])
        memory_gb = Decimal(snapshot["request"]["memory_mb"]) / _MB_PER_GB
    except (KeyError, TypeError, ValueError) as error:
        raise UsagePendingError("The Blaxel sandbox admission has no usable price snapshot.") from error
    return Decimal(_seconds(duration_ms)) * memory_gb * rate


def quote_blaxel_sandbox(request: Mapping[str, Any]) -> OperationQuote:
    """Price a Blaxel sandbox for its whole lifetime before it exists.

    Args:
        request: Final create parameters with ``lifetime_ms`` (the ``ttl``
            that Blaxel gets) and ``memory_mb``.

    Returns:
        Coverage for the ttl plus the boot and delete margin, at the requested memory size.

    Raises:
        UnpricedOperationError: When the lifetime or memory size is not a bounded whole number.
    """
    lifetime_ms = request.get("lifetime_ms")
    memory_mb = request.get("memory_mb")
    if (
        isinstance(lifetime_ms, bool)
        or not isinstance(lifetime_ms, int)
        or not _MIN_LIFETIME_MS <= lifetime_ms <= _LIFETIME_CEILING_MS
        or isinstance(memory_mb, bool)
        or not isinstance(memory_mb, int)
        or memory_mb <= 0
    ):
        raise UnpricedOperationError("Blaxel requires a ttl from 5 minutes to 30 days and a memory size.")
    evidence = {
        "provider": "blaxel",
        "price_version": _PRICE_VERSION,
        "source": _PRICING_URL,
        "gb_ram_second_usd": str(_GB_RAM_SECOND_USD),
        "request": dict(request),
    }
    return operation_quote(
        request, _usd(evidence, lifetime_ms + _LIFETIME_MARGIN_MS), ChargePolicy("sandbox"), evidence
    )


class BlaxelUsageReservation:
    """Own the budget hold of one Blaxel sandbox from reservation to settlement."""

    def __init__(
        self,
        runtime: BudgetRuntime,
        request: Mapping[str, Any],
        *,
        operation_key: str,
        reserved: OperationSnapshot | None = None,
    ) -> None:
        """Reserve and claim one sandbox before any Blaxel call.

        Args:
            runtime: Authenticated budget authority retained outside the guest.
            request: Final create parameters that :func:`quote_blaxel_sandbox` prices.
            operation_key: Stable logical identity of the box.
            reserved: A pool lease slice already reserved for this box.

        Raises:
            BudgetConflictError: When the lease slice was reserved for a different request.
            OperationCompletedError: When the operation already settled.
            UsagePendingError: When another caller already dispatched the box.
        """
        self.runtime = runtime
        self.request = dict(request)
        self.quote = quote_blaxel_sandbox(request)
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
        self.lifetime_ms: int = self.request["lifetime_ms"] + _LIFETIME_MARGIN_MS
        # The name the create sends; it is the evidence for a box whose create had no answer.
        self.sandbox_name: str | None = None
        # The name of a box that surely exists: a create returned it or a lookup found it.
        self.sandbox_id: str | None = None
        self.sent_at: float | None = None
        self.stopped_at: float | None = None
        self._sent_wall: float | None = None

    def sent(self, name: str) -> None:
        """Mark the create call as sent, and start the runtime clock.

        Args:
            name: The unique sandbox name that the create sends.
        """
        self.sandbox_name = name
        self.sent_at = time.monotonic()
        self._sent_wall = time.time()

    def confirm_created(self, sandbox: Any) -> None:
        """Bind the created sandbox to the operation and record its start.

        The start time goes into the pending evidence at once, so a worker
        that dies before the settle leaves enough to reconcile the hold.

        Args:
            sandbox: The Blaxel ``SandboxInstance``.

        Raises:
            UsagePendingError: When Blaxel returned a box with another name or no name.
        """
        name = getattr(getattr(sandbox, "metadata", None), "name", None)
        if not isinstance(name, str) or not name or name != self.sandbox_name:
            raise UsagePendingError("The Blaxel sandbox returned no name, or another name than the create sent.")
        self.sandbox_id = name
        self.runtime.service.mark_dispatched(self.operation.id, self.runtime.username, name)
        self.pending()

    def stopped(self) -> None:
        """Stop the runtime clock once Blaxel accepted the delete."""
        self.stopped_at = time.monotonic()

    def extend(self, lifetime_ms: int, *, deadline: float) -> None:
        """Refuse an extension, because the provider never lengthens a running box's ttl.

        Args:
            lifetime_ms: Total lifetime after the extension.
            deadline: ``time.monotonic()`` instant after which waiting stops.

        Raises:
            UnpricedOperationError: Always.
        """
        raise UnpricedOperationError("The Blaxel provider cannot lengthen the ttl of a running sandbox.")

    def _evidence(self) -> dict[str, Any]:
        """Return the provider evidence of this box.

        Returns:
            The sandbox name, the wall-clock start and the measured and funded runtimes.
        """
        measured = None
        if self.sent_at is not None and self.stopped_at is not None:
            measured = max(0, math.ceil((self.stopped_at - self.sent_at) * 1000))
        return {
            "provider": "blaxel",
            "sandbox_name": self.sandbox_name,
            "sandbox_confirmed": self.sandbox_id is not None,
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

    def fail(self, absent: bool = False) -> None:
        """Release the hold when no sandbox can exist, otherwise retain it.

        No sandbox exists when the create was never sent. A create that was
        sent and failed may still have made one, so its hold is released
        only when no box was ever confirmed, Blaxel refused the create, and
        a lookup by name found no box.

        Args:
            absent: True when Blaxel refused the create and a lookup by name found no box.
        """
        current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
        if current.state in {"settled", "released"}:
            self.runtime.unpin(self.operation.id)
            return
        if self.sent_at is None or (absent and self.sandbox_id is None):
            if self.sent_at is None:
                key = f"blaxel-unsent:{self.operation.id}"
                reason = "The create call was never sent."
            else:
                key = f"blaxel-absent:{self.operation.id}"
                reason = "Blaxel refused the create and no box has its name."
            try:
                self.runtime.service.reject(
                    self.operation.id,
                    self.runtime.username,
                    evidence_key=key,
                    evidence={
                        "provider": "blaxel",
                        "reason": reason,
                        "sandbox_name": self.sandbox_name,
                        "sent_at": self._sent_wall,
                    },
                )
                self.runtime.unpin(self.operation.id)
                return
            except BudgetError:
                pass
        self.pending()

    def settle(self) -> None:
        """Settle the box at its measured runtime, or at its whole lifetime when its end is unknown.

        Blaxel deletes a box at its ttl, so the funded lifetime (the ttl plus
        the boot and delete margin) bounds the bill of a box whose delete was
        not confirmed. The billed time never exceeds the funded lifetime, so
        the charge stays inside the hold.

        Raises:
            UsagePendingError: When the box was never confirmed and its delete was not confirmed.
            BudgetError: When the charge breaks the reserved bounds.
        """
        try:
            current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
            if current.state == "settled":
                self.runtime.unpin(self.operation.id)
                return
            if self.sandbox_id is None and self.stopped_at is None:
                raise UsagePendingError("The Blaxel sandbox has no confirmed box or confirmed delete to settle.")
            evidence = self._evidence()
            measured = evidence["measured_ms"]
            billed_ms = self.lifetime_ms if measured is None else min(measured, self.lifetime_ms)
            over_funded = measured is not None and measured > self.lifetime_ms
            if over_funded:
                logger.warning(
                    "Blaxel box %s ran %d ms, more than its funded %d ms; it settles at the funded time.",
                    self.sandbox_name or self.operation.id,
                    measured,
                    self.lifetime_ms,
                )
            usd = _usd(self.quote.price_snapshot, billed_ms)
            charge = vercel_charge_policy(self.quote.price_snapshot).convert(usd)
            self.runtime.service.settle(
                self.operation.id,
                self.runtime.username,
                evidence_key=f"blaxel-stop:{self.sandbox_name or self.operation.id}",
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
            self.pending()
            raise


class BlaxelUsage:
    """Meter Blaxel sandboxes through the provider-neutral usage protocol."""

    name = "blaxel"

    def quote(self, request: Mapping[str, Any]) -> OperationQuote:
        """Price one sandbox before any provider resource exists.

        Args:
            request: Final create parameters.

        Returns:
            Coverage for the sandbox's whole funded lifetime.
        """
        return quote_blaxel_sandbox(request)

    def reserve(
        self,
        runtime: BudgetRuntime,
        request: Mapping[str, Any],
        *,
        operation_key: str,
        reserved: OperationSnapshot | None = None,
    ) -> BlaxelUsageReservation:
        """Reserve and claim one sandbox, or claim a lease slice already reserved for it.

        Args:
            runtime: Authenticated budget authority.
            request: Final create parameters.
            operation_key: Stable logical identity of the box.
            reserved: Optional pool lease slice for this box.

        Returns:
            The claimed reservation that the box confirms and settles.
        """
        return BlaxelUsageReservation(runtime, request, operation_key=operation_key, reserved=reserved)

    def confirm(self, reservation: BlaxelUsageReservation, session: Any) -> None:
        """Bind the created sandbox to its reservation.

        Args:
            reservation: The box's claimed reservation.
            session: The Blaxel ``SandboxInstance``.
        """
        reservation.confirm_created(session)

    def extend(self, reservation: BlaxelUsageReservation, lifetime_ms: int, *, deadline: float) -> None:
        """Refuse an extension, because the provider never lengthens a running box's ttl.

        Args:
            reservation: The box's claimed reservation.
            lifetime_ms: Total lifetime after the extension.
            deadline: ``time.monotonic()`` instant after which waiting stops.

        Raises:
            UnpricedOperationError: Always.
        """
        reservation.extend(lifetime_ms, deadline=deadline)

    def settle(self, reservation: BlaxelUsageReservation) -> None:
        """Settle the stopped sandbox at its measured cost.

        Args:
            reservation: The box's claimed reservation.
        """
        reservation.settle()


USAGE = BlaxelUsage()
