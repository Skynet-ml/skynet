"""Reserve Mosaic sandbox compute and settle it from Mosaic's usage history or the measured lifetime.

Mosaic bills one rate for each billing unit-hour of awake time. A box's
units are ``max(ceil(vcpu / 2), ceil(memory_mb / 4096))``. Hibernation and
egress are free, and no creation fee is published. The quote funds the box's
whole lifetime at its units, so the funded hold is a ceiling. The settle
reads Mosaic's per-sandbox usage history. When that history is not final and
complete, the settle charges the measured wall time from the create call to
the confirmed destroy, plus a two-second margin for Mosaic's whole-second
timestamps, which is never less than the awake time. A known box without a
confirmed destroy, or whose shape Mosaic did not confirm, is charged the
whole hold, because it may still run or run larger. Every charge is clamped
to the reserved hold.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from .budget_amounts import cent_units
from .budgets import BudgetConflictError, OperationSnapshot
from .operation_pricing import ChargePolicy, OperationQuote, UnpricedOperationError, json_fingerprint, operation_quote
from .runtime import BudgetRuntime, OperationCompletedError, UsagePendingError
from .usage_tags import box_tag
from .vercel_usage import vercel_charge_policy

# Read 2026-11-24 from https://sandbox.mosaicos.com/pricing/ ("$0.05 / hour"
# of active compute for one 2 vCPU / 4 GB unit; hibernation and egress $0; no
# creation fee is published) and https://sandbox.mosaicos.com/llms.txt (whole billing
# units: max(ceil(vCPU / 2), ceil(MiB / 4096))).
_PRICING_URL = "https://sandbox.mosaicos.com/pricing/"
_PRICE_VERSION = "mosaic-sandbox-public-2026-11-24"
_UNIT_HOUR_USD = Decimal("0.05")
_UNIT_VCPUS = 2
_UNIT_MEMORY_MB = 4096
_MS_PER_HOUR = Decimal(3_600_000)
_SECONDS_PER_HOUR = Decimal(3600)
_MICROS_PER_USD = Decimal(1_000_000)
# The most history records one read asks for; the SDK refuses more.
_HISTORY_LIMIT = 500
# Mosaic's create accepts 1 to 64 vCPUs and 128 MiB to 256 GiB of memory.
_MAX_VCPU = 64
_MEMORY_MB_RANGE = (128, 262_144)
# Mosaic stamps a usage segment's start and end in whole seconds, so rounding
# can widen the segment by up to one second at each end. The settle still
# clamps the charge to the hold.
_WALL_MARGIN_MS = 2_000

_clock = time.monotonic


def _positive_int(value: Any, low: int, high: int | None = None) -> bool:
    """Report whether a request field is an int inside its range.

    Args:
        value: The request field.
        low: The smallest allowed value.
        high: The largest allowed value, or ``None`` for no upper bound.

    Returns:
        True for an int (not a bool) from ``low`` to ``high``.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value >= low and (high is None or value <= high)


def billing_units(vcpu: int, memory_mb: int) -> int:
    """Return the whole Mosaic billing units of one box shape.

    Args:
        vcpu: The box's vCPUs.
        memory_mb: The box's memory in MiB.

    Returns:
        The larger of the CPU and memory unit counts.
    """
    return max(math.ceil(vcpu / _UNIT_VCPUS), math.ceil(memory_mb / _UNIT_MEMORY_MB))


def mosaic_measured_usd(units: int, wall_ms: int) -> Decimal:
    """Price a box's measured lifetime at the published rate.

    Args:
        units: The box's billing units.
        wall_ms: Milliseconds from the create call to the confirmed destroy.

    Returns:
        The provider cost in USD.
    """
    return Decimal(units * wall_ms) * _UNIT_HOUR_USD / _MS_PER_HOUR


def quote_mosaic_sandbox(request: Mapping[str, Any]) -> OperationQuote:
    """Price one Mosaic box for its whole funded lifetime before it exists.

    Args:
        request: ``vcpu``, ``memory_mb`` and ``lifetime_ms`` of the box.

    Returns:
        Coverage for the box's units over the whole lifetime.

    Raises:
        UnpricedOperationError: The shape or the lifetime is missing or out of range.
    """
    vcpu, memory_mb, lifetime_ms = request.get("vcpu"), request.get("memory_mb"), request.get("lifetime_ms")
    if not (
        _positive_int(vcpu, 1, _MAX_VCPU)
        and _positive_int(memory_mb, *_MEMORY_MB_RANGE)
        and _positive_int(lifetime_ms, 1)
    ):
        raise UnpricedOperationError("Mosaic requires an explicit vCPU count, memory size and bounded lifetime.")
    units = billing_units(vcpu, memory_mb)
    return operation_quote(
        request,
        mosaic_measured_usd(units, lifetime_ms),
        ChargePolicy("sandbox"),
        {
            "provider": "mosaic",
            "price_version": _PRICE_VERSION,
            "source": _PRICING_URL,
            "unit_hour_usd": str(_UNIT_HOUR_USD),
            "billing_units": units,
            "request": dict(request),
        },
    )


def history_usd(history: Mapping[str, Any], *, units: int) -> Decimal | None:
    """Price a stopped box from Mosaic's usage history, when the history is final and complete.

    The SDK's ``Sandbox.usage_history`` returns the API JSON unchanged
    (``mosaic_sandbox/client.py:3197-3222`` in mosaic-sandbox 0.15.8). The
    field names are those of the response and ``SandboxUsageHistoryRecord``
    schemas in https://sandbox.mosaicos.com/openapi.json (read 2026-11-24):
    the response has ``records``, ``next_cursor`` and ``billing_final``; a
    record has ``ended_at``, ``billing_units``, ``unit_seconds``,
    ``accounting_status``, ``running_seconds``, ``pricing_status`` and
    ``billing_snapshot.rate``. ``unit_seconds`` is already shape-weighted:
    ``running_seconds`` is ``unit_seconds / billing_units`` for a metered
    record, so a 2-unit box's ``unit_seconds`` is twice its awake seconds.

    Args:
        history: One ``usage_history`` page of the box.
        units: The box's funded billing units.

    Returns:
        The billed USD, or ``None`` when the page is not ``billing_final``,
        has more pages, or any record is open, not metered, has no recorded
        USD tariff, has another shape, or has inconsistent quantities.
    """
    records = history.get("records")
    if (
        history.get("billing_final") is not True
        or not isinstance(records, list)
        or not records
        or history.get("next_cursor") is not None
    ):
        return None
    total = Decimal(0)
    for record in records:
        if (
            not isinstance(record, Mapping)
            or record.get("ended_at") is None
            or record.get("accounting_status") != "metered"
            or record.get("pricing_status") != "recorded"
            or record.get("billing_units") != units
        ):
            return None
        unit_seconds, running_seconds = _amount(record.get("unit_seconds")), _amount(record.get("running_seconds"))
        # A one-second gap allows the vendor's rounding; a larger gap means
        # the record does not use the shape multiplier this code expects.
        if unit_seconds is None or running_seconds is None or abs(unit_seconds - running_seconds * units) > 1:
            return None
        snapshot = record.get("billing_snapshot")
        rate = snapshot.get("rate") if isinstance(snapshot, Mapping) else None
        if not isinstance(rate, Mapping) or rate.get("currency") != "USD":
            return None
        micros = rate.get("micros_per_unit_hour")
        if not _positive_int(micros, 1):
            return None
        total += unit_seconds * Decimal(micros) / _MICROS_PER_USD / _SECONDS_PER_HOUR
    return total


def _amount(value: Any) -> Decimal | None:
    """Read one non-negative history quantity.

    Args:
        value: The JSON number, or anything else.

    Returns:
        The quantity, or ``None`` when it is missing, not a number or negative.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and amount >= 0 else None


class MosaicUsageReservation:
    """Own the hold and the lifetime evidence of one Mosaic box."""

    def __init__(
        self,
        runtime: BudgetRuntime,
        request: Mapping[str, Any],
        *,
        operation_key: str,
        reserved: OperationSnapshot | None = None,
    ) -> None:
        """Reserve and claim one box before its create call.

        Args:
            runtime: Authenticated budget authority retained outside the guest.
            request: ``vcpu``, ``memory_mb`` and ``lifetime_ms`` of the box.
            operation_key: Stable logical identity for create replay protection.
            reserved: A pool lease slice already reserved for this box. The box
                claims it instead of making its own reservation write.

        Raises:
            BudgetConflictError: The lease slice was reserved for a different request.
            OperationCompletedError: The operation already settled.
            UsagePendingError: The operation was already dispatched.
        """
        self.runtime = runtime
        self.request = dict(request)
        self.quote = quote_mosaic_sandbox(request)
        self.units = billing_units(request["vcpu"], request["memory_mb"])
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
            raise UsagePendingError("This sandbox was already dispatched; reconcile its exact usage.")
        self.sandbox_id: str | None = None
        self._handle: Any = None
        self._shape_unconfirmed = False
        self._dispatched_at: float | None = None
        self._stopped_at: float | None = None

    def dispatched(self) -> None:
        """Record the instant just before the create call, where the measured lifetime starts."""
        if self._dispatched_at is None:
            self._dispatched_at = _clock()

    def bind(self, sandbox_id: str, handle: Any = None) -> None:
        """Record the id of this operation's box, so the settle can charge it.

        A bound box's shape stays unconfirmed until ``confirm_created``
        checks it, so a box found only by its tag is charged the whole hold.

        Args:
            sandbox_id: The Mosaic sandbox id.
            handle: The SDK ``Sandbox``, or ``None`` when only the id is known.
        """
        self._shape_unconfirmed = True
        self.sandbox_id = sandbox_id
        self._handle = handle
        self.runtime.service.mark_dispatched(self.operation.id, self.runtime.username, sandbox_id)

    def confirm_created(self, sandbox: Any) -> None:
        """Bind the created box and check that Mosaic gave it the funded shape.

        A failed or different answer marks the shape unconfirmed, so the
        settle charges the whole hold. The SDK reports a missing shape as 0,
        which never equals a funded shape.

        Args:
            sandbox: The SDK ``Sandbox`` that the create returned.

        Raises:
            UsagePendingError: Mosaic reports another vCPU count or memory size.
        """
        self.bind(sandbox.id, sandbox)
        summary = sandbox.info()
        if summary.vcpu != self.request["vcpu"] or summary.memory_mb != self.request["memory_mb"]:
            raise UsagePendingError("The cloud sandbox did not confirm the admitted CPU and memory.")
        self._shape_unconfirmed = False

    def extend(self, lifetime_ms: int, *, deadline: float) -> None:
        """Fund the box up to ``lifetime_ms`` before it may run that long.

        Args:
            lifetime_ms: The box's total lifetime once extended.
            deadline: ``time.monotonic()`` instant after which waiting for in-flight work stops.

        Raises:
            ValueError: The new lifetime is not longer than the funded one.
        """
        if lifetime_ms <= self.lifetime_ms:
            raise ValueError("An extension must lengthen the funded lifetime.")
        quote = quote_mosaic_sandbox({**self.request, "lifetime_ms": lifetime_ms})
        self.operation = self.runtime.extend(
            self.operation.id,
            quote,
            evidence_key=f"mosaic-extend:{lifetime_ms}",
            evidence={"provider": "mosaic", "lifetime_ms": lifetime_ms, "request": quote.request_fingerprint},
            deadline=deadline,
        )
        self.runtime.pin(self.operation.id, cent_units(self.operation.max_cents), box=self.box)
        self.lifetime_ms = lifetime_ms

    def stopped(self) -> None:
        """Record the instant Mosaic confirmed the destroy, where the measured lifetime ends."""
        if self._stopped_at is None:
            self._stopped_at = _clock()

    def _evidence(self) -> dict[str, Any]:
        """Return the audit record of this box's identity and measured lifetime.

        Returns:
            The provider, sandbox id and wall time, when known.
        """
        return {"provider": "mosaic", "sandbox_id": self.sandbox_id, "wall_ms": self._wall_ms()}

    def _wall_ms(self) -> int | None:
        """Return the measured lifetime, or ``None`` before a confirmed destroy.

        Returns:
            Whole milliseconds from the create call to the confirmed destroy,
            plus ``_WALL_MARGIN_MS``.
        """
        if self._dispatched_at is None or self._stopped_at is None:
            return None
        return math.ceil((self._stopped_at - self._dispatched_at) * 1000) + _WALL_MARGIN_MS

    def pending(self) -> None:
        """Retain the hold when the box's create, destroy or usage is uncertain."""
        current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
        if current.state in {"dispatched", "pending"}:
            evidence = self._evidence()
            self.runtime.service.mark_pending(
                self.operation.id,
                self.runtime.username,
                evidence_key=json_fingerprint(evidence),
                evidence=evidence,
            )

    def release(self, reason: str) -> None:
        """Release the hold of a create that Mosaic refused, so no box exists.

        Args:
            reason: Why the create made no box, for the audit record.
        """
        self.runtime.service.reject(
            self.operation.id,
            self.runtime.username,
            evidence_key=f"mosaic-refusal:{self.operation.id}",
            evidence={"provider": "mosaic", "reason": reason[:1000]},
        )
        self.runtime.unpin(self.operation.id)

    def _usage(self) -> tuple[Decimal, str, Any]:
        """Return the box's cost, its source and the raw evidence.

        Returns:
            The USD cost, its source (``"usage_history"``,
            ``"measured_wall_time"`` or ``"funded_ceiling"``), and the history
            records, the wall time or the funded lifetime.

        Raises:
            UsagePendingError: No box id is known.
        """
        if self.sandbox_id is None:
            raise UsagePendingError("The cloud sandbox is unknown; its usage stays pending.")
        wall_ms = self._wall_ms()
        if wall_ms is None or self._shape_unconfirmed:
            # No reconciler reads this box later, and it may still run or run
            # at a larger shape, so the whole hold is the only safe charge.
            return mosaic_measured_usd(self.units, self.lifetime_ms), "funded_ceiling", self.lifetime_ms
        history = None
        if self._handle is not None:
            try:
                # Needs the billing:read scope; without it the measured time applies.
                history = self._handle.usage_history(limit=_HISTORY_LIMIT, include_daily_finality=True)
            except Exception:
                history = None
        usd = history_usd(history, units=self.units) if isinstance(history, Mapping) else None
        if usd is not None:
            return usd, "usage_history", history.get("records")
        return mosaic_measured_usd(self.units, wall_ms), "measured_wall_time", wall_ms

    def settle(self) -> None:
        """Settle the destroyed box inside its hold, retaining the hold on any doubt.

        Raises:
            UsagePendingError: No box id is known.
            BudgetError: The settle write failed.
        """
        try:
            current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
            if current.state == "settled":
                self.runtime.unpin(self.operation.id)
                return
            usd, source, raw = self._usage()
            charge = vercel_charge_policy(self.quote.price_snapshot).convert(usd)
            self.runtime.service.settle(
                self.operation.id,
                self.runtime.username,
                evidence_key=f"mosaic-stop:{self.sandbox_id}",
                # Mosaic does not cap a box's billed time, so the charge stops at the hold.
                actual_cents=min(charge.total, current.max_cents),
                actual_wallet_cents=min(charge.wallet, current.max_wallet_cents),
                evidence={
                    **self._evidence(),
                    "source": source,
                    "usage": raw,
                    "provider_usd": str(usd),
                    "clamped": charge.total > current.max_cents,
                },
            )
            self.runtime.unpin(self.operation.id)
        except BaseException:
            self.pending()
            raise


class MosaicUsage:
    """Meter Mosaic sandboxes through the provider-neutral usage protocol."""

    name = "mosaic"

    def quote(self, request: Mapping[str, Any]) -> OperationQuote:
        """Price one box before any provider resource exists.

        Args:
            request: ``vcpu``, ``memory_mb`` and ``lifetime_ms`` of the box.

        Returns:
            Coverage for the box's whole funded lifetime.
        """
        return quote_mosaic_sandbox(request)

    def reserve(
        self,
        runtime: BudgetRuntime,
        request: Mapping[str, Any],
        *,
        operation_key: str,
        reserved: OperationSnapshot | None = None,
    ) -> MosaicUsageReservation:
        """Reserve and claim one box, or claim a lease slice already reserved for it.

        Args:
            runtime: Authenticated budget authority.
            request: ``vcpu``, ``memory_mb`` and ``lifetime_ms`` of the box.
            operation_key: Stable logical identity of the box.
            reserved: Optional pool lease slice for this box.

        Returns:
            The claimed reservation that the box confirms and settles.
        """
        return MosaicUsageReservation(runtime, request, operation_key=operation_key, reserved=reserved)

    def confirm(self, reservation: MosaicUsageReservation, session: Any) -> None:
        """Bind the created box and check its shape.

        Args:
            reservation: The box's claimed reservation.
            session: The SDK ``Sandbox`` that the create returned.
        """
        reservation.confirm_created(session)

    def extend(self, reservation: MosaicUsageReservation, lifetime_ms: int, *, deadline: float) -> None:
        """Fund a longer box lifetime before the box may run that long.

        Args:
            reservation: The box's claimed reservation.
            lifetime_ms: Total lifetime after the extension.
            deadline: ``time.monotonic()`` instant after which waiting stops.
        """
        reservation.extend(lifetime_ms, deadline=deadline)

    def settle(self, reservation: MosaicUsageReservation) -> None:
        """Settle the destroyed box at its actual cost, inside its hold.

        Args:
            reservation: The box's claimed reservation.
        """
        reservation.settle()


USAGE = MosaicUsage()
