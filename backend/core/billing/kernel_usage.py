"""Reserve Kernel browser VMs and settle them from Kernel's reported uptime.

Kernel bills a browser per GB-second of memory while it is active, with no
per-browser fee and no network charge. The adapter funds a headful browser
for its whole possible uptime and settles from the ``usage.uptime_ms`` that
Kernel reports for the deleted session. When that report is not ready, it
settles from the measured time between the create call and the delete.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from .budget_amounts import cent_units
from .budgets import BudgetConflictError, BudgetError, OperationSnapshot
from .operation_pricing import ChargePolicy, OperationQuote, UnpricedOperationError, json_fingerprint, operation_quote
from .runtime import BudgetRuntime, OperationCompletedError, UsagePendingError
from .usage_tags import box_tag
from .vercel_usage import vercel_charge_policy

# Read on 2026-11-24. Kernel lists a headless browser (1 GB) at
# $0.0000166667/s and a headful browser (8 GB) at $0.0001333336/s, which is
# 8 x the same GB-second rate. A GPU browser costs more and is refused here.
_PRICING_URL = "https://www.kernel.sh/docs/info/pricing"
_USAGE_URL = "https://kernel.sh/docs/api-reference/browsers/get-browser-session-details"
_PRICE_VERSION = "kernel-public-2026-11-24"
GB_SECOND_USD = Decimal("0.0000166667")
# Kernel accepts only 8 or 16 GiB for a headful browser.
HEADFUL_MEMORY_GIB = frozenset({8, 16})
# Kernel's longest inactivity timeout (72 hours).
MAX_LIFETIME_MS = 259_200_000
# Covers the create call before the funded clock starts and the delete call
# after the last work.
STOP_MARGIN_MS = 120_000


def _positive_int(value: Any) -> bool:
    """Tell whether a value is a positive int and not a bool.

    Args:
        value: The value to check.

    Returns:
        True for a positive int.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _maximum_usd(request: Mapping[str, Any]) -> Decimal:
    """Bound the uptime cost of one browser.

    A worker that stops calling leaves the browser idle, and Kernel deletes
    it after its inactivity timeout. So the browser can run for its funded
    lifetime, then for one more timeout, then for the stop margin.

    Args:
        request: A request accepted by :func:`quote_kernel_browser`.

    Returns:
        The most the browser can cost in USD.
    """
    ceiling_ms = request["lifetime_ms"] + request["timeout_seconds"] * 1000 + STOP_MARGIN_MS
    return Decimal(ceiling_ms) * request["memory_gib"] * GB_SECOND_USD / 1000


def quote_kernel_browser(request: Mapping[str, Any]) -> OperationQuote:
    """Price one headful Kernel browser before it exists.

    Args:
        request: ``lifetime_ms``, ``timeout_seconds`` (the inactivity
            timeout), ``memory_gib`` (8 or 16), ``headless`` (False) and
            ``gpu`` (False).

    Returns:
        Coverage for the browser's whole possible uptime.

    Raises:
        UnpricedOperationError: When the request is not a bounded headful browser without a GPU.
    """
    lifetime_ms = request.get("lifetime_ms")
    timeout_seconds = request.get("timeout_seconds")
    if (
        not _positive_int(lifetime_ms)
        or lifetime_ms > MAX_LIFETIME_MS
        or not _positive_int(timeout_seconds)
        or timeout_seconds * 1000 > MAX_LIFETIME_MS
        or request.get("memory_gib") not in HEADFUL_MEMORY_GIB
        or isinstance(request.get("memory_gib"), bool)
        or request.get("headless") is not False
        or request.get("gpu") is not False
    ):
        raise UnpricedOperationError(
            "Protected Kernel runs need a bounded headful browser with 8 or 16 GiB and no GPU."
        )
    return operation_quote(
        request,
        _maximum_usd(request),
        ChargePolicy("sandbox"),
        {
            "provider": "kernel",
            "price_version": _PRICE_VERSION,
            "source": _PRICING_URL,
            "gb_second_usd": str(GB_SECOND_USD),
            "stop_margin_ms": STOP_MARGIN_MS,
            "request": dict(request),
        },
    )


def _uptime_ms(receipt: Mapping[str, Any], session_id: str) -> int | None:
    """Read the final uptime from Kernel's report of a deleted session.

    Args:
        receipt: The browser details that Kernel returned after the delete.
        session_id: The admitted session.

    Returns:
        The uptime in milliseconds, or ``None`` when the report is not final.
    """
    usage = receipt.get("usage")
    uptime = usage.get("uptime_ms") if isinstance(usage, Mapping) else None
    if (
        receipt.get("session_id") != session_id
        or receipt.get("deleted_at") is None
        or receipt.get("usage_status") != "ready"
        or isinstance(uptime, bool)
        or not isinstance(uptime, int)
        or uptime < 0
    ):
        return None
    return uptime


def _status_code(error: BaseException) -> int | None:
    """Return the HTTP status of an SDK error, if it has one.

    Args:
        error: The create failure.

    Returns:
        The status code, or ``None``.
    """
    status = getattr(error, "status_code", None)
    return status if isinstance(status, int) and not isinstance(status, bool) else None


class KernelUsageReservation:
    """Own the coverage and the usage evidence of one Kernel browser."""

    def __init__(
        self,
        runtime: BudgetRuntime,
        request: Mapping[str, Any],
        *,
        operation_key: str,
        reserved: OperationSnapshot | None = None,
    ) -> None:
        """Reserve and claim one browser creation before any network call.

        Args:
            runtime: Authenticated budget authority outside the guest.
            request: The final browser request.
            operation_key: Stable logical identity for replay protection.
            reserved: A pool lease slice already reserved for this browser.

        Raises:
            BudgetConflictError: When the lease slice was reserved for a different request.
            OperationCompletedError: When the operation already settled.
            UsagePendingError: When another worker already dispatched the operation.
        """
        self.runtime = runtime
        self.request = dict(request)
        self.lifetime_ms = request["lifetime_ms"]
        self.quote = quote_kernel_browser(request)
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
            raise BudgetConflictError("The lease slice was reserved for a different browser request.")
        else:
            self.operation = reserved
        if self.operation.state == "settled":
            raise OperationCompletedError("This browser operation already completed; retrieve its saved result.")
        runtime.pin(self.operation.id, cent_units(self.operation.max_cents), box=self.box)
        try:
            claimed = runtime.service.mark_dispatched(self.operation.id, runtime.username)
        except BaseException:
            # A refused claim means no browser exists for this operation.
            runtime.unpin(self.operation.id)
            raise
        if not claimed.dispatch_claimed:
            raise UsagePendingError("This browser was already dispatched; reconcile its exact session.")
        self.session_id: str | None = None
        self.receipt: dict[str, Any] = {}
        self.measured_ms: int | None = None

    def confirm(self, browser: Any) -> None:
        """Bind the created session and check it against the funded request.

        Args:
            browser: The SDK browser that the create call returned.

        Raises:
            UsagePendingError: When the browser differs from the funded request.
        """
        self.session_id = browser.session_id
        self.runtime.service.mark_dispatched(self.operation.id, self.runtime.username, browser.session_id)
        timeout = getattr(browser, "timeout_seconds", None)
        if (
            getattr(browser, "headless", None) is not False
            or getattr(browser, "gpu", None)
            or getattr(browser, "memory", None) != f"{self.request['memory_gib']}GiB"
            or not _positive_int(timeout)
            or timeout > self.request["timeout_seconds"]
        ):
            raise UsagePendingError("The Kernel browser did not confirm the funded memory, mode and timeout.")

    def record(self, receipt: Mapping[str, Any], *, measured_ms: int | None) -> None:
        """Keep the evidence that the provider read after the delete.

        Args:
            receipt: Kernel's details of the deleted session, or an empty mapping.
            measured_ms: Wall time from the create call to the confirmed delete,
                or ``None`` when the delete is not confirmed.
        """
        self.receipt = dict(receipt)
        self.measured_ms = measured_ms

    def extend(self, lifetime_ms: int, *, deadline: float) -> None:
        """Fund a longer lifetime before the browser gets it.

        Args:
            lifetime_ms: The total lifetime after the extension.
            deadline: ``time.monotonic()`` instant after which waiting stops.

        Raises:
            ValueError: When the lifetime does not grow.
            UnpricedOperationError: When the longer lifetime cannot be bounded.
            UsagePendingError: When covered work did not settle before ``deadline``.
            BudgetReached: When the budget cannot pay for the extension.
        """
        if lifetime_ms <= self.lifetime_ms:
            raise ValueError("An extension must lengthen the funded lifetime.")
        request = {**self.request, "lifetime_ms": lifetime_ms}
        quote = quote_kernel_browser(request)
        extended = self.runtime.extend(
            self.operation.id,
            quote,
            evidence_key=f"kernel-extend:{lifetime_ms}",
            evidence={"provider": "kernel", "lifetime_ms": lifetime_ms, "request": quote.request_fingerprint},
            deadline=deadline,
        )
        self.runtime.pin(self.operation.id, cent_units(extended.max_cents), box=self.box)
        self.request = request
        self.quote = quote
        self.lifetime_ms = lifetime_ms

    def pending(self) -> None:
        """Keep the hold when the browser or its usage is uncertain."""
        current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
        if current.state in {"dispatched", "pending"}:
            evidence = {
                "provider": "kernel",
                "session_id": self.session_id,
                "receipt": self.receipt,
                "measured_ms": self.measured_ms,
            }
            self.runtime.service.mark_pending(
                self.operation.id,
                self.runtime.username,
                evidence_key=json_fingerprint(evidence),
                evidence=evidence,
            )

    def fail(self, error: BaseException) -> None:
        """Release the hold when Kernel refused the create call, otherwise keep it.

        A 4xx answer proves that no browser exists. Any other failure, such
        as a timeout or a 5xx answer, can leave a browser, so the hold stays.

        Args:
            error: The failure of the create call.
        """
        status = _status_code(error)
        if self.session_id is None and status is not None and 400 <= status < 500:
            try:
                self.runtime.service.reject(
                    self.operation.id,
                    self.runtime.username,
                    evidence_key=f"kernel-refusal:{self.operation.id}",
                    evidence={"provider": "kernel", "status_code": status, "message": str(error)[:1000]},
                )
                self.runtime.unpin(self.operation.id)
                return
            except BudgetError:
                pass
        self.pending()

    def settle(self) -> None:
        """Settle the deleted browser at its reported or measured uptime.

        Kernel's reported uptime comes first. Without it, the measured wall
        time is used, which is never shorter than the uptime. The charge never
        goes above the reservation.

        Raises:
            UsagePendingError: When the browser has no session or no usable evidence.
            BudgetError: When the ledger refuses the settlement.
        """
        try:
            current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
            if current.state == "settled":
                self.runtime.unpin(self.operation.id)
                return
            if self.session_id is None:
                raise UsagePendingError("Kernel browser creation has not returned an exact session identity.")
            uptime = _uptime_ms(self.receipt, self.session_id)
            basis = "kernel-usage"
            if uptime is None:
                if self.measured_ms is None:
                    raise UsagePendingError(
                        "Kernel has not reported the browser's usage, and its delete is unconfirmed."
                    )
                uptime, basis = self.measured_ms, "measured"
            reported = Decimal(uptime) * self.request["memory_gib"] * GB_SECOND_USD / 1000
            maximum = _maximum_usd(self.request)
            usd = min(reported, maximum)
            charge = vercel_charge_policy(self.quote.price_snapshot).convert(usd)
            self.runtime.service.settle(
                self.operation.id,
                self.runtime.username,
                evidence_key=f"kernel-stop:{self.session_id}",
                actual_cents=charge.total,
                actual_wallet_cents=charge.wallet,
                evidence={
                    "provider": "kernel",
                    "source": _USAGE_URL,
                    "basis": basis,
                    "uptime_ms": uptime,
                    "receipt": self.receipt,
                    "provider_usd": str(reported),
                    "clamped": reported > maximum,
                },
            )
            self.runtime.unpin(self.operation.id)
        except BaseException:
            self.pending()
            raise


class KernelUsage:
    """Meter Kernel browsers through the provider-neutral usage protocol."""

    name = "kernel"

    def quote(self, request: Mapping[str, Any]) -> OperationQuote:
        """Price one browser before it exists.

        Args:
            request: The final browser request.

        Returns:
            Coverage for the browser's whole possible uptime.
        """
        return quote_kernel_browser(request)

    def reserve(
        self,
        runtime: BudgetRuntime,
        request: Mapping[str, Any],
        *,
        operation_key: str,
        reserved: OperationSnapshot | None = None,
    ) -> KernelUsageReservation:
        """Reserve and claim one browser, or claim a lease slice reserved for it.

        Args:
            runtime: Authenticated budget authority.
            request: The final browser request.
            operation_key: Stable logical identity of the browser.
            reserved: Optional pool lease slice for this browser.

        Returns:
            The claimed reservation.
        """
        return KernelUsageReservation(runtime, request, operation_key=operation_key, reserved=reserved)

    def confirm(self, reservation: KernelUsageReservation, session: Any) -> None:
        """Check the created browser against its funded request.

        Args:
            reservation: The browser's reservation.
            session: The SDK browser.
        """
        reservation.confirm(session)

    def extend(self, reservation: KernelUsageReservation, lifetime_ms: int, *, deadline: float) -> None:
        """Fund a longer lifetime before the browser gets it.

        Args:
            reservation: The browser's reservation.
            lifetime_ms: The total lifetime after the extension.
            deadline: ``time.monotonic()`` instant after which waiting stops.
        """
        reservation.extend(lifetime_ms, deadline=deadline)

    def settle(self, reservation: KernelUsageReservation) -> None:
        """Settle the deleted browser at its actual uptime.

        Args:
            reservation: The browser's reservation.
        """
        reservation.settle()


USAGE = KernelUsage()
