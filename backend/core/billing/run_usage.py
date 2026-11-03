"""Break one run's charges and model activity down from its billing records.

Every billed model call and sandbox charge of a run is an execution operation
on the run's budget, so summing operations adds up exactly to what the run
charged, for every optimizer alike. Rows are pre-aggregated per role, model,
stage, pair, candidate and kind; the client regroups them by any of those.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ..provider_registry import BYOK_TO_LITELLM_PROVIDER
from ..storage.models import ExecutionBudgetModel, ExecutionOperationModel, ExecutionUsageEvidenceModel
from .budget_amounts import CENT_SCALE, cents_from_units
from .model_dispatch import usage_charge
from .operation_pricing import UnpricedOperationError, exact_nonnegative
from .pricing import CENT_USD_VALUE, ModelUsage, cached_token_prices, known_token_prices, usage_cost_usd
from .usage_tags import CALLER_PROPOSER, CALLER_REFLECTION

ROLE_TASK = "task"
ROLE_REFLECTION = "reflection"
ROLE_PROPOSER = "proposer"
ROLE_SCORER = "scorer"
ROLE_SANDBOX = "sandbox"
ROLE_SETUP = "setup"
ROLE_OTHER = "other"
ROLE_ROUNDING = "rounding"

BILLING_SKYNET = "skynet"
BILLING_BYOK = "byok"
BILLING_DIRECT = "direct"
BILLING_SUBSCRIPTION = "subscription"

_CALL_STATES = ("dispatched", "pending", "settled")
_UNSETTLED_STATES = ("reserved", "dispatched", "pending")


@dataclass
class UsageRow:
    """One aggregate of operations sharing a role, model, stage, pair, candidate, billing and kind."""

    role: str
    model: str | None
    stage: str | None
    pair: str | None
    candidate: str | None
    billing: str
    kind: str | None = None
    charged_units: int = 0
    provider_cents: Decimal | None = None
    api_rate_cents: Decimal | None = None
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms_total: int = 0
    latency_calls: int = 0
    unpriced_calls: int = 0
    pending_calls: int = 0


@dataclass(frozen=True)
class OperationRecord:
    """The fields of one operation and its latest evidence that the breakdown reads."""

    phase: str
    cost_kind: str
    role: str | None
    state: str
    policy: Mapping[str, Any]
    wallet_units: int
    evidence: Mapping[str, Any]
    quoted_model: str | None = None


def _role(record: OperationRecord, *, proposer: bool) -> str:
    """Name the run role an operation served.

    Args:
        record: The operation.
        proposer: Whether the run's optimization model is a proposer agent rather than DSPy reflection;
            a ``caller`` tag on the call overrides it, since auto mode runs both on one route.

    Returns:
        One of the ``ROLE_*`` names.
    """
    if record.phase == "setup":
        return ROLE_SETUP
    if record.cost_kind == "sandbox":
        return ROLE_SANDBOX
    tags = record.evidence.get("tags")
    caller = tags.get("caller") if isinstance(tags, Mapping) else None
    if caller in (CALLER_PROPOSER, CALLER_REFLECTION):
        proposer = caller == CALLER_PROPOSER
    return {
        "task": ROLE_TASK,
        "optimization": ROLE_PROPOSER if proposer else ROLE_REFLECTION,
        "judge": ROLE_SCORER,
    }.get(record.role or "", ROLE_OTHER)


def _int(value: Any) -> int:
    """Read a nonnegative token count, treating anything else as zero."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _tokens(usage: Mapping[str, Any] | None) -> tuple[int, int]:
    """Read input and output tokens from any provider usage shape.

    Args:
        usage: OpenAI-style, Anthropic-style or Responses usage, or ``None``.

    Returns:
        ``(input, output)`` tokens; Anthropic's cache tokens count as input.
    """
    if not isinstance(usage, Mapping):
        return 0, 0
    if "prompt_tokens" in usage or "completion_tokens" in usage:
        return _int(usage.get("prompt_tokens")), _int(usage.get("completion_tokens"))
    # Anthropic reports cached input apart from ``input_tokens``; Responses folds it in.
    cached = _int(usage.get("cache_read_input_tokens")) + _int(usage.get("cache_creation_input_tokens"))
    return _int(usage.get("input_tokens")) + cached, _int(usage.get("output_tokens"))


def _byok_provider_cents(record: OperationRecord) -> Decimal | None:
    """Read a BYOK provider charge from its usage evidence.

    Args:
        record: A BYOK model operation.

    Returns:
        Provider cents, or ``None`` when no valid cost was reported.
    """
    try:
        usage = record.evidence.get("usage")
        if isinstance(usage, Mapping):
            if usage.get("is_byok") is True:
                cost = usage_charge(usage)
                if cost is not None:
                    return cost / Decimal(str(CENT_USD_VALUE))
                if usage.get("cost") is not None:
                    return None
            elif usage.get("cost") is not None:
                return exact_nonnegative(usage.get("cost")) / Decimal(str(CENT_USD_VALUE))
    except UnpricedOperationError:
        return None
    try:
        fraction = exact_nonnegative(record.policy.get("byok_fee_fraction"))
        if fraction > 0:
            return cents_from_units(record.wallet_units) / fraction
    except UnpricedOperationError:
        return None
    if not isinstance(usage, Mapping):
        return None
    keys = (
        ("prompt_tokens", "completion_tokens")
        if "prompt_tokens" in usage or "completion_tokens" in usage
        else ("input_tokens", "output_tokens")
    )
    if not all(type(usage.get(key)) is int and usage[key] >= 0 for key in keys):
        return None
    provider = str(record.evidence.get("provider") or "")
    if provider == "custom":
        return None
    model = str(record.evidence.get("model") or record.quoted_model or "")
    prefix = BYOK_TO_LITELLM_PROVIDER.get(provider, provider)
    if prefix and prefix != "openrouter" and model and not model.startswith(f"{prefix}/"):
        model = f"{prefix}/{model}"
    prices = cached_token_prices(model) if model else None
    if prices is None:
        return None
    input_tokens, output_tokens = _tokens(usage)
    cache_read = _int(usage.get("cache_read_input_tokens"))
    prompt_details = usage.get("prompt_tokens_details")
    if isinstance(prompt_details, Mapping) and "prompt_tokens" in usage:
        cache_read = _int(prompt_details.get("cached_tokens"))
    cache_write = _int(usage.get("cache_creation_input_tokens"))
    return Decimal(
        str(
            usage_cost_usd(
                ModelUsage(
                    model, input_tokens, output_tokens, cache_read_tokens=cache_read, cache_write_tokens=cache_write
                ),
                prices=prices,
            )
        )
    ) / Decimal(str(CENT_USD_VALUE))


def aggregate(
    records: Iterable[OperationRecord],
    *,
    proposer: bool,
    direct_usage: Iterable[Mapping[str, Any]] = (),
    pair: str | None = None,
) -> list[UsageRow]:
    """Group a run's operations into usage rows.

    Args:
        records: The run budget's operations.
        proposer: Whether the run's optimization model is a proposer agent.
        direct_usage: Proposer usage paid on the owner's own key, which has no operation.
        pair: Keep only operations tagged with this grid pair.

    Returns:
        Rows in first-seen order.
    """
    rows: dict[tuple[Any, ...], UsageRow] = {}

    def row_for(role: str, model: str | None, tags: Mapping[str, Any], billing: str) -> UsageRow:
        """Return the row for one grouping key, creating it on first use."""
        key = (role, model, tags.get("stage"), tags.get("pair"), tags.get("candidate"), billing, tags.get("kind"))
        if key not in rows:
            rows[key] = UsageRow(role, model, *key[2:5], billing, kind=key[6])
        return rows[key]

    for record in records:
        tags = record.evidence.get("tags") if isinstance(record.evidence.get("tags"), Mapping) else {}
        if pair is not None and tags.get("pair") != pair:
            continue
        model_call = record.cost_kind == "model"
        byok = record.policy.get("kind") == "byok_model"
        subscription = record.policy.get("kind") == "subscription_model"
        model = (str(record.evidence.get("model") or record.quoted_model or "") or None) if model_call else None
        billing = BILLING_BYOK if byok else BILLING_SUBSCRIPTION if subscription else BILLING_SKYNET
        row = row_for(_role(record, proposer=proposer), model, tags, billing)
        row.charged_units += record.wallet_units
        if byok:
            provider = _byok_provider_cents(record)
            if provider is None:
                row.unpriced_calls += 1
            else:
                row.provider_cents = (row.provider_cents or Decimal(0)) + provider
        if record.state in _UNSETTLED_STATES:
            row.pending_calls += 1
        if not model_call or record.state not in _CALL_STATES:
            continue
        row.calls += 1
        input_tokens, output_tokens = _tokens(record.evidence.get("usage"))
        row.input_tokens += input_tokens
        row.output_tokens += output_tokens
        # Display only: what the subscription's tokens would cost at API rates. It never enters
        # charged_units, so credit totals, spend caps and the ledger stay unaffected.
        if subscription and model is not None:
            usd = _subscription_api_rate_usd(record.evidence.get("usage"), model, input_tokens, output_tokens)
            if usd is not None:
                row.api_rate_cents = (row.api_rate_cents or Decimal(0)) + Decimal(str(usd)) / Decimal(
                    str(CENT_USD_VALUE)
                )
        latency = record.evidence.get("latency_ms")
        if isinstance(latency, int) and latency >= 0:
            row.latency_ms_total += latency
            row.latency_calls += 1
    if pair is None:
        for usage in direct_usage:
            model = str(usage.get("model") or "") or None
            row = row_for(ROLE_PROPOSER, model, {}, BILLING_DIRECT)
            row.calls += _int(usage.get("calls"))
            cache_read, cache_write = (
                _int(usage.get("cache_read_input_tokens")),
                _int(usage.get("cache_creation_input_tokens")),
            )
            prompt, completion = (
                _int(usage.get("prompt_tokens")) + cache_read + cache_write,
                _int(usage.get("completion_tokens")),
            )
            row.input_tokens += prompt
            row.output_tokens += completion
            if model is not None:
                usd = usage_cost_usd(
                    ModelUsage(model, prompt, completion, cache_read_tokens=cache_read, cache_write_tokens=cache_write)
                )
                row.provider_cents = (row.provider_cents or Decimal(0)) + Decimal(str(usd)) / Decimal(
                    str(CENT_USD_VALUE)
                )
    return list(rows.values())


def _subscription_api_rate_usd(usage: Any, model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Return what one subscription call would cost at API rates, in USD.

    Args:
        usage: The call's recorded usage block.
        model: The model the call ran on.
        input_tokens: Its input tokens.
        output_tokens: Its output tokens.

    Returns:
        The list-price cost the CLI dispatcher recorded at call time, else the
        current price table's cost, else ``None`` when no table prices the model.
    """
    cost = usage.get("cost") if isinstance(usage, Mapping) else None
    if isinstance(cost, int | float) and not isinstance(cost, bool) and math.isfinite(cost) and cost >= 0:
        return float(cost)
    # One lookup only: a second one could miss after a price refresh and fall back to the
    # high billing rate.
    prices = known_token_prices(model)
    if prices is None:
        return None
    return usage_cost_usd(ModelUsage(model, input_tokens, output_tokens), prices=prices)


def _model_key(model: str | None) -> str:
    """Reduce a model id to its bare name, so a route prefix doesn't break matching.

    The submitted model and the one the LM history recorded often differ only
    in their route (``openrouter/openai/gpt-4.1`` vs ``litellm_proxy/openai/gpt-4.1``).

    Args:
        model: A model id, or ``None``.

    Returns:
        The lowercased last path segment, or ``""``.
    """
    return (model or "").rsplit("/", 1)[-1].lower()


def _leg_rows(
    leg: Mapping[str, Any],
    *,
    task_model: str | None,
    reflection_model: str | None,
    reflection_role: str,
    billing: str,
    pair: str | None,
) -> list[UsageRow]:
    """Build the usage rows of one run or grid pair from its recorded result.

    Args:
        leg: A single-run result or one grid pair's result.
        task_model: Model the task program ran on.
        reflection_model: Model the optimizer reflected or proposed with.
        reflection_role: ``ROLE_REFLECTION`` or ``ROLE_PROPOSER``.
        billing: Billing label every row carries.
        pair: Grid pair tag, or ``None`` for a single run.

    Returns:
        One row per role and model, with tokens, calls and latency but no charge yet.
    """
    rows: dict[tuple[str, str | None], UsageRow] = {}

    def row_for(role: str, model: str | None) -> UsageRow:
        """Return the row for one role and model, creating it on first use."""
        if (role, model) not in rows:
            rows[(role, model)] = UsageRow(role, model, None, pair, None, billing)
        return rows[(role, model)]

    reflection_key = _model_key(reflection_model)
    usages = leg.get("usage_by_model")
    for usage in usages if isinstance(usages, list) else ():
        if not isinstance(usage, Mapping) or not isinstance(usage.get("model"), str):
            continue
        key = _model_key(usage["model"])
        reflection = bool(reflection_key) and key == reflection_key and key != _model_key(task_model)
        row = row_for(reflection_role if reflection else ROLE_TASK, usage["model"])
        row.input_tokens += _int(usage.get("input_tokens"))
        row.output_tokens += _int(usage.get("output_tokens"))
    activity = leg.get("lm_activity") if isinstance(leg.get("lm_activity"), Mapping) else {}
    for side, role, model in (
        ("generation", ROLE_TASK, task_model),
        ("reflection", reflection_role, reflection_model),
    ):
        stages = activity.get(side)
        if not isinstance(stages, Mapping):
            continue
        row = next((row for (row_role, _), row in rows.items() if row_role == role), None)
        for stats in stages.values():
            calls = _int(stats.get("calls")) if isinstance(stats, Mapping) else 0
            if not calls:
                continue
            row = row or row_for(role, model)
            row.calls += calls
            latency = stats.get("avg_response_time_ms")
            if isinstance(latency, int | float) and not isinstance(latency, bool) and latency >= 0:
                row.latency_ms_total += round(latency * calls)
                row.latency_calls += calls
    return list(rows.values())


def _usd(row: UsageRow) -> float:
    """Price a row's tokens at provider cost, or ``0.0`` when it names no model."""
    if row.model is None:
        return 0.0
    return usage_cost_usd(ModelUsage(row.model, row.input_tokens, row.output_tokens))


def _spread(rows: list[UsageRow], units: int) -> None:
    """Split a charge across rows by provider cost, falling back to tokens, then evenly.

    Args:
        rows: Rows to charge; mutated in place.
        units: The charge in cent units; the rows sum to it exactly.
    """
    if not rows or units <= 0:
        return
    weights = [_usd(row) for row in rows]
    if sum(weights) <= 0:
        weights = [float(row.input_tokens + row.output_tokens) for row in rows]
    if sum(weights) <= 0:
        weights = [1.0] * len(rows)
    total = sum(weights)
    for row, weight in zip(rows, weights, strict=True):
        row.charged_units = int(units * weight / total)
    heaviest = max(range(len(rows)), key=weights.__getitem__)
    rows[heaviest].charged_units += units - sum(row.charged_units for row in rows)


def from_result(
    result: Mapping[str, Any],
    *,
    task_model: str | None,
    reflection_model: str | None,
    proposer: bool,
    byok: bool,
    pair: str | None = None,
) -> list[UsageRow]:
    """Rebuild a run's usage rows from its own result, for runs charged without an execution budget.

    Older runs were debited once at the end from the result's ``usage_by_model``
    and left no billing operations, so this is the only record of what they
    used. The charge is the worker's ``details.billing`` stamp, split across the
    model rows so they add up to exactly what the run header shows.

    Args:
        result: The run's result, single run or grid envelope.
        task_model: The run's task model, used when the result is a single run.
        reflection_model: The run's reflection or proposer model, likewise.
        proposer: Whether the optimizer model is a proposer agent rather than DSPy reflection.
        byok: Whether the run's tokens were paid on the owner's own key.
        pair: Keep only this grid pair.

    Returns:
        Rows in result order; empty when the result recorded no usage.
    """
    reflection_role = ROLE_PROPOSER if proposer else ROLE_REFLECTION
    billing = BILLING_BYOK if byok else BILLING_SKYNET
    pairs = result.get("pair_results")
    if isinstance(pairs, list):
        rows: list[UsageRow] = []
        for leg in pairs:
            if not isinstance(leg, Mapping) or (pair is not None and str(leg.get("pair_index")) != pair):
                continue
            rows += _leg_rows(
                leg,
                task_model=leg.get("generation_model"),
                reflection_model=leg.get("reflection_model"),
                reflection_role=reflection_role,
                billing=billing,
                pair=str(leg.get("pair_index")),
            )
        return rows
    if pair is not None:
        return []
    rows = _leg_rows(
        result,
        task_model=task_model,
        reflection_model=reflection_model,
        reflection_role=reflection_role,
        billing=billing,
        pair=None,
    )
    details = result.get("details") if isinstance(result.get("details"), Mapping) else {}
    stamp = details.get("billing") if isinstance(details.get("billing"), Mapping) else {}
    units = _int(stamp.get("cents")) * CENT_SCALE if stamp.get("outcome") == "billed" else 0
    if units and not rows:
        rows.append(UsageRow(ROLE_OTHER, None, None, None, None, billing))
    _spread(rows, units)
    if byok:
        for row in rows:
            usd = _usd(row)
            row.provider_cents = Decimal(str(usd)) / Decimal(str(CENT_USD_VALUE)) if usd > 0 else None
            row.unpriced_calls = row.calls if row.provider_cents is None else 0
    return rows


def load_records(engine: Engine, budget_id: str) -> list[OperationRecord]:
    """Read a budget's operations with each one's latest evidence.

    Args:
        engine: Database engine holding the billing tables.
        budget_id: The run's execution budget.

    Returns:
        One record per operation attempt, oldest first.
    """
    with Session(engine) as session:
        operations = session.execute(
            select(ExecutionOperationModel)
            .where(ExecutionOperationModel.budget_id == budget_id)
            .order_by(ExecutionOperationModel.created_at)
        ).scalars()
        operations = list(operations)
        evidence: dict[str, Mapping[str, Any]] = {}
        if operations:
            for operation_id, document in session.execute(
                select(ExecutionUsageEvidenceModel.operation_id, ExecutionUsageEvidenceModel.evidence)
                .where(ExecutionUsageEvidenceModel.operation_id.in_([operation.id for operation in operations]))
                .order_by(ExecutionUsageEvidenceModel.created_at)
            ):
                if isinstance(document, Mapping):
                    evidence[operation_id] = {**evidence.get(operation_id, {}), **document}
        return [
            OperationRecord(
                phase=operation.phase,
                cost_kind=operation.cost_kind,
                role=operation.role,
                state=operation.state,
                policy=(operation.price_snapshot or {}).get("policy") or {},
                wallet_units=operation.actual_wallet_units,
                evidence=evidence.get(operation.id, {}),
                quoted_model=(operation.price_snapshot or {}).get("model"),
            )
            for operation in operations
        ]


def billed_cents(engine: Engine, budget_id: str) -> int | None:
    """Read how many whole cents a budget has billed to the wallet.

    Args:
        engine: Database engine holding the billing tables.
        budget_id: The run's execution budget.

    Returns:
        The billed cents, or ``None`` when the budget does not exist.
    """
    with Session(engine) as session:
        return session.execute(
            select(ExecutionBudgetModel.billed_cents).where(ExecutionBudgetModel.id == budget_id)
        ).scalar_one_or_none()


def with_rounding(rows: list[UsageRow], billed: int | None) -> list[UsageRow]:
    """Add a row for the wallet rounding its fractional charges up to whole cents.

    The wallet bills the ceiling of the run's running total, so the operations
    sum to slightly less than the charge. The extra row keeps every grouping
    adding up to exactly what the wallet charged.

    Args:
        rows: The run's usage rows.
        billed: Whole cents the budget billed, or ``None`` when unknown.

    Returns:
        ``rows``, plus a rounding row when the charge exceeds their sum.
    """
    if billed is None:
        return rows
    gap = billed * CENT_SCALE - sum(row.charged_units for row in rows)
    if gap <= 0:
        return rows
    return [
        *rows,
        UsageRow(
            role=ROLE_ROUNDING,
            model=None,
            stage=None,
            pair=None,
            candidate=None,
            billing=BILLING_SKYNET,
            charged_units=gap,
        ),
    ]


def serialize(rows: Iterable[UsageRow]) -> list[dict[str, Any]]:
    """Render rows for JSON, with cents as decimals rounded to a hundredth of a cent.

    Args:
        rows: Aggregated usage rows.

    Returns:
        JSON-ready dicts.
    """
    result = []
    for row in rows:
        document = asdict(row)
        document["charged_cents"] = float(round(cents_from_units(document.pop("charged_units")), 4))
        provider = document.pop("provider_cents")
        document["provider_cents"] = None if provider is None else float(round(provider, 4))
        api_rate = document.pop("api_rate_cents")
        document["api_rate_cents"] = None if api_rate is None else float(round(api_rate, 4))
        result.append(document)
    return result
