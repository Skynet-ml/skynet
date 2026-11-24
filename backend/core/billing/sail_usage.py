"""Reserve Sail sandbox compute and settle it from Sail's spend report (SKY-554).

Sail bills the CPU, memory and disk that a Sailbox uses, sampled each
second, and not its limits. The quote funds the limits of the box for its
whole lifetime, so it is a ceiling. After the box stops, the settle reads
the box's spend from ``GET /v1/sailboxes/spend``. Sail reports usage in
batches of about one minute, so the settle waits, then polls until the
box's line stays the same for at least a minute. The waits are async, so a
settle holds no worker thread. A box whose spend does not arrive stays
pending. The settled amount never passes the reservation.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import email.utils
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
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

logger = logging.getLogger(__name__)

# Published usage rates, read on 2026-11-24 from
# https://docs.sailresearch.com/pricing.md and
# https://docs.sailresearch.com/sailboxes-billing.md. The creation charge
# applies on the Free plan only; Pro and Enterprise waive it. The spend API
# has a creation rate for each size (``rates.<size>_creation_usd_nanos``),
# but the pricing page publishes only one fee, so the quote and the measured
# settle use that fee. Without account prices the reported rates are likely
# zero, so they would drop the creation charge. With account prices, the
# settle recomputes the cost from the measured seconds and Sail's rates; see
# ``sail_actual_usd``. The rates go into the settle evidence for audit.
_PRICING_URL = "https://docs.sailresearch.com/pricing.md"
_SPEND_URL = "https://docs.sailresearch.com/api-reference/usage/get-sailbox-spend.md"
_PRICE_VERSION = "sail-public-2026-11-24"
_VCPU_HOUR_USD = Decimal("0.015")
_MEMORY_GIB_HOUR_USD = Decimal("0.008")
_DISK_GIB_HOUR_USD = Decimal("0.0007")
_CREATION_USD = Decimal("0.01")
_SECONDS_PER_HOUR = Decimal(3600)
_MS_PER_HOUR = Decimal(3_600_000)
_NANOS_PER_USD = Decimal(1_000_000_000)
# The spend report's per-second rates for each measured counter.
_SAIL_SECOND_RATES = {
    "vcpu_seconds": "vcpu_second_usd_nanos",
    "memory_gib_seconds": "memory_gib_second_usd_nanos",
    "state_disk_gib_seconds": "state_disk_gib_second_usd_nanos",
}
# The size ladder of the sail SDK 0.17.2 core, the version that uv.lock
# pins: vCPUs, then the default and the most memory and disk in GiB. The
# public docs do not list it; it comes from the size checks in that SDK's
# compiled core (``validate_size_limits``), so it can drift from the server.
# That is why ``confirm_created`` compares the created box's real limits
# with its funding and refuses a larger box. Sail rounds a limit up to the
# next power of two. A create without a size gets "l". A size that is not in
# this table (ultra, or a size of a newer SDK) raises UnpricedOperationError
# at the reserve, before the create call.
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
# after the deadline, which keeps the hold pending with the last line read.
# The deadline counts from the first wait. The real bound is the provider's
# ``TEARDOWN_SECONDS`` (``core/sandbox/providers/sail.py``): the teardown
# passes the time it has left as the ``final_spend`` budget, so the spend
# wait ends inside the 240 s that a session close gives one stop.
SPEND_DELAY_SECONDS = 60.0
SPEND_POLL_SECONDS = 15.0
SPEND_DEADLINE_SECONDS = 150.0
# Two equal reads close together can both come before the last usage
# batch, so a line counts as final only after it stays the same this long.
SPEND_AGREE_SECONDS = 60.0
# Sail documents 429 (the request never started) and 503 as retryable. A
# 5xx answer is a server fault, not a refusal, so it is read again too.
_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})
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


def _retry_after(response: httpx.Response) -> float | None:
    """Read the wait that a ``Retry-After`` header asks for.

    Args:
        response: The refused spend response.

    Returns:
        The wait in seconds, or ``None`` when the header is missing or unreadable.
    """
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    return max(0.0, (when - dt.datetime.now(dt.UTC)).total_seconds())


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
    # A whole float such as 20.0 is the same limit; Sail takes only whole GiB.
    if isinstance(value, float) and value.is_integer():
        value = int(value)
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


def _box_spend(report: Any, sailbox_id: str) -> dict[str, Any] | None:
    """Pick the line of one box from a spend report.

    Args:
        report: The decoded JSON spend report.
        sailbox_id: The box to find.

    Returns:
        The box's spend fields with the report's pricing flags, or ``None``
        when the report is not an object or does not list the box yet.
    """
    if not isinstance(report, Mapping):
        return None
    boxes = report.get("sailboxes")
    if not isinstance(boxes, list):
        return None
    for line in boxes:
        if isinstance(line, Mapping) and line.get("sailbox_id") == sailbox_id:
            spend = {name: line.get(name) for name in _SPEND_FIELDS}
            spend["pricing_configured"] = report.get("pricing_configured")
            spend["creation_fees_waived"] = report.get("creation_fees_waived")
            spend["rates"] = report.get("rates")
            return spend
    return None


def _creation_usd(spend: Mapping[str, Any], fee: Any) -> Decimal:
    """Price the creation charges of one box's spend line.

    Args:
        spend: The box's spend line with the report's pricing flags.
        fee: The fee in USD for one creation.

    Returns:
        The creation charges in USD, or zero when Sail waives them.
    """
    # creation_fees_waived shows the current plan, not the plan at the
    # create. A box made on Free just before a change to Pro can miss its
    # fee, at most $0.01; the cost_usd_nanos floor covers it when Sail's
    # cost holds the fee.
    if spend.get("creation_fees_waived") is True:
        return Decimal(0)
    return exact_nonnegative(spend["creation_count"]) * exact_nonnegative(fee)


def sail_actual_usd(spend: Mapping[str, Any], price_snapshot: Mapping[str, Any]) -> Decimal:
    """Price one box from its spend line.

    The measured usage is priced per second, plus the creation charge
    unless Sail waives it. With account prices, Sail's reported rates are
    used and the result is never below Sail's own cost. Otherwise the
    admitted rates are used.

    Args:
        spend: The box's spend line with the report's pricing flags.
        price_snapshot: The admitted quote.

    Returns:
        The box cost in USD.

    Raises:
        UsagePendingError: When the line or the snapshot has no usable numbers.
    """
    try:
        per_second = {
            "vcpu_seconds": exact_nonnegative(price_snapshot["vcpu_hour_usd"]) / _SECONDS_PER_HOUR,
            "memory_gib_seconds": exact_nonnegative(price_snapshot["memory_gib_hour_usd"]) / _SECONDS_PER_HOUR,
            "state_disk_gib_seconds": exact_nonnegative(price_snapshot["disk_gib_hour_usd"]) / _SECONDS_PER_HOUR,
        }
        fee = price_snapshot["creation_usd"]
        configured = spend.get("pricing_configured") is True
        rates = spend.get("rates")
        if configured and isinstance(rates, Mapping):
            for counter, name in _SAIL_SECOND_RATES.items():
                if rates.get(name) is not None:
                    per_second[counter] = exact_nonnegative(rates[name]) / _NANOS_PER_USD
            size = (price_snapshot.get("request") or {}).get("size") or _DEFAULT_SIZE
            rate = rates.get(f"{size}_creation_usd_nanos")
            # With account prices, a zero rate is the account's real price.
            if rate is not None:
                fee = exact_nonnegative(rate) / _NANOS_PER_USD
        cost = sum(
            (exact_nonnegative(spend[counter]) * price for counter, price in per_second.items()), Decimal(0)
        ) + _creation_usd(spend, fee)
        if configured:
            # The spend docs (_SPEND_URL) call cost_usd_nanos "the cost of the
            # Sailbox's usage" and list creation_count apart from it; they do
            # not say if the cost holds the creation charge. The recomputed
            # cost holds it once. Sail's own cost is the floor, for rates that
            # this code does not know ("New rates can appear over time").
            cost = max(cost, exact_nonnegative(spend["cost_usd_nanos"]) / _NANOS_PER_USD)
        return cost
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
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Reserve and claim one box before any network call.

        Args:
            runtime: Authenticated budget authority, outside the guest.
            request: The parameters that :func:`quote_sail_sandbox` prices.
            operation_key: Stable logical identity of the box.
            reserved: A pool lease slice already reserved for this box.
            fetch: Reads the spend report of one box.
            sleep: Waits between spend reads, without holding a thread.
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

    def release(self, *, refused: bool = False) -> None:
        """Return the hold when no box can exist, otherwise keep it pending.

        A box can exist once the create call is sent, even when its handle
        never arrived. So only a box that was never requested, or whose
        create Sail clearly refused, is released.

        Args:
            refused: True when the create failed before Sail could make a box.
        """
        if self.sailbox_id is not None or (self.create_sent_at is not None and not refused):
            self.pending()
            return
        reason = "Sail refused the create call" if refused else "the create call was never sent"
        self.runtime.service.reject(
            self.operation.id,
            self.runtime.username,
            evidence_key=f"sail-unsent:{self.operation.id}",
            evidence={"provider": "sail", "reason": reason},
        )
        self.runtime.unpin(self.operation.id)

    def _identity(self) -> tuple[str, dt.datetime]:
        """Return the box id and the start of its spend window.

        Returns:
            The Sail id of the box and the window start.

        Raises:
            UsagePendingError: When the create returned no box identity.
        """
        if self.sailbox_id is None or self.create_sent_at is None:
            raise UsagePendingError("The Sail create returned no box identity to settle.")
        return self.sailbox_id, self.create_sent_at - _WINDOW_MARGIN

    async def final_spend(self, budget: float | None = None) -> dict[str, Any]:
        """Poll the spend report until the box's line stays the same for a minute.

        Only the short spend reads run in threads; the waits do not hold one.
        A 429 or 5xx answer, or a transport error, is read again later.

        Args:
            budget: The seconds that the caller can still wait, including the
                last read; ``None`` waits up to ``SPEND_DEADLINE_SECONDS``.

        Returns:
            The box's final spend line.

        Raises:
            UsagePendingError: When there is no box identity, the line does
                not settle before the deadline, or the read fails in a way
                that no retry can fix.
            httpx.HTTPStatusError: When Sail refuses the read for good.
        """
        sailbox_id, since = self._identity()
        wait_for = SPEND_DEADLINE_SECONDS
        if budget is not None:
            # The last read starts before the deadline and can take its timeout.
            wait_for = min(wait_for, budget - _SPEND_TIMEOUT_SECONDS)
        give_up = self._clock() + wait_for
        reads_end = give_up + _SPEND_TIMEOUT_SECONDS
        wait = SPEND_DELAY_SECONDS
        previous: dict[str, Any] | None = None
        first_seen = 0.0
        while True:
            if self._clock() + wait > give_up:
                raise UsagePendingError("Sail did not report a final spend for the box in time.")
            await self._sleep(wait)
            wait = SPEND_POLL_SECONDS
            try:
                # The httpx timeout bounds each phase, so a slow trickle could
                # pass it; this bounds the whole read inside the budget.
                report = await asyncio.wait_for(
                    asyncio.to_thread(self._fetch, sailbox_id, since),
                    max(0.0, min(_SPEND_TIMEOUT_SECONDS, reads_end - self._clock())),
                )
            except httpx.HTTPStatusError as error:
                if error.response.status_code not in _TRANSIENT_STATUSES:
                    raise
                retry_after = _retry_after(error.response)
                if retry_after is not None:
                    # A zero or past Retry-After must not make reads back to back.
                    wait = max(retry_after, SPEND_POLL_SECONDS)
                continue
            except (httpx.RequestError, TimeoutError, ValueError):
                # ValueError: the body is not JSON, which a proxy error page can cause.
                continue
            except UsagePendingError:
                raise
            except Exception as error:
                # An SDK or code error repeats on every read; the caller keeps
                # the hold pending with its evidence.
                raise UsagePendingError("Sail spend could not be read; the hold stays pending.") from error
            line = _box_spend(report, sailbox_id)
            if line is None:
                continue
            self.spend = line
            now = self._clock()
            if line != previous:
                previous, first_seen = line, now
            elif now - first_seen >= SPEND_AGREE_SECONDS:
                return line

    def settle_spend(self, spend: Mapping[str, Any]) -> None:
        """Settle the stopped box at its final spend, inside the reservation.

        Args:
            spend: The box's final spend line from :meth:`final_spend`.

        Raises:
            UsagePendingError: When the spend is unusable.
            BudgetError: When the ledger refuses the settlement.
        """
        try:
            current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
            if current.state == "settled":
                self.runtime.unpin(self.operation.id)
                return
            usd = sail_actual_usd(spend, self.quote.price_snapshot)
            charge = vercel_charge_policy(self.quote.price_snapshot).convert(usd)
            # Sail's per-second samples and its own rates can pass the funded
            # ceiling by a little; the charge must stay inside the reservation.
            total = min(charge.total, current.max_cents)
            wallet = min(charge.wallet, current.max_wallet_cents, total)
            evidence: dict[str, Any] = {
                "provider": "sail",
                "source": _SPEND_URL,
                "spend": dict(spend),
                "provider_usd": str(usd),
            }
            if total < charge.total or wallet < charge.wallet:
                evidence["clamped"] = True
                evidence["unclamped_cents"] = str(charge.total)
                evidence["unclamped_wallet_cents"] = str(charge.wallet)
                logger.warning(
                    "Sail spend of box %s passed its reservation; the charge is clamped to the reservation.",
                    self.sailbox_id,
                )
            self.runtime.service.settle(
                self.operation.id,
                self.runtime.username,
                evidence_key=f"sail-spend:{self.sailbox_id}",
                actual_cents=total,
                actual_wallet_cents=wallet,
                evidence=evidence,
            )
            self.runtime.unpin(self.operation.id)
        except BaseException:
            self.pending()
            raise

    def settle(self) -> None:
        """Wait for the final spend, then settle the box, from a thread with no event loop.

        Raises:
            UsagePendingError: When the spend is missing, late or unusable.
            BudgetError: When the ledger refuses the settlement.
            httpx.HTTPError: When the spend report cannot be read.
        """
        try:
            spend = asyncio.run(self.final_spend())
        except BaseException:
            self.pending()
            raise
        self.settle_spend(spend)


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
        """Settle the stopped box at its reported spend, from a thread with no event loop.

        Args:
            reservation: The box's claimed reservation.
        """
        reservation.settle()


USAGE = SailUsage()
