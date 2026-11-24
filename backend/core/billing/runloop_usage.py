"""Reserve Runloop devbox compute and settle it from Runloop's own usage counters.

A devbox reserves its whole keep-alive lifetime at the published rates before
the create call. After the box stops, it settles at the vCPU, memory and disk
seconds that Runloop's ``GET /v1/devboxes/{id}/usage`` reports. When that
report is missing or not final, it settles from the measured time between the
create call and the confirmed shutdown, which bounds the billed time from
above. With neither, the hold stays pending for reconciliation instead of
charging the whole keep-alive. The create is checked against the funded size
and keep-alive; measured time of a box of another size is priced at the
larger of the two sizes. Every settlement stays inside the reservation.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from .budget_amounts import cent_units
from .budgets import BudgetConflictError, OperationSnapshot
from .operation_pricing import ChargePolicy, OperationQuote, UnpricedOperationError, json_fingerprint, operation_quote
from .runtime import BudgetRuntime, OperationCompletedError, UsagePendingError
from .usage_tags import box_tag
from .vercel_usage import vercel_charge_policy

logger = logging.getLogger(__name__)

# Published usage rates, read on 2026-11-24 from https://www.runloop.ai/pricing.
# The page bills usage per second with no minimum, and its one usage table
# applies to every plan. It publishes no creation fee and no network price.
_PRICING_URL = "https://www.runloop.ai/pricing"
# Operation getDevboxResourceUsage, GET /v1/devboxes/{id}/usage (read 2026-11-24).
_USAGE_API_URL = "https://docs.runloop.ai/openapi-specs/stainless-processed-openapi.json"
_PRICE_VERSION = "runloop-public-2026-11-24"
_CPU_HOUR_USD = Decimal("0.108")
_MEMORY_GB_HOUR_USD = Decimal("0.0252")
_DISK_GB_HOUR_USD = Decimal("0.00034236")
# https://docs.runloop.ai/docs/devboxes/configuration/sizes (read 2026-11-24):
# a custom size with no disk gets 16 GiB.
DEFAULT_DISK_GB = 16
# Runloop's keep_alive_time_seconds limit: 48 hours.
_LIFETIME_CEILING_MS = 172_800_000
_SECONDS_PER_HOUR = Decimal(3600)
_MS_PER_HOUR = Decimal(3_600_000)
_FINAL_STATUSES = frozenset({"failure", "shutdown"})
_USAGE_FIELDS = (
    "id",
    "status",
    "start_time_ms",
    "end_time_ms",
    "total_active_seconds",
    "total_elapsed_seconds",
    "vcpu_seconds",
    "memory_gb_seconds",
    "disk_gb_seconds",
)


def _size(request: Mapping[str, Any]) -> tuple[int, int, int]:
    """Read the funded devbox size from a create request.

    Args:
        request: Final create parameters.

    Returns:
        The vCPUs, memory GiB and disk GiB.

    Raises:
        UnpricedOperationError: When the size is missing or not a Runloop custom size.
    """
    vcpus, memory, disk = request.get("vcpus"), request.get("memory_gb"), request.get("disk_gb")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (vcpus, memory, disk)):
        raise UnpricedOperationError("Runloop requires an explicit vCPU, memory and disk size.")
    if (
        vcpus not in {1, *range(2, 17, 2)}
        or not 2 * vcpus <= memory <= min(64, 8 * vcpus)
        or (memory != 1 and memory % 2)
        or not 2 <= disk <= 64
        or disk % 2
    ):
        raise UnpricedOperationError("Runloop requires a supported custom devbox size.")
    return vcpus, memory, disk


def _hourly_usd(vcpus: int, memory_gb: int, disk_gb: int) -> Decimal:
    """Return the price of one hour of a running devbox of this size.

    Args:
        vcpus: CPU allocation.
        memory_gb: Memory in GiB.
        disk_gb: Disk in GiB.

    Returns:
        The hourly price in USD.
    """
    return vcpus * _CPU_HOUR_USD + memory_gb * _MEMORY_GB_HOUR_USD + disk_gb * _DISK_GB_HOUR_USD


def _launch_sizes(launch: Any) -> dict[str, int] | None:
    """Read the size and keep-alive that Runloop gave a devbox.

    Args:
        launch: The ``launch_parameters`` of the SDK ``DevboxView``, or a
            mapping of its fields.

    Returns:
        The vCPUs, memory GiB, disk GiB and keep-alive seconds, or ``None``
        when a field is missing or not a whole number.
    """
    names = ("custom_cpu_cores", "custom_gb_memory", "custom_disk_size", "keep_alive_time_seconds")
    if isinstance(launch, Mapping):
        values = [launch.get(name) for name in names]
    else:
        values = [getattr(launch, name, None) for name in names]
    if values[2] is None:
        # Runloop gives a custom size with no disk its default disk.
        values[2] = DEFAULT_DISK_GB
    if any(
        isinstance(value, bool) or not isinstance(value, int | float) or value < 0 or not float(value).is_integer()
        for value in values
    ):
        return None
    return dict(zip(("vcpus", "memory_gb", "disk_gb", "keep_alive_seconds"), map(int, values), strict=True))


def quote_runloop_devbox(request: Mapping[str, Any]) -> OperationQuote:
    """Price a devbox for its whole keep-alive lifetime before any provider call.

    Args:
        request: Final create parameters with ``lifetime_ms``, ``vcpus``,
            ``memory_gb`` and ``disk_gb``.

    Returns:
        Coverage for the devbox running at its full size for its whole lifetime.

    Raises:
        UnpricedOperationError: When the lifetime or the size is unbounded.
    """
    lifetime_ms = request.get("lifetime_ms")
    if isinstance(lifetime_ms, bool) or not isinstance(lifetime_ms, int) or not 0 < lifetime_ms <= _LIFETIME_CEILING_MS:
        raise UnpricedOperationError("Runloop requires a bounded keep-alive lifetime.")
    vcpus, memory, disk = _size(request)
    maximum = Decimal(lifetime_ms) * _hourly_usd(vcpus, memory, disk) / _MS_PER_HOUR
    return operation_quote(
        request,
        maximum,
        ChargePolicy("sandbox"),
        {
            "provider": "runloop",
            "price_version": _PRICE_VERSION,
            "source": _PRICING_URL,
            "cpu_hour_usd": str(_CPU_HOUR_USD),
            "memory_gb_hour_usd": str(_MEMORY_GB_HOUR_USD),
            "disk_gb_hour_usd": str(_DISK_GB_HOUR_USD),
            "request": dict(request),
        },
    )


def _counter(value: Any, name: str) -> int:
    """Require an explicit nonnegative whole-number usage counter.

    Args:
        value: Counter from Runloop's usage report.
        name: Field name, to explain incomplete evidence.

    Returns:
        The counter.

    Raises:
        UsagePendingError: When the counter is missing or invalid.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise UsagePendingError(f"Runloop usage is awaiting a valid {name} counter.")
    return value


def runloop_reported_usd(usage: Mapping[str, Any], *, devbox_id: str, price_snapshot: Mapping[str, Any]) -> Decimal:
    """Price a stopped devbox from Runloop's own usage report.

    Args:
        usage: The ``DevboxResourceUsageView`` fields of the devbox.
        devbox_id: The one admitted devbox.
        price_snapshot: The admitted quote's rates.

    Returns:
        vCPU, memory and disk seconds at the admitted rates.

    Raises:
        UsagePendingError: When the report is for another box, not final, or incomplete.
    """
    if usage.get("id") != devbox_id or usage.get("status") not in _FINAL_STATUSES or usage.get("end_time_ms") is None:
        raise UsagePendingError("Runloop has not reported final usage for the admitted devbox.")
    cpu = _counter(usage.get("vcpu_seconds"), "vcpu_seconds")
    memory = _counter(usage.get("memory_gb_seconds"), "memory_gb_seconds")
    disk = _counter(usage.get("disk_gb_seconds"), "disk_gb_seconds")
    cpu_rate, memory_rate, disk_rate = _rates(price_snapshot)
    return (cpu * cpu_rate + memory * memory_rate + disk * disk_rate) / _SECONDS_PER_HOUR


def _rates(price_snapshot: Mapping[str, Any]) -> tuple[Decimal, Decimal, Decimal]:
    """Read the admitted hourly rates.

    Args:
        price_snapshot: The admitted quote.

    Returns:
        The CPU, memory and disk hourly rates in USD.

    Raises:
        UsagePendingError: When the snapshot is not a usable Runloop quote.
    """
    try:
        if price_snapshot.get("provider") != "runloop":
            raise ValueError("Unsupported billing policy")
        return (
            Decimal(str(price_snapshot["cpu_hour_usd"])),
            Decimal(str(price_snapshot["memory_gb_hour_usd"])),
            Decimal(str(price_snapshot["disk_gb_hour_usd"])),
        )
    except (KeyError, TypeError, ValueError, ArithmeticError) as error:
        raise UsagePendingError("The Runloop admission has no usable price snapshot.") from error


class RunloopUsageReservation:
    """Own the budget hold and the usage evidence of one Runloop devbox."""

    def __init__(
        self,
        runtime: BudgetRuntime,
        request: Mapping[str, Any],
        *,
        operation_key: str,
        reserved: OperationSnapshot | None = None,
    ) -> None:
        """Reserve and claim one devbox before the create call.

        Args:
            runtime: Authenticated budget authority, outside the guest.
            request: Final create parameters that :func:`quote_runloop_devbox` prices.
            operation_key: Stable logical identity of the devbox.
            reserved: A pool lease slice already reserved for this box.

        Raises:
            BudgetConflictError: When the lease slice was reserved for another request.
            OperationCompletedError: When this operation already settled.
            UsagePendingError: When another caller already dispatched it.
        """
        self.runtime = runtime
        self.request = dict(request)
        # The quote checks the lifetime and size, so read them only after it.
        self.quote = quote_runloop_devbox(request)
        self.vcpus, self.memory_gb, self.disk_gb = _size(request)
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
            # A refused claim means no devbox exists for this operation.
            runtime.unpin(self.operation.id)
            raise
        if not claimed.dispatch_claimed:
            raise UsagePendingError("This sandbox was already dispatched; reconcile its exact devbox.")
        self.devbox_id: str | None = None
        self.actual_sizes: dict[str, int] | None = None
        self.usage: dict[str, Any] | None = None
        self.measured_ms: int | None = None

    def bind(self, devbox_id: str) -> None:
        """Record the provider identity of the created devbox.

        Args:
            devbox_id: The Runloop devbox id.

        Raises:
            UsagePendingError: When the create returned another devbox than the one already bound.
        """
        if self.devbox_id not in {None, devbox_id}:
            raise UsagePendingError("Runloop returned a second devbox for one admitted operation.")
        self.runtime.service.mark_dispatched(self.operation.id, self.runtime.username, devbox_id)
        self.devbox_id = devbox_id

    def confirm(self, devbox_id: str, launch: Any) -> None:
        """Bind the created devbox and check that it has the funded size and keep-alive.

        The settle of a box of another size prices measured time at the
        larger sizes, so the caller stops the box and settles it as usual.

        Args:
            devbox_id: The Runloop devbox id.
            launch: The ``launch_parameters`` of the created devbox.

        Raises:
            UsagePendingError: When the size or the keep-alive differs from
                the funded one, or cannot be read.
        """
        self.bind(devbox_id)
        sizes = _launch_sizes(launch)
        if sizes is None:
            raise UsagePendingError("Runloop did not report the size of the admitted devbox.")
        self.actual_sizes = sizes
        funded = (self.vcpus, self.memory_gb, self.disk_gb)
        if (sizes["vcpus"], sizes["memory_gb"], sizes["disk_gb"]) != funded or (
            sizes["keep_alive_seconds"] * 1000 > self.lifetime_ms
        ):
            raise UsagePendingError("Runloop did not confirm the admitted size and keep-alive of the devbox.")

    def record_usage(self, usage: Any) -> None:
        """Keep Runloop's usage report of the stopped devbox.

        Args:
            usage: The SDK ``DevboxResourceUsageView``, or a mapping of its fields.
        """
        if isinstance(usage, Mapping):
            self.usage = {name: usage.get(name) for name in _USAGE_FIELDS}
        else:
            self.usage = {name: getattr(usage, name, None) for name in _USAGE_FIELDS}

    def record_stop(self, measured_ms: int) -> None:
        """Keep the measured time from the create call to the confirmed shutdown.

        Args:
            measured_ms: Wall time in milliseconds.
        """
        self.measured_ms = max(0, measured_ms)

    def extend(self, lifetime_ms: int, *, deadline: float) -> None:
        """Fund a longer keep-alive before Runloop is asked for it.

        Args:
            lifetime_ms: The devbox's total lifetime once extended.
            deadline: ``time.monotonic()`` instant after which waiting for in-flight work stops.

        Raises:
            ValueError: When the lifetime does not grow.
            UnpricedOperationError: When the new lifetime has no price.
            UsagePendingError: When covered work did not settle before ``deadline``.
            BudgetReached: When the budget cannot pay for the extension.
        """
        if lifetime_ms <= self.lifetime_ms:
            raise ValueError("An extension must lengthen the funded lifetime.")
        quote = quote_runloop_devbox({**self.request, "lifetime_ms": lifetime_ms})
        self.operation = self.runtime.extend(
            self.operation.id,
            quote,
            evidence_key=f"runloop-extend:{lifetime_ms}",
            evidence={"provider": "runloop", "lifetime_ms": lifetime_ms, "request": quote.request_fingerprint},
            deadline=deadline,
        )
        self.runtime.pin(self.operation.id, cent_units(self.operation.max_cents), box=self.box)
        self.lifetime_ms = lifetime_ms

    def release(self, reason: str) -> None:
        """Release the hold of a devbox that Runloop never created.

        The provider calls it only when the create call was never sent, or
        when Runloop answered the create POST with a status in its
        ``_REFUSED_STATUSES`` and a clean tag search then found no devbox.
        Any other failure keeps the hold pending.

        Args:
            reason: Why no devbox exists, for the evidence.
        """
        try:
            self.runtime.service.reject(
                self.operation.id,
                self.runtime.username,
                evidence_key=f"runloop-not-created:{self.operation.id}",
                evidence={"provider": "runloop", "reason": reason[:1000]},
            )
        except BaseException:
            self._keep_pending()
            raise
        self.runtime.unpin(self.operation.id)

    def _usd(self) -> tuple[Decimal, str]:
        """Price the stopped devbox from the best evidence it has.

        Returns:
            The provider cost in USD and the evidence it came from.

        Raises:
            UsagePendingError: When there is no final report and no confirmed
                shutdown, or the admitted price snapshot is unusable.
        """
        if self.usage is not None and self.devbox_id is not None:
            try:
                usd = runloop_reported_usd(
                    self.usage, devbox_id=self.devbox_id, price_snapshot=self.quote.price_snapshot
                )
                return usd, "usage_api"
            except UsagePendingError:
                pass
        if self.measured_ms is not None:
            cpu_rate, memory_rate, disk_rate = _rates(self.quote.price_snapshot)
            actual = self.actual_sizes or {}
            # A box of another size than funded is priced at the larger size;
            # the settle clamps the charge to the reservation.
            vcpus = max(self.vcpus, actual.get("vcpus", 0))
            memory = max(self.memory_gb, actual.get("memory_gb", 0))
            disk = max(self.disk_gb, actual.get("disk_gb", 0))
            hourly = vcpus * cpu_rate + memory * memory_rate + disk * disk_rate
            seconds = math.ceil(self.measured_ms / 1000)
            return Decimal(seconds) * hourly / _SECONDS_PER_HOUR, "measured"
        # Charging the whole keep-alive here would bill a box that ran for a
        # minute as if it ran for days, so the hold waits for reconciliation.
        raise UsagePendingError("Runloop reported no final usage and the shutdown is not confirmed.")

    def _keep_pending(self) -> None:
        """Mark the hold pending after a failure, without hiding that failure."""
        try:
            self.pending()
        except Exception as error:
            # The caller re-raises the first failure; a ledger error here
            # would replace it with a less useful one.
            logger.warning("Runloop hold %s could not be marked pending: %s", self.operation.id, type(error).__name__)

    def pending(self) -> None:
        """Keep the hold for manual reconciliation when settlement fails."""
        current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
        if current.state in {"dispatched", "pending"}:
            evidence = {"provider": "runloop", "devbox_id": self.devbox_id, "usage": self.usage}
            self.runtime.service.mark_pending(
                self.operation.id,
                self.runtime.username,
                evidence_key=json_fingerprint(evidence),
                evidence=evidence,
            )

    def settle(self) -> None:
        """Settle the stopped devbox, never above its reservation.

        Raises:
            UsagePendingError: When there is no usable evidence or the admitted
                price snapshot is unusable.
            BudgetError: When the ledger refuses the settlement.
        """
        try:
            current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
            if current.state == "settled":
                self.runtime.unpin(self.operation.id)
                return
            usd, basis = self._usd()
            charge = vercel_charge_policy(self.quote.price_snapshot).convert(usd)
            # Runloop may count boot or disk time past the keep-alive; Skynet
            # absorbs that rather than charge beyond what the user funded.
            total = min(charge.total, current.max_cents)
            wallet = min(charge.wallet, current.max_wallet_cents, total)
            self.runtime.service.settle(
                self.operation.id,
                self.runtime.username,
                evidence_key=f"runloop-stop:{self.devbox_id or self.operation.id}",
                actual_cents=total,
                actual_wallet_cents=wallet,
                evidence={
                    "provider": "runloop",
                    "source": _USAGE_API_URL if basis == "usage_api" else _PRICING_URL,
                    "basis": basis,
                    "devbox_id": self.devbox_id,
                    "usage": self.usage,
                    "measured_ms": self.measured_ms,
                    "actual_sizes": self.actual_sizes,
                    "provider_usd": str(usd),
                },
            )
            self.runtime.unpin(self.operation.id)
        except BaseException:
            self._keep_pending()
            raise


class RunloopUsage:
    """Meter Runloop devboxes through the provider-neutral usage protocol."""

    name = "runloop"

    def quote(self, request: Mapping[str, Any]) -> OperationQuote:
        """Price one devbox before any provider resource exists.

        Args:
            request: Final create parameters.

        Returns:
            Coverage for the devbox's whole keep-alive lifetime.
        """
        return quote_runloop_devbox(request)

    def reserve(
        self,
        runtime: BudgetRuntime,
        request: Mapping[str, Any],
        *,
        operation_key: str,
        reserved: OperationSnapshot | None = None,
    ) -> RunloopUsageReservation:
        """Reserve and claim one devbox, or claim a lease slice already reserved for it.

        Args:
            runtime: Authenticated budget authority.
            request: Final create parameters.
            operation_key: Stable logical identity of the devbox.
            reserved: Optional pool lease slice for this devbox.

        Returns:
            The claimed reservation.
        """
        return RunloopUsageReservation(runtime, request, operation_key=operation_key, reserved=reserved)

    def confirm(self, reservation: RunloopUsageReservation, session: Any) -> None:
        """Bind the created devbox and check its size and keep-alive.

        Args:
            reservation: The devbox's claimed reservation.
            session: The SDK ``DevboxView`` of the created devbox.
        """
        reservation.confirm(session.id, session.launch_parameters)

    def extend(self, reservation: RunloopUsageReservation, lifetime_ms: int, *, deadline: float) -> None:
        """Fund a longer keep-alive before Runloop grants it.

        Args:
            reservation: The devbox's claimed reservation.
            lifetime_ms: Total lifetime after the extension.
            deadline: ``time.monotonic()`` instant after which waiting stops.
        """
        reservation.extend(lifetime_ms, deadline=deadline)

    def settle(self, reservation: RunloopUsageReservation) -> None:
        """Settle the stopped devbox at its reported or measured cost.

        Args:
            reservation: The devbox's claimed reservation.
        """
        reservation.settle()


USAGE = RunloopUsage()
