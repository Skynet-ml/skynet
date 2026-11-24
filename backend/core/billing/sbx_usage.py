"""Reserve Docker Sandboxes (sbx) Cloud compute and settle it at measured runtime.

sbx has no API that reports the usage or cost of one sandbox: its console
shows totals only, up to 48 hours late. So a box settles at the wall time
that Skynet measures, from the create call to the confirmed delete, at the
published hourly rate of its shape, billed by the second.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

from .budget_amounts import cent_units
from .budgets import BudgetConflictError, OperationSnapshot
from .operation_pricing import ChargePolicy, OperationQuote, UnpricedOperationError, operation_quote
from .runtime import BudgetRuntime, OperationCompletedError, UsagePendingError
from .usage_tags import box_tag
from .vercel_usage import vercel_charge_policy

# Read on 2026-11-24 from https://www.docker.com/blog/introducing-cloud-sandboxes/
# ("We meter compute by the second, and nothing else"; volumes and egress are
# free). Shapes from https://docs.docker.com/ai/sandboxes-api/limits/.
_PRICING_URL = "https://www.docker.com/blog/introducing-cloud-sandboxes/"
_PRICE_VERSION = "sbx-cloud-payg-2026-11-24"
SHAPE_USD_PER_HOUR = {
    (1, 2048): Decimal("0.07"),
    (2, 4096): Decimal("0.14"),
    (4, 8192): Decimal("0.28"),
    (8, 16384): Decimal("0.56"),
    (16, 32768): Decimal("1.12"),
}
# Skynet's sbx provider caps every sandbox timeout at 12 hours
# (``_MAX_LIFETIME_SECONDS``); sbx itself allows 24. A test keeps the two equal.
LIFETIME_CEILING_MS = 43_200_000
# sbx bills from start to stop, but does not publish how long the create and
# the delete take outside the box timeout. This grace covers that time: the
# hold funds the lifetime plus the grace, and a box that runs longer settles
# at that cap.
# The sbx timeout is a deadline, not an idle timeout: the API calls it "the
# initial TTL" of "deadline behavior"
# (https://docs.docker.com/reference/api/sandboxes/latest/schemas/TimeoutOptions),
# and the CLI says expiry cannot pass 24 hours "from creation"
# (https://docs.docker.com/reference/cli/sbx/create/), both read 2026-11-24.
# Neither says when the server starts that clock, so this assumes it starts
# no later than this grace after the create call. The provider makes a
# protected box run within half the grace, and the other half covers the
# delete. A sandbox whose delete fails then bills at most its lifetime plus
# the grace, and settle never charges more than the hold.
BILLING_GRACE_MS = 600_000
_MS_PER_HOUR = Decimal(3_600_000)


def _billed_usd(milliseconds: int, rate: Decimal) -> Decimal:
    """Price a runtime at a per-hour rate, rounded up to whole seconds.

    Args:
        milliseconds: The runtime.
        rate: The shape's USD per hour.

    Returns:
        The cost in USD.
    """
    return Decimal(-(-milliseconds // 1000) * 1000) * rate / _MS_PER_HOUR


def quote_sbx_sandbox(request: Mapping[str, Any]) -> OperationQuote:
    """Price one sbx sandbox before it exists.

    Args:
        request: ``cpus`` and ``memory_mib`` of a published shape, and
            ``lifetime_ms``, the sandbox timeout.

    Returns:
        Coverage for the lifetime plus the create and delete grace.

    Raises:
        UnpricedOperationError: When the shape is not published or the lifetime is unbounded.
    """
    shape = (request.get("cpus"), request.get("memory_mib"))
    lifetime_ms = request.get("lifetime_ms")
    if (
        any(type(value) is not int for value in shape)
        or shape not in SHAPE_USD_PER_HOUR
        or type(lifetime_ms) is not int
        or not 0 < lifetime_ms <= LIFETIME_CEILING_MS
    ):
        raise UnpricedOperationError("sbx requires a published shape and a lifetime of at most 12 hours.")
    rate = SHAPE_USD_PER_HOUR[shape]
    billable_ms = lifetime_ms + BILLING_GRACE_MS
    return operation_quote(
        request,
        _billed_usd(billable_ms, rate),
        ChargePolicy("sandbox"),
        {
            "provider": "sbx",
            "price_version": _PRICE_VERSION,
            "source": _PRICING_URL,
            "usd_per_hour": str(rate),
            "maximum_billable_ms": billable_ms,
            "request": dict(request),
        },
    )


class SbxUsageReservation:
    """Hold the coverage of one sbx sandbox and its measured runtime."""

    def __init__(
        self,
        runtime: BudgetRuntime,
        request: Mapping[str, Any],
        *,
        operation_key: str,
        reserved: OperationSnapshot | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Reserve and claim one sandbox before the create call.

        Args:
            runtime: Authenticated budget authority.
            request: The parameters that :func:`quote_sbx_sandbox` prices.
            operation_key: Stable logical identity of the box.
            reserved: A pool lease slice already reserved for this box.
            clock: Monotonic seconds, for the measured runtime.

        Raises:
            BudgetConflictError: The lease slice was reserved for another request.
            OperationCompletedError: The operation already settled.
            UsagePendingError: Another caller already dispatched the operation.
        """
        self.runtime = runtime
        self.request = dict(request)
        self.quote = quote_sbx_sandbox(request)
        self.lifetime_ms = request.get("lifetime_ms")
        self.box = box_tag(operation_key)
        self._clock = clock
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
        self.uid: str | None = None
        self.started_at: float | None = None
        self.stopped_at: float | None = None

    def start_clock(self) -> None:
        """Start the measured runtime just before the create call goes out."""
        self.started_at = self._clock()

    def stop_clock(self) -> None:
        """End the measured runtime once sbx confirms the sandbox is gone."""
        self.stopped_at = self._clock()

    def confirm_created(self, uid: str, body: Mapping[str, Any]) -> None:
        """Bind the sandbox uid and check the create body against the funded request.

        Args:
            uid: The uid sbx gave the sandbox.
            body: The create body the provider sent.

        Raises:
            UsagePendingError: The body asks for another shape or a longer lifetime.
        """
        self.uid = uid
        self.runtime.service.mark_dispatched(self.operation.id, self.runtime.username, uid)
        resources = body.get("resources", {})
        timeout = body.get("features", {}).get("timeouts", {}).get("timeout", "")
        if (
            resources.get("cpus") != self.request["cpus"]
            or resources.get("memoryMib") != self.request["memory_mib"]
            or not isinstance(timeout, str)
            or not timeout.endswith("s")
            or not timeout[:-1].isdecimal()
            or int(timeout[:-1]) * 1000 > self.lifetime_ms
        ):
            raise UsagePendingError("The sandbox create body does not match the funded shape and lifetime.")

    def extend(self, lifetime_ms: int, *, deadline: float) -> None:
        """Fund a longer lifetime before sbx is asked to keep the sandbox that long.

        Args:
            lifetime_ms: The total lifetime once extended.
            deadline: ``time.monotonic()`` instant after which waiting stops.

        Raises:
            ValueError: The lifetime does not grow.
            UnpricedOperationError: The extended lifetime cannot be bounded.
            UsagePendingError: Covered work did not settle before ``deadline``.
            BudgetReached: The budget cannot pay for the extension.
        """
        if lifetime_ms <= self.lifetime_ms:
            raise ValueError("An extension must lengthen the funded lifetime.")
        quote = quote_sbx_sandbox({**self.request, "lifetime_ms": lifetime_ms})
        extended = self.runtime.extend(
            self.operation.id,
            quote,
            evidence_key=f"sbx-extend:{lifetime_ms}",
            evidence={"provider": "sbx", "lifetime_ms": lifetime_ms, "request": quote.request_fingerprint},
            deadline=deadline,
        )
        self.runtime.pin(self.operation.id, cent_units(extended.max_cents), box=self.box)
        self.lifetime_ms = lifetime_ms
        self.quote = quote

    def pending(self) -> None:
        """Keep the hold when no one knows whether a sandbox exists."""
        current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
        if current.state in {"dispatched", "pending"}:
            self.runtime.service.mark_pending(
                self.operation.id,
                self.runtime.username,
                evidence_key=f"sbx-unknown:{self.operation.id}",
                evidence={"provider": "sbx", "uid": self.uid, "started": self.started_at is not None},
            )

    def release(self) -> None:
        """Release the hold of a create that sbx refused or that never went out.

        If the release fails, the hold stays pending and the error goes on.
        """
        try:
            self.runtime.service.reject(
                self.operation.id,
                self.runtime.username,
                evidence_key=f"sbx-refusal:{self.operation.id}",
                evidence={"provider": "sbx", "sent": self.started_at is not None},
            )
        except BaseException:
            self.pending()
            raise
        self.runtime.unpin(self.operation.id)

    def settle(self) -> None:
        """Settle the stopped sandbox at its measured runtime, never above the hold.

        A sandbox whose delete sbx never confirmed runs until its timeout, so
        it settles at the whole funded lifetime.

        Raises:
            UsagePendingError: The create never went out, so there is no runtime.
            BudgetError: The settlement breaks the reserved bounds.
        """
        try:
            current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
            if current.state == "settled":
                self.runtime.unpin(self.operation.id)
                return
            if self.started_at is None:
                raise UsagePendingError("The sandbox create never went out; there is no runtime to settle.")
            snapshot = self.quote.price_snapshot
            ceiling_ms = snapshot["maximum_billable_ms"]
            measured = self.stopped_at is not None
            if self.stopped_at is not None:
                runtime_ms = math.ceil((self.stopped_at - self.started_at) * 1000)
            else:
                runtime_ms = ceiling_ms
            billed_ms = min(max(runtime_ms, 0), ceiling_ms)
            usd = _billed_usd(billed_ms, Decimal(snapshot["usd_per_hour"]))
            # The Vercel helper is reused, not copied: it rebuilds the charge
            # policy from any quote's price snapshot, so the settle uses the
            # markup frozen at reservation time. Nothing in it is Vercel-only.
            charge = vercel_charge_policy(snapshot).convert(usd)
            self.runtime.service.settle(
                self.operation.id,
                self.runtime.username,
                evidence_key=f"sbx-stop:{self.operation.id}",
                actual_cents=charge.total,
                actual_wallet_cents=charge.wallet,
                evidence={
                    "provider": "sbx",
                    "source": _PRICING_URL,
                    "uid": self.uid,
                    "measured_ms": runtime_ms if measured else None,
                    "billed_ms": billed_ms,
                    "provider_usd": str(usd),
                },
            )
            self.runtime.unpin(self.operation.id)
        except BaseException:
            self.pending()
            raise


class SbxUsage:
    """Meter sbx sandboxes through the provider-neutral usage protocol."""

    name = "sbx"

    def quote(self, request: Mapping[str, Any]) -> OperationQuote:
        """Price one sandbox before it exists.

        Args:
            request: Final create parameters.

        Returns:
            Coverage for the sandbox's whole funded lifetime.
        """
        return quote_sbx_sandbox(request)

    def reserve(
        self,
        runtime: BudgetRuntime,
        request: Mapping[str, Any],
        *,
        operation_key: str,
        reserved: OperationSnapshot | None = None,
    ) -> SbxUsageReservation:
        """Reserve and claim one sandbox, or claim a lease slice already reserved for it.

        Args:
            runtime: Authenticated budget authority.
            request: Final create parameters.
            operation_key: Stable logical identity of the box.
            reserved: Optional pool lease slice for this box.

        Returns:
            The claimed reservation.
        """
        return SbxUsageReservation(runtime, request, operation_key=operation_key, reserved=reserved)

    def confirm(self, reservation: SbxUsageReservation, session: Mapping[str, Any]) -> None:
        """Bind the created sandbox and check it against its funded envelope.

        Args:
            reservation: The box's claimed reservation.
            session: ``uid`` and the create ``body`` the provider sent.
        """
        reservation.confirm_created(session["uid"], session["body"])

    def extend(self, reservation: SbxUsageReservation, lifetime_ms: int, *, deadline: float) -> None:
        """Fund a longer lifetime before the provider grants it.

        Args:
            reservation: The box's claimed reservation.
            lifetime_ms: Total lifetime after the extension.
            deadline: ``time.monotonic()`` instant after which waiting stops.
        """
        reservation.extend(lifetime_ms, deadline=deadline)

    def settle(self, reservation: SbxUsageReservation) -> None:
        """Settle the stopped sandbox at its measured runtime.

        Args:
            reservation: The box's claimed reservation.
        """
        reservation.settle()


USAGE = SbxUsage()
