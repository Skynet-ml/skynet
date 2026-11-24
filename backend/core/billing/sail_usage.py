"""Reserve Sail sandbox compute and settle it from Sail's spend report (SKY-554).

Sail bills the CPU, memory and disk that a Sailbox uses, sampled each
second, and not its limits. The quote funds the limits of the box for its
whole lifetime, so it is a ceiling. After the box stops, the settle reads
the box's spend from ``GET /v1/sailboxes/spend``. Sail reports usage in
batches of about one minute, so the settle waits, then polls until two
reads agree. A box whose spend does not arrive stays pending. The settled
amount never passes the reservation.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

import httpx

from .budget_amounts import cent_units
from .budgets import BudgetConflictError, OperationSnapshot
from .operation_pricing import ChargePolicy, OperationQuote, UnpricedOperationError, exact_nonnegative, operation_quote
from .runtime import BudgetRuntime, OperationCompletedError, UsagePendingError
from .usage_tags import box_tag
from .vercel_usage import vercel_charge_policy

try:
    from sail import Config
except ImportError:
    Config = None

# Published usage rates, read on 2026-11-24 from
# https://docs.sailresearch.com/pricing.md and
# https://docs.sailresearch.com/sailboxes-billing.md. The creation charge
# applies on the Free plan only; Pro and Enterprise waive it.
_PRICING_URL = "https://docs.sailresearch.com/pricing.md"
_SPEND_URL = "https://docs.sailresearch.com/sailboxes-billing.md"
_PRICE_VERSION = "sail-public-2026-11-24"
_VCPU_HOUR_USD = Decimal("0.015")
_MEMORY_GIB_HOUR_USD = Decimal("0.008")
_DISK_GIB_HOUR_USD = Decimal("0.0007")
_CREATION_USD = Decimal("0.01")
_SECONDS_PER_HOUR = Decimal(3600)
_MS_PER_HOUR = Decimal(3_600_000)
_NANOS_PER_USD = Decimal(1_000_000_000)
# The size ladder of the sail SDK 0.17.2 core: vCPUs, then the default and
# the most memory and disk in GiB. Sail rounds a limit up to the next power
# of two. A create without a size gets "l". The ultra size is not funded.
_SIZES = {
    "s": (1, 16, 64, 32, 128),
    "m": (4, 32, 128, 128, 512),
    "l": (8, 64, 256, 256, 1024),
}
_DEFAULT_SIZE = "l"
# Skynet's own bound on one funded box, not a Sail limit.
_LIFETIME_CEILING_MS = 24 * 3_600_000
# Sail reports usage in batches of about one minute and shows spend "within
# a few minutes", so the settle waits before the first read and gives up
# after the deadline, which keeps the hold pending.
SPEND_DELAY_SECONDS = 90.0
SPEND_POLL_SECONDS = 30.0
SPEND_DEADLINE_SECONDS = 360.0
_SPEND_TIMEOUT_SECONDS = 30.0
# The spend window opens before the create, so clock drift between this
# worker and Sail cannot drop the box's first samples.
_WINDOW_MARGIN = dt.timedelta(minutes=10)
_SPEND_FIELDS = (
    "sailbox_id",
    "cost_usd_nanos",
    "running_seconds",
    "vcpu_seconds",
    "memory_gib_seconds",
    "state_disk_gib_seconds",
    "creation_count",
)


def _power_of_two(value: int) -> int:
    """Round a positive limit up to the next power of two, as Sail does.

    Args:
        value: The requested limit in GiB.

    Returns:
        The limit that Sail applies.
    """
    return 1 << (value - 1).bit_length()


def _limit(value: Any, default: int, maximum: int) -> int:
    """Return the funded memory or disk limit of a box.

    Args:
        value: The requested limit in GiB, or ``None`` for the size default.
        default: The size's default limit.
        maximum: The size's largest limit.

    Returns:
        The limit in GiB that Sail applies.

    Raises:
        UnpricedOperationError: When the limit is not a bounded whole number.
    """
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise UnpricedOperationError("Sail requires whole memory and disk limits within the box size.")
    return max(default, _power_of_two(value))


def sail_envelope(request: Mapping[str, Any]) -> tuple[int, int, int]:
    """Return the vCPUs, memory and disk that a create request can use.

    Args:
        request: The ``size``, ``memory_limit_gib`` and ``disk_limit_gib`` of the box.

    Returns:
        The vCPU count, and the memory and disk limits in GiB.

    Raises:
        UnpricedOperationError: When the size is not one that Skynet funds.
    """
    size = request.get("size") or _DEFAULT_SIZE
    if size not in _SIZES:
        raise UnpricedOperationError(f"Skynet does not fund the Sail size {size!r}.")
    vcpus, memory, memory_max, disk, disk_max = _SIZES[size]
    return (
        vcpus,
        _limit(request.get("memory_limit_gib"), memory, memory_max),
        _limit(request.get("disk_limit_gib"), disk, disk_max),
    )


def quote_sail_sandbox(request: Mapping[str, Any]) -> OperationQuote:
    """Price one box at its limits for its whole lifetime before it exists.

    Args:
        request: ``lifetime_ms`` and the size fields of the box, with its image
            and network for the fingerprint.

    Returns:
        Coverage for the creation charge and the box using all of its limits
        for the whole lifetime.

    Raises:
        UnpricedOperationError: When the lifetime or the size is not bounded.
    """
    lifetime_ms = request.get("lifetime_ms")
    if isinstance(lifetime_ms, bool) or not isinstance(lifetime_ms, int) or not 0 < lifetime_ms <= _LIFETIME_CEILING_MS:
        raise UnpricedOperationError("Sail requires a bounded box lifetime.")
    vcpus, memory_gib, disk_gib = sail_envelope(request)
    hourly = vcpus * _VCPU_HOUR_USD + memory_gib * _MEMORY_GIB_HOUR_USD + disk_gib * _DISK_GIB_HOUR_USD
    maximum = _CREATION_USD + Decimal(lifetime_ms) / _MS_PER_HOUR * hourly
    return operation_quote(
        request,
        maximum,
        ChargePolicy("sandbox"),
        {
            "provider": "sail",
            "price_version": _PRICE_VERSION,
            "source": _PRICING_URL,
            "vcpu_hour_usd": str(_VCPU_HOUR_USD),
            "memory_gib_hour_usd": str(_MEMORY_GIB_HOUR_USD),
            "disk_gib_hour_usd": str(_DISK_GIB_HOUR_USD),
            "creation_usd": str(_CREATION_USD),
            "envelope": {"vcpus": vcpus, "memory_gib": memory_gib, "disk_gib": disk_gib},
            "request": dict(request),
        },
    )


def fetch_sail_spend(sailbox_id: str, since: dt.datetime) -> Mapping[str, Any]:
    """Read the spend report of one Sailbox from the Sail API.

    Args:
        sailbox_id: The box to report.
        since: The start of the report window.

    Returns:
        The JSON spend report.

    Raises:
        UsagePendingError: When the SDK or the credentials are missing.
        httpx.HTTPError: When the call fails.
    """
    if Config is None:
        raise UsagePendingError("The sail package is not installed, so Sail spend cannot be read.")
    try:
        config = Config.from_env()
    except ValueError as error:
        raise UsagePendingError("Sail found no credentials to read the box spend.") from error
    response = httpx.get(
        f"{config.sailbox_api_url.rstrip('/')}/v1/sailboxes/spend",
        params={"sailbox_id": sailbox_id, "from": since.isoformat().replace("+00:00", "Z")},
        headers={"Authorization": f"Bearer {config.api_key}"},
        timeout=_SPEND_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def _box_spend(report: Mapping[str, Any], sailbox_id: str) -> dict[str, Any] | None:
    """Pick the line of one box from a spend report.

    Args:
        report: The JSON spend report.
        sailbox_id: The box to find.

    Returns:
        The box's spend fields with the report's pricing flags, or ``None``
        when the report does not list the box yet.
    """
    boxes = report.get("sailboxes")
    if not isinstance(boxes, list):
        return None
    for line in boxes:
        if isinstance(line, Mapping) and line.get("sailbox_id") == sailbox_id:
            spend = {name: line.get(name) for name in _SPEND_FIELDS}
            spend["pricing_configured"] = report.get("pricing_configured")
            spend["creation_fees_waived"] = report.get("creation_fees_waived")
            return spend
    return None


def sail_actual_usd(spend: Mapping[str, Any], price_snapshot: Mapping[str, Any]) -> Decimal:
    """Price one box from its spend line.

    Sail's own cost is used when Sail has prices set for the account.
    Otherwise the measured usage is priced at the admitted rates.

    Args:
        spend: The box's spend line with the report's pricing flags.
        price_snapshot: The admitted quote.

    Returns:
        The box cost in USD.

    Raises:
        UsagePendingError: When the line or the snapshot has no usable numbers.
    """
    try:
        if spend.get("pricing_configured") is True:
            return exact_nonnegative(spend["cost_usd_nanos"]) / _NANOS_PER_USD
        usage = (
            exact_nonnegative(spend["vcpu_seconds"]) * exact_nonnegative(price_snapshot["vcpu_hour_usd"])
            + exact_nonnegative(spend["memory_gib_seconds"]) * exact_nonnegative(price_snapshot["memory_gib_hour_usd"])
            + exact_nonnegative(spend["state_disk_gib_seconds"])
            * exact_nonnegative(price_snapshot["disk_gib_hour_usd"])
        ) / _SECONDS_PER_HOUR
        creation = Decimal(0)
        if spend.get("creation_fees_waived") is not True:
            creation = exact_nonnegative(spend["creation_count"]) * exact_nonnegative(price_snapshot["creation_usd"])
        return usage + creation
    except (KeyError, TypeError, ValueError) as error:
        raise UsagePendingError("The Sail spend report has no usable cost for the box.") from error


class SailUsageReservation:
    """Own the budget coverage of one Sailbox and its spend evidence."""

    def __init__(
        self,
        runtime: BudgetRuntime,
        request: Mapping[str, Any],
        *,
        operation_key: str,
        reserved: OperationSnapshot | None = None,
        fetch: Callable[[str, dt.datetime], Mapping[str, Any]] = fetch_sail_spend,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Reserve and claim one box before any network call.

        Args:
            runtime: Authenticated budget authority, outside the guest.
            request: The parameters that :func:`quote_sail_sandbox` prices.
            operation_key: Stable logical identity of the box.
            reserved: A pool lease slice already reserved for this box.
            fetch: Reads the spend report of one box.
            sleep: Waits between spend reads.
            clock: Monotonic clock for the spend deadline.

        Raises:
            BudgetConflictError: When the lease slice was reserved for another request.
            OperationCompletedError: When the box operation already settled.
            UsagePendingError: When another caller already dispatched the box.
        """
        self.runtime = runtime
        self.request = dict(request)
        self.lifetime_ms = request["lifetime_ms"]
        self.quote = quote_sail_sandbox(request)
        self.box = box_tag(operation_key)
        self._fetch = fetch
        self._sleep = sleep
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
            # A refused claim means no box exists for this operation, so a kept
            # pin would count coverage this runtime no longer holds.
            runtime.unpin(self.operation.id)
            raise
        if not claimed.dispatch_claimed:
            raise UsagePendingError("This sandbox was already dispatched; reconcile it by hand.")
        self.sailbox_id: str | None = None
        self.create_sent_at: dt.datetime | None = None
        self.spend: dict[str, Any] | None = None

    def begin_create(self) -> None:
        """Record that the create call may now open a box."""
        self.create_sent_at = dt.datetime.now(dt.UTC)

    def bind(self, sailbox_id: str) -> None:
        """Record the identity of the created box.

        Args:
            sailbox_id: The Sail id of the box.
        """
        self.sailbox_id = sailbox_id
        self.runtime.service.mark_dispatched(self.operation.id, self.runtime.username, sailbox_id)

    def confirm_created(self, sailbox: Any) -> None:
        """Bind the created box and check that it fits its funded limits.

        Args:
            sailbox: The full SDK ``Sailbox`` from ``Sailbox.get``.

        Raises:
            UsagePendingError: When Sail did not confirm limits inside the funding.
        """
        self.bind(sailbox.sailbox_id)
        vcpus, memory_gib, disk_gib = sail_envelope(self.request)
        if (
            sailbox.vcpu_count is None
            or sailbox.memory_mib is None
            or sailbox.state_disk_size_gib is None
            or sailbox.vcpu_count > vcpus
            or sailbox.memory_mib > memory_gib * 1024
            or sailbox.state_disk_size_gib > disk_gib
        ):
            raise UsagePendingError("The Sailbox did not confirm CPU, memory and disk limits inside its funding.")

    def extend(self, lifetime_ms: int, *, deadline: float) -> None:
        """Fund the box up to ``lifetime_ms`` before Sail keeps it alive that long.

        Args:
            lifetime_ms: Total lifetime once extended.
            deadline: ``time.monotonic()`` instant after which waiting for in-flight work stops.

        Raises:
            ValueError: When the lifetime does not grow.
            UnpricedOperationError: When the extended lifetime cannot be bounded.
            UsagePendingError: When covered work did not settle before ``deadline``.
            BudgetReached: When the budget cannot pay for the extension.
        """
        if lifetime_ms <= self.lifetime_ms:
            raise ValueError("An extension must lengthen the funded lifetime.")
        quote = quote_sail_sandbox({**self.request, "lifetime_ms": lifetime_ms})
        self.operation = self.runtime.extend(
            self.operation.id,
            quote,
            evidence_key=f"sail-extend:{lifetime_ms}",
            evidence={"provider": "sail", "lifetime_ms": lifetime_ms, "request": quote.request_fingerprint},
            deadline=deadline,
        )
        self.runtime.pin(self.operation.id, cent_units(self.operation.max_cents), box=self.box)
        self.lifetime_ms = lifetime_ms

    def pending(self) -> None:
        """Keep the coverage pending for manual reconciliation."""
        current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
        if current.state in {"dispatched", "pending"}:
            self.runtime.service.mark_pending(
                self.operation.id,
                self.runtime.username,
                evidence_key=f"sail-pending:{self.operation.id}",
                evidence={"provider": "sail", "sailbox_id": self.sailbox_id, "spend": self.spend},
            )

    def release(self) -> None:
        """Return the hold when no create call went out, otherwise keep it pending.

        A box can exist once the create call is sent, even when its handle
        never arrived, so only a box that was never requested is released.
        """
        if self.create_sent_at is not None or self.sailbox_id is not None:
            self.pending()
            return
        self.runtime.service.reject(
            self.operation.id,
            self.runtime.username,
            evidence_key=f"sail-unsent:{self.operation.id}",
            evidence={"provider": "sail", "reason": "the create call was never sent"},
        )
        self.runtime.unpin(self.operation.id)

    def _final_spend(self) -> dict[str, Any]:
        """Poll the spend report until the box's line stops changing.

        Returns:
            The box's final spend line.

        Raises:
            UsagePendingError: When the line does not settle before the deadline.
        """
        since = self.create_sent_at - _WINDOW_MARGIN
        give_up = self._clock() + SPEND_DEADLINE_SECONDS
        self._sleep(SPEND_DELAY_SECONDS)
        previous = None
        while True:
            line = _box_spend(self._fetch(self.sailbox_id, since), self.sailbox_id)
            if line is not None:
                self.spend = line
                if line == previous:
                    return line
            previous = line
            if self._clock() + SPEND_POLL_SECONDS > give_up:
                raise UsagePendingError("Sail did not report a final spend for the box in time.")
            self._sleep(SPEND_POLL_SECONDS)

    def settle(self) -> None:
        """Settle the stopped box at its reported spend, inside the reservation.

        Raises:
            UsagePendingError: When the spend is missing, late or unusable.
            BudgetError: When the ledger refuses the settlement.
            httpx.HTTPError: When the spend report cannot be read.
        """
        try:
            current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
            if current.state == "settled":
                self.runtime.unpin(self.operation.id)
                return
            if self.sailbox_id is None or self.create_sent_at is None:
                raise UsagePendingError("The Sail create returned no box identity to settle.")
            spend = self._final_spend()
            usd = sail_actual_usd(spend, self.quote.price_snapshot)
            charge = vercel_charge_policy(self.quote.price_snapshot).convert(usd)
            # Sail's per-second samples and its own rates can pass the funded
            # ceiling by a little; the charge must stay inside the reservation.
            total = min(charge.total, current.max_cents)
            wallet = min(charge.wallet, current.max_wallet_cents, total)
            self.runtime.service.settle(
                self.operation.id,
                self.runtime.username,
                evidence_key=f"sail-spend:{self.sailbox_id}",
                actual_cents=total,
                actual_wallet_cents=wallet,
                evidence={"provider": "sail", "source": _SPEND_URL, "spend": spend, "provider_usd": str(usd)},
            )
            self.runtime.unpin(self.operation.id)
        except BaseException:
            self.pending()
            raise


class SailUsage:
    """Meter Sail sandboxes through the provider-neutral usage protocol."""

    name = "sail"

    def quote(self, request: Mapping[str, Any]) -> OperationQuote:
        """Price one box before any provider resource exists.

        Args:
            request: Final create parameters.

        Returns:
            Coverage for the box's whole funded lifetime.
        """
        return quote_sail_sandbox(request)

    def reserve(
        self,
        runtime: BudgetRuntime,
        request: Mapping[str, Any],
        *,
        operation_key: str,
        reserved: OperationSnapshot | None = None,
    ) -> SailUsageReservation:
        """Reserve and claim one box, or claim a lease slice already reserved for it.

        Args:
            runtime: Authenticated budget authority.
            request: Final create parameters.
            operation_key: Stable logical identity of the box.
            reserved: Optional pool lease slice for this box.

        Returns:
            The claimed reservation that the box confirms, extends and settles.
        """
        return SailUsageReservation(runtime, request, operation_key=operation_key, reserved=reserved)

    def confirm(self, reservation: SailUsageReservation, session: Any) -> None:
        """Check that the created box fits its funded limits.

        Args:
            reservation: The box's claimed reservation.
            session: The full SDK ``Sailbox``.
        """
        reservation.confirm_created(session)

    def extend(self, reservation: SailUsageReservation, lifetime_ms: int, *, deadline: float) -> None:
        """Fund a longer box lifetime before Sail grants it.

        Args:
            reservation: The box's claimed reservation.
            lifetime_ms: Total lifetime after the extension.
            deadline: ``time.monotonic()`` instant after which waiting stops.
        """
        reservation.extend(lifetime_ms, deadline=deadline)

    def settle(self, reservation: SailUsageReservation) -> None:
        """Settle the stopped box at its reported spend.

        Args:
            reservation: The box's claimed reservation.
        """
        reservation.settle()


USAGE = SailUsage()
