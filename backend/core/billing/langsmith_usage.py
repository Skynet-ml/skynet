"""Reserve LangSmith sandbox compute and settle it from the measured box lifetime.

LangSmith bills a sandbox per second for its allocated vCPUs, memory and
root filesystem. Its usage API (``GET /api/v2/sandboxes/usage/costs``)
reports usage from hourly samples, so right after a delete it can still
miss the last hour. A settle from it would undercharge. The box is
therefore settled from the time between its reservation and its confirmed
delete, at the published rates. That time contains the whole billed life
of the box, so it is never less than the vendor's bill. The reservation
records the sandbox id, so a later check can compare it with the usage API.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from .budget_amounts import cent_units
from .budgets import BudgetConflictError, OperationSnapshot
from .operation_pricing import ChargePolicy, OperationQuote, UnpricedOperationError, json_fingerprint, operation_quote
from .runtime import BudgetRuntime, OperationCompletedError, UsagePendingError
from .usage_tags import box_tag
from .vercel_usage import vercel_charge_policy

# Published LangSmith Sandbox rates, read on 2026-11-24 from
# https://www.langchain.com/pricing ("All rates billed per second", 1 LSU = $1).
# The page names no per-sandbox fee and no network charge.
_PRICING_URL = "https://www.langchain.com/pricing"
_PRICE_VERSION = "langsmith-sandbox-public-2026-11-24"
_CPU_HOUR_USD = Decimal("0.0576")
_MEMORY_GIB_HOUR_USD = Decimal("0.01845")
_STORAGE_GIB_HOUR_USD = Decimal("0.000123")
_BYTES_PER_GIB = Decimal(1024**3)
_MS_PER_HOUR = Decimal(3_600_000)
_USAGE_API = "https://docs.langchain.com/langsmith/smith-api/sandboxes/list-hourly-sandbox-usage-costs"


def _positive_int(request: Mapping[str, Any], name: str) -> int:
    """Read one required positive whole number from a create request.

    Args:
        request: Final create parameters.
        name: The field name.

    Returns:
        The value.

    Raises:
        UnpricedOperationError: When the value is missing, not a whole number, or not positive.
    """
    value = request.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise UnpricedOperationError(f"A protected LangSmith sandbox needs a positive {name}.")
    return value


def _usd(*, vcpus: int, mem_bytes: int, fs_bytes: int, running_ms: int, stored_ms: int) -> Decimal:
    """Price a box's compute and memory for its running time and its disk for its stored time.

    Args:
        vcpus: Allocated vCPUs.
        mem_bytes: Allocated memory.
        fs_bytes: Allocated root filesystem.
        running_ms: Time the box runs.
        stored_ms: Time the box's filesystem exists.

    Returns:
        The provider cost in USD.
    """
    compute = Decimal(vcpus) * _CPU_HOUR_USD + Decimal(mem_bytes) / _BYTES_PER_GIB * _MEMORY_GIB_HOUR_USD
    storage = Decimal(fs_bytes) / _BYTES_PER_GIB * _STORAGE_GIB_HOUR_USD
    return (compute * Decimal(running_ms) + storage * Decimal(stored_ms)) / _MS_PER_HOUR


def quote_langsmith_sandbox(request: Mapping[str, Any]) -> OperationQuote:
    """Price the most one sandbox can cost, also when its worker dies.

    A protected box is deleted when its lifetime ends. If its worker dies
    first, the server stops the box after ``idle_ttl_seconds`` without a
    call, and deletes its filesystem ``delete_after_stop_seconds`` later.

    Args:
        request: Final create parameters with ``lifetime_ms``, ``startup_ms``,
            ``vcpus``, ``mem_bytes``, ``fs_capacity_bytes``,
            ``idle_ttl_seconds`` and ``delete_after_stop_seconds``.

    Returns:
        Coverage for the box's start, lifetime, idle stop and filesystem retention.

    Raises:
        UnpricedOperationError: When a size or a time is missing or unbounded.
    """
    lifetime_ms = _positive_int(request, "lifetime_ms")
    startup_ms = _positive_int(request, "startup_ms")
    vcpus = _positive_int(request, "vcpus")
    mem_bytes = _positive_int(request, "mem_bytes")
    fs_bytes = _positive_int(request, "fs_capacity_bytes")
    # Zero turns the server's idle stop or delete off, so the box's cost has no bound.
    idle_ms = _positive_int(request, "idle_ttl_seconds") * 1000
    retention_ms = _positive_int(request, "delete_after_stop_seconds") * 1000
    running_ms = startup_ms + lifetime_ms + idle_ms
    maximum = _usd(
        vcpus=vcpus, mem_bytes=mem_bytes, fs_bytes=fs_bytes, running_ms=running_ms, stored_ms=running_ms + retention_ms
    )
    return operation_quote(
        request,
        maximum,
        ChargePolicy("sandbox"),
        {
            "provider": "langsmith",
            "price_version": _PRICE_VERSION,
            "source": _PRICING_URL,
            "cpu_hour_usd": str(_CPU_HOUR_USD),
            "memory_gib_hour_usd": str(_MEMORY_GIB_HOUR_USD),
            "storage_gib_hour_usd": str(_STORAGE_GIB_HOUR_USD),
            "billing_increment_ms": 1000,
            "request": dict(request),
        },
    )


class LangSmithUsageReservation:
    """Own the coverage of one LangSmith sandbox and the times that price it."""

    def __init__(
        self,
        runtime: BudgetRuntime,
        request: Mapping[str, Any],
        *,
        operation_key: str,
        reserved: OperationSnapshot | None = None,
    ) -> None:
        """Reserve and claim one sandbox before its create call.

        The measured lifetime starts here, before the create call, so it
        contains all the time the vendor can bill.

        Args:
            runtime: Authenticated budget authority.
            request: Final create parameters.
            operation_key: Stable logical identity of the box.
            reserved: A pool lease slice already reserved for this box.

        Raises:
            BudgetConflictError: When the lease slice was reserved for a different request.
            OperationCompletedError: When the operation already settled.
            UsagePendingError: When another caller already dispatched the box.
        """
        self.runtime = runtime
        self.request = dict(request)
        self.quote = quote_langsmith_sandbox(request)
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
        self.sandbox_id: str | None = None
        self.opened_at = time.monotonic()
        self.deleted_at: float | None = None

    def confirm_created(self, box: Any) -> None:
        """Record the sandbox id and check the created sizes against the funded ones.

        Args:
            box: The SDK sandbox that the create call returned.

        Raises:
            UsagePendingError: When the box reports a size or a TTL beyond its coverage.
        """
        self.sandbox_id = getattr(box, "id", None)
        if self.sandbox_id:
            self.runtime.service.mark_dispatched(self.operation.id, self.runtime.username, self.sandbox_id)
        for field in ("vcpus", "mem_bytes", "fs_capacity_bytes", "idle_ttl_seconds", "delete_after_stop_seconds"):
            # The SDK leaves a field None when the server omits it; the create
            # call sent the funded value.
            value = getattr(box, field, None)
            if value is not None and value != self.request[field]:
                raise UsagePendingError(f"The LangSmith sandbox did not confirm the funded {field}.")

    def mark_deleted(self) -> None:
        """Record that the provider confirmed the sandbox is deleted, so its billed life is over."""
        if self.deleted_at is None:
            self.deleted_at = time.monotonic()

    def extend(self, lifetime_ms: int, *, deadline: float) -> None:
        """Fund a longer lifetime before the box may run that long.

        Args:
            lifetime_ms: Total lifetime after the extension.
            deadline: ``time.monotonic()`` instant after which waiting for in-flight work stops.

        Raises:
            ValueError: When the lifetime does not grow.
            UsagePendingError: When covered work did not settle before ``deadline``.
            BudgetReached: When the budget cannot pay for the extension.
        """
        if lifetime_ms <= self.request["lifetime_ms"]:
            raise ValueError("An extension must lengthen the funded lifetime.")
        quote = quote_langsmith_sandbox({**self.request, "lifetime_ms": lifetime_ms})
        extended = self.runtime.extend(
            self.operation.id,
            quote,
            evidence_key=f"langsmith-extend:{lifetime_ms}",
            evidence={"provider": "langsmith", "lifetime_ms": lifetime_ms, "request": quote.request_fingerprint},
            deadline=deadline,
        )
        self.runtime.pin(self.operation.id, cent_units(extended.max_cents), box=self.box)
        self.request["lifetime_ms"] = lifetime_ms

    def pending(self) -> None:
        """Keep the coverage held when the delete or the usage is uncertain."""
        current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
        if current.state in {"dispatched", "pending"}:
            evidence = {"provider": "langsmith", "sandbox_id": self.sandbox_id, "name": self.request.get("name")}
            self.runtime.service.mark_pending(
                self.operation.id,
                self.runtime.username,
                evidence_key=json_fingerprint(evidence),
                evidence=evidence,
            )

    def settle(self) -> None:
        """Settle a deleted sandbox at its measured lifetime, inside its coverage.

        Raises:
            UsagePendingError: When the delete is not confirmed.
            BudgetError: When the ledger refuses the settlement.
        """
        try:
            current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
            if current.state == "settled":
                self.runtime.unpin(self.operation.id)
                return
            if self.deleted_at is None:
                raise UsagePendingError("The LangSmith sandbox delete is not confirmed; its coverage stays held.")
            # The vendor bills whole seconds, so round the measured time up.
            elapsed_ms = math.ceil(max(0.0, self.deleted_at - self.opened_at)) * 1000
            usd = _usd(
                vcpus=self.request["vcpus"],
                mem_bytes=self.request["mem_bytes"],
                fs_bytes=self.request["fs_capacity_bytes"],
                running_ms=elapsed_ms,
                stored_ms=elapsed_ms,
            )
            charge = vercel_charge_policy(self.quote.price_snapshot).convert(usd)
            # A slow delete can pass the funded time; the vendor's own idle stop
            # bounds the real bill, so the charge never leaves the coverage.
            total = min(charge.total, current.max_cents)
            wallet = min(charge.wallet, current.max_wallet_cents, total)
            self.runtime.service.settle(
                self.operation.id,
                self.runtime.username,
                evidence_key=f"langsmith-delete:{self.sandbox_id or self.operation.id}",
                actual_cents=total,
                actual_wallet_cents=wallet,
                evidence={
                    "provider": "langsmith",
                    "source": _PRICING_URL,
                    "usage_api": _USAGE_API,
                    "sandbox_id": self.sandbox_id,
                    "name": self.request.get("name"),
                    "measured_ms": elapsed_ms,
                    "provider_usd": str(usd),
                    "clamped": total < charge.total,
                },
            )
            self.runtime.unpin(self.operation.id)
        except BaseException:
            self.pending()
            raise


class LangSmithUsage:
    """Meter LangSmith sandboxes through the provider-neutral usage protocol."""

    name = "langsmith"

    def quote(self, request: Mapping[str, Any]) -> OperationQuote:
        """Price one sandbox before any provider resource exists.

        Args:
            request: Final create parameters.

        Returns:
            Coverage for the sandbox's whole funded lifetime.
        """
        return quote_langsmith_sandbox(request)

    def reserve(
        self,
        runtime: BudgetRuntime,
        request: Mapping[str, Any],
        *,
        operation_key: str,
        reserved: OperationSnapshot | None = None,
    ) -> LangSmithUsageReservation:
        """Reserve and claim one sandbox, or claim a lease slice already reserved for it.

        Args:
            runtime: Authenticated budget authority.
            request: Final create parameters.
            operation_key: Stable logical identity of the box.
            reserved: Optional pool lease slice for this box.

        Returns:
            The claimed reservation that the box confirms, extends and settles.
        """
        return LangSmithUsageReservation(runtime, request, operation_key=operation_key, reserved=reserved)

    def confirm(self, reservation: LangSmithUsageReservation, session: Any) -> None:
        """Verify the created sandbox matches its funded envelope.

        Args:
            reservation: The box's claimed reservation.
            session: The SDK sandbox.
        """
        reservation.confirm_created(session)

    def extend(self, reservation: LangSmithUsageReservation, lifetime_ms: int, *, deadline: float) -> None:
        """Fund a longer lifetime before the box may run that long.

        Args:
            reservation: The box's claimed reservation.
            lifetime_ms: Total lifetime after the extension.
            deadline: ``time.monotonic()`` instant after which waiting stops.
        """
        reservation.extend(lifetime_ms, deadline=deadline)

    def settle(self, reservation: LangSmithUsageReservation) -> None:
        """Settle the deleted sandbox at its measured cost.

        Args:
            reservation: The box's claimed reservation.
        """
        reservation.settle()


USAGE = LangSmithUsage()
