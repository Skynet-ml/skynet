"""Reserve Daytona sandbox compute and settle it at billed or measured usage (SKY-545).

A box settles at the larger of two amounts: the price that Daytona's
analytics API reports for it, and its measured wall-clock time at the
published rates. Daytona can post a charge up to 48 hours after the usage,
so a report at settle time can be incomplete; the measured amount is the
floor. The charge never exceeds the reservation.
"""

from __future__ import annotations

import logging
import math
import os
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from .budget_amounts import cent_units
from .budgets import BudgetConflictError, BudgetError, OperationSnapshot
from .operation_pricing import (
    ChargePolicy,
    OperationCharge,
    OperationQuote,
    UnpricedOperationError,
    exact_nonnegative,
    json_fingerprint,
    operation_quote,
)
from .runtime import BudgetRuntime, OperationCompletedError, UsagePendingError
from .usage_tags import box_tag
from .vercel_usage import vercel_charge_policy

logger = logging.getLogger(__name__)

# Published pay-as-you-go rates, read on 2026-11-24 from
# https://www.daytona.io/pricing. Daytona bills each second. The page gives
# the first 5 GiB of storage free for the whole organization; a box cannot
# count on that share, so the full disk is charged.
_PRICING_URL = "https://www.daytona.io/pricing"
_PRICE_VERSION = "daytona-public-2026-11-24"
_VCPU_HOUR_USD = Decimal("0.0504")
_MEMORY_GIB_HOUR_USD = Decimal("0.0162")
_DISK_GIB_HOUR_USD = Decimal("0.000108")
_SECONDS_PER_HOUR = Decimal(3600)
# Per-sandbox usage periods with a ``price`` field, read on 2026-11-24 from
# https://www.daytona.io/docs/analytics-openapi.json.
_ANALYTICS_URL = "https://analytics.app.daytona.io"
_USAGE_TIMEOUT_SEC = 10.0
# The usage window starts a little before the create call and ends a little
# after the delete, so clock skew cannot cut a period off.
_WINDOW_MARGIN_SEC = 300


def _whole(value: Any) -> bool:
    """Report whether a value is a positive whole number and not a bool.

    Args:
        value: The value to check.

    Returns:
        True for a positive ``int``.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _usd(seconds: int, vcpus: int, memory_gib: int, disk_gib: int, rates: Mapping[str, Any]) -> Decimal:
    """Price a box of one size for a number of seconds.

    Args:
        seconds: Billed seconds.
        vcpus: CPU count.
        memory_gib: Memory in GiB.
        disk_gib: Disk in GiB.
        rates: Hourly USD rates with ``vcpu_hour_usd``, ``memory_gib_hour_usd`` and ``disk_gib_hour_usd``.

    Returns:
        The cost in USD.
    """
    hourly = (
        vcpus * exact_nonnegative(rates["vcpu_hour_usd"])
        + memory_gib * exact_nonnegative(rates["memory_gib_hour_usd"])
        + disk_gib * exact_nonnegative(rates["disk_gib_hour_usd"])
    )
    return Decimal(seconds) * hourly / _SECONDS_PER_HOUR


def _rates() -> dict[str, str]:
    """Return the published rates as snapshot text.

    Returns:
        The hourly USD rates.
    """
    return {
        "vcpu_hour_usd": str(_VCPU_HOUR_USD),
        "memory_gib_hour_usd": str(_MEMORY_GIB_HOUR_USD),
        "disk_gib_hour_usd": str(_DISK_GIB_HOUR_USD),
    }


def quote_daytona_sandbox(request: Mapping[str, Any]) -> OperationQuote:
    """Price one Daytona box for its whole funded lifetime.

    Args:
        request: Final create parameters with ``lifetime_ms`` (the longest the
            box can exist), ``vcpus``, ``memory_gib`` and ``disk_gib``.

    Returns:
        Coverage for the full size over the whole lifetime.

    Raises:
        UnpricedOperationError: When the lifetime or a size is missing or not a positive whole number.
    """
    sizes = [request.get(name) for name in ("lifetime_ms", "vcpus", "memory_gib", "disk_gib")]
    if not all(_whole(size) for size in sizes):
        raise UnpricedOperationError("Daytona requires a bounded lifetime and explicit CPU, memory and disk sizes.")
    lifetime_ms, vcpus, memory_gib, disk_gib = sizes
    rates = _rates()
    maximum = _usd(math.ceil(lifetime_ms / 1000), vcpus, memory_gib, disk_gib, rates)
    return operation_quote(
        request,
        maximum,
        ChargePolicy("sandbox"),
        {
            "provider": "daytona",
            "price_version": _PRICE_VERSION,
            "source": _PRICING_URL,
            "rates": rates,
            "billing_increment_ms": 1000,
            "request": dict(request),
        },
    )


def _rfc3339(epoch: float) -> str:
    """Format an epoch time for the analytics API.

    Args:
        epoch: Seconds since the epoch.

    Returns:
        The UTC time in RFC 3339 form.
    """
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _http_client() -> httpx.Client:
    """Create the HTTP client of the analytics API.

    Tests replace this function with a client over a fake transport.

    Returns:
        A new client.
    """
    return httpx.Client(base_url=_ANALYTICS_URL, timeout=_USAGE_TIMEOUT_SEC)


def _usage_periods(sandbox_id: str, start: float, end: float) -> list[Any] | None:
    """Read a box's usage periods from Daytona's analytics API.

    Args:
        sandbox_id: The Daytona box id.
        start: Epoch seconds at the window start.
        end: Epoch seconds at the window end.

    Returns:
        The decoded periods, or ``None`` when the API is not configured or
        the read fails.
    """
    organization = os.environ.get("DAYTONA_ORGANIZATION_ID")
    token = os.environ.get("DAYTONA_API_KEY") or os.environ.get("DAYTONA_JWT_TOKEN")
    if not organization or not token:
        return None
    try:
        with _http_client() as client:
            response = client.get(
                f"/organization/{organization}/sandbox/{sandbox_id}/usage",
                params={"from": _rfc3339(start), "to": _rfc3339(end)},
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            body = response.json()
    except (httpx.HTTPError, ValueError) as error:
        logger.warning("The sandbox usage read failed: %s", type(error).__name__)
        return None
    return body if isinstance(body, list) else None


def billed_usd(periods: list[Any], *, vcpus: int, memory_gib: int, disk_gib: int) -> Decimal | None:
    """Sum the price of a stopped box's usage periods.

    Args:
        periods: The periods the analytics API returned.
        vcpus: The admitted CPU count.
        memory_gib: The admitted memory in GiB.
        disk_gib: The admitted disk in GiB.

    Returns:
        The billed USD, or ``None`` when the report is empty, still open or
        malformed, so the caller uses measured time alone.

    Raises:
        UsagePendingError: When a period bills a size other than the admitted one.
    """
    if not periods:
        return None
    total = Decimal(0)
    for period in periods:
        if not isinstance(period, Mapping) or not period.get("endAt"):
            return None
        if (period.get("cpu"), period.get("ramGB"), period.get("diskGB")) != (vcpus, memory_gib, disk_gib):
            raise UsagePendingError("Cloud sandbox billed a size other than the admitted one.")
        price = period.get("price")
        if isinstance(price, bool) or not isinstance(price, int | float):
            return None
        try:
            total += exact_nonnegative(Decimal(str(price)))
        except (InvalidOperation, ValueError):
            return None
    return total


class DaytonaUsageReservation:
    """Own the coverage of one Daytona box from its create call to its settle."""

    def __init__(
        self,
        runtime: BudgetRuntime,
        request: Mapping[str, Any],
        *,
        operation_key: str,
        reserved: OperationSnapshot | None = None,
    ) -> None:
        """Reserve and claim one box before the create call.

        Args:
            runtime: Authenticated budget authority.
            request: Final create parameters that :func:`quote_daytona_sandbox` prices.
            operation_key: Stable logical identity of the box.
            reserved: A pool lease slice already reserved for this box.

        Raises:
            BudgetConflictError: When the lease slice was reserved for a different request.
            OperationCompletedError: When the operation already settled.
            UsagePendingError: When another caller already dispatched the box.
        """
        self.runtime = runtime
        self.request = dict(request)
        self.quote = quote_daytona_sandbox(request)
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
            # A refused claim means no box exists for this operation.
            runtime.unpin(self.operation.id)
            raise
        if not claimed.dispatch_claimed:
            raise UsagePendingError("This sandbox was already dispatched; reconcile its exact session.")
        # Measured from the claim, just before the create call, so measured
        # time is never shorter than the time Daytona bills.
        self.dispatched_at = time.time()
        self.sandbox_id: str | None = None

    def bind(self, sandbox: Any) -> None:
        """Record the box id, so the settle and the reconciler can find the box.

        A box that a timed-out or cancelled create made gets its id here,
        without the size check, before its settle.

        Args:
            sandbox: The Daytona box handle.
        """
        if self.sandbox_id == sandbox.id:
            return
        self.runtime.service.mark_dispatched(self.operation.id, self.runtime.username, sandbox.id)
        self.sandbox_id = sandbox.id

    def confirm_created(self, sandbox: Any) -> None:
        """Bind the created box and check that it has the funded size.

        Args:
            sandbox: The Daytona box handle.

        Raises:
            UsagePendingError: When the box size differs from the funded one.
        """
        self.bind(sandbox)
        funded = (self.request["vcpus"], self.request["memory_gib"], self.request["disk_gib"])
        if (sandbox.cpu, sandbox.memory, sandbox.disk) != funded:
            raise UsagePendingError("The cloud sandbox did not confirm the admitted CPU, memory and disk sizes.")

    def extend(self, lifetime_ms: int, *, deadline: float) -> None:
        """Fund a longer lifetime before the box gets it.

        Args:
            lifetime_ms: The box's total lifetime once extended.
            deadline: ``time.monotonic()`` instant after which waiting for in-flight work stops.

        Raises:
            ValueError: When the lifetime does not grow.
        """
        if lifetime_ms <= self.request["lifetime_ms"]:
            raise ValueError("An extension must lengthen the funded lifetime.")
        request = {**self.request, "lifetime_ms": lifetime_ms}
        quote = quote_daytona_sandbox(request)
        extended = self.runtime.extend(
            self.operation.id,
            quote,
            evidence_key=f"daytona-extend:{lifetime_ms}",
            evidence={"provider": "daytona", "lifetime_ms": lifetime_ms, "request": quote.request_fingerprint},
            deadline=deadline,
        )
        self.runtime.pin(self.operation.id, cent_units(extended.max_cents), box=self.box)
        self.request, self.quote = request, quote

    def pending(self) -> None:
        """Keep the coverage when the box's existence or its end is uncertain."""
        current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
        if current.state in {"dispatched", "pending"}:
            evidence = {"provider": "daytona", "sandbox_id": self.sandbox_id, "dispatched_at": self.dispatched_at}
            self.runtime.service.mark_pending(
                self.operation.id,
                self.runtime.username,
                evidence_key=json_fingerprint(evidence),
                evidence=evidence,
            )

    def fail(self, error: BaseException, *, refused: bool) -> None:
        """Release the coverage when Daytona refused the create call, otherwise keep it.

        Args:
            error: The failure of the create call.
            refused: True when Daytona answered that it made no box.
        """
        if refused and self.sandbox_id is None:
            try:
                self.runtime.service.reject(
                    self.operation.id,
                    self.runtime.username,
                    evidence_key=f"daytona-refusal:{self.operation.id}",
                    evidence={"provider": "daytona", "error": type(error).__name__},
                )
                self.runtime.unpin(self.operation.id)
                return
            except BudgetError:
                pass
        self.pending()

    def _charge(self, usd: Decimal) -> tuple[OperationCharge, bool]:
        """Convert USD to the charge, clamped to the reservation.

        Args:
            usd: The provider cost.

        Returns:
            The charge and whether the clamp lowered it.
        """
        charge = vercel_charge_policy(self.quote.price_snapshot).convert(usd)
        if charge.total > self.quote.maximum.total:
            return self.quote.maximum, True
        return charge, False

    def settle(self) -> None:
        """Settle the deleted box at the larger of its billed price and its measured time.

        Raises:
            UsagePendingError: When the box identity is unknown or the bill shows another size.
            BudgetError: When the evidence breaks the reserved bounds.
        """
        try:
            current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
            if current.state == "settled":
                self.runtime.unpin(self.operation.id)
                return
            if self.sandbox_id is None:
                raise UsagePendingError("Cloud sandbox creation has not returned an exact box identity.")
            ended = time.time()
            seconds = max(1, math.ceil(ended - self.dispatched_at))
            sizes = {name: self.request[name] for name in ("vcpus", "memory_gib", "disk_gib")}
            periods = _usage_periods(
                self.sandbox_id, self.dispatched_at - _WINDOW_MARGIN_SEC, ended + _WINDOW_MARGIN_SEC
            )
            billed = billed_usd(periods, **sizes) if periods is not None else None
            measured = _usd(seconds, **sizes, rates=self.quote.price_snapshot["rates"])
            # Daytona can post a charge up to 48 hours late, so a closed
            # report can still miss periods; never settle below measured time.
            usd = measured if billed is None else max(billed, measured)
            source = "usage-api" if billed is not None and billed >= measured else "measured"
            charge, clamped = self._charge(usd)
            self.runtime.service.settle(
                self.operation.id,
                self.runtime.username,
                evidence_key=f"daytona-delete:{self.sandbox_id}",
                actual_cents=charge.total,
                actual_wallet_cents=charge.wallet,
                evidence={
                    "provider": "daytona",
                    "sandbox_id": self.sandbox_id,
                    "source": source,
                    "measured_seconds": seconds,
                    "periods": periods if billed is not None else None,
                    "billed_usd": None if billed is None else str(billed),
                    "measured_usd": str(measured),
                    "provider_usd": str(usd),
                    "clamped": clamped,
                },
            )
            self.runtime.unpin(self.operation.id)
        except BaseException:
            self.pending()
            raise


class DaytonaUsage:
    """Meter Daytona boxes through the provider-neutral usage protocol."""

    name = "daytona"

    def quote(self, request: Mapping[str, Any]) -> OperationQuote:
        """Price one box before any provider resource exists.

        Args:
            request: Final create parameters.

        Returns:
            Coverage for the box's whole funded lifetime.
        """
        return quote_daytona_sandbox(request)

    def reserve(
        self,
        runtime: BudgetRuntime,
        request: Mapping[str, Any],
        *,
        operation_key: str,
        reserved: OperationSnapshot | None = None,
    ) -> DaytonaUsageReservation:
        """Reserve and claim one box, or claim a lease slice already reserved for it.

        Args:
            runtime: Authenticated budget authority.
            request: Final create parameters.
            operation_key: Stable logical identity of the box.
            reserved: Optional pool lease slice for this box.

        Returns:
            The claimed reservation that the box confirms, extends and settles.
        """
        return DaytonaUsageReservation(runtime, request, operation_key=operation_key, reserved=reserved)

    def confirm(self, reservation: DaytonaUsageReservation, session: Any) -> None:
        """Bind the created box and check its size.

        Args:
            reservation: The box's claimed reservation.
            session: The Daytona box handle.
        """
        reservation.confirm_created(session)

    def extend(self, reservation: DaytonaUsageReservation, lifetime_ms: int, *, deadline: float) -> None:
        """Fund a longer lifetime before the box gets it.

        Args:
            reservation: The box's claimed reservation.
            lifetime_ms: Total lifetime after the extension.
            deadline: ``time.monotonic()`` instant after which waiting stops.
        """
        reservation.extend(lifetime_ms, deadline=deadline)

    def settle(self, reservation: DaytonaUsageReservation) -> None:
        """Settle the deleted box.

        Args:
            reservation: The box's claimed reservation.
        """
        reservation.settle()


USAGE = DaytonaUsage()
