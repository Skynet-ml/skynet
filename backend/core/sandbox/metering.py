"""Meter sandbox providers through one usage protocol and lease budget slices to a pool.

Every provider that may run paid user work in production needs a usage
adapter here and a staging cost test (SKY-406 D1). A pool lease reserves the
budget for all of its boxes in one write. Each box claims one slice and
settles against it, and the lease returns the slices it never handed out.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping
from typing import Any, Protocol

from core.billing.budgets import (
    BudgetInFlightError,
    BudgetInsufficientError,
    BudgetUnreconciledError,
    OperationSnapshot,
)
from core.billing.operation_pricing import OperationQuote
from core.billing.runtime import BudgetRuntime, UsagePendingError, generation_operation_key
from core.billing.vercel_usage import VercelUsage
from core.config import settings

from .errors import SandboxFatalError
from .factory import PROVIDERS, ProductionNotAllowedError, UnknownProviderError

logger = logging.getLogger(__name__)

_PRODUCTION = "production"
_WAIT_STEP_SECONDS = 1.0


class ProviderUsage(Protocol):
    """Quote, reserve, confirm, extend and settle the paid usage of one provider."""

    name: str

    def quote(self, request: Mapping[str, Any]) -> OperationQuote:
        """Price one box before any provider resource exists.

        Args:
            request: Final create parameters.

        Returns:
            Coverage for the box's whole funded lifetime.
        """
        ...

    def reserve(
        self,
        runtime: BudgetRuntime,
        request: Mapping[str, Any],
        *,
        operation_key: str,
        reserved: OperationSnapshot | None = None,
    ) -> Any:
        """Reserve one box, or claim a lease slice already reserved for it.

        Args:
            runtime: Authenticated budget authority.
            request: Final create parameters.
            operation_key: Stable logical identity of the box.
            reserved: Optional pool lease slice for this box.

        Returns:
            The claimed reservation that the box confirms, extends and settles.
        """
        ...

    def confirm(self, reservation: Any, session: Any) -> None:
        """Verify the created resource matches its funded envelope.

        Args:
            reservation: The box's claimed reservation.
            session: Provider session handle.
        """
        ...

    def extend(self, reservation: Any, lifetime_ms: int, *, deadline: float) -> None:
        """Fund a longer lifetime before the provider grants it.

        Args:
            reservation: The box's claimed reservation.
            lifetime_ms: Total lifetime after the extension.
            deadline: ``time.monotonic()`` instant after which waiting stops.
        """
        ...

    def settle(self, reservation: Any) -> None:
        """Settle the stopped box at its actual provider cost.

        Args:
            reservation: The box's claimed reservation.
        """
        ...


USAGE_ADAPTERS: dict[str, ProviderUsage] = {"vercel": VercelUsage()}


class MissingUsageAdapterError(SandboxFatalError):
    """Report a provider whose paid usage Skynet cannot meter."""


class LeaseExhaustedError(SandboxFatalError):
    """Report a box that asked a lease for more slices than it reserved."""


def usage_for(name: str) -> ProviderUsage:
    """Return the usage adapter of a provider that may run here.

    Args:
        name: The provider name, a key of ``PROVIDERS``.

    Returns:
        The provider's usage adapter.

    Raises:
        UnknownProviderError: The name is not in the registry.
        ProductionNotAllowedError: Production asked for a provider that is not allowed there.
        MissingUsageAdapterError: The provider has no usage adapter.
    """
    entry = PROVIDERS.get(name)
    if entry is None:
        raise UnknownProviderError(f"Unknown sandbox provider {name!r}.")
    adapter = USAGE_ADAPTERS.get(name)
    if settings.app_environment == _PRODUCTION and (not entry.production_allowed or adapter is None):
        raise ProductionNotAllowedError(f"Sandbox provider {name!r} is not metered for production.")
    if adapter is None:
        raise MissingUsageAdapterError(f"Sandbox provider {name!r} has no usage adapter.")
    return adapter


class PoolLease:
    """Hold one batch reservation for a pool and hand each box its own slice."""

    def __init__(
        self,
        runtime: BudgetRuntime,
        usage: ProviderUsage,
        request: Mapping[str, Any],
        *,
        lease_key: str,
        boxes: int,
    ) -> None:
        """Reserve one slice for each box with a single budget write.

        Args:
            runtime: Authenticated budget authority.
            usage: Usage adapter of the pool's provider.
            request: Final create parameters that every box of the pool uses.
            lease_key: Stable logical identity of the lease.
            boxes: Number of boxes the pool may start.

        Raises:
            UsagePendingError: When old or unconfirmed work prevents safe admission.
            BudgetReached: When the budget cannot fund the whole lease in the run phase.
            BudgetInsufficientError: When the budget cannot fund the whole lease in setup.
        """
        self.runtime = runtime
        self.usage = usage
        self.request = dict(request)
        self.quote = usage.quote(self.request)
        self._lock = threading.Lock()
        self._slices = self._reserve(lease_key, boxes)
        self._next = 0
        self._closed = False
        self._released = False

    def _reserve(self, lease_key: str, boxes: int) -> list[OperationSnapshot]:
        """Reserve the lease, waiting for in-flight work like a single reservation does.

        Args:
            lease_key: Stable logical identity of the lease.
            boxes: Number of slices.

        Returns:
            The slices in index order.
        """
        runtime = self.runtime
        deadline = time.monotonic() + runtime.wait_timeout
        while True:
            runtime.stop.check()
            try:
                return runtime.service.reserve_lease(
                    runtime.budget_id,
                    runtime.username,
                    lease_key=generation_operation_key(lease_key, runtime.generation),
                    slices=boxes,
                    generation=runtime.generation,
                    phase=runtime.phase,
                    cost_kind="sandbox",
                    request_fingerprint=self.quote.request_fingerprint,
                    price_snapshot=self.quote.price_snapshot,
                    max_cents=self.quote.maximum.total,
                    max_wallet_cents=self.quote.maximum.wallet,
                    role="runtime",
                )
            except BudgetUnreconciledError as error:
                raise UsagePendingError(
                    "Previous work is awaiting confirmed usage; its coverage is retained."
                ) from error
            except BudgetInFlightError as error:
                # A setup check's own open sandboxes never make room, so the
                # lease gives up early exactly like a single reservation.
                refusal = runtime._wait_cannot_help(error)
                if refusal is not None:
                    raise refusal from error
                if time.monotonic() >= deadline:
                    raise UsagePendingError("Covered work has not settled; the lease was not admitted.") from error
                # The lease cannot see the runtime's private condition, so it
                # polls; each try is one short locked transaction.
                time.sleep(min(_WAIT_STEP_SECONDS, max(0.0, deadline - time.monotonic())))
            except BudgetInsufficientError as error:
                if runtime.phase == "setup":
                    raise
                runtime.service.stop_admission(runtime.budget_id, runtime.username, reason="budget_reached")
                raise runtime.stop.trip(str(error)) from error

    @property
    def slices(self) -> tuple[OperationSnapshot, ...]:
        """Return every slice the lease reserved.

        Returns:
            The slices in index order.
        """
        return tuple(self._slices)

    def take(self) -> OperationSnapshot:
        """Hand the next untaken slice to one box.

        Returns:
            A slice that no other box received.

        Raises:
            LeaseExhaustedError: When the lease is closed or every slice is taken.
        """
        with self._lock:
            if self._closed or self._next >= len(self._slices):
                raise LeaseExhaustedError("The pool lease has no untaken budget slice.")
            taken = self._slices[self._next]
            self._next += 1
            return taken

    def usage_factory(self, budget: BudgetRuntime, request: Mapping[str, Any], *, operation_key: str) -> Any:
        """Claim the next slice for a box, with the provider's ``usage_factory`` signature.

        Args:
            budget: The box's budget authority; it must be the lease's.
            request: The box's create parameters; they must match the lease's.
            operation_key: The box's logical identity.

        Returns:
            The box's claimed usage reservation.

        Raises:
            ValueError: When the box uses another budget authority.
        """
        if budget is not self.runtime:
            raise ValueError("A pool box must use the budget authority of its lease.")
        return self.usage.reserve(budget, request, operation_key=operation_key, reserved=self.take())

    def close(self) -> None:
        """Return every undispatched slice in one budget write; later takes fail.

        A taken slice whose box failed before dispatch goes back too. A box
        that took its slice but has not dispatched yet then fails its claim
        before any provider call, so it never runs unfunded. If the release
        fails, takes still fail and a later close tries the release again.
        """
        with self._lock:
            if self._released:
                return
            self._closed = True
        # Two closes may both release; release_slices skips slices that are
        # no longer reserved, so the second release changes nothing.
        self.runtime.service.release_slices(
            self.runtime.budget_id, self.runtime.username, [operation.id for operation in self._slices]
        )
        with self._lock:
            self._released = True

    def __enter__(self) -> PoolLease:
        """Return the lease for a ``with`` block.

        Returns:
            This lease.
        """
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: object) -> None:
        """Return the untaken slices when the ``with`` block ends.

        Args:
            exc_type: Type of the exception that ends the block, if any.
            exc: The exception that ends the block, if any.
            tb: Its traceback, unused.
        """
        if exc is None:
            self.close()
            return
        try:
            self.close()
        except Exception:
            # The block's own error says why the pool failed; a release error
            # raised here would replace it, so it is only logged.
            logger.exception("The pool lease could not return its untaken slices.")
