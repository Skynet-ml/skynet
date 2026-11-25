"""Reserve Hugging Face Sandbox compute and settle it from the job's billed minutes.

A Hugging Face Sandbox is one HF Job. HF has no API that reports the cost
of one job, so a box settles at its billed minutes times the published
hourly rate of its flavor. HF bills by the minute while a job is Starting
or Running. The settle reads the job's own ``created_at`` and
``finished_at`` once the job is in a terminal stage, because that span
holds every billed minute. When HF reports no such times, the settle uses
the measured wall time from the create call to the confirmed terminal
stage. Both are rounded up to whole minutes.

Every sandbox exposes one port for its in-box server, so each box also pays
HF's flat exposed-ports rate.

``huggingface_hub.Sandbox.create`` gives every job a fixed 24 hour timeout
and has no option to change it. The provider cancels the job at the spec
lifetime, but a lost worker cannot do that, so the hold always funds the
24 hour timeout plus a start margin. The settled time never exceeds that
funded time; a longer measured time settles at the funded time, logs a
warning and flags ``over_funded`` in its evidence.

HF cannot make the timeout of a running job longer, so a reservation
refuses an extension.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import ROUND_CEILING, Decimal
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

# Read from https://huggingface.co/docs/hub/jobs-pricing on 2026-11-24. The
# page bills by the minute while a job is Starting or Running, and lists no
# egress, storage or creation charge. Only these flavors are pinned; every
# other flavor is refused before the create.
_PRICING_URL = "https://huggingface.co/docs/hub/jobs-pricing"
_PRICE_READ_DATE = "2026-11-24"
_PRICE_VERSION = "hf-jobs-public-2026-11-24"
FLAVOR_HOURLY_USD: Mapping[str, Decimal] = {
    "cpu-basic": Decimal("0.01"),
    "cpu-upgrade": Decimal("0.03"),
    "a10g-small": Decimal("1.00"),
}
# The same page: a flat rate for each job that exposes one or more ports.
_EXPOSED_PORTS_HOURLY_USD = Decimal("0.01")
_MINUTE_MS = 60_000
_MINUTES_PER_HOUR = Decimal(60)
# Sub-cent precision for the provider charge; the cent conversion rounds up again.
_USD_QUANTUM = Decimal("0.0000000001")
# huggingface_hub 1.33.0 hardcodes SANDBOX_MAX_LIFETIME = "24h" as the job timeout.
LIFETIME_CEILING_MS = 24 * 60 * _MINUTE_MS
# HF does not say whether the job timeout counts the Starting time, which HF
# bills, so the hold funds this much more than the timeout. A design choice,
# not a price.
_LIFETIME_MARGIN_MS = 30 * _MINUTE_MS
FUNDED_MS = LIFETIME_CEILING_MS + _LIFETIME_MARGIN_MS


def _minutes(duration_ms: int) -> int:
    """Round a duration up to HF's per-minute billing.

    Args:
        duration_ms: The duration in milliseconds.

    Returns:
        Whole minutes, at least one.
    """
    return max(1, math.ceil(duration_ms / _MINUTE_MS))


def _usd(snapshot: Mapping[str, Any], duration_ms: int) -> Decimal:
    """Price one box of the admitted flavor for a duration.

    Args:
        snapshot: The admitted price snapshot with its rates and request.
        duration_ms: The billed duration.

    Returns:
        The cost of the duration, rounded up.

    Raises:
        UsagePendingError: When the snapshot has no usable rates.
    """
    try:
        if snapshot.get("provider") != "hf_sandbox":
            raise ValueError("Not an HF Sandbox price snapshot")
        hourly = exact_nonnegative(snapshot["flavor_hourly_usd"])
        if snapshot["request"]["exposed_ports"]:
            hourly += exact_nonnegative(snapshot["exposed_ports_hourly_usd"])
    except (KeyError, TypeError, ValueError) as error:
        raise UsagePendingError("The HF Sandbox admission has no usable price snapshot.") from error
    usd = Decimal(_minutes(duration_ms)) * hourly / _MINUTES_PER_HOUR
    return usd.quantize(_USD_QUANTUM, rounding=ROUND_CEILING)


def quote_hf_sandbox(request: Mapping[str, Any]) -> OperationQuote:
    """Price an HF Sandbox for its whole possible lifetime before it exists.

    Args:
        request: Final create parameters with ``flavor``, ``lifetime_ms``
            (the provider's own cap), ``exposed_ports`` and ``image``.

    Returns:
        Coverage for HF's fixed job timeout plus the start margin, at the flavor rate.

    Raises:
        UnpricedOperationError: When the flavor has no pinned rate, or the
            lifetime or the port count is not bounded.
    """
    flavor = request.get("flavor")
    lifetime_ms = request.get("lifetime_ms")
    ports = request.get("exposed_ports")
    if flavor not in FLAVOR_HOURLY_USD:
        raise UnpricedOperationError(f"HF Sandbox flavor {flavor!r} has no pinned rate.")
    if (
        isinstance(lifetime_ms, bool)
        or not isinstance(lifetime_ms, int)
        or not 0 < lifetime_ms <= LIFETIME_CEILING_MS
        or isinstance(ports, bool)
        or not isinstance(ports, int)
        or ports < 0
    ):
        raise UnpricedOperationError("HF Sandbox requires a lifetime of at most 24 hours and a port count.")
    evidence = {
        "provider": "hf_sandbox",
        "price_version": _PRICE_VERSION,
        "source": _PRICING_URL,
        "read_date": _PRICE_READ_DATE,
        "flavor_hourly_usd": str(FLAVOR_HOURLY_USD[flavor]),
        "exposed_ports_hourly_usd": str(_EXPOSED_PORTS_HOURLY_USD),
        "funded_ms": FUNDED_MS,
        "request": dict(request),
    }
    return operation_quote(request, _usd(evidence, FUNDED_MS), ChargePolicy("sandbox"), evidence)


def _job_ms(created_at: Any, finished_at: Any) -> int | None:
    """Return the span between a job's creation and its end.

    Args:
        created_at: The job's ``created_at``.
        finished_at: The job's ``finished_at``.

    Returns:
        The span in whole milliseconds, rounded up, or ``None`` when HF
        reported no usable times.
    """
    if not isinstance(created_at, datetime) or not isinstance(finished_at, datetime):
        return None
    try:
        span = (finished_at - created_at).total_seconds()
    except TypeError:
        # One aware and one naive time cannot be compared.
        return None
    if span < 0:
        return None
    return math.ceil(span * 1000)


class HfSandboxUsageReservation:
    """Own the budget hold of one HF Sandbox from reservation to settlement."""

    def __init__(
        self,
        runtime: BudgetRuntime,
        request: Mapping[str, Any],
        *,
        operation_key: str,
        reserved: OperationSnapshot | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Reserve and claim one sandbox before any HF call.

        Args:
            runtime: Authenticated budget authority retained outside the guest.
            request: Final create parameters that :func:`quote_hf_sandbox` prices.
            operation_key: Stable logical identity of the box.
            reserved: A pool lease slice already reserved for this box.
            clock: The monotonic clock that measures the wall time.

        Raises:
            BudgetConflictError: When the lease slice was reserved for a different request.
            OperationCompletedError: When the operation already settled.
            UsagePendingError: When another caller already dispatched the box.
        """
        self.runtime = runtime
        self.request = dict(request)
        self.quote = quote_hf_sandbox(request)
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
        self.lifetime_ms = FUNDED_MS
        self.job_id: str | None = None
        self.label: str | None = None
        self.sent_at: float | None = None
        self.stopped_at: float | None = None
        self.job_ms: int | None = None
        self._clock = clock
        self._sent_wall: float | None = None
        self._settled = False

    def sent(self, label: str) -> None:
        """Mark the create call as sent and start the wall clock.

        Args:
            label: The unique label value that the lookup after a failed create searches for.
        """
        self.label = label
        self.sent_at = self._clock()
        self._sent_wall = time.time()

    def confirm_created(self, job: Any) -> None:
        """Bind the created job to the operation and record its start.

        The job id goes into the pending evidence at once, so a worker that
        dies before the settle leaves enough to reconcile the hold.

        Args:
            job: The HF Sandbox or ``JobInfo``; its ``id`` is the job id.

        Raises:
            UsagePendingError: When HF returned no job id.
        """
        job_id = getattr(job, "id", None)
        if not isinstance(job_id, str) or not job_id:
            raise UsagePendingError("The HF Sandbox returned no job id.")
        self.job_id = job_id
        self.runtime.service.mark_dispatched(self.operation.id, self.runtime.username, job_id)
        self.pending()

    def stopped(self, created_at: Any = None, finished_at: Any = None) -> None:
        """Stop the wall clock once HF confirmed a terminal stage, and keep the job's own times.

        Args:
            created_at: The job's ``created_at``, or ``None``.
            finished_at: The job's ``finished_at``, or ``None``.
        """
        self.stopped_at = self._clock()
        self.job_ms = _job_ms(created_at, finished_at)

    def extend(self, lifetime_ms: int, *, deadline: float) -> None:
        """Refuse an extension, because HF cannot lengthen a running job's timeout.

        Args:
            lifetime_ms: Total lifetime after the extension.
            deadline: ``time.monotonic()`` instant after which waiting stops.

        Raises:
            UnpricedOperationError: Always.
        """
        raise UnpricedOperationError("HF cannot lengthen the timeout of a running sandbox.")

    def _measured_ms(self) -> int | None:
        """Return the wall time from the create call to the confirmed terminal stage.

        Returns:
            The measured milliseconds, or ``None`` before both ends are known.
        """
        if self.sent_at is None or self.stopped_at is None:
            return None
        return max(0, math.ceil((self.stopped_at - self.sent_at) * 1000))

    def _evidence(self) -> dict[str, Any]:
        """Return the provider evidence of this box.

        Returns:
            The job id, the label, the wall-clock start and the measured, job and funded times.
        """
        return {
            "provider": "hf_sandbox",
            "job_id": self.job_id,
            "label": self.label,
            "sent_at": self._sent_wall,
            "measured_ms": self._measured_ms(),
            "job_ms": self.job_ms,
            "funded_ms": self.lifetime_ms,
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
        """Release the hold when no job can exist, otherwise retain it.

        No job exists when the create was never sent. A sent create that
        failed may still have made one, so its hold is released only when no
        job id came back, HF refused the create and the label lookup found
        no job.

        Args:
            absent: True when HF refused the create and the label lookup found no job.
        """
        current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
        if current.state in {"settled", "released"}:
            self.runtime.unpin(self.operation.id)
            return
        if self.sent_at is None or (absent and self.job_id is None):
            if self.sent_at is None:
                key = f"hf-sandbox-unsent:{self.operation.id}"
                reason = "The create call was never sent."
            else:
                key = f"hf-sandbox-absent:{self.operation.id}"
                reason = "HF refused the create and the label lookup found no job."
            try:
                self.runtime.service.reject(
                    self.operation.id,
                    self.runtime.username,
                    evidence_key=key,
                    evidence={"provider": "hf_sandbox", "reason": reason, "label": self.label},
                )
                self.runtime.unpin(self.operation.id)
                return
            except BudgetError:
                pass
        self.pending()

    def settle(self) -> None:
        """Settle the stopped box once, at its billed minutes, never above the hold.

        Raises:
            UsagePendingError: When the box has no job id or no confirmed terminal stage.
            BudgetError: When the charge breaks the reserved bounds.
        """
        if self._settled:
            return
        try:
            current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
            if current.state == "settled":
                self._settled = True
                self.runtime.unpin(self.operation.id)
                return
            if self.job_id is None or self.stopped_at is None:
                raise UsagePendingError("The HF Sandbox has no job id or confirmed terminal stage to settle.")
            evidence = self._evidence()
            source = "job" if self.job_ms is not None else "measured"
            used_ms = self.job_ms if self.job_ms is not None else evidence["measured_ms"]
            billed_ms = min(used_ms, self.lifetime_ms)
            over_funded = used_ms > self.lifetime_ms
            if over_funded:
                logger.warning(
                    "HF Sandbox job %s ran %d ms, more than its funded %d ms; it settles at the funded time.",
                    self.job_id,
                    used_ms,
                    self.lifetime_ms,
                )
            usd = _usd(self.quote.price_snapshot, billed_ms)
            charge = vercel_charge_policy(self.quote.price_snapshot).convert(usd)
            self.runtime.service.settle(
                self.operation.id,
                self.runtime.username,
                evidence_key=f"hf-sandbox-stop:{self.job_id}",
                actual_cents=charge.total,
                actual_wallet_cents=charge.wallet,
                evidence={
                    **evidence,
                    "billed_ms": billed_ms,
                    "billed_minutes": _minutes(billed_ms),
                    "time_source": source,
                    "over_funded": over_funded,
                    "source": _PRICING_URL,
                    "provider_usd": str(usd),
                },
            )
            self._settled = True
            self.runtime.unpin(self.operation.id)
        except BaseException:
            self.pending()
            raise


class HfSandboxUsage:
    """Meter HF Sandboxes through the provider-neutral usage protocol."""

    name = "hf_sandbox"

    def quote(self, request: Mapping[str, Any]) -> OperationQuote:
        """Price one sandbox before any provider resource exists.

        Args:
            request: Final create parameters.

        Returns:
            Coverage for the sandbox's whole funded lifetime.
        """
        return quote_hf_sandbox(request)

    def reserve(
        self,
        runtime: BudgetRuntime,
        request: Mapping[str, Any],
        *,
        operation_key: str,
        reserved: OperationSnapshot | None = None,
    ) -> HfSandboxUsageReservation:
        """Reserve and claim one sandbox, or claim a lease slice already reserved for it.

        Args:
            runtime: Authenticated budget authority.
            request: Final create parameters.
            operation_key: Stable logical identity of the box.
            reserved: Optional pool lease slice for this box.

        Returns:
            The claimed reservation that the box confirms and settles.
        """
        return HfSandboxUsageReservation(runtime, request, operation_key=operation_key, reserved=reserved)

    def confirm(self, reservation: HfSandboxUsageReservation, session: Any) -> None:
        """Bind the created job to its reservation.

        Args:
            reservation: The box's claimed reservation.
            session: The HF Sandbox handle.
        """
        reservation.confirm_created(session)

    def extend(self, reservation: HfSandboxUsageReservation, lifetime_ms: int, *, deadline: float) -> None:
        """Refuse an extension, because HF cannot lengthen a running job's timeout.

        Args:
            reservation: The box's claimed reservation.
            lifetime_ms: Total lifetime after the extension.
            deadline: ``time.monotonic()`` instant after which waiting stops.

        Raises:
            UnpricedOperationError: Always.
        """
        reservation.extend(lifetime_ms, deadline=deadline)

    def settle(self, reservation: HfSandboxUsageReservation) -> None:
        """Settle the stopped sandbox at its billed minutes.

        Args:
            reservation: The box's claimed reservation.
        """
        reservation.settle()


USAGE = HfSandboxUsage()
