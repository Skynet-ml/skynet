"""Shared helpers used by the domain routers.

Contains the small number of functions, constants, and caches that were
closures inside ``create_app`` but are needed by more than one extracted
router. Kept under a leading underscore to signal "package-internal".
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import pickle
import time
from collections import OrderedDict
from collections.abc import AsyncIterable, AsyncIterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ...billing import StripeBillingService
from ...billing.credential_safety import scrub_model_config
from ...billing.metering import estimate_run_cents, meter_llm_run
from ...config import settings
from ...constants import (
    OPTIMIZATION_TYPE_BLACKBOX,
    OPTIMIZATION_TYPE_GRID_SEARCH,
    OPTIMIZATION_TYPE_RUN,
    PAYLOAD_OVERVIEW_COMPILE_KWARGS,
    PAYLOAD_OVERVIEW_MODEL_NAME,
    PAYLOAD_OVERVIEW_MODULE_KWARGS,
    PAYLOAD_OVERVIEW_MODULE_NAME,
    PAYLOAD_OVERVIEW_OPTIMIZATION_TYPE,
    PAYLOAD_OVERVIEW_OPTIMIZER_KWARGS,
    PAYLOAD_OVERVIEW_SEED,
    PAYLOAD_OVERVIEW_SHUFFLE,
    PAYLOAD_OVERVIEW_SIGNATURE_CODE,
    PAYLOAD_OVERVIEW_SPLIT_FRACTIONS,
    PAYLOAD_OVERVIEW_TOOL_SOURCE,
    PAYLOAD_OVERVIEW_WORKFLOW,
    TOKEN_SOURCE_MANAGED,
    WORKER_RESTART_CAUSE,
)
from ...exceptions import ServiceError
from ...models import (
    GridSearchResponse,
    OptimizationStatus,
    OptimizationSummaryResponse,
    ProgramArtifact,
    RunResponse,
    WorkflowSpec,
)
from ...models.results import result_scores
from ...models.serve import WorkflowNodeTrace
from ...registry import ResolverError, resolve_module_factory
from ...service_gateway.language_models import usage_by_model_from_history
from ...service_gateway.optimization.data import load_signature_from_code
from ...service_gateway.optimization.retrying_react import RetryingReActV2
from ...service_gateway.optimization.tool_overlay import (
    ToolSchemaDriftError,
    _apply_bundle_tool_overrides,
    _apply_tool_name_overrides,
    _assert_tool_set_matches,
)
from ...service_gateway.optimization.training_ground.run_react import (
    resolve_react_tools,
)
from ...service_gateway.optimization.workflow import build_workflow_program, workflow_tool_users
from ...storage.models import ContinuousCycleModel, ContinuousPlanModel
from ..auth import AuthenticatedUser, is_admin
from ..converters import (
    compute_elapsed,
    extract_estimated_remaining,
    job_owner,
    overview_to_base_fields,
    parse_overview,
    parse_timestamp,
    status_to_job_status,
)
from ..errors import DomainError
from ..folder_access import folder_ids_for_runs, run_folder_roles
from ..response_limits import AGENT_MAX_TEXT, truncate_text
from ..sharing_access import (
    MEMBER_ROLES,
    ShareRole,
    get_grant,
    list_grants_for_user,
    role_rank,
)
from .constants import TERMINAL_STATUSES

logger = logging.getLogger(__name__)

_USER_FACING_OPTIMIZATION_TYPES = frozenset(
    {OPTIMIZATION_TYPE_RUN, OPTIMIZATION_TYPE_GRID_SEARCH, OPTIMIZATION_TYPE_BLACKBOX}
)


class _BoundedProgramCache(OrderedDict[str, Any]):
    """OrderedDict-backed LRU for deserialized DSPy programs.

    A plain dict here grew without bound — every served optimization pinned
    its compiled module in process memory until the API restarted. This
    bounded variant evicts the least-recently-used entry once
    ``settings.program_cache_max_entries`` is exceeded so a long-lived API
    process can't be DoS'd by serving many distinct optimizations.
    """

    def __init__(self) -> None:
        """Initialise an empty cache; capacity is read lazily from settings."""
        super().__init__()

    @property
    def _max_entries(self) -> int:
        """Resolve the live cache ceiling from application settings.

        Reading on every mutation lets test fixtures override the cap by
        tweaking ``settings.program_cache_max_entries`` between cases without
        re-importing this module.
        """
        return max(int(settings.program_cache_max_entries), 1)

    def __setitem__(self, key: str, value: Any) -> None:
        """Insert ``value`` at most-recently-used and evict if past capacity.

        Args:
            key: Cache key (optimization id or pair-scoped key).
            value: Deserialized DSPy program object.
        """
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        while len(self) > self._max_entries:
            self.popitem(last=False)

    def __getitem__(self, key: str) -> Any:
        """Return the cached program and mark it most-recently-used.

        Args:
            key: Cache key to look up.

        Returns:
            The previously stored program object.

        Raises:
            KeyError: When ``key`` is not present in the cache.
        """
        value = super().__getitem__(key)
        self.move_to_end(key)
        return value


# Cache deserialized programs to avoid repeated pickle loads.
# Keyed by optimization_id (for single runs + grid-search best pair) or
# f"{optimization_id}_pair_{pair_index}" (for per-pair serving). Bounded by
# ``settings.program_cache_max_entries`` to cap process resident memory.
_program_cache: _BoundedProgramCache = _BoundedProgramCache()


def clear_program_cache() -> None:
    """Clear the module-level deserialized-program cache.

    Intended for test teardown; production code should not call this.
    """
    _program_cache.clear()


def enforce_llm_balance(job_store, username: str) -> None:
    """Refuse an interactive LLM turn for an account below the minimum turn balance.

    The turn-surface twin of the submit gate: agent chats, interview turns and
    tagging predictions spend managed tokens and are billed only after the
    turn, so the account must hold at least
    ``settings.interactive_min_balance_cents`` (never less than one cent)
    before the LLM call. Otherwise a 1-cent balance could buy a turn costing
    far more, with the overrun absorbed at the balance floor. A store with no
    SQL engine (legacy/in-memory) skips the gate, matching the submit path.

    Args:
        job_store: Job-store instance whose ORM engine backs the billing tables.
        username: Account attempting the turn.

    Raises:
        DomainError: 402 when the account's spendable balance is below the
            minimum turn balance (including accounts carrying refund debt).
    """
    engine = getattr(job_store, "engine", None)
    if engine is None or not username:
        return
    floor = max(1, settings.interactive_min_balance_cents)
    if StripeBillingService(engine=engine).spendable_cents(username) >= floor:
        return
    raise DomainError("billing.insufficient_funds", status=402)


async def stream_with_llm_metering(
    source: AsyncIterable[dict[str, Any]],
    *,
    job_store,
    username: str,
    description: str,
    usage_sink: list,
    token_source: str = TOKEN_SOURCE_MANAGED,
) -> AsyncIterator[dict[str, Any]]:
    """Mirror SSE events and meter the turn's LLM usage when the stream ends.

    Wraps a turn's event stream; on teardown — normal completion, error, or
    the client dropping the SSE connection mid-turn — it debits and
    Stripe-meters whatever usage the LMs in ``usage_sink`` accumulated. The
    sink is harvested in ``finally`` (not on the ``done`` event) because the
    frontend routinely tears streams down early, and a ``MeteredLM``'s running
    totals are current at any point. The synchronous billing write is
    offloaded to a worker thread, mirroring the persistence wrapper.

    Args:
        source: Upstream async event stream for one turn.
        job_store: Job-store whose engine backs the billing tables; a store
            without an engine streams unmetered.
        username: Account the turn is billed to.
        description: Ledger-row label for the charge (e.g. ``"Agent chat"``).
        usage_sink: List the run function appends its ``MeteredLM``(s) to.
        token_source: Billing source for this interactive model call.

    Yields:
        The upstream events; a ``done`` event gains a ``stats`` block with the
        turn's token usage, cost and timing. When model calls made after
        ``done`` (late thinking labels) changed the usage, one last
        ``turn_stats`` event carries the full block, so the shown cost is
        what gets debited.
    """
    started = time.monotonic()
    first_token_at: float | None = None
    done_stats: dict[str, Any] | None = None
    try:
        async for event in source:
            name = event.get("event")
            if name == "message_patch" and first_token_at is None:
                first_token_at = time.monotonic()
            elif name == "done" and isinstance(event.get("data"), dict):
                done_stats = _turn_stats(usage_sink, token_source, started, first_token_at)
                event = {**event, "data": {**event["data"], "stats": done_stats}}
            yield event
        if done_stats is not None:
            final = _turn_stats(usage_sink, token_source, started, first_token_at)
            usage = ("input_tokens", "output_tokens", "cents")
            if any(final[key] != done_stats[key] for key in usage):
                # Timings stay as of ``done``: the reply was over by then.
                yield {"event": "turn_stats", "data": {"stats": {**done_stats, **{k: final[k] for k in usage}}}}
    finally:
        engine = getattr(job_store, "engine", None)
        if engine is not None and usage_sink:
            try:
                await asyncio.to_thread(
                    meter_llm_run,
                    engine,
                    username,
                    list(usage_sink),
                    description=description,
                    token_source=token_source,
                )
            except Exception:
                logger.exception("LLM turn metering failed for %s", username)


def _turn_stats(
    usage_sink: list,
    token_source: str,
    started: float,
    first_token_at: float | None,
) -> dict[str, Any]:
    """Summarize a finished turn for the reply's info popover.

    Args:
        usage_sink: The turn's LM objects.
        token_source: Billing source, so the cost matches what gets debited.
        started: ``time.monotonic()`` when the stream opened.
        first_token_at: ``time.monotonic()`` of the first reply token, if any.

    Returns:
        Token counts (``None`` when untracked), cents and millisecond timings.
        ``reply_output_tokens`` leaves out LMs marked ``side_call`` (such as
        thinking labels), which run beside the reply and would inflate its speed.
    """
    now = time.monotonic()
    breakdown = usage_by_model_from_history(*usage_sink) if usage_sink else None
    input_tokens = sum(pair[0] for pair in breakdown.values()) if breakdown else None
    output_tokens = sum(pair[1] for pair in breakdown.values()) if breakdown else None
    reply_lms = [lm for lm in usage_sink if not getattr(lm, "side_call", False)]
    reply_breakdown = usage_by_model_from_history(*reply_lms) if reply_lms else None
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reply_output_tokens": sum(pair[1] for pair in reply_breakdown.values()) if reply_breakdown else None,
        "cents": estimate_run_cents(usage_sink, token_source) if breakdown else None,
        "duration_ms": round((now - started) * 1000),
        "ttft_ms": round((first_token_at - started) * 1000) if first_token_at is not None else None,
    }


# Idle gap after which the SSE serializer emits a comment line to keep the
# connection alive through proxy/LB idle timeouts (commonly 30-60s).
SSE_KEEPALIVE_SECONDS = 15.0


async def sse_from_events(
    source: AsyncIterable[dict[str, Any]],
) -> AsyncIterator[str]:
    """Serialize an ``{event, data}`` async iterable as Server-Sent Events.

    Every SSE route in this codebase shares the same formatting contract:
    each yielded mapping has an ``event`` name and a ``data`` dict that is
    JSON-encoded (``ensure_ascii=False``) and emitted as
    ``"event: <name>\\ndata: <json>\\n\\n"``. Centralising it here lets
    route handlers stay free of nested ``event_generator`` closures.

    A ``: keep-alive`` comment is emitted whenever the source stays quiet for
    ``SSE_KEEPALIVE_SECONDS`` — a model with a long time-to-first-token
    otherwise leaves the connection idle long enough for an intermediary
    (ingress/LB idle timeout) to silently drop it mid-turn.

    A mapping that also carries an ``id`` gets an ``id: <id>`` line, which a
    resumable stream's client echoes back to resume after that event.

    Args:
        source: Async iterable yielding ``{"event": str, "data": dict}``
            mappings, optionally with an ``id``.

    Yields:
        SSE-formatted strings ready for ``StreamingResponse``.
    """
    iterator = aiter(source)
    next_event: asyncio.Task | None = None
    try:
        while True:
            if next_event is None:
                next_event = asyncio.ensure_future(anext(iterator))
            done, _ = await asyncio.wait({next_event}, timeout=SSE_KEEPALIVE_SECONDS)
            if not done:
                yield ": keep-alive\n\n"
                continue
            try:
                event = next_event.result()
            except StopAsyncIteration:
                next_event = None
                return
            next_event = None
            name = event["event"]
            payload = json.dumps(event["data"], ensure_ascii=False, default=str)
            event_id = event.get("id")
            prefix = f"id: {event_id}\n" if event_id is not None else ""
            yield f"{prefix}event: {name}\ndata: {payload}\n\n"
    finally:
        if next_event is not None:
            next_event.cancel()


def strip_api_key(d: dict) -> dict:
    """Return a deep copy of a model configuration without credentials.

    Args:
        d: Model settings that may contain inline or nested credential fields.

    Returns:
        A copy safe for persistence and API responses.
    """
    return scrub_model_config(d)


def stable_seed(optimization_id: str) -> int:
    """Derive a process-stable 31-bit RNG seed from an optimization id.

    Python's built-in :func:`hash` is salted per process via
    ``PYTHONHASHSEED``, so the same id maps to a different int in every
    worker. Read paths (``/dataset``, ``/test-results``,
    ``/pair/{i}/test-results``) recompute the train/val/test split with
    this seed when the payload's stored seed is missing — using
    :func:`hash` there silently desyncs splits across workers and breaks
    UI index remapping. SHA256 truncated to 31 bits keeps the same numeric
    shape but is byte-stable across processes.

    Args:
        optimization_id: Optimization identifier to derive a seed from.

    Returns:
        A non-negative ``int`` strictly less than ``2 ** 31``.
    """
    digest = hashlib.sha256(optimization_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % (2**31)


def compute_task_fingerprint(
    signature_code: str,
    metric_code: str,
    dataset: list[dict[str, Any]],
) -> str:
    """Return a stable SHA256 fingerprint identifying the ML task.

    Two jobs share a fingerprint iff their signature source, metric source,
    and dataset content are byte-identical. The stable identity makes default
    data splits repeatable across submissions and retries.

    Args:
        signature_code: Source code of the user's DSPy signature.
        metric_code: Source code of the user's metric function.
        dataset: The full list of dataset rows used for the optimization.

    Returns:
        A hex-encoded SHA256 digest derived from the three inputs.
    """
    dataset_blob = json.dumps(dataset, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    dataset_hash = hashlib.sha256(dataset_blob.encode("utf-8")).hexdigest()
    task_blob = f"{signature_code}\x00{metric_code}\x00{dataset_hash}"
    return hashlib.sha256(task_blob.encode("utf-8")).hexdigest()


def _grant_role(job_store, optimization_id: str, username: str) -> ShareRole | None:
    """Resolve a member grant's role for ``username`` on an optimization.

    Opens a short-lived session on the job store's SQLAlchemy engine to read
    the ``optimization_share_grants`` row and the role the run inherits from
    its folder (see :mod:`core.api.folder_access`); the better of the two wins. Job stores without an ``engine``
    (the in-memory/local store used in tests and offline mode) carry no grant
    table, so this returns ``None`` and access falls back to owner/admin only.

    Args:
        job_store: Job-store whose ``engine`` backs the grant table.
        optimization_id: Optimization the grant would apply to.
        username: Caller's username (compared case-insensitively by the query).

    Returns:
        The grant's :class:`ShareRole`, or ``None`` when there is no grant or
        the store exposes no grant-bearing engine.
    """
    engine = getattr(job_store, "engine", None)
    if engine is None:
        return None
    with Session(engine) as session:
        grant = get_grant(session, optimization_id, username)
        inherited = run_folder_roles(session, [optimization_id], username).get(optimization_id)
    candidates = [ShareRole(r) for r in (grant.role if grant else None, inherited) if r in MEMBER_ROLES]
    return max(candidates, key=role_rank) if candidates else None


def load_job_with_role(
    job_store,
    optimization_id: str,
    user: AuthenticatedUser,
    *,
    include_payload: bool = True,
) -> tuple[dict[str, Any], ShareRole]:
    """Load a job row and resolve the caller's effective role on it.

    Access is granted to the owner, any admin, or an invited member (Google-
    Drive-style grants — see :mod:`core.api.sharing_access`). A caller with no
    ownership and no grant gets 404 (not 403): not leaking existence keeps
    unauthorized observers from confirming an ID exists.

    Args:
        job_store: Job-store the row is read from.
        optimization_id: Optimization id to load.
        user: Authenticated caller.
        include_payload: When ``False``, the row's ``payload`` JSONB is never
            materialized — the status-poll endpoint passes ``False`` because
            its response carries no raw payload.

    Returns:
        ``(job_row, effective_role)`` where ``effective_role`` is
        :attr:`ShareRole.owner` for the creator/admin or the member grant's tier.

    Raises:
        DomainError: 404 when the id is unknown or the caller has no access.
    """
    try:
        # Narrow through get_job_no_payload so store doubles that predate the
        # kwarg fall back to the full read instead of a TypeError.
        if include_payload:
            job_data = job_store.get_job(optimization_id)
        else:
            job_data = get_job_no_payload(job_store, optimization_id)
    except KeyError:
        raise DomainError("optimization.not_found", status=404, optimization_id=optimization_id) from None
    overview = parse_overview(job_data)
    optimization_type = (
        overview.get(PAYLOAD_OVERVIEW_OPTIMIZATION_TYPE) or job_data.get("optimization_type") or OPTIMIZATION_TYPE_RUN
    )
    if optimization_type not in _USER_FACING_OPTIMIZATION_TYPES:
        raise DomainError("optimization.not_found", status=404, optimization_id=optimization_id)
    if is_admin(user):
        return job_data, ShareRole.owner
    owner = job_owner(job_data)
    username = user.username.strip().lower()
    if owner is not None and owner == username:
        return job_data, ShareRole.owner
    role = _grant_role(job_store, optimization_id, username)
    if role is not None:
        return job_data, role
    raise DomainError("optimization.not_found", status=404, optimization_id=optimization_id)


def load_job_for_user(
    job_store,
    optimization_id: str,
    user: AuthenticatedUser,
) -> dict[str, Any]:
    """Load a job row, enforcing access (owner, admin, or invited member).

    The viewer-floor gate: any caller with access (owner / admin / a grant of
    any tier) gets the row; everyone else 404s. Routes that mutate require a
    higher tier — see :func:`require_role_at_least`.

    Args:
        job_store: Job-store the row is read from.
        optimization_id: Optimization id to load.
        user: Authenticated caller.

    Returns:
        The raw job-row mapping.

    Raises:
        DomainError: 404 when the id is unknown or the caller has no access.
    """
    job_data, _role = load_job_with_role(job_store, optimization_id, user)
    return job_data


def require_role_at_least(
    job_store,
    optimization_id: str,
    user: AuthenticatedUser,
    minimum: ShareRole,
) -> tuple[dict[str, Any], ShareRole]:
    """Load a job row, requiring the caller's effective role to meet ``minimum``.

    A caller with access but below the required tier (e.g. a viewer hitting a
    delete route) gets 403 — they already know the run exists, so hiding it as
    404 would be misleading. A caller with no access at all still 404s (via
    :func:`load_job_with_role`).

    Args:
        job_store: Job-store the row is read from.
        optimization_id: Optimization id to load.
        user: Authenticated caller.
        minimum: The lowest :class:`ShareRole` permitted to proceed.

    Returns:
        ``(job_row, effective_role)`` for a caller meeting the tier.

    Raises:
        DomainError: 404 when unknown/inaccessible; 403 when the caller has
            access but a role below ``minimum``.
    """
    job_data, role = load_job_with_role(job_store, optimization_id, user)
    if role_rank(role) < role_rank(minimum):
        raise DomainError(
            "optimization.insufficient_role",
            status=403,
            optimization_id=optimization_id,
            required=str(minimum),
            role=str(role),
        )
    return job_data, role


def grant_roles_for(job_store, optimization_ids: list[str], username: str) -> dict[str, str]:
    """Batch-resolve the caller's grant roles across many optimizations.

    Direct grants and folder-inherited roles are merged, keeping the better
    role per id (see :func:`list_grants_for_user`, :func:`run_folder_roles`); stores
    without a grant-bearing ``engine`` return ``{}``.

    Args:
        job_store: Job-store whose ``engine`` backs the grant table.
        optimization_ids: Ids to scope the lookup to.
        username: Caller's username.

    Returns:
        ``{optimization_id: role}`` for the caller's intersecting grants.
    """
    engine = getattr(job_store, "engine", None)
    if engine is None:
        return {}
    with Session(engine) as session:
        direct = list_grants_for_user(session, optimization_ids, username)
        inherited = run_folder_roles(session, optimization_ids, username)
    merged = dict(direct)
    for oid, role in inherited.items():
        if oid not in merged or role_rank(role) > role_rank(merged[oid]):
            merged[oid] = role
    return merged


def run_folder_ids(job_store, optimization_ids: list[str]) -> dict[str, str]:
    """Return the folder each run is filed in, for runs that are filed.

    Args:
        job_store: Job-store whose ``engine`` backs the folder tables.
        optimization_ids: Runs to look up.

    Returns:
        ``{optimization_id: folder_id}``; empty for stores without an engine.
    """
    engine = getattr(job_store, "engine", None)
    if engine is None or not optimization_ids:
        return {}
    with Session(engine) as session:
        return folder_ids_for_runs(session, optimization_ids)


def automated_run_ids(job_store, optimization_ids: list[str]) -> set[str]:
    """Return the runs that belong to a continuous plan.

    A run belongs when a plan was enabled from it, it is a plan's current
    version, or a plan's scheduled check started it.

    Args:
        job_store: Job-store whose ``engine`` backs the plan tables.
        optimization_ids: Runs to look up.

    Returns:
        The ids among ``optimization_ids`` that belong to a plan; empty for stores without an engine.
    """
    engine = getattr(job_store, "engine", None)
    if engine is None or not optimization_ids:
        return set()
    wanted = set(optimization_ids)
    with Session(engine) as session:
        plans = session.execute(
            select(ContinuousPlanModel.source_optimization_id, ContinuousPlanModel.incumbent_optimization_id).where(
                or_(
                    ContinuousPlanModel.source_optimization_id.in_(wanted),
                    ContinuousPlanModel.incumbent_optimization_id.in_(wanted),
                )
            )
        ).all()
        started = session.scalars(
            select(ContinuousCycleModel.optimization_id).where(ContinuousCycleModel.optimization_id.in_(wanted))
        ).all()
    return wanted & ({run for pair in plans for run in pair} | set(started))


def get_job_no_payload(job_store, optimization_id: str) -> dict[str, Any]:
    """Read a job row without materializing its payload when the store can.

    Bulk ownership/metadata paths only read the overview and owner, so the
    (potentially multi-MB) payload JSONB is skipped on stores that support the
    narrowing kwarg; older store doubles fall back to the full read.

    Args:
        job_store: Job-store to read from.
        optimization_id: Row to fetch.

    Returns:
        The job row (``payload`` reported as ``None`` when deferred).

    Raises:
        KeyError: When the job does not exist.
    """
    try:
        return job_store.get_job(optimization_id, include_payload=False)
    except TypeError:
        return job_store.get_job(optimization_id)


def filter_ids_at_least(
    job_store,
    optimization_ids: list[str],
    user: AuthenticatedUser,
    minimum: ShareRole,
) -> tuple[list[str], list[str]]:
    """Split ids into ``(allowed, denied)`` by whether the caller meets ``minimum``.

    The bulk-action counterpart of :func:`require_role_at_least`: admins keep
    every id; otherwise an id is allowed when the caller is its owner or holds a
    grant whose tier is at least ``minimum``. Unknown ids and below-tier ids land
    in ``denied`` so the caller can mark them skipped.

    Args:
        job_store: Job-store used to read each row's owner.
        optimization_ids: Already-deduplicated list of ids.
        user: Authenticated caller.
        minimum: The lowest :class:`ShareRole` permitted per id.

    Returns:
        ``(allowed, denied)``.
    """
    if is_admin(user):
        return list(optimization_ids), []
    username = user.username.strip().lower()
    min_rank = role_rank(minimum)
    granted = grant_roles_for(job_store, optimization_ids, username)
    allowed: list[str] = []
    denied: list[str] = []
    for oid in optimization_ids:
        try:
            job_data = get_job_no_payload(job_store, oid)
        except KeyError:
            denied.append(oid)
            continue
        owner = job_owner(job_data)
        if owner is not None and owner == username:
            role: ShareRole | None = ShareRole.owner
        else:
            grant_role = granted.get(oid)
            role = ShareRole(grant_role) if grant_role in MEMBER_ROLES else None
        if role is not None and role_rank(role) >= min_rank:
            allowed.append(oid)
        else:
            denied.append(oid)
    return allowed, denied


def _mb(num_bytes: int) -> float:
    """Return ``num_bytes`` as megabytes rounded to one decimal for messages."""
    return round(num_bytes / (1024 * 1024), 1)


def enforce_job_quota(job_store, username: str, incoming_jobs: int = 1) -> None:
    """Raise if creating ``incoming_jobs`` would put the user over their saved-job cap.

    The cap resolves through the store's live admin override, then the Skynet
    Pro cap, then ``max_jobs_per_user``; an override of ``None`` means
    unlimited. Runs beside :func:`enforce_storage_quota`, which bounds bytes
    rather than rows.

    Args:
        job_store: Store exposing ``count_jobs`` and, when it has a database,
            ``get_effective_user_quota``.
        username: The user creating the jobs.
        incoming_jobs: How many jobs the request creates (a clone may make several).

    Raises:
        DomainError: ``quota.reached`` (HTTP 409) when the user would exceed the cap.
    """
    resolver = getattr(job_store, "get_effective_user_quota", None)
    quota = resolver(username) if callable(resolver) else settings.get_user_quota(username)
    if quota is None:
        return
    if job_store.count_jobs(username=username) + incoming_jobs > quota:
        raise DomainError("quota.reached", status=409, quota=quota)


def enforce_storage_quota(job_store, username: str, incoming_bytes: int) -> None:
    """Raise if persisting ``incoming_bytes`` would exceed the storage budget.

    The unified per-user storage total (jobs, datasets, logs, agent chats,
    staged uploads, embeddings) plus the incoming write is compared against the
    user's effective byte budget. This is the single gate that supersedes the
    legacy per-job count cap and the per-library dataset quota.

    Args:
        job_store: Store exposing ``get_effective_user_storage_quota`` and
            ``compute_user_storage``.
        username: The user the budget belongs to.
        incoming_bytes: Size of the not-yet-persisted write (payload or dataset).

    Raises:
        DomainError: ``user.storage.quota_exceeded`` (HTTP 409) when the user's
            total would cross their budget.
    """
    quota = job_store.get_effective_user_storage_quota(username)
    used = job_store.compute_user_storage(username).total
    if used + incoming_bytes <= quota:
        return
    raise DomainError(
        "user.storage.quota_exceeded",
        status=409,
        used_mb=_mb(used),
        quota_mb=_mb(quota),
        incoming_mb=_mb(incoming_bytes),
    )


def is_resumable(job_store: Any, job_data: dict) -> bool:
    """Return whether a stopped run can be resumed in place.

    True only for the narrow cases the Resume affordance targets: a terminal
    ``failed``/``cancelled`` run that still has attempts left under
    ``job_max_attempts``, a manually ``paused`` run, or a run ``stopped`` at
    its spending limit, each with a saved GEPA checkpoint. A top-level run the
    orphan sweep stopped as interrupted needs no checkpoint: it starts again
    from the beginning. The cheap, indexed checkpoint lookup is gated behind
    the status/attempts checks so list pages query only the few candidate rows,
    never the whole page.

    Args:
        job_store: The job store used to test for a saved checkpoint.
        job_data: Raw job row from the store.

    Returns:
        ``True`` when the run should offer Resume rather than Restart.
    """
    optimization_id = _resume_candidate_id(job_data)
    if not optimization_id:
        return False
    return _has_resumable_state(job_store, optimization_id) or _restarts_from_beginning(job_store, job_data)


def is_interrupted_stop(job_data: dict) -> bool:
    """Return whether the orphan sweep stopped the run because its usage did not settle.

    A resume of such a run may start again without a checkpoint. The marker
    stays while that resume waits for the usage.

    Args:
        job_data: Raw job row from the store.

    Returns:
        ``True`` when the run's recovery carries the interrupted budget generation.
    """
    recovery = job_data.get("recovery")
    return isinstance(recovery, dict) and recovery.get("interrupted_budget_generation") is not None


def is_worker_restart_stop(job_data: dict) -> bool:
    """Return whether an interrupted stop came from a worker restart, not from the run.

    Only then is a resume recovery that neither uses up an attempt nor meets
    the cap. A crash also ends in an interrupted stop, and a marker without a
    cause predates this check, so both still count, or a run that keeps
    crashing its worker would loop forever.

    Args:
        job_data: Raw job row from the store.

    Returns:
        ``True`` for an interrupted stop whose recovery cause is a worker restart.
    """
    return is_interrupted_stop(job_data) and job_data["recovery"].get("cause") == WORKER_RESTART_CAUSE


def _restarts_from_beginning(job_store: Any, job_data: dict) -> bool:
    """Return whether an interrupted run with no checkpoint can start again from the beginning.

    Args:
        job_store: The job store used to test for a saved checkpoint.
        job_data: Raw job row from the store.

    Returns:
        ``True`` for an interrupted top-level run that has no checkpoint.
    """
    if not is_interrupted_stop(job_data) or job_data.get("parent_optimization_id"):
        return False
    has_checkpoint = getattr(job_store, "has_gepa_checkpoint", None)
    return callable(has_checkpoint) and not has_checkpoint(job_data["optimization_id"])


def _resume_candidate_id(job_data: dict) -> str | None:
    """Apply the cheap (no-query) resume gates and return the job id when they pass.

    Shared by :func:`is_resumable` (per-row) and :func:`resumable_id_flags`
    (batched) so both paths gate identically before the checkpoint lookup.

    Args:
        job_data: Raw job row from the store.

    Returns:
        The optimization id when the row is a resume candidate, else ``None``.
    """
    status = status_to_job_status(job_data.get("status", "pending"))
    budget_stop = is_budget_stop(status, job_data)
    resumable_status = status in {OptimizationStatus.failed, OptimizationStatus.cancelled, OptimizationStatus.paused}
    if not resumable_status and not budget_stop:
        return None
    # Grid searches are retired: a stored grid stays readable but never resumes.
    if parse_overview(job_data).get(PAYLOAD_OVERVIEW_OPTIMIZATION_TYPE) == OPTIMIZATION_TYPE_GRID_SEARCH:
        return None
    # A manual pause or a budget stop is user-driven, and a worker restart is not
    # the run's fault, so each is exempt from the attempt cap; only other
    # failed/cancelled runs are bounded by it.
    cap_exempt = status == OptimizationStatus.paused or budget_stop or is_worker_restart_stop(job_data)
    if not cap_exempt and int(job_data.get("attempts") or 0) >= settings.job_max_attempts:
        return None
    return job_data.get("optimization_id") or None


def is_budget_stop(status: OptimizationStatus, job_data: dict) -> bool:
    """Return whether the run halted at its spending limit with its work intact.

    Args:
        status: Parsed status of the row.
        job_data: Raw job row from the store.

    Returns:
        ``True`` for a ``stopped`` row whose stop reason is the budget limit,
        which raising the limit lets continue from its checkpoint.
    """
    return status == OptimizationStatus.stopped and job_data.get("stop_reason") == "budget_reached"


def resumable_id_flags(job_store: Any, rows: list[dict]) -> set[str]:
    """Return the ids in ``rows`` whose runs can offer Resume, in one batch.

    The batched counterpart of :func:`is_resumable` for list pages: the cheap
    gates run in Python, then a single ``IN (...)`` existence query covers all
    candidates instead of one probe per row (up to page-size round trips).
    Stores without the batch method fall back to the per-row probe with
    identical results.

    Args:
        job_store: The job store used to test for saved checkpoints.
        rows: Raw job rows from the store.

    Returns:
        The set of resumable optimization ids.
    """
    candidates = [oid for row in rows if (oid := _resume_candidate_id(row))]
    if not candidates:
        return set()
    restarts = {
        row["optimization_id"]
        for row in rows
        if row.get("optimization_id") in candidates and _restarts_from_beginning(job_store, row)
    }
    batch = getattr(job_store, "resumable_state_ids", None)
    if callable(batch):
        return set(batch(candidates)) | restarts
    return {oid for oid in candidates if _has_resumable_state(job_store, oid)} | restarts


def is_pausable(job_store: Any, job_data: dict) -> bool:
    """Return whether a running optimization can be manually paused.

    True only while the run is actively ``running`` (not a grid) and already has
    saved optimizer state, so a pause is guaranteed to be resumable — pausing
    before the first checkpoint would otherwise strand the run. The checkpoint
    lookup is gated behind the status/type checks so it runs for the single
    candidate row only.

    Args:
        job_store: The job store used to test for a saved checkpoint.
        job_data: Raw job row from the store.

    Returns:
        ``True`` when the run should offer a Pause control.
    """
    optimization_id = _pause_candidate_id(job_data)
    if not optimization_id:
        return False
    return _has_resumable_state(job_store, optimization_id)


def _pause_candidate_id(job_data: dict) -> str | None:
    """Apply the cheap pause gates and return the job id when they pass.

    Args:
        job_data: Raw job row from the store.

    Returns:
        The optimization id for a running non-grid row, else ``None``.
    """
    status = status_to_job_status(job_data.get("status", "pending"))
    if status != OptimizationStatus.running:
        return None
    if parse_overview(job_data).get(PAYLOAD_OVERVIEW_OPTIMIZATION_TYPE) == OPTIMIZATION_TYPE_GRID_SEARCH:
        return None
    return job_data.get("optimization_id") or None


def pausable_id_flags(job_store: Any, rows: list[dict]) -> set[str]:
    """Return the ids in ``rows`` that can offer Pause, in one batch.

    Args:
        job_store: The job store used to test for saved checkpoints.
        rows: Raw job rows from the store.

    Returns:
        The set of pausable optimization ids.
    """
    candidates = [oid for row in rows if (oid := _pause_candidate_id(row))]
    if not candidates:
        return set()
    batch = getattr(job_store, "resumable_state_ids", None)
    if callable(batch):
        return set(batch(candidates))
    return {oid for oid in candidates if _has_resumable_state(job_store, oid)}


def _has_resumable_state(job_store: Any, optimization_id: str) -> bool:
    """Return whether saved state exists to resume from (single checkpoint or any grid pair).

    A single run is resumable from its GEPA checkpoint; a grid is resumable when
    any pair has an in-flight checkpoint OR any pair already finished (so the rest
    can be re-run while finished pairs are kept).

    Args:
        job_store: The job store to query.
        optimization_id: The job id to test.

    Returns:
        ``True`` when there is state to resume from.
    """
    compatible = getattr(job_store, "has_compatible_gepa_checkpoint", None)
    if callable(compatible):
        return bool(compatible(optimization_id))
    has_checkpoint = getattr(job_store, "has_gepa_checkpoint", None)
    if callable(has_checkpoint) and has_checkpoint(optimization_id):
        return True
    has_pairs = getattr(job_store, "has_grid_pair_results", None)
    return bool(callable(has_pairs) and has_pairs(optimization_id))


def build_summary(job_data: dict) -> OptimizationSummaryResponse:
    """Build a compact dashboard-card summary from a raw job dict.

    Handles both single-run and grid-search jobs. For grid search the best pair
    is used as the representative result. Live pair counters fall back to
    ``latest_metrics`` while the sweep is still in progress.

    Args:
        job_data: Raw job row from the job store.

    Returns:
        A populated :class:`OptimizationSummaryResponse` for dashboard rendering.
    """
    created_at = parse_timestamp(job_data.get("created_at")) or datetime.now(UTC)
    started_at = parse_timestamp(job_data.get("started_at"))
    completed_at = parse_timestamp(job_data.get("completed_at"))
    overview = parse_overview(job_data)
    job_status = status_to_job_status(job_data.get("status", "pending"))
    optimization_type = overview.get(PAYLOAD_OVERVIEW_OPTIMIZATION_TYPE, OPTIMIZATION_TYPE_RUN)

    est_remaining = None
    if job_status not in TERMINAL_STATUSES:
        est_remaining = extract_estimated_remaining(job_data)

    result_data = job_data.get("result")
    latest_metrics = job_data.get("latest_metrics", {})
    baseline = None
    optimized = None
    completed_pairs = None
    failed_pairs = None
    best_pair_label = None

    if isinstance(result_data, dict):
        if optimization_type == OPTIMIZATION_TYPE_GRID_SEARCH:
            best_pair = result_data.get("best_pair")
            if isinstance(best_pair, dict):
                baseline = best_pair.get("baseline_test_metric")
                optimized = best_pair.get("optimized_test_metric")
                gen = best_pair.get("generation_model", "")
                ref = best_pair.get("reflection_model", "")
                best_pair_label = f"{gen} + {ref}"
            completed_pairs = result_data.get("completed_pairs")
            failed_pairs = result_data.get("failed_pairs")
        else:
            baseline, optimized = result_scores(result_data)

    # For grid search, pull live counters from latest_metrics if result not yet available
    if optimization_type == OPTIMIZATION_TYPE_GRID_SEARCH:
        if completed_pairs is None:
            live_completed = latest_metrics.get("completed_so_far")
            completed_pairs = live_completed if isinstance(live_completed, int) else 0
        if failed_pairs is None:
            live_failed = latest_metrics.get("failed_so_far")
            failed_pairs = live_failed if isinstance(live_failed, int) else 0

    metric_improvement = None
    if baseline is not None and optimized is not None:
        metric_improvement = round(optimized - baseline, 6)

    elapsed_str, elapsed_secs = compute_elapsed(
        created_at, started_at, completed_at, job_data.get("accumulated_runtime_seconds") or 0.0
    )

    return OptimizationSummaryResponse(
        optimization_id=job_data["optimization_id"],
        status=job_status,
        message=job_data.get("message"),
        created_at=created_at,
        started_at=started_at,
        completed_at=completed_at,
        elapsed=elapsed_str,
        elapsed_seconds=elapsed_secs,
        estimated_remaining=est_remaining,
        **overview_to_base_fields(overview),
        split_fractions=overview.get(PAYLOAD_OVERVIEW_SPLIT_FRACTIONS),
        shuffle=overview.get(PAYLOAD_OVERVIEW_SHUFFLE),
        seed=overview.get(PAYLOAD_OVERVIEW_SEED),
        optimizer_kwargs=overview.get(PAYLOAD_OVERVIEW_OPTIMIZER_KWARGS, {}),
        compile_kwargs=overview.get(PAYLOAD_OVERVIEW_COMPILE_KWARGS, {}),
        latest_metrics=latest_metrics,
        progress_count=job_data.get("progress_count", 0),
        log_count=job_data.get("log_count", 0),
        stored_bytes=job_data.get("stored_bytes", 0),
        baseline_test_metric=baseline,
        optimized_test_metric=optimized,
        metric_improvement=metric_improvement,
        completed_pairs=completed_pairs,
        failed_pairs=failed_pairs,
        best_pair_label=best_pair_label,
        summary_text=job_data.get("summary_text"),
    )


def _artifact_has_payload(artifact: ProgramArtifact | None) -> bool:
    """Return whether the artifact carries either JSON state or a legacy pickle.

    Args:
        artifact: The artifact pulled off a ``RunResponse`` or ``PairResult``.

    Returns:
        ``True`` when the artifact has something we can materialize a program
        from; ``False`` otherwise.
    """
    if artifact is None:
        return False
    return artifact.program_state_json is not None or bool(artifact.program_pickle_base64)


def _materialize_program(artifact: ProgramArtifact, overview: dict) -> Any:
    """Materialize a runnable DSPy program from an artifact.

    The state-JSON path is preferred: reconstruct the module shell from the
    stored ``signature_code`` / ``module_name`` / ``module_kwargs`` and apply
    the saved state via ``program.load_state``. The pickle path is retained
    only as a fallback for artifacts written before the JSON migration.

    Args:
        artifact: The artifact carrying either ``program_state_json`` (new
            jobs) or ``program_pickle_base64`` (legacy jobs).
        overview: Parsed payload-overview dict from the job row; supplies
            ``signature_code``, ``module_name``, and ``module_kwargs`` for
            the JSON path.

    Returns:
        A live DSPy program object ready for inference.

    Raises:
        DomainError: 409 when the artifact lacks both payload variants, when
            ``signature_code`` is missing from the overview for a JSON
            artifact, or when module reconstruction fails.
    """
    workflow_spec = workflow_spec_from_overview(overview)
    if workflow_spec is not None:
        return _materialize_workflow_program(artifact, overview, workflow_spec)

    if artifact.react_overlay is not None:
        return _materialize_react_program(artifact, overview)

    if artifact.program_state_json is not None:
        signature_code = overview.get(PAYLOAD_OVERVIEW_SIGNATURE_CODE)
        if not signature_code:
            # Pre-migration overviews don't carry signature_code. Force the
            # legacy pickle path if it's present; otherwise fail loudly so
            # the caller can prompt a re-run instead of producing nonsense.
            if artifact.program_pickle_base64:
                return _legacy_pickle_load(artifact.program_pickle_base64)
            raise DomainError("optimization.no_signature_code_for_reload", status=409)

        module_name = overview.get(PAYLOAD_OVERVIEW_MODULE_NAME) or "predict"
        module_kwargs = dict(overview.get(PAYLOAD_OVERVIEW_MODULE_KWARGS, {}))
        try:
            signature_cls = load_signature_from_code(signature_code)
            module_factory, auto_signature = resolve_module_factory(module_name)
        except ResolverError as exc:
            raise DomainError("optimization.module_reconstruction_failed", status=409, error=str(exc)) from exc

        if auto_signature or "signature" not in module_kwargs:
            module_kwargs["signature"] = signature_cls
        program = module_factory(**module_kwargs)
        program.load_state(artifact.program_state_json)
        return program

    return _legacy_pickle_load(artifact.program_pickle_base64)


def workflow_spec_from_overview(overview: dict) -> WorkflowSpec | None:
    """Parse the persisted workflow spec off a payload overview, if any.

    Args:
        overview: Parsed payload-overview dict from the job row.

    Returns:
        The parsed ``WorkflowSpec``, or ``None`` for non-workflow runs.
    """
    payload = overview.get(PAYLOAD_OVERVIEW_WORKFLOW)
    if not payload:
        return None
    return WorkflowSpec.model_validate(payload)


def sanitize_node_traces(raw_traces: list[dict[str, Any]]) -> list[WorkflowNodeTrace]:
    """Convert raw workflow trace dicts into response-safe trace models.

    Node port values are arbitrary Python objects produced by user code;
    anything that is not JSON-native is stringified so the response always
    serializes, and long strings are capped like serve outputs.

    Args:
        raw_traces: Trace dicts collected by ``capture_node_traces``.

    Returns:
        Response-ready ``WorkflowNodeTrace`` models in execution order.
    """
    return [
        WorkflowNodeTrace(
            node_id=trace["node_id"],
            kind=trace["kind"],
            name=trace["name"],
            inputs=sanitize_port_values(trace.get("inputs") or {}),
            outputs=sanitize_port_values(trace["outputs"]) if trace.get("outputs") is not None else None,
            elapsed_ms=trace.get("elapsed_ms", 0.0),
            error=trace.get("error"),
        )
        for trace in raw_traces
    ]


def sanitize_port_values(values: dict[str, Any]) -> dict[str, Any]:
    """Make one trace value dict JSON-safe and size-capped.

    Args:
        values: Raw port-name to value mapping from a node trace.

    Returns:
        A new dict with non-JSON-native values stringified and long
        strings truncated.
    """
    sanitized: dict[str, Any] = {}
    for key, value in values.items():
        if isinstance(value, str):
            sanitized[key] = truncate_text(value, AGENT_MAX_TEXT)
        elif value is None or isinstance(value, bool | int | float):
            sanitized[key] = value
        else:
            sanitized[key] = truncate_text(str(value), AGENT_MAX_TEXT)
    return sanitized


def _materialize_workflow_program(artifact: ProgramArtifact, overview: dict, workflow_spec: WorkflowSpec) -> Any:
    """Rebuild a served workflow program from its spec and state JSON.

    The shell is reconstructed with the same deterministic builder the run
    path used, so the saved per-node predictor state keys line up 1:1.
    Tool-using graphs re-source their roster from the overview's scrubbed
    ``tool_source``; a ``dataset_snapshot`` source cannot be re-sourced
    without the original dataset and is rejected until snapshot specs are
    persisted for workflows.

    Args:
        artifact: The artifact carrying ``program_state_json``.
        overview: Parsed payload-overview dict.
        workflow_spec: The workflow spec parsed from the overview.

    Returns:
        A live ``WorkflowProgram`` ready for inference.

    Raises:
        DomainError: 409 when the artifact lacks state JSON, the graph needs
            tools it cannot re-source, or reconstruction fails.
    """
    if artifact.program_state_json is None:
        raise DomainError("optimization.no_program_artifact_scoped", status=409)
    tool_source = overview.get(PAYLOAD_OVERVIEW_TOOL_SOURCE)
    if workflow_tool_users(workflow_spec):
        if not tool_source:
            raise DomainError(
                "optimization.module_reconstruction_failed",
                status=409,
                error="workflow uses tools but no tool_source was persisted.",
            )
        if tool_source.get("kind") == "dataset_snapshot":
            raise DomainError(
                "optimization.module_reconstruction_failed",
                status=409,
                error="serving a workflow with dataset_snapshot tools is not supported yet.",
            )
    try:
        program, _schema_hashes = build_workflow_program(workflow_spec, tool_source=tool_source)
    except (ServiceError, ValueError) as exc:
        raise DomainError("optimization.module_reconstruction_failed", status=409, error=str(exc)) from exc
    program.load_state(artifact.program_state_json)
    return program


def _materialize_react_program(artifact: ProgramArtifact, overview: dict) -> Any:
    """Rebuild a served ``ReActV2`` program from a persisted react artifact.

    Unlike the scalar JSON path, a react program owns a tool roster that the
    state dump deliberately drops (``save_program=False``). We re-source that
    roster from ``react_overlay.tool_source`` — the same resolver the run path
    uses — drift-check it against the schema-hash snapshot taken at training
    time, re-apply the optimized per-tool wording, then build ``ReActV2`` and
    load the stored state.

    Args:
        artifact: The artifact carrying ``program_state_json`` and a
            ``react_overlay`` (signature reconstruction reads ``overview``).
        overview: Parsed payload-overview dict; supplies ``signature_code``.

    Returns:
        A live ``dspy.ReActV2`` program ready for inference.

    Raises:
        DomainError: 409 when ``signature_code`` is missing, when module/tool
            reconstruction fails, or when the live tool schema has drifted
            from the snapshot recorded at training time.
    """
    react_overlay = artifact.react_overlay
    signature_code = overview.get(PAYLOAD_OVERVIEW_SIGNATURE_CODE)
    if not signature_code:
        raise DomainError("optimization.no_signature_code_for_reload", status=409)

    try:
        signature_cls = load_signature_from_code(signature_code)
        # live_mcp re-sources with auth None: the MCP auth header is a secret
        # and is intentionally never persisted on the overlay (see
        # _persist_react_program), so serve falls back to the settings-default
        # URL with no header rather than replaying a stored credential.
        tools, _live_hashes = resolve_react_tools(react_overlay.tool_source, signature_cls, settings)
    except (ResolverError, ValueError) as exc:
        raise DomainError("optimization.module_reconstruction_failed", status=409, error=str(exc)) from exc

    try:
        # Strict: a served run must materialise against the exact tool surface
        # it was optimised against — any added/removed tool, not just a hash
        # mismatch, is drift. Mirrors the chat driver so both react-serve
        # surfaces gate identically.
        _assert_tool_set_matches(react_overlay.tool_schema_hashes, tools, strict=True)
    except ToolSchemaDriftError as exc:
        raise DomainError("optimization.tool_schema_drift", status=409, error=str(exc)) from exc

    _apply_bundle_tool_overrides(
        tools,
        tool_descriptions=react_overlay.tool_descriptions,
        tool_arg_descriptions=react_overlay.tool_arg_descriptions,
    )
    # Rename to the proposed display names last (canonical drift-check + overlays
    # above key on the original names). None preserves pre-rename behavior.
    _apply_tool_name_overrides(tools, react_overlay.tool_names)

    program = RetryingReActV2(signature_cls, tools=tools, max_iters=react_overlay.max_iters)
    program.load_state(artifact.program_state_json)
    return program


def _legacy_pickle_load(program_pickle_base64: str | None) -> Any:
    """Deserialize a legacy pickle artifact.

    Retained for artifacts written before the JSON migration. New jobs use
    the state-JSON path in :func:`_materialize_program` and never reach this
    branch. The same trust boundary applies: artifact bytes were produced by
    our own worker, never accepted from API input.

    Args:
        program_pickle_base64: Base64-encoded ``program.pkl`` bytes from a
            legacy artifact.

    Returns:
        The deserialized DSPy program.

    Raises:
        DomainError: 409 when the pickle payload is empty (caller forgot to
            guard via :func:`_artifact_has_payload`).
    """
    if not program_pickle_base64:
        raise DomainError("optimization.no_program_artifact_scoped", status=409)
    program_bytes = base64.b64decode(program_pickle_base64)
    # pickle.loads is RCE if an attacker can write to the jobs table. The
    # artifact bytes are produced by our own worker and never accepted from
    # API input, so today the only attacker who reaches this branch already
    # has DB write — at which point they own the process anyway. New jobs
    # don't take this path; this exists only to serve already-finished jobs
    # that predate the JSON migration.
    return pickle.loads(program_bytes)


def _stable_hash(payload: Any) -> str:
    """Return a process-stable short SHA256 over a JSON-able payload.

    Used to fold a react program's tool-roster identity into its cache key.
    Built-in ``hash`` is salted per process via ``PYTHONHASHSEED``, so two
    workers would derive different keys for the same roster; canonical JSON +
    SHA256 keeps the key byte-stable across processes.

    Args:
        payload: Any JSON-serializable value (here, the schema-hash map).

    Returns:
        A 16-hex-char prefix of the SHA256 digest — enough entropy to
        distinguish rosters without bloating the cache key.
    """
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def require_unprotected_api_execution(job_data: dict[str, Any]) -> None:
    """Reject authored-code execution when a managed runtime is required.

    Every production job resolves to a managed runtime. The ``None`` branch is
    retained only for isolated compatibility tests of the retired materializers.
    This guard runs before loading a signature, metric, workflow, ReAct tool
    roster, or serialized program.

    Args:
        job_data: Stored optimization row, including its protected budget and
            original payload when available.

    Raises:
        DomainError: 409 when the job belongs to the protected execution path.
    """
    runtime = _protected_api_runtime(job_data)
    if runtime is None:
        return
    raise DomainError(
        "optimization.protected_interactive_sandbox_required",
        status=409,
        runtime=runtime,
    )


def _protected_api_runtime(job_data: dict[str, Any]) -> str | None:
    """Return the mandatory managed runtime for a completed-job interaction.

    Args:
        job_data: Stored optimization row retained for the shared helper
            signature and future provider routing.

    Returns:
        ``vercel`` for every stored job. A fresh caller-funded interaction
        budget supplies authority for historical jobs as well, so no stored
        runtime value can restore API-host execution.
    """
    return "vercel"


def _raw_artifact_has_payload(artifact: Any) -> bool:
    """Check a persisted artifact mapping without decoding its program payload.

    Args:
        artifact: Raw JSON value stored under ``program_artifact``.

    Returns:
        ``True`` when the mapping carries JSON state or an encoded legacy
        pickle. The encoded value is never decoded here.
    """
    if not isinstance(artifact, dict):
        return False
    return artifact.get("program_state_json") is not None or bool(artifact.get("program_pickle_base64"))


def load_program_metadata(
    job_store,
    optimization_id: str,
    user: AuthenticatedUser,
) -> tuple[ProgramArtifact | dict[str, Any], dict[str, Any], str]:
    """Load serve-form metadata without materializing protected authored code.

    Legacy jobs retain the existing :func:`load_program` path and its exact
    reconstruction checks. Protected jobs read only the already-scrubbed
    overview and artifact prompt metadata persisted by the worker. They never
    decode a pickle, load program state, import signature code, or resolve a
    tool roster in the API process.

    Args:
        job_store: The job store to read the job row from.
        optimization_id: Optimization whose serve metadata should be loaded.
        user: Authenticated caller; protected interactions are viewer-accessible.

    Returns:
        ``(artifact, overview, model_name)`` with an unmaterialized artifact.

    Raises:
        DomainError: 404 when the job is unknown or inaccessible; 403 when a
            legacy caller is below editor; 409 when the job is not successful
            or has no persisted program artifact.
    """
    job_data, role = load_job_with_role(job_store, optimization_id, user)
    if _protected_api_runtime(job_data) is None:
        if role_rank(role) < role_rank(ShareRole.editor):
            raise DomainError(
                "optimization.insufficient_role",
                status=403,
                optimization_id=optimization_id,
                required=str(ShareRole.editor),
                role=str(role),
            )
        _program, result, overview = load_program(job_store, optimization_id, user)
        artifact = result.program_artifact
        if not _artifact_has_payload(artifact):
            raise DomainError("optimization.no_program_artifact_scoped", status=409)
        return artifact, overview, str(overview.get(PAYLOAD_OVERVIEW_MODEL_NAME, ""))

    overview = parse_overview(job_data)
    status = status_to_job_status(job_data.get("status", "pending"))
    if status != OptimizationStatus.success:
        raise DomainError(
            "optimization.not_success_status_for_serve",
            status=409,
            params={"status": status.value},
        )

    result_data = job_data.get("result")
    if not isinstance(result_data, dict):
        raise DomainError("optimization.no_result", status=409)

    optimization_type = overview.get(PAYLOAD_OVERVIEW_OPTIMIZATION_TYPE, OPTIMIZATION_TYPE_RUN)
    model_name = str(overview.get(PAYLOAD_OVERVIEW_MODEL_NAME, ""))
    if optimization_type == OPTIMIZATION_TYPE_GRID_SEARCH:
        best_pair = result_data.get("best_pair")
        if not isinstance(best_pair, dict):
            raise DomainError("grid_search.no_best_pair", status=409)
        artifact = best_pair.get("program_artifact")
        if not _raw_artifact_has_payload(artifact):
            raise DomainError("grid_search.no_best_program_artifact", status=409)
        model_name = str(best_pair.get("generation_model") or model_name)
    else:
        artifact = result_data.get("program_artifact")
        if not _raw_artifact_has_payload(artifact):
            raise DomainError("optimization.no_program_artifact_scoped", status=409)

    return artifact, overview, model_name


def load_program(
    job_store,
    optimization_id: str,
    user: AuthenticatedUser,
) -> tuple[Any, RunResponse, dict]:
    """Load and cache an optimized program from a completed job.

    For grid-search jobs, loads the best pair's program automatically and
    synthesizes a ``RunResponse`` from the grid result envelope. Serving spends
    the owner's API key, so this requires editor-tier access (owner / admin /
    editor-or-owner grant): a viewer-tier member gets 403, a caller with no
    access 404s — see :func:`require_role_at_least`.

    Args:
        job_store: The job store to read the job row from.
        optimization_id: The optimization to load.
        user: Authenticated caller; must hold editor-tier access to serve.

    Returns:
        A ``(program, RunResponse, overview)`` tuple where ``program`` is the
        deserialized DSPy module, ``RunResponse`` is the synthesized result,
        and ``overview`` is the parsed payload-overview dict.

    Raises:
        DomainError: 404 when the job is unknown or inaccessible; 403 when the
            caller's role is below editor; 409 when the job is not in a success
            state, has no result, lacks a serialized program artifact, or its
            protected interactive sandbox path is unavailable.
    """
    job_data, _role = require_role_at_least(job_store, optimization_id, user, ShareRole.editor)
    require_unprotected_api_execution(job_data)

    overview = parse_overview(job_data)
    optimization_type = overview.get(PAYLOAD_OVERVIEW_OPTIMIZATION_TYPE, OPTIMIZATION_TYPE_RUN)

    status = status_to_job_status(job_data.get("status", "pending"))
    if status != OptimizationStatus.success:
        raise DomainError(
            "optimization.not_success_status_for_serve",
            status=409,
            params={"status": status.value},
        )

    result_data = job_data.get("result")
    if not result_data or not isinstance(result_data, dict):
        raise DomainError("optimization.no_result", status=409)

    if optimization_type == OPTIMIZATION_TYPE_GRID_SEARCH:
        grid_result = GridSearchResponse.model_validate(result_data)
        if not grid_result.best_pair:
            raise DomainError("grid_search.no_best_pair", status=409)
        artifact = grid_result.best_pair.program_artifact
        if not _artifact_has_payload(artifact):
            raise DomainError("grid_search.no_best_program_artifact", status=409)
        result = RunResponse(
            module_name=grid_result.module_name,
            optimizer_name=grid_result.optimizer_name,
            metric_name=grid_result.metric_name,
            split_counts=grid_result.split_counts,
            baseline_test_metric=grid_result.best_pair.baseline_test_metric,
            optimized_test_metric=grid_result.best_pair.optimized_test_metric,
            metric_improvement=grid_result.best_pair.metric_improvement,
            program_artifact=artifact,
        )
        overview[PAYLOAD_OVERVIEW_MODEL_NAME] = grid_result.best_pair.generation_model
    else:
        result = RunResponse.model_validate(result_data)
        artifact = result.program_artifact
        if not _artifact_has_payload(artifact):
            raise DomainError("optimization.no_program_artifact_scoped", status=409)

    # Fold the tool-roster identity into the key for react programs so a
    # re-sourced or drifted roster never serves a stale cached program. The
    # non-react key stays the bare optimization_id.
    cache_key = optimization_id
    if artifact.react_overlay is not None:
        cache_key = f"{optimization_id}:{_stable_hash(artifact.react_overlay.tool_schema_hashes)}"

    if cache_key not in _program_cache:
        _program_cache[cache_key] = _materialize_program(artifact, overview)

    return _program_cache[cache_key], result, overview
