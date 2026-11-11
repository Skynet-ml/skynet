"""Per-run LLM usage metering for the interactive surfaces.

Optimization jobs already debit through the worker at completion; this module
gives the interactive surfaces — agent turns, interview turns, tagging
predictions — the same seam. A surface hands over the ``MeteredLM`` objects a
run used; the helper harvests their per-model token usage, prices it, and
writes the debit (with measured token counts stamped on the ledger row), all
best-effort: a metering failure never breaks the user-facing turn. It is not
free either — a failed debit is logged at ``ERROR`` (which the alert handler
forwards) and retried as a conservative charge at the fallback frontier price
for the tokens that were measured.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import replace

from ..constants import TOKEN_SOURCE_MANAGED
from ..service_gateway.language_models import (
    canonical_model_id,
    model_usages_from_history,
    served_model_from,
    usage_by_model_from_history,
)
from .pricing import ModelUsage, combine_usages, fallback_priced_usages, usages_from_breakdown
from .service import StripeBillingService, run_cost_cents

logger = logging.getLogger("app.billing.metering")


def _normalize_model_key(model: str) -> str:
    """Strip the managed-gateway transport prefix off a usage-breakdown key.

    Args:
        model: Model id as the LM reports it (``litellm_proxy/openrouter/…``
            behind the proxy, ``openrouter/…`` on the direct path).

    Returns:
        The catalog-shaped id the ledger and pricing table understand.
    """
    return canonical_model_id(model)


def _coerce_lms(language_models) -> list:
    """Return the non-``None`` LM objects behind a single-LM-or-iterable arg.

    Args:
        language_models: A single LM object or a list/tuple of them.
    """
    lms = list(language_models) if isinstance(language_models, (list, tuple)) else [language_models]
    return [lm for lm in lms if lm is not None]


def _harvest_usages(lms: list) -> tuple[list[ModelUsage], str | None]:
    """Harvest per-model usage from LMs, rekeyed to catalog-priced model ids.

    Args:
        lms: Non-empty list of the run's LM objects.

    Returns:
        The priced usage rows plus the model id to stamp on a ledger row, or
        ``([], None)`` when the LMs tracked no usage.
    """
    rows = model_usages_from_history(*lms)
    if not rows:
        return [], None
    served = served_model_from(lms[-1]) if len(lms) == 1 else None
    rekeyed = []
    for row in rows:
        normalized = _normalize_model_key(row.model)
        # An auto-routed turn's usage is keyed by the router's own id;
        # the concrete pick is what the price table knows.
        if served and len(rows) == 1:
            normalized = served
        rekeyed.append(replace(row, model=normalized))
    usages = combine_usages(rekeyed)
    return usages, usages[0].model


def debit_at_fallback_price(
    service: StripeBillingService,
    username: str,
    usages: list[ModelUsage],
    *,
    model: str | None,
    description: str,
    token_source: str = TOKEN_SOURCE_MANAGED,
    token_sources_by_model: Mapping[str, str] | None = None,
    optimization_id: str | None = None,
    settlement_key: str | None = None,
) -> int:
    """Charge measured usage at the fallback frontier rates after exact metering failed.

    The caller has already logged the original failure. This second attempt
    prices every token at the conservative fallback rate — no price lookup is
    involved — so a pricing or harvesting fault still collects for the tokens
    the platform paid for. If this attempt fails too (e.g. the database is
    down) it logs at ``ERROR`` and gives up: the turn or job must not break.

    Args:
        service: Billing service to debit through.
        username: Account to charge.
        usages: Measured usage whose token counts are trusted.
        model: Model id to stamp on the ledger row.
        description: Human label for the ledger row.
        token_source: Billing source, forwarded to :meth:`StripeBillingService.debit_run`.
        token_sources_by_model: Optional per-model source map for a mixed job.
        optimization_id: Legacy job whose held ceiling the debit may consume.
        settlement_key: Idempotency key forwarded to the debit.

    Returns:
        The cents charged, or ``0`` when there were no tokens or the debit failed.
    """
    fallback = fallback_priced_usages(usages)
    if not fallback:
        return 0
    try:
        return service.debit_run(
            username,
            fallback,
            model=model,
            description=description,
            token_source=token_source,
            token_sources_by_model=token_sources_by_model,
            optimization_id=optimization_id,
            settlement_key=settlement_key,
        )
    except Exception:
        logger.exception(
            "fallback-price debit also failed for %s (%s); usage unbilled: %s",
            username,
            description,
            _usage_summary(usages),
        )
        return 0


def _usage_summary(usages: list[ModelUsage]) -> str:
    """Render usage rows as a compact ``model=in/out`` string for log context.

    Args:
        usages: Usage rows to describe.

    Returns:
        A comma-separated summary, or ``"none"`` when empty.
    """
    return ", ".join(f"{u.model}={u.input_tokens}/{u.output_tokens}" for u in usages) or "none"


def _raw_usages(lms: list) -> list[ModelUsage]:
    """Read bare per-model token counts from LMs without any rekeying or pricing.

    The recovery source when :func:`_harvest_usages` itself raised: it uses the
    simplest aggregate the LMs expose, so a fault in the richer harvest still
    leaves token counts to charge at the fallback rate. Never raises.

    Args:
        lms: The run's LM objects.

    Returns:
        One usage row per model, or ``[]`` when nothing could be read.
    """
    try:
        breakdown = usage_by_model_from_history(*lms) or {}
        return usages_from_breakdown(breakdown)
    except Exception:
        logger.exception("could not read raw LLM token counts for fallback billing")
        return []


def meter_llm_usage(
    engine,
    username: str,
    breakdown: Mapping[str, tuple[int, int]],
    *,
    description: str,
    token_source: str = TOKEN_SOURCE_MANAGED,
) -> int:
    """Debit a per-model token breakdown that was harvested elsewhere.

    The sandboxed scorer dry run reads its ``llm()`` usage inside the child
    process and hands back only the numbers, so there is no LM object for
    :func:`meter_llm_run` to harvest. Same best-effort contract: never raises.

    Args:
        engine: SQLAlchemy engine backing the billing tables; ``None`` skips.
        username: Account the usage is billed to.
        breakdown: ``model → (input_tokens, output_tokens)`` as the LM reports it.
        description: Human label for the ledger row.
        token_source: Billing source for the call.

    Returns:
        The cents charged, or ``0`` when nothing was billed.
    """
    if engine is None or not username or not breakdown:
        return 0
    service = StripeBillingService(engine=engine)
    usages = usages_from_breakdown(breakdown)
    try:
        rekeyed: dict[str, tuple[int, int]] = {}
        for key, in_out in breakdown.items():
            normalized = _normalize_model_key(key)
            prior = rekeyed.get(normalized, (0, 0))
            rekeyed[normalized] = (prior[0] + in_out[0], prior[1] + in_out[1])
        usages = usages_from_breakdown(rekeyed)
        return service.debit_run(
            username,
            usages,
            model=next(iter(rekeyed)),
            description=description,
            token_source=token_source,
        )
    except Exception:
        logger.exception(
            "failed to debit LLM usage for %s (%s); charging at fallback price: %s",
            username,
            description,
            _usage_summary(usages),
        )
        return debit_at_fallback_price(
            service,
            username,
            usages,
            model=usages[0].model if usages else None,
            description=description,
            token_source=token_source,
        )


def estimate_run_cents(language_models, token_source: str = TOKEN_SOURCE_MANAGED) -> int:
    """Price the billable usage a set of LMs has accumulated so far.

    A ``MeteredLM``'s running totals are current at any point, so this can be
    polled mid-run — the auto-tag job's balance watch uses it to stop a bulk
    job once its accrued cost reaches the account's balance, before the final
    debit clamps. Read-only: nothing is debited. Never raises; a harvest
    failure prices as ``0`` so a watcher fails open rather than killing a run
    on a metering hiccup.

    Args:
        language_models: The run's LM objects (a single LM or an iterable).
        token_source: ``managed`` for full model cost or ``byok`` for the
            platform-fee portion only.

    Returns:
        The billable cost in cents of the usage so far (``0`` when nothing was
        tracked).
    """
    lms = _coerce_lms(language_models)
    if not lms:
        return 0
    try:
        usages, _ = _harvest_usages(lms)
        return run_cost_cents(usages, token_source)
    except Exception:
        logger.exception("failed to price in-flight LLM usage")
        return 0


def meter_llm_run(
    engine,
    username: str,
    language_models,
    *,
    description: str,
    model: str | None = None,
    token_source: str = TOKEN_SOURCE_MANAGED,
) -> int:
    """Debit the tokens a finished interactive run consumed.

    Harvests per-model usage from ``language_models``, keys an auto-routed
    run's usage by the concrete model the router served (so pricing hits the
    real price row instead of the router's unpriced group id), and debits the
    account. A run with no tracked usage (mocked LM, zero calls) writes
    nothing. Never raises — the turn already succeeded for the user, so a
    billing failure is logged at ``ERROR`` and retried at the fallback price
    (:func:`debit_at_fallback_price`) rather than surfaced on a delivered reply.

    Args:
        engine: SQLAlchemy engine backing the billing tables; ``None`` skips
            metering entirely (legacy/in-memory stores).
        username: Account the run is billed to.
        language_models: The run's LM objects (a single LM or an iterable).
        description: Human label for the ledger row (e.g. ``"Agent chat"``).
        model: Model id to stamp on the ledger row; ``None`` derives it from
            the served/requested model of the harvest.
        token_source: Billing source for the interactive model call.

    Returns:
        The cents charged, or ``0`` when nothing was billed.
    """
    if engine is None or not username:
        return 0
    # Hosted inference settles against the linked account before returning its response.
    lms = [lm for lm in _coerce_lms(language_models) if not getattr(lm, "hosted_inference", False)]
    if not lms:
        return 0
    service = StripeBillingService(engine=engine)
    usages: list[ModelUsage] = []
    try:
        usages, harvested_model = _harvest_usages(lms)
        if not usages:
            return 0
        return service.debit_run(
            username,
            usages,
            model=model or harvested_model,
            description=description,
            token_source=token_source,
        )
    except Exception:
        usages = usages or _raw_usages(lms)
        logger.exception(
            "failed to debit LLM usage for %s (%s); charging at fallback price: %s",
            username,
            description,
            _usage_summary(usages),
        )
        return debit_at_fallback_price(
            service,
            username,
            usages,
            model=model or (usages[0].model if usages else None),
            description=description,
            token_source=token_source,
        )
