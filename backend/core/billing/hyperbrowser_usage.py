"""Reserve Hyperbrowser sandbox compute and settle it from measured runtime.

Hyperbrowser bills a sandbox for its allocated vCPU and memory while it
runs. The pricing pages publish no disk, egress or creation charge for a
CPU sandbox. The SDK reports a ``credits_used`` field on a sandbox, but no
document says when that value is final, so it is only kept as evidence.
A box therefore settles at its measured runtime times the published rates.

The runtime runs from the create call to the confirmed stop. Hyperbrowser
does not bill provisioning, so the clock is never shorter than the billed
time. The create sends ``timeout_minutes``, so Hyperbrowser ends a box that
nobody stops. The hold funds that timeout plus a margin for the stop call,
and the settled time never exceeds the funded time. A box whose measured
time is longer settles at the funded time, logs a warning and flags
``over_funded`` in its evidence. A box whose stop is not confirmed settles at
the whole funded time.

Hyperbrowser has no call that makes the timeout of a running sandbox
longer, so a reservation refuses an extension.
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
# module only for history (SKY-546).
from .vercel_usage import vercel_charge_policy

logger = logging.getLogger(__name__)

# Read from https://www.hyperbrowser.ai/pricing and
# https://www.hyperbrowser.ai/docs/pricing on 2026-11-24: 45 credits per
# vCPU-hour and 15 credits per GiB-hour of allocated memory, with
# 1,000 credits = $1. Memory is billed per GiB of 1024 MiB. The pages list no
# disk or egress charge for a CPU sandbox, and do not state the billing
# granularity, so the settle rounds up to whole seconds.
_PRICING_URL = "https://www.hyperbrowser.ai/docs/pricing"
_PRICE_VERSION = "hyperbrowser-sandbox-public-2026-11-24"
_VCPU_HOUR_USD = Decimal("0.045")
_MEMORY_GIB_HOUR_USD = Decimal("0.015")
_SECONDS_PER_HOUR = Decimal(3600)
_MIB_PER_GIB = Decimal(1024)
# Hyperbrowser publishes no maximum timeout; this is Skynet's own ceiling.
_MAX_TIMEOUT_MINUTES = 24 * 60
# The stop call can end after the timeout ends, so the hold funds this much
# more. It equals the provider's 120 s SDK request timeout, so it covers a
# slow stop in the normal case. httpx applies that timeout to each phase of
# the request, not to the whole request, so a stop can still run longer; the
# settle then clamps the bill to the funded time and flags ``over_funded``.
_LIFETIME_MARGIN_MS = 120_000


def _seconds(duration_ms: int) -> int:
    """Round a duration up to whole seconds.

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
        if snapshot.get("provider") != "hyperbrowser":
            raise ValueError("Not a Hyperbrowser price snapshot")
        vcpu_rate = exact_nonnegative(snapshot["vcpu_hour_usd"])
        memory_rate = exact_nonnegative(snapshot["memory_gib_hour_usd"])
        vcpus = Decimal(snapshot["request"]["vcpus"])
        memory_gib = Decimal(snapshot["request"]["memory_mib"]) / _MIB_PER_GIB
    except (KeyError, TypeError, ValueError) as error:
        raise UsagePendingError("The Hyperbrowser sandbox admission has no usable price snapshot.") from error
    hours = Decimal(_seconds(duration_ms)) / _SECONDS_PER_HOUR
    return hours * (vcpus * vcpu_rate + memory_gib * memory_rate)


def _positive_int(value: Any) -> bool:
    """Return True for a positive int that is not a bool.

    Args:
        value: The value to check.

    Returns:
        True when the value is a positive int.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def quote_hyperbrowser_sandbox(request: Mapping[str, Any]) -> OperationQuote:
    """Price a Hyperbrowser sandbox for its whole lifetime before it exists.

    Args:
        request: Final create parameters with ``timeout_minutes`` (the
            timeout that Hyperbrowser gets), ``vcpus`` and ``memory_mib``.

    Returns:
        Coverage for the timeout plus the stop margin, at the requested size.

    Raises:
        UnpricedOperationError: When the lifetime or size is unbounded.
    """
    timeout_minutes = request.get("timeout_minutes")
    if (
        not _positive_int(timeout_minutes)
        or timeout_minutes > _MAX_TIMEOUT_MINUTES
        or not _positive_int(request.get("vcpus"))
        or not _positive_int(request.get("memory_mib"))
    ):
        raise UnpricedOperationError("Hyperbrowser requires a bounded timeout, vCPU count and memory.")
    evidence = {
        "provider": "hyperbrowser",
        "price_version": _PRICE_VERSION,
        "source": _PRICING_URL,
        "vcpu_hour_usd": str(_VCPU_HOUR_USD),
        "memory_gib_hour_usd": str(_MEMORY_GIB_HOUR_USD),
        "request": dict(request),
    }
    return operation_quote(request, _usd(evidence, _funded_ms(timeout_minutes)), ChargePolicy("sandbox"), evidence)


def _funded_ms(timeout_minutes: int) -> int:
    """Return the funded lifetime of a box with this timeout.

    Args:
        timeout_minutes: The timeout that Hyperbrowser gets.

    Returns:
        The timeout plus the stop margin, in milliseconds.
    """
    return timeout_minutes * 60_000 + _LIFETIME_MARGIN_MS


class HyperbrowserUsageReservation:
    """Own the budget hold of one Hyperbrowser sandbox from reservation to settlement."""

    def __init__(
        self,
        runtime: BudgetRuntime,
        request: Mapping[str, Any],
        *,
        operation_key: str,
        reserved: OperationSnapshot | None = None,
    ) -> None:
        """Reserve and claim one sandbox before any Hyperbrowser call.

        Args:
            runtime: Authenticated budget authority retained outside the guest.
            request: Final create parameters that :func:`quote_hyperbrowser_sandbox` prices.
            operation_key: Stable logical identity of the box.
            reserved: A pool lease slice already reserved for this box.

        Raises:
            BudgetConflictError: When the lease slice was reserved for a different request.
            OperationCompletedError: When the operation already settled.
            UsagePendingError: When another caller already dispatched the box.
        """
        self.runtime = runtime
        self.request = dict(request)
        self.quote = quote_hyperbrowser_sandbox(request)
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
        self.lifetime_ms: int = _funded_ms(self.request["timeout_minutes"])
        self.sandbox_id: str | None = None
        self.sent_at: float | None = None
        self.stopped_at: float | None = None
        self.vendor: dict[str, Any] | None = None
        self._sent_wall: float | None = None
        # True once a create returned a handle, so a sandbox surely existed.
        self._returned = False

    @property
    def returned(self) -> bool:
        """Return True once a create returned a handle, so a sandbox surely existed."""
        return self._returned

    def sent(self) -> None:
        """Mark the create call as sent and start the runtime clock."""
        self.sent_at = time.monotonic()
        self._sent_wall = time.time()

    def confirm_created(self, sandbox: Any) -> None:
        """Bind the created sandbox to the operation and record its start.

        The start time goes into the pending evidence at once, so a worker
        that dies before the settle leaves enough to reconcile the hold.

        Args:
            sandbox: The Hyperbrowser sandbox handle.

        Raises:
            UsagePendingError: When Hyperbrowser returned no sandbox id.
        """
        self._returned = True
        sandbox_id = getattr(sandbox, "id", None)
        if not isinstance(sandbox_id, str) or not sandbox_id:
            raise UsagePendingError("The Hyperbrowser sandbox returned no sandbox id.")
        self.sandbox_id = sandbox_id
        self.runtime.service.mark_dispatched(self.operation.id, self.runtime.username, sandbox_id)
        self.pending()

    def stopped(self) -> None:
        """Stop the runtime clock once Hyperbrowser confirmed the stop."""
        self.stopped_at = time.monotonic()

    def report(self, vendor: Mapping[str, Any] | None) -> None:
        """Keep the usage fields that Hyperbrowser reported for the stopped box.

        Args:
            vendor: The reported fields, kept as evidence only, or ``None``.
        """
        self.vendor = dict(vendor) if vendor is not None else None

    def extend(self, lifetime_ms: int, *, deadline: float) -> None:
        """Refuse an extension, because Hyperbrowser cannot lengthen a running sandbox's timeout.

        Args:
            lifetime_ms: Total lifetime after the extension.
            deadline: ``time.monotonic()`` instant after which waiting stops.

        Raises:
            UnpricedOperationError: Always.
        """
        raise UnpricedOperationError("Hyperbrowser cannot lengthen the timeout of a running sandbox.")

    def _evidence(self) -> dict[str, Any]:
        """Return the provider evidence of this box.

        Returns:
            The sandbox id, the wall-clock start, the measured and funded
            runtimes, and the usage that Hyperbrowser reported.
        """
        measured = None
        if self.sent_at is not None and self.stopped_at is not None:
            # Monotonic readings are large on a long-running host, so their difference carries
            # float noise (10.2 s reads as 10.2000000001). Rounding to whole microseconds first
            # keeps that noise from billing a millisecond that did not run (SKY-988).
            measured = max(0, math.ceil(round((self.stopped_at - self.sent_at) * 1000, 3)))
        return {
            "provider": "hyperbrowser",
            "sandbox_id": self.sandbox_id,
            "sent_at": self._sent_wall,
            "measured_ms": measured,
            "funded_ms": self.lifetime_ms,
            "vendor": self.vendor,
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

        No sandbox exists when the create was never sent, or when
        Hyperbrowser refused the sent create and no create returned a handle.
        Any other failed create may still have made a box, so its hold stays
        pending.

        Args:
            absent: True when Hyperbrowser refused the create with a definite error status.
        """
        current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
        if current.state in {"settled", "released"}:
            self.runtime.unpin(self.operation.id)
            return
        if self.sent_at is None or (absent and self.sandbox_id is None and not self._returned):
            if self.sent_at is None:
                key = f"hyperbrowser-unsent:{self.operation.id}"
                reason = "The create call was never sent."
            else:
                key = f"hyperbrowser-refused:{self.operation.id}"
                reason = "Hyperbrowser refused the create, so no sandbox exists."
            try:
                self.runtime.service.reject(
                    self.operation.id,
                    self.runtime.username,
                    evidence_key=key,
                    evidence={"provider": "hyperbrowser", "reason": reason, "sent_at": self._sent_wall},
                )
                self.runtime.unpin(self.operation.id)
                return
            except BudgetError:
                pass
        self.pending()

    def settle(self) -> None:
        """Settle the box at its measured runtime, or at its whole lifetime when its end is unknown.

        Hyperbrowser ends a sandbox at its timeout, so the funded lifetime
        (the timeout plus the stop margin) bounds the bill of a box whose
        stop was not confirmed. The billed time never exceeds the funded
        lifetime, and the charge is clamped to the hold.

        Raises:
            UsagePendingError: When the box has no sandbox id and no confirmed stop.
            BudgetError: When the charge breaks the reserved bounds.
        """
        try:
            current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
            if current.state == "settled":
                self.runtime.unpin(self.operation.id)
                return
            if self.sandbox_id is None and self.stopped_at is None:
                raise UsagePendingError("The Hyperbrowser sandbox has no sandbox id or confirmed stop to settle.")
            evidence = self._evidence()
            measured = evidence["measured_ms"]
            billed_ms = self.lifetime_ms if measured is None else min(measured, self.lifetime_ms)
            over_funded = measured is not None and measured > self.lifetime_ms
            if over_funded:
                logger.warning(
                    "Hyperbrowser box %s ran %d ms, more than its funded %d ms; it settles at the funded time.",
                    self.sandbox_id or self.operation.id,
                    measured,
                    self.lifetime_ms,
                )
            usd = _usd(self.quote.price_snapshot, billed_ms)
            charge = vercel_charge_policy(self.quote.price_snapshot).convert(usd)
            total = min(charge.total, current.max_cents)
            wallet = min(charge.wallet, current.max_wallet_cents, total)
            self.runtime.service.settle(
                self.operation.id,
                self.runtime.username,
                evidence_key=f"hyperbrowser-stop:{self.sandbox_id or self.operation.id}",
                actual_cents=total,
                actual_wallet_cents=wallet,
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
                logger.warning("Hyperbrowser could not mark the hold pending: %s", type(pending_error).__name__)
            raise


class HyperbrowserUsage:
    """Meter Hyperbrowser sandboxes through the provider-neutral usage protocol."""

    name = "hyperbrowser"

    def quote(self, request: Mapping[str, Any]) -> OperationQuote:
        """Price one sandbox before any provider resource exists.

        Args:
            request: Final create parameters.

        Returns:
            Coverage for the sandbox's whole funded lifetime.
        """
        return quote_hyperbrowser_sandbox(request)

    def reserve(
        self,
        runtime: BudgetRuntime,
        request: Mapping[str, Any],
        *,
        operation_key: str,
        reserved: OperationSnapshot | None = None,
    ) -> HyperbrowserUsageReservation:
        """Reserve and claim one sandbox, or claim a lease slice already reserved for it.

        Args:
            runtime: Authenticated budget authority.
            request: Final create parameters.
            operation_key: Stable logical identity of the box.
            reserved: Optional pool lease slice for this box.

        Returns:
            The claimed reservation that the box confirms and settles.
        """
        return HyperbrowserUsageReservation(runtime, request, operation_key=operation_key, reserved=reserved)

    def confirm(self, reservation: HyperbrowserUsageReservation, session: Any) -> None:
        """Bind the created sandbox to its reservation.

        Args:
            reservation: The box's claimed reservation.
            session: The Hyperbrowser sandbox handle.
        """
        reservation.confirm_created(session)

    def extend(self, reservation: HyperbrowserUsageReservation, lifetime_ms: int, *, deadline: float) -> None:
        """Refuse an extension, because Hyperbrowser cannot lengthen a running sandbox's timeout.

        Args:
            reservation: The box's claimed reservation.
            lifetime_ms: Total lifetime after the extension.
            deadline: ``time.monotonic()`` instant after which waiting stops.

        Raises:
            UnpricedOperationError: Always.
        """
        reservation.extend(lifetime_ms, deadline=deadline)

    def settle(self, reservation: HyperbrowserUsageReservation) -> None:
        """Settle the stopped sandbox at its measured cost.

        Args:
            reservation: The box's claimed reservation.
        """
        reservation.settle()


USAGE = HyperbrowserUsage()
