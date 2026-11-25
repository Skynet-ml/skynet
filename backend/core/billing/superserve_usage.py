"""Reserve Superserve sandbox compute and settle it from measured runtime.

Superserve has no API that reports the cost of one sandbox: ``get_metrics``
gives only CPU, memory and disk observations. A box therefore settles at its
measured wall time times the published rates for its vCPUs, memory and
disk. The provider builds a template with a fixed size and no growth range,
so the template size is the billed size, and it checks the created box
against that size.

The hold funds the box's whole lifetime plus a margin. The provider sends
the lifetime, at least 60 s, as Superserve's ``timeout_seconds`` with a
short ``auto_delete_seconds``, so Superserve pauses and then deletes a box
that nobody stops; the lifetime in the request is that vendor bound. SDK
0.14.1 calls ``timeout_seconds`` the auto-pause timeout
(``async_sandbox.py`` lines 876-877), separate from the activity-based
``idle_timeout_seconds`` (line 260). It does not say whether the timer
counts wall time, so if the Skynet process dies, a busy box may run past
its hold. The provider also kills the box itself at the lifetime. The clock runs from the create
call to the confirmed kill. A box whose measured time or size is larger than
its funding settles at the funded amount, logs a warning and flags
``over_funded`` in its evidence, so the settle never goes above the hold.

Superserve lists no egress rate and no creation fee, so the quote and the
settlement carry neither. The template is shared by every box of the same
image and size, so its build time and its stored image are not part of one
box's hold; the disk rate covers the box's own disk while it exists.

Superserve cannot price a lifetime extension for a running box here, so a
reservation refuses an extension.
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

# Read from https://superserve.ai/pricing on 2026-11-24. Superserve bills per
# second. Compute stops while a box is paused; storage is billed while it is
# kept. The page lists no creation fee and no egress rate.
_PRICING_URL = "https://superserve.ai/pricing"
_PRICE_VERSION = "superserve-public-2026-11-24"
_VCPU_HOUR_USD = Decimal("0.0504")
_MEMORY_GIB_HOUR_USD = Decimal("0.0162")
_STORAGE_GIB_HOUR_USD = Decimal("0.000108")
_SECONDS_PER_HOUR = Decimal(3600)
_MIB_PER_GIB = Decimal(1024)
# Superserve publishes no range for timeout_seconds; this is Skynet's own
# ceiling, equal to the longest idle timeout that Superserve accepts.
_LIFETIME_CEILING_MS = 604_800 * 1000
# Superserve can bill the boot before the timeout starts and the kill after
# it ends, so the hold funds this much more than the timeout it gets.
_LIFETIME_MARGIN_MS = 60_000

# A test replaces this clock with a fake one.
_monotonic = time.monotonic


def _seconds(duration_ms: int) -> int:
    """Round a duration up to Superserve's per-second billing.

    Args:
        duration_ms: The duration in milliseconds.

    Returns:
        Whole seconds, at least one.
    """
    return max(1, math.ceil(duration_ms / 1000))


def _usd(snapshot: Mapping[str, Any], duration_ms: int) -> Decimal:
    """Price one box of the admitted size for a duration.

    Args:
        snapshot: The admitted price snapshot with its rates and request.
        duration_ms: The billed duration.

    Returns:
        The compute, memory and disk cost for the duration.

    Raises:
        UsagePendingError: When the snapshot has no usable rates.
    """
    try:
        if snapshot.get("provider") != "superserve":
            raise ValueError("Not a Superserve price snapshot")
        vcpu_rate = exact_nonnegative(snapshot["vcpu_hour_usd"])
        memory_rate = exact_nonnegative(snapshot["memory_gib_hour_usd"])
        storage_rate = exact_nonnegative(snapshot["storage_gib_hour_usd"])
        request = snapshot["request"]
        vcpu = exact_nonnegative(request["vcpu"])
        memory_gib = exact_nonnegative(request["memory_mib"]) / _MIB_PER_GIB
        disk_gib = exact_nonnegative(request["disk_mib"]) / _MIB_PER_GIB
    except (KeyError, TypeError, ValueError) as error:
        raise UsagePendingError("The Superserve sandbox admission has no usable price snapshot.") from error
    hourly = vcpu * vcpu_rate + memory_gib * memory_rate + disk_gib * storage_rate
    return Decimal(_seconds(duration_ms)) * hourly / _SECONDS_PER_HOUR


def _positive_int(value: Any) -> bool:
    """Tell whether a request field is a positive int and not a bool.

    Args:
        value: The field value.

    Returns:
        True for a positive int.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def quote_superserve_sandbox(request: Mapping[str, Any]) -> OperationQuote:
    """Price a Superserve sandbox for its whole lifetime before it exists.

    Args:
        request: Final create parameters with ``lifetime_ms`` (the timeout
            that Superserve gets), ``vcpu``, ``memory_mib``, ``disk_mib`` and ``gpu``.

    Returns:
        Coverage for the lifetime plus the boot and kill margin, at the requested size.

    Raises:
        UnpricedOperationError: When the lifetime or size is unbounded, or the box asks for a GPU.
    """
    lifetime_ms = request.get("lifetime_ms")
    if not _positive_int(lifetime_ms) or lifetime_ms > _LIFETIME_CEILING_MS:
        raise UnpricedOperationError("Superserve requires a bounded lifetime of at most seven days.")
    if not all(_positive_int(request.get(key)) for key in ("vcpu", "memory_mib", "disk_mib")):
        raise UnpricedOperationError("Superserve requires a bounded vCPU, memory and disk size.")
    # The pricing page names no GPU rate, so a protected box takes no GPU.
    if request.get("gpu"):
        raise UnpricedOperationError("Protected Superserve sandboxes cannot use a GPU.")
    evidence = {
        "provider": "superserve",
        "price_version": _PRICE_VERSION,
        "source": _PRICING_URL,
        "vcpu_hour_usd": str(_VCPU_HOUR_USD),
        "memory_gib_hour_usd": str(_MEMORY_GIB_HOUR_USD),
        "storage_gib_hour_usd": str(_STORAGE_GIB_HOUR_USD),
        "request": dict(request),
    }
    return operation_quote(
        request, _usd(evidence, lifetime_ms + _LIFETIME_MARGIN_MS), ChargePolicy("sandbox"), evidence
    )


class SuperserveUsageReservation:
    """Own the budget hold of one Superserve sandbox from reservation to settlement."""

    def __init__(
        self,
        runtime: BudgetRuntime,
        request: Mapping[str, Any],
        *,
        operation_key: str,
        reserved: OperationSnapshot | None = None,
    ) -> None:
        """Reserve and claim one sandbox before any Superserve call.

        Args:
            runtime: Authenticated budget authority retained outside the guest.
            request: Final create parameters that :func:`quote_superserve_sandbox` prices.
            operation_key: Stable logical identity of the box.
            reserved: A pool lease slice already reserved for this box.

        Raises:
            BudgetConflictError: When the lease slice was reserved for a different request.
            OperationCompletedError: When the operation already settled.
            UsagePendingError: When another caller already dispatched the box.
        """
        self.runtime = runtime
        self.request = dict(request)
        self.quote = quote_superserve_sandbox(request)
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
        self.funded_usd = _usd(self.quote.price_snapshot, self.lifetime_ms)
        self.sandbox_id: str | None = None
        self.name: str | None = None
        self.attempt: str | None = None
        self.sent_at: float | None = None
        self.stopped_at: float | None = None
        self._sent_wall: float | None = None
        # None until the created box's size is read; False when it is larger than its funding.
        self.size_ok: bool | None = None

    def sent(self, name: str, attempt: str) -> None:
        """Mark the create call as sent and start the runtime clock.

        Args:
            name: The sandbox name of the create.
            attempt: The attempt label in the create's metadata, for the lookup after a failed create.
        """
        self.name = name
        self.attempt = attempt
        self.sent_at = _monotonic()
        self._sent_wall = time.time()

    def bind(self, sandbox_id: str) -> None:
        """Bind the created sandbox to the operation and record its start.

        The start time goes into the pending evidence at once, so a worker
        that dies before the settle leaves enough to reconcile the hold.

        Args:
            sandbox_id: The Superserve sandbox id.

        Raises:
            UsagePendingError: When Superserve returned no sandbox id.
        """
        if not isinstance(sandbox_id, str) or not sandbox_id:
            raise UsagePendingError("The Superserve sandbox returned no sandbox id.")
        self.sandbox_id = sandbox_id
        self.runtime.service.mark_dispatched(self.operation.id, self.runtime.username, sandbox_id)
        self.pending()

    def check_size(self, info: Any) -> None:
        """Check that the created box is no larger than its funding and cannot grow.

        The SDK 0.14.1 ``SandboxInfo`` has no disk field; the template fixes
        the disk. A disk that a later SDK reports must match the funded disk.

        Args:
            info: The ``SandboxInfo`` of the created box.

        Raises:
            UsagePendingError: When the box is larger than its funding or can grow.
        """
        self.size_ok = (
            getattr(info, "vcpu_count", None) == self.request["vcpu"]
            and getattr(info, "memory_mib", None) == self.request["memory_mib"]
            and getattr(info, "vcpu_max", None) is None
            and getattr(info, "memory_max_mib", None) is None
            and getattr(info, "disk_mib", None) in (None, self.request["disk_mib"])
        )
        if not self.size_ok:
            raise UsagePendingError("The Superserve sandbox does not have the funded size.")

    def stopped(self) -> None:
        """Stop the runtime clock once Superserve confirmed the kill."""
        self.stopped_at = _monotonic()

    def extend(self, lifetime_ms: int, *, deadline: float) -> None:
        """Refuse an extension of a running box.

        Args:
            lifetime_ms: Total lifetime after the extension.
            deadline: ``time.monotonic()`` instant after which waiting stops.

        Raises:
            UnpricedOperationError: Always.
        """
        raise UnpricedOperationError("Skynet does not lengthen the lifetime of a running Superserve sandbox.")

    def _evidence(self) -> dict[str, Any]:
        """Return the provider evidence of this box.

        Returns:
            The sandbox id, name and attempt, the wall-clock start and the measured and funded runtimes.
        """
        measured = None
        if self.sent_at is not None and self.stopped_at is not None:
            measured = max(0, math.ceil((self.stopped_at - self.sent_at) * 1000))
        return {
            "provider": "superserve",
            "sandbox_id": self.sandbox_id,
            "name": self.name,
            "attempt": self.attempt,
            "sent_at": self._sent_wall,
            "measured_ms": measured,
            "funded_ms": self.lifetime_ms,
            "size_ok": self.size_ok,
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

        No sandbox exists when the create was never sent. A sent create may
        still have made one, so its hold is released only when Superserve
        refused it, the lookup found no box, and no box id was ever bound.

        Args:
            absent: True when Superserve refused the create and the lookup found no box.
        """
        current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
        if current.state in {"settled", "released"}:
            self.runtime.unpin(self.operation.id)
            return
        if self.sent_at is None or (absent and self.sandbox_id is None):
            if self.sent_at is None:
                key = f"superserve-unsent:{self.operation.id}"
                reason = "The create call was never sent."
            else:
                key = f"superserve-absent:{self.operation.id}"
                reason = "Superserve refused the create and lists no box for it."
            try:
                self.runtime.service.reject(
                    self.operation.id,
                    self.runtime.username,
                    evidence_key=key,
                    evidence={"provider": "superserve", "reason": reason, "attempt": self.attempt},
                )
                self.runtime.unpin(self.operation.id)
                return
            except BudgetError:
                pass
        self.pending()

    def settle(self) -> None:
        """Settle the box at its measured cost, or at its funded cost when its end or size is unknown.

        Superserve pauses and deletes a box at its timeout, so the funded
        lifetime bounds the bill of a box whose kill was not confirmed. The
        bill never exceeds the funded cost, so the charge stays inside the hold.

        Raises:
            UsagePendingError: When the box has no sandbox id and no confirmed kill.
            BudgetError: When the charge breaks the reserved bounds.
        """
        try:
            current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
            if current.state == "settled":
                self.runtime.unpin(self.operation.id)
                return
            if self.sandbox_id is None and self.stopped_at is None:
                raise UsagePendingError("The Superserve sandbox has no sandbox id or confirmed kill to settle.")
            evidence = self._evidence()
            measured = evidence["measured_ms"]
            measured_usd = None if measured is None else _usd(self.quote.price_snapshot, measured)
            # A box of the wrong size has no priced size, so it bills its whole funding.
            over_funded = self.size_ok is False or (measured is not None and measured > self.lifetime_ms)
            usd = self.funded_usd if measured_usd is None or over_funded else min(measured_usd, self.funded_usd)
            if over_funded:
                logger.warning(
                    "Superserve box %s ran %s ms (size ok: %s), more than its funded %d ms; it settles at the hold.",
                    self.sandbox_id or self.operation.id,
                    measured,
                    self.size_ok,
                    self.lifetime_ms,
                )
            charge = vercel_charge_policy(self.quote.price_snapshot).convert(usd)
            self.runtime.service.settle(
                self.operation.id,
                self.runtime.username,
                evidence_key=f"superserve-stop:{self.sandbox_id or self.operation.id}",
                actual_cents=charge.total,
                actual_wallet_cents=charge.wallet,
                evidence={
                    **evidence,
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
                logger.warning("Superserve could not mark the hold pending: %s", type(pending_error).__name__)
            raise


class SuperserveUsage:
    """Meter Superserve sandboxes through the provider-neutral usage protocol."""

    name = "superserve"

    def quote(self, request: Mapping[str, Any]) -> OperationQuote:
        """Price one sandbox before any provider resource exists.

        Args:
            request: Final create parameters.

        Returns:
            Coverage for the sandbox's whole funded lifetime.
        """
        return quote_superserve_sandbox(request)

    def reserve(
        self,
        runtime: BudgetRuntime,
        request: Mapping[str, Any],
        *,
        operation_key: str,
        reserved: OperationSnapshot | None = None,
    ) -> SuperserveUsageReservation:
        """Reserve and claim one sandbox, or claim a lease slice already reserved for it.

        Args:
            runtime: Authenticated budget authority.
            request: Final create parameters.
            operation_key: Stable logical identity of the box.
            reserved: Optional pool lease slice for this box.

        Returns:
            The claimed reservation that the box confirms and settles.
        """
        return SuperserveUsageReservation(runtime, request, operation_key=operation_key, reserved=reserved)

    def confirm(self, reservation: SuperserveUsageReservation, session: Any) -> None:
        """Bind the created sandbox to its reservation and check its size.

        Args:
            reservation: The box's claimed reservation.
            session: The ``SandboxInfo`` of the created box.
        """
        reservation.bind(session.id)
        reservation.check_size(session)

    def extend(self, reservation: SuperserveUsageReservation, lifetime_ms: int, *, deadline: float) -> None:
        """Refuse an extension of a running box.

        Args:
            reservation: The box's claimed reservation.
            lifetime_ms: Total lifetime after the extension.
            deadline: ``time.monotonic()`` instant after which waiting stops.
        """
        reservation.extend(lifetime_ms, deadline=deadline)

    def settle(self, reservation: SuperserveUsageReservation) -> None:
        """Settle the stopped sandbox at its measured cost.

        Args:
            reservation: The box's claimed reservation.
        """
        reservation.settle()


USAGE = SuperserveUsage()
