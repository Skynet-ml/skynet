"""Reserve Kernel browser VMs and settle them from Kernel's reported uptime.

Kernel bills a headful browser at a flat rate for each second it is active.
The pricing page lists no per-browser fee and no network fee. The adapter
funds a headful 8 GiB browser for its whole possible uptime and settles from
the ``usage.uptime_ms`` that Kernel reports for the deleted session. When
that report is not ready, it settles from the measured time between the
create call and the delete. The provider caps that measured time at the
funded lifetime plus one inactivity timeout plus the stop margin, and the
settle clamps every charge to the reservation.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from .budget_amounts import cent_units
from .budgets import BudgetConflictError, BudgetError, OperationSnapshot
from .operation_pricing import ChargePolicy, OperationQuote, UnpricedOperationError, json_fingerprint, operation_quote
from .runtime import BudgetRuntime, OperationCompletedError, UsagePendingError
from .usage_tags import box_tag

# Kernel usage is sandbox usage, so it reuses the shared sandbox markup and
# wallet split instead of a second policy that could drift from it.
from .vercel_usage import vercel_charge_policy

logger = logging.getLogger(__name__)

# Read on 2026-11-24 from _PRICING_URL. The page states one flat price per
# second for each browser type: headful $0.0001333336. It names no browser
# memory size. Inferred, not stated: that rate is for the 8 GiB headful
# default (the SDK's ``BrowserMemoryRequest`` docs). Other sizes, such as
# 16 GiB, are refused until Kernel confirms their price. A GPU browser costs
# more and is refused here.
_PRICING_URL = "https://www.kernel.sh/docs/info/pricing"
_USAGE_URL = "https://kernel.sh/docs/api-reference/browsers/get-browser-session-details"
_PRICE_VERSION = "kernel-public-2026-11-24"
HEADFUL_SECOND_USD = Decimal("0.0001333336")
HEADFUL_MEMORY_GIB = 8
# Kernel's longest inactivity timeout (72 hours).
MAX_LIFETIME_MS = 259_200_000
# Covers the provider's delete call (``STOP_TIMEOUT`` in the Kernel provider)
# and the read wait of a last command that ends at the funded end.
STOP_MARGIN_MS = 120_000
# Statuses on the create call that prove Kernel made no browser. A 408 or a
# 409 can follow a create that Kernel still finishes, so they stay pending.
# A 502 or 503 stays pending by choice: Kernel does not document whether a
# create that gets one made a browser.
_REFUSED_CREATE_STATUSES = frozenset({400, 401, 403, 404, 422, 429})


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

    Kernel's create call has no hard session lifetime, only an inactivity
    timeout. The bound assumes that the browser goes idle after its funded
    lifetime, so Kernel deletes it one timeout later, and adds the stop
    margin. A background process that holds a CDP connection keeps the
    browser active, so that assumption can fail. Then the delete in the
    provider's ``stop()`` ends the browser, and the settle clamp keeps the
    charge at this bound. Close this gap before ``production_allowed``
    becomes True.

    Args:
        request: A request accepted by :func:`quote_kernel_browser`.

    Returns:
        The most the browser can cost in USD.
    """
    ceiling_ms = request["lifetime_ms"] + request["timeout_seconds"] * 1000 + STOP_MARGIN_MS
    return _uptime_usd(ceiling_ms)


def _uptime_usd(uptime_ms: int) -> Decimal:
    """Price a headful browser's uptime.

    Args:
        uptime_ms: The uptime in milliseconds.

    Returns:
        The cost in USD.
    """
    return Decimal(uptime_ms) * HEADFUL_SECOND_USD / 1000


def quote_kernel_browser(request: Mapping[str, Any]) -> OperationQuote:
    """Price one headful Kernel browser before it exists.

    Args:
        request: ``lifetime_ms``, ``timeout_seconds`` (the inactivity
            timeout), ``memory_gib`` (8), ``headless`` (False) and
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
        or type(request.get("memory_gib")) is not int
        or request.get("memory_gib") != HEADFUL_MEMORY_GIB
        or request.get("headless") is not False
        or request.get("gpu") is not False
    ):
        raise UnpricedOperationError("Protected Kernel runs need a bounded headful browser with 8 GiB and no GPU.")
    return operation_quote(
        request,
        _maximum_usd(request),
        ChargePolicy("sandbox"),
        {
            "provider": "kernel",
            "price_version": _PRICE_VERSION,
            "source": _PRICING_URL,
            "headful_second_usd": str(HEADFUL_SECOND_USD),
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
        self.confirmed = False
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
        self.confirmed = True

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

    def _reject(self, evidence: dict[str, Any]) -> bool:
        """Release the hold of a browser that does not exist.

        Args:
            evidence: Why no browser exists.

        Returns:
            True when the ledger released the hold.

        Raises:
            Exception: A ledger or database error, after the hold is marked
                pending, so the hold never stays in the dispatched state.
        """
        try:
            self.runtime.service.reject(
                self.operation.id,
                self.runtime.username,
                evidence_key=f"kernel-refusal:{self.operation.id}",
                evidence={"provider": "kernel", **evidence},
            )
        except BudgetError:
            return False
        except Exception:
            try:
                self.pending()
            except Exception as pending_error:
                logger.warning(
                    "Kernel hold %s could not be marked pending: %s",
                    self.operation.id,
                    type(pending_error).__name__,
                )
            raise
        self.runtime.unpin(self.operation.id)
        return True

    def release(self, reason: str) -> None:
        """Release the hold when the create call never went out.

        Args:
            reason: Why the create call did not go out.
        """
        if not self._reject({"reason": reason}):
            self.pending()

    def fail(self, error: BaseException, *, no_browser: bool) -> None:
        """Release the hold when Kernel refused the create call, otherwise keep it.

        Only a listed refusal status proves that no browser exists, and only
        when the tag sweep also found none. A timeout, a 408, a 409 or a 5xx
        answer can leave a browser, so the hold stays.

        Args:
            error: The failure of the create call.
            no_browser: The tag sweep finished and found no browser of the box.
        """
        status = _status_code(error)
        refused = self.session_id is None and no_browser and status in _REFUSED_CREATE_STATUSES
        if refused and self._reject({"status_code": status, "message": str(error)[:1000]}):
            return
        self.pending()

    def settle(self) -> None:
        """Settle the deleted browser at its reported or measured uptime.

        Kernel's reported uptime comes first. Without it, the measured wall
        time is used, which is never shorter than the uptime. The charge never
        goes above the reservation.

        Raises:
            UsagePendingError: When the browser has no confirmed session or no usable evidence.
            BudgetError: When the ledger refuses the settlement.
        """
        try:
            current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
            if current.state == "settled":
                self.runtime.unpin(self.operation.id)
                return
            if self.session_id is None:
                raise UsagePendingError("Kernel browser creation has not returned an exact session identity.")
            if not self.confirmed:
                raise UsagePendingError("The Kernel browser does not match its funded request.")
            uptime = _uptime_ms(self.receipt, self.session_id)
            basis = "kernel-usage"
            if uptime is None:
                if self.measured_ms is None:
                    raise UsagePendingError(
                        "Kernel has not reported the browser's usage, and its delete is unconfirmed."
                    )
                uptime, basis = self.measured_ms, "measured"
            reported = _uptime_usd(uptime)
            maximum = _maximum_usd(self.request)
            if reported > maximum:
                # A longer uptime than the funded ceiling means a bound did not
                # hold; Skynet absorbs the difference, so make it visible.
                logger.warning(
                    "Kernel session %s used %s ms, above its funded ceiling; the charge is clamped.",
                    self.session_id,
                    uptime,
                )
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
