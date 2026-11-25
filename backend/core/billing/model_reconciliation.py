"""Reconcile already-dispatched model requests without repeating inference."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from .budgets import BudgetError, BudgetService, OperationSnapshot
from .model_dispatch import GenerationNotFoundError, OpenRouterDispatcher
from .operation_pricing import ChargePolicy, exact_nonnegative, json_fingerprint
from .runtime import BudgetRuntime, UsagePendingError
from .vercel_reconciliation import ReconciliationPage, ReconciliationResult

# A model call runs at most 10 minutes and OpenRouter confirms its bill soon
# after. A bill still missing 15 minutes after dispatched_at is absorbed: the
# hold is released at no further charge, so the user never pays for it or
# waits on it (Gilad, SKY-784). This window covers every pending reason except
# UnsettleableUsageError.
UNCONFIRMED_RELEASE_AFTER = timedelta(minutes=15)
# A returned call with no provider request id can never be looked up. A call
# whose original credential this worker cannot resolve is also not looked up:
# for a BYOK key no worker can resolve it, and only during a managed-key
# rotation might another worker still confirm the bill. A worker with no managed
# key would also release managed holds early, but every deployed app and worker
# gets OPENROUTER_API_KEY (shared_app_secret_keys in the environment
# module's secrets.tf). Both holds are released
# at no further charge one minute after dispatched_at: usage already settled
# stays billed, and Skynet accepts the gap for the rest, because a 15-minute
# wait blocks the account's setup check (SKY-914). The minute absorbs clock skew
# between workers. The sweep releases the hold on its next pass that reaches
# it; a long queue of pending holds can delay that.
UNSETTLEABLE_RELEASE_AFTER = timedelta(minutes=1)
# OpenRouter answers a generation lookup with HTTP 404 for a short time while it
# indexes the bill (SKY-657); settle() already asked four times over about 3.5
# seconds. A 404 that lasts 5 minutes after the call returned means OpenRouter
# has no bill to give, so the hold is released at no further charge with a log
# line. The window counts from the return (the operation's last change), not
# from dispatched_at, because a call can run 10 minutes.
GENERATION_NOT_FOUND_RELEASE_AFTER = timedelta(minutes=5)

logger = logging.getLogger(__name__)


class UnsettleableUsageError(UsagePendingError):
    """Report a pending model bill that this worker cannot confirm with the provider.

    This error releases nothing. ``sweep`` releases the hold at no further charge
    1 minute after dispatch, but only a returned (pending) hold with no
    quarantine evidence. Skynet accepts the revenue gap (SKY-914).
    """


def _stored_refusal(documents: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """Return the provider's refusal when every retained receipt proves no generation existed.

    Runtimes that predate immediate refusal release parked such answers as pending usage;
    their stored documents still carry the provider's complete error answer.

    Args:
        documents: Evidence documents recorded for the operation, oldest first.

    Returns:
        The latest refusal, or None when any receipt names a generation, reports usage,
        was interrupted, or is not an OpenRouter error answer.
    """
    if not documents:
        return None
    for document in documents:
        status = document.get("status")
        if (
            document.get("provider") != "openrouter"
            or document.get("request_id") is not None
            or document.get("usage") is not None
            or document.get("interrupted") is not False
            or not isinstance(status, int)
            or status < 400
        ):
            return None
    return documents[-1]


class OpenRouterUsageReconciler:
    """Recover confirmed usage under the exact credential identity and historical prices."""

    def __init__(self, service: BudgetService, resolve_key: Callable[[str, str], str | None]) -> None:
        """Bind reconciliation to a trusted credential resolver.

        Args:
            service: Authoritative operation ledger.
            resolve_key: Account and original credential digest to current usable secret, or None.
        """
        self.service = service
        self.resolve_key = resolve_key

    def reconcile(self, operation_id: str, username: str, *, client: httpx.Client | None = None) -> OperationSnapshot:
        """Look up only the admitted generation and settle the original conversion policy.

        Args:
            operation_id: Durable physical attempt requiring final usage.
            username: Its authoritative owner.
            client: Optional deterministic HTTP test transport.

        Returns:
            Confirmed operation with cumulative exact settlement, or the released hold of a
            stored refusal that names no generation.

        Raises:
            UnsettleableUsageError: When a returned call has no provider request id and no
                stored refusal, or this worker cannot resolve the original credential.
            GenerationNotFoundError: When OpenRouter's generation lookup answers HTTP 404.
            UsagePendingError: When the operation is not an admitted OpenRouter generation, the
                model call has not returned, or final billing evidence or the original policy
                is unavailable.
        """
        record = self.service.get_reconciliation(operation_id, username)
        operation, prices = record.operation, record.price_snapshot
        if operation.state == "settled":
            return operation
        if prices.get("provider") != "openrouter" or operation.cost_kind != "model":
            raise UsagePendingError("This operation is not an admitted OpenRouter generation.")
        identity = operation.provider_request_id
        if not identity:
            refusal = _stored_refusal(record.evidence)
            if refusal is not None:
                return self.service.reject(
                    operation_id, username, evidence_key=f"openrouter-refusal:{operation_id}", evidence=refusal
                )
            if operation.state == "dispatched":
                raise UsagePendingError("The model call has not returned; its provider request id can still come.")
            raise UnsettleableUsageError("The provider request id is unavailable; no bill can be looked up.")
        digest = prices.get("credential_fingerprint")
        key = self.resolve_key(username, str(digest)) if digest else None
        if not key or json_fingerprint(key) != digest:
            raise UnsettleableUsageError(
                "The original provider credential is unavailable; this worker cannot look up the bill."
            )
        values = prices.get("policy") or {}
        if values.get("version") != "skynet-operation-pricing-v1":
            raise UsagePendingError("The original conversion policy is unavailable.")
        kind = values.get("kind")
        if kind not in {"managed_model", "byok_model"}:
            raise UsagePendingError("The original model billing scope is unavailable.")
        policy = ChargePolicy(
            kind=kind,
            cent_usd=exact_nonnegative(values["cent_usd"]),
            model_markup=exact_nonnegative(values["model_markup"]),
            byok_fee_fraction=exact_nonnegative(values["byok_fee_fraction"]),
        )
        if policy.cent_usd == Decimal(0):
            raise UsagePendingError("The original cent conversion is invalid.")
        if client is None:
            with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as owned:
                return self.reconcile(operation_id, username, client=owned)
        dispatcher = OpenRouterDispatcher(
            BudgetRuntime(
                self.service,
                username=username,
                budget_id=operation.budget_id,
                generation=operation.generation,
                phase=operation.phase,
            ),
            api_key=key,
            model=str(prices["model"]),
            role="reconciliation",
            policy=policy,
            client=client,
        )
        confirmed = dispatcher.reconcile(identity)
        if confirmed is None:
            raise UsagePendingError("The provider has not confirmed this generation's final usage.")
        usd, generation = confirmed
        charge = policy.convert(usd)
        return self.service.settle(
            operation_id,
            username,
            evidence_key=f"openrouter-generation:{identity}",
            actual_cents=charge.total,
            actual_wallet_cents=charge.wallet,
            evidence={"provider": "openrouter", "generation": generation, "provider_usd": str(usd)},
        )

    def sweep(self, *, limit: int = 100, after_id: str | None = None) -> ReconciliationPage:
        """Reconcile one bounded page and release, at no further charge, holds whose bill stays unknown.

        Only a returned (pending) hold with no quarantine evidence is released. A
        dispatched (in-flight) hold is never released. The release comes 1 minute
        after dispatched_at for UnsettleableUsageError: no provider request id and
        no stored refusal, or this worker cannot resolve the original credential.
        It comes 5 minutes after the call returned for GenerationNotFoundError.
        It comes 15 minutes after dispatched_at for every other pending reason.

        Args:
            limit: Maximum number of operations visited.
            after_id: Previous page cursor, if any.

        Returns:
            Settled, released or pending results and the next non-starving page cursor.
        """
        attempts = self.service.unsettled_operations(
            provider="openrouter",
            cost_kind="model",
            limit=limit,
            after_id=after_id,
        )
        results = []
        now = datetime.now(UTC)
        for operation_id, username in attempts:
            try:
                operation = self.reconcile(operation_id, username)
                results.append(ReconciliationResult(operation_id, operation.state))
            except UsagePendingError as error:
                returned_before = None
                if isinstance(error, UnsettleableUsageError):
                    wait = UNSETTLEABLE_RELEASE_AFTER
                elif isinstance(error, GenerationNotFoundError):
                    wait = GENERATION_NOT_FOUND_RELEASE_AFTER
                    returned_before = now - wait
                else:
                    wait = UNCONFIRMED_RELEASE_AFTER
                try:
                    operation = self.service.release_unconfirmed(
                        operation_id,
                        username,
                        dispatched_before=now - wait,
                        reason=str(error),
                        returned_before=returned_before,
                    )
                except BudgetError as release_error:
                    results.append(ReconciliationResult(operation_id, "pending", str(release_error)))
                    continue
                if operation.state == "released":
                    # A racing sweep can log a release that another sweep made; the
                    # row lock still releases the hold only once.
                    logger.info("Model hold %s is released at no further charge: %s", operation_id, error)
                results.append(
                    ReconciliationResult(
                        operation_id, operation.state, None if operation.state == "released" else str(error)
                    )
                )
            except BudgetError as error:
                results.append(ReconciliationResult(operation_id, "pending", str(error)))
        return ReconciliationPage(tuple(results), attempts[-1][0] if len(attempts) == limit else None)
