"""Single-optimization read routes and evaluate-examples. [MIXED]

Public dev surface (in ``_SCALAR_PUBLIC_PATHS``):
- ``GET /optimizations/{id}`` — full job state.
- ``GET /optimizations/{id}/summary`` — compact metrics summary.
- ``GET /optimizations/{id}/artifact`` — download the trained program.
- ``GET /optimizations/{id}/grid-result`` — per-pair grid-search results.

Internal (frontend-only, hidden from public docs):
- ``GET /optimizations/{id}/dataset`` — dataset reshuffle for the UI.
- ``POST /optimizations/{id}/evaluate-examples`` — per-user playground.
- ``GET /optimizations/{id}/test-results``
- ``GET /optimizations/{id}/pair/{idx}/test-results``
- ``GET /optimizations/{id}/agent-runs/{run_id}`` — one sandboxed agent run's answer and transcript.
- ``GET /optimizations/{id}/candidates`` — the run's candidates with state, call count and cost (owner only).
- ``GET /optimizations/{id}/candidates/{candidate_id}/calls`` — one candidate's relay calls after a cursor (owner only).
"""

from __future__ import annotations

import hashlib
import logging
import random
from datetime import UTC, datetime
from typing import Annotated, Literal

import dspy
from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import AliasChoices, BaseModel, Field, ValidationError

from ....billing import ProviderKeyVault, resolve_byok_model_config
from ....billing.budget_amounts import MAX_CENTS
from ....config import settings
from ....constants import (
    OPTIMIZATION_TYPE_BLACKBOX,
    OPTIMIZATION_TYPE_GRID_SEARCH,
    OPTIMIZATION_TYPE_RUN,
    PAYLOAD_OVERVIEW_MODEL_NAME,
    PAYLOAD_OVERVIEW_MODEL_SETTINGS,
    PAYLOAD_OVERVIEW_MODULE_KWARGS,
    PAYLOAD_OVERVIEW_MODULE_NAME,
    PAYLOAD_OVERVIEW_OPTIMIZATION_TYPE,
    PAYLOAD_OVERVIEW_SIGNATURE_CODE,
    PAYLOAD_OVERVIEW_WORKFLOW,
    TOKEN_SOURCE_BYOK,
    TOKEN_SOURCE_MANAGED,
)
from ....models import (
    BlackboxAgentRunResponse,
    BlackboxRunResponse,
    ColumnMapping,
    GridSearchResponse,
    JobLogEntry,
    ModelConfig,
    OptimizationStatus,
    OptimizationStatusResponse,
    OptimizationSummaryResponse,
    ProgramArtifactResponse,
    RunResponse,
    SplitFractions,
)
from ....models.run_calls import RunCallPage, RunCandidateList
from ....registry import ResolverError, resolve_module_factory
from ....service_gateway.language_models import build_language_model
from ....service_gateway.optimization.data import load_metric_from_code, load_signature_from_code
from ...auth import AuthenticatedUser, get_authenticated_user
from ...converters import (
    compute_elapsed,
    extract_estimated_remaining,
    job_owner,
    overview_to_base_fields,
    parse_overview,
    parse_timestamp,
    status_to_job_status,
)
from ...errors import DomainError
from ...protected_interaction import run_protected_interaction
from ...sharing_access import ShareRole
from .._helpers import (
    _artifact_has_payload,
    _materialize_program,
    _program_cache,
    _protected_api_runtime,
    build_summary,
    is_pausable,
    is_resumable,
    load_job_for_user,
    load_job_with_role,
    require_unprotected_api_execution,
    stable_seed,
)
from ..constants import TERMINAL_STATUSES
from ._local import execution_budget_view, remap_test_indices
from ._program_export import build_program_export_zip

logger = logging.getLogger(__name__)

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]

# Bumped whenever the GET /optimizations/{id} response shape changes in a way
# that adds, removes, or renames fields. Mixed into the ETag so cached 304s
# can't serve a pre-change body that's missing the new fields. Last bump:
# added progress_offset / logs_offset for delta (tail) stream fetches.
_RESPONSE_SCHEMA_VERSION = "v6"


def _load_job_for_owner(job_store, optimization_id: str, user: AuthenticatedUser) -> dict:
    """Load a job row only for its owner (or an admin); everyone else gets 404.

    The run record holds request and response bodies, so a shared viewer or
    editor must not read it, and must not learn that it exists.

    Args:
        job_store: Job-store the row is read from.
        optimization_id: Optimization id to load.
        user: Authenticated caller.

    Returns:
        The job row, without its payload.

    Raises:
        DomainError: 404 when the id is unknown or the caller is not the owner.
    """
    job_data, role = load_job_with_role(job_store, optimization_id, user, include_payload=False)
    if role != ShareRole.owner:
        raise DomainError("optimization.not_found", status=404, optimization_id=optimization_id)
    return job_data


def _effective_candidate_state(state: str, job_status: OptimizationStatus) -> str:
    """Return a candidate's state, closed out when its run already ended.

    A candidate still marked ``running`` in a finished run never got its end
    mark (for example, the worker died), so it takes the run's outcome.

    Args:
        state: The stored candidate state.
        job_status: The run's status.

    Returns:
        ``running``, ``done`` or ``failed``.
    """
    if state != "running" or job_status not in TERMINAL_STATUSES:
        return state
    return "failed" if job_status in {OptimizationStatus.failed, OptimizationStatus.cancelled} else "done"


def _delta_offset(raw: str | None, count: int, cap: int | None) -> int:
    """Resolve a client stream cursor to a safe slice offset.

    Position cursors are exact only while a stream is append-only, which holds
    precisely below the retention cap: ``count`` climbs monotonically to ``cap``
    and then plateaus as each insert evicts the oldest row, shifting every
    position. So at/past the cap, or when the cursor runs past ``count`` (a
    stale or shrunk stream), the full stream is sent so the client never
    silently misses post-eviction rows.

    Args:
        raw: The raw ``since_*`` query-param value, or ``None`` when absent.
        count: Current number of rows in the stream.
        cap: Per-job retention cap for the stream; ``None`` for a stream that never evicts.

    Returns:
        The parsed cursor when it is a valid position into an append-only
        (sub-cap) stream; ``0`` (send everything) for an out-of-range or
        post-eviction cursor.

    Raises:
        HTTPException: 422 when ``raw`` is present but is not an integer.
    """
    if raw is None:
        return 0
    try:
        since = int(raw)
    except ValueError as exc:
        # A non-integer cursor is a client bug, not an eviction edge — reject it
        # loudly instead of silently re-sending the whole stream every tick.
        raise HTTPException(status_code=422, detail="optimization.invalid_cursor") from exc
    if since <= 0 or since > count or (cap is not None and count >= cap):
        return 0
    return since


_EVALUATE_EXAMPLES_MAX_INDICES = 100


class EvaluateExamplesRequest(BaseModel):
    """Request body for ``POST /optimizations/{id}/evaluate-examples``."""

    indices: list[int] = Field(
        default_factory=list,
        max_length=_EVALUATE_EXAMPLES_MAX_INDICES,
        description="Dataset row indices to evaluate; out-of-range indices are skipped.",
    )
    program_type: Literal["optimized", "baseline"] = Field(
        default="optimized",
        description="Which program to run: the optimized result or the baseline.",
    )
    max_cost_cents: int | None = Field(
        validation_alias=AliasChoices("max_cost_cents", "max_cost_credits"),
        default=None,
        ge=1,
        le=MAX_CENTS,
        strict=True,
        description="Optional cap in cents for this one evaluation request; omitted, it draws on the account balance.",
    )


def register_detail_routes(router: APIRouter, *, job_store) -> None:
    """Register single-optimization read routes on ``router``.

    Args:
        router: The router to attach the detail routes to.
        job_store: Job-store the routes read from.
    """

    # ``response_model`` is kept for the OpenAPI schema, but the handler
    # returns a ``Response`` directly so it can emit a 304 path and attach
    # ETag / Cache-Control headers. FastAPI skips response_model validation
    # whenever the handler returns a Response instance, so the model class is
    # documentation-only here — every code path still constructs an
    # ``OptimizationStatusResponse`` before serialising.
    @router.get(
        "/optimizations/{optimization_id}",
        response_model=OptimizationStatusResponse,
        summary="Full optimization detail with logs, progress, metrics, and result",
    )
    def get_job(optimization_id: str, request: Request, current_user: AuthenticatedUserDep) -> Response:
        """Return full optimization detail with logs, progress, metrics, and result.

        Supports conditional GET via ``If-None-Match`` / ``ETag`` (304 when
        unchanged) and delta fetches via the optional ``since_progress`` /
        ``since_log`` query params, which return only the stream rows past the
        given offsets (the response echoes the honored start index in
        ``progress_offset`` / ``logs_offset``). Grid searches include partial
        ``grid_result`` while still running. Corrupted result data is omitted
        with a warning rather than raising 500. 404 if the optimization id is
        unknown or the caller doesn't own it (non-admins).

        Args:
            optimization_id: The id of the optimization to fetch.
            request: Starlette request used to check ``If-None-Match``.
            current_user: Authenticated caller resolved from the bearer token.

        Returns:
            A JSON :class:`fastapi.Response` whose body is a serialized
            :class:`OptimizationStatusResponse` (or a bare 304 with no body
            when ``If-None-Match`` matches the current ETag).

        Raises:
            DomainError: 404 when the optimization id is unknown or
                inaccessible to the caller.
        """

        # ``include_payload=False``: this is the endpoint an active-run client
        # re-polls every 1-2s, and nothing in the response reads the raw
        # payload (the training dataset) — deferring it skips a multi-MB JSONB
        # read per poll. Ownership still resolves via ``payload_overview``,
        # which is denormalized alongside the payload (see _reassign_job_owner).
        job_data, role = load_job_with_role(job_store, optimization_id, current_user, include_payload=False)
        # Null for the owner's own view (implicitly owner); the grant tier when a
        # member reached this run via sharing, so the UI can gate actions.
        effective_role = None if role == ShareRole.owner else str(role)

        status = status_to_job_status(job_data.get("status", "pending"))

        overview = parse_overview(job_data)
        latest_metrics = job_data.get("latest_metrics", {})

        # Compute the ETag from cheap COUNT queries and short-circuit a 304
        # *before* materializing the full progress/log streams — which can be
        # thousands of rows / multi-MB on a long run and were previously fetched
        # only to be thrown away on every revalidation hit. The counts equal the
        # lengths of the unbounded stream fetches below, so the ETag is unchanged.
        log_count = job_store.get_log_count(optimization_id)
        progress_count = job_store.get_progress_count(optimization_id)
        # Rename / pin / description edits change ``payload_overview`` without
        # touching logs, progress, or metrics — so they must be mixed into the
        # ETag, otherwise the next revalidation returns 304 against a body
        # that's still carrying the old name and the UI never updates. The
        # caller's role is folded in too so two users sharing a run (different
        # tiers) never collide on a cached 304 with the wrong effective_role.
        overview_sig = overview.get("name"), overview.get("description"), overview.get("pinned")
        etag_src = f"{_RESPONSE_SCHEMA_VERSION}:{status}:{log_count}:{progress_count}:{latest_metrics!s}:{overview_sig!s}:{effective_role}"
        etag = '"' + hashlib.md5(etag_src.encode()).hexdigest()[:12] + '"'
        if_none_match = request.headers.get("if-none-match")
        if if_none_match == etag:
            # A 304 must carry no body — ``JSONResponse(content=None)`` renders
            # ``b"null"`` (4 bytes), which h11 rejects with "Too much data for
            # declared Content-Length" since the framing for 304 allows zero body
            # bytes. A bodyless ``Response`` is the correct not-modified reply.
            return Response(status_code=304, headers={"ETag": etag})

        # Delta tail: an active-run client re-polls this endpoint on every
        # progress tick. ``since_progress`` / ``since_log`` let it say "I already
        # hold N rows", so we return only the newer tail instead of re-sending
        # the entire (capped, but still multi-thousand-row) history each time.
        # ``_delta_offset`` falls back to a full send for an out-of-range or
        # post-eviction cursor, and rejects a non-integer cursor with 422.
        # ``progress_offset`` / ``logs_offset`` echo the honored start index so
        # the client knows whether to splice the tail on or replace wholesale.
        progress_offset = _delta_offset(
            request.query_params.get("since_progress"), progress_count, settings.progress_events_per_job_cap
        )
        logs_offset = _delta_offset(request.query_params.get("since_log"), log_count, None)
        progress_events = job_store.get_progress_events(optimization_id, since=progress_offset)
        logs = job_store.get_logs(optimization_id, offset=logs_offset)

        optimization_type = overview.get(PAYLOAD_OVERVIEW_OPTIMIZATION_TYPE, OPTIMIZATION_TYPE_RUN)

        result = None
        grid_result = None
        blackbox_result = None
        result_data = job_data.get("result")
        if result_data and isinstance(result_data, dict):
            try:
                if optimization_type == OPTIMIZATION_TYPE_GRID_SEARCH:
                    grid_result = GridSearchResponse.model_validate(result_data)
                elif (
                    status in {OptimizationStatus.success, OptimizationStatus.stopped}
                    and optimization_type == OPTIMIZATION_TYPE_BLACKBOX
                ):
                    blackbox_result = BlackboxRunResponse.model_validate(result_data)
                elif status in {OptimizationStatus.success, OptimizationStatus.stopped}:
                    result = RunResponse.model_validate(result_data)
            except ValidationError:
                logger.warning("Optimization %s has corrupted result data", optimization_id)

        created_at = parse_timestamp(job_data.get("created_at")) or datetime.now(UTC)
        started_at = parse_timestamp(job_data.get("started_at"))
        completed_at = parse_timestamp(job_data.get("completed_at"))

        est_remaining = None
        if status not in TERMINAL_STATUSES:
            est_remaining = extract_estimated_remaining(job_data)

        completed_pairs = None
        failed_pairs = None
        if optimization_type == OPTIMIZATION_TYPE_GRID_SEARCH:
            if grid_result:
                completed_pairs = grid_result.completed_pairs
                failed_pairs = grid_result.failed_pairs
            else:
                live_completed = latest_metrics.get("completed_so_far")
                completed_pairs = live_completed if isinstance(live_completed, int) else 0
                live_failed = latest_metrics.get("failed_so_far")
                failed_pairs = live_failed if isinstance(live_failed, int) else 0

        elapsed_str, elapsed_secs = compute_elapsed(
            created_at, started_at, completed_at, job_data.get("accumulated_runtime_seconds") or 0.0
        )

        logger.debug("Returning status for optimization_id=%s state=%s", optimization_id, status)
        execution_budget = execution_budget_view(job_store, job_data)
        response_data = OptimizationStatusResponse(
            optimization_id=optimization_id,
            status=status,
            created_at=created_at,
            started_at=started_at,
            completed_at=completed_at,
            elapsed=elapsed_str,
            elapsed_seconds=elapsed_secs,
            estimated_remaining=est_remaining,
            **overview_to_base_fields(overview),
            message=job_data.get("message"),
            stop_reason=job_data.get("stop_reason"),
            result_availability=job_data.get("result_availability"),
            terminal_evidence=job_data.get("terminal_evidence"),
            execution_budget=execution_budget,
            recovery=job_data.get("recovery"),
            stored_bytes=job_data.get("stored_bytes", 0),
            latest_metrics=latest_metrics,
            completed_pairs=completed_pairs,
            failed_pairs=failed_pairs,
            progress_events=progress_events,
            progress_offset=progress_offset,
            logs=[JobLogEntry(**log) for log in logs],
            logs_offset=logs_offset,
            result=result,
            grid_result=grid_result,
            blackbox_result=blackbox_result,
            effective_role=effective_role,
            resumable=is_resumable(job_store, job_data),
            pausable=is_pausable(job_store, job_data),
        )

        headers = {"ETag": etag}
        # ``no-cache`` (not ``no-store``) tells the browser to revalidate via
        # If-None-Match every time, so a schema bump in ``_RESPONSE_SCHEMA_VERSION``
        # invalidates terminal-job bodies immediately instead of being shadowed
        # by ``max-age`` until the next deploy + N seconds pass.
        if status in TERMINAL_STATUSES:
            headers["Cache-Control"] = "private, no-cache"
        else:
            headers["Cache-Control"] = "private, max-age=1"

        # Single pydantic→JSON pass; ``JSONResponse(model_dump(...))`` would
        # serialize the (potentially multi-MB, all-logs-and-progress) body
        # twice — once to a dict tree and once through json.dumps.
        return Response(
            content=response_data.model_dump_json(),
            media_type="application/json",
            headers=headers,
        )

    @router.get(
        "/optimizations/{optimization_id}/summary",
        response_model=OptimizationSummaryResponse,
        summary="Lightweight summary card for one optimization",
        tags=["agent"],
    )
    def get_job_summary(optimization_id: str, current_user: AuthenticatedUserDep) -> OptimizationSummaryResponse:
        """Return the compact dashboard-card shape for a single optimization.

        Args:
            optimization_id: Optimization id to summarise.
            current_user: Authenticated caller resolved from the bearer token.

        Returns:
            An ``OptimizationSummaryResponse`` for dashboard display.

        Raises:
            DomainError: 404 if the optimization is unknown or inaccessible
                to the caller.
        """

        job_data = load_job_for_user(job_store, optimization_id, current_user)

        job_data["progress_count"] = job_store.get_progress_count(optimization_id)
        job_data["log_count"] = job_store.get_log_count(optimization_id)
        summary = build_summary(job_data)
        summary.resumable = is_resumable(job_store, job_data)
        return summary

    @router.get(
        "/optimizations/{optimization_id}/dataset",
        summary="Reconstruct the train/val/test split used by this optimization",
    )
    def get_job_dataset(optimization_id: str, current_user: AuthenticatedUserDep) -> dict:
        """Reconstruct the train/val/test split deterministically from the stored seed.

        Each row includes its global dataset index for UI highlighting.

        Args:
            optimization_id: Optimization id whose dataset should be returned.
            current_user: Authenticated caller resolved from the bearer token.

        Returns:
            A dict with ``total_rows``, ``splits``, ``column_mapping``, and
            ``split_counts``.

        Raises:
            DomainError: 404 (optimization unknown / payload/dataset
                missing / inaccessible), 500 (corrupt mapping).
        """
        job_data = load_job_for_user(job_store, optimization_id, current_user)

        payload = job_data.get("payload")
        if not payload or not isinstance(payload, dict):
            raise DomainError("optimization.payload_unavailable", status=404)

        dataset = payload.get("dataset")
        if not dataset or not isinstance(dataset, list):
            raise DomainError("optimization.dataset_unavailable", status=404)

        raw_mapping = payload.get("column_mapping", {})
        try:
            column_mapping = ColumnMapping.model_validate(raw_mapping)
        except ValidationError:
            raise DomainError("optimization.corrupt_column_mapping", status=500) from None

        raw_fractions = payload.get("split_fractions", {})
        try:
            fractions = SplitFractions.model_validate(raw_fractions)
        except ValidationError:
            fractions = SplitFractions()

        shuffle = payload.get("shuffle", True)
        seed = payload.get("seed")

        # Replicates the service_gateway/data.py split algorithm. When seed
        # is None, derive a stable seed from optimization_id so repeated
        # calls produce the same shuffle (needed for index remapping).
        effective_seed = seed if seed is not None else stable_seed(optimization_id)
        total = len(dataset)
        indices = list(range(total))
        if shuffle:
            rng = random.Random(effective_seed)
            rng.shuffle(indices)

        train_end = int(total * fractions.train)
        val_end = train_end + int(total * fractions.val)
        train_indices = indices[:train_end]
        val_indices = indices[train_end:val_end]
        test_indices = indices[val_end:]

        splits = {
            "train": [{"index": i, "row": dataset[i]} for i in train_indices],
            "val": [{"index": i, "row": dataset[i]} for i in val_indices],
            "test": [{"index": i, "row": dataset[i]} for i in test_indices],
        }

        return {
            "total_rows": total,
            "splits": splits,
            "column_mapping": {
                "inputs": column_mapping.inputs,
                "outputs": column_mapping.outputs,
            },
            "split_counts": {
                "train": len(train_indices),
                "val": len(val_indices),
                "test": len(test_indices),
            },
        }

    @router.post(
        "/optimizations/{optimization_id}/evaluate-examples",
        summary="Run the optimized or baseline program on specific dataset rows",
    )
    def evaluate_examples(
        optimization_id: str,
        req: EvaluateExamplesRequest,
        current_user: AuthenticatedUserDep,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict:
        """Run the optimized or baseline program on specific dataset rows.

        Out-of-range indices are silently skipped.

        Args:
            optimization_id: Optimization id whose program should run.
            req: Validated body with bounded ``indices`` and ``program_type``.
            current_user: Authenticated caller resolved from the bearer token.

        Returns:
            ``{"results": [...], "program_type": ...}`` with one entry per
            evaluated row.

        Raises:
            DomainError: 404 (missing/inaccessible optimization or payload),
                400 (no metric/model/module), 409 (no result available for
                the optimized program or no protected interactive sandbox).
        """
        indices = req.indices
        program_type = req.program_type

        job_data = load_job_for_user(job_store, optimization_id, current_user)
        if _protected_api_runtime(job_data) is not None:
            key = (idempotency_key or "").strip()
            if not key:
                raise DomainError("budget.idempotency_required", status=400)
            payload = job_data.get("payload")
            if not isinstance(payload, dict):
                raise DomainError("optimization.no_payload", status=404)
            overview = parse_overview(job_data)
            model_settings = payload.get("model_config") or overview.get(PAYLOAD_OVERVIEW_MODEL_SETTINGS, {})
            model_name = str(overview.get(PAYLOAD_OVERVIEW_MODEL_NAME) or "")
            if model_settings:
                model_config = ModelConfig.model_validate(model_settings)
            elif model_name:
                model_config = ModelConfig(name=model_name)
            else:
                raise DomainError("optimization.no_model_config", status=400)
            artifact = None
            if req.program_type == "optimized":
                result_data = job_data.get("result")
                if not isinstance(result_data, dict):
                    raise DomainError("optimization.no_result_for_artifact", status=409)
                artifact = result_data.get("program_artifact")
                if artifact is None and isinstance(result_data.get("best_pair"), dict):
                    artifact = result_data["best_pair"].get("program_artifact")
                if not isinstance(artifact, dict):
                    raise DomainError("optimization.no_program_artifact", status=409)
            interaction_payload = {
                "dataset": payload.get("dataset") or [],
                "column_mapping": payload.get("column_mapping") or {},
                "metric_code": payload.get("metric_code") or "",
                "signature_code": payload.get("signature_code") or overview.get(PAYLOAD_OVERVIEW_SIGNATURE_CODE) or "",
                "module_name": payload.get("module_name") or overview.get(PAYLOAD_OVERVIEW_MODULE_NAME) or "predict",
                "module_kwargs": payload.get("module_kwargs") or overview.get(PAYLOAD_OVERVIEW_MODULE_KWARGS) or {},
                "model_config": model_config.model_dump(mode="json"),
                "token_source": model_config.token_source or TOKEN_SOURCE_MANAGED,
                "payload_overview": overview,
                "program_artifact": artifact,
                "_interaction": {
                    "kind": "evaluate",
                    "indices": req.indices,
                    "program_type": req.program_type,
                },
            }
            if payload.get("tool_source") is not None:
                interaction_payload["tool_source"] = payload["tool_source"]
            stored_budget_id = job_data.get("execution_budget_id") or payload.get("execution_budget_id")
            result = run_protected_interaction(
                interaction_payload,
                kind="evaluate",
                max_cost_cents=req.max_cost_cents,
                idempotency_key=key,
                user=current_user,
                job_store=job_store,
                credential_owner=job_owner(job_data),
                credential_binding_id=stored_budget_id,
            )
            if result.get("error"):
                raise DomainError("serve.protected_interaction_failed", status=502, error=result["error"])
            return result
        require_unprotected_api_execution(job_data)

        overview = parse_overview(job_data)
        payload = job_data.get("payload")
        if not payload or not isinstance(payload, dict):
            raise DomainError("optimization.no_payload", status=404)

        dataset = payload.get("dataset", [])
        total = len(dataset)
        column_mapping_raw = payload.get("column_mapping", {})
        try:
            column_mapping = ColumnMapping.model_validate(column_mapping_raw)
        except ValidationError:
            raise DomainError("optimization.corrupt_column_mapping", status=500) from None

        metric_code = payload.get("metric_code", "")
        if not metric_code:
            raise DomainError("optimization.no_metric_code", status=400)
        metric = load_metric_from_code(metric_code)

        model_settings = payload.get("model_config") or overview.get(PAYLOAD_OVERVIEW_MODEL_SETTINGS, {})
        model_name_str = overview.get(PAYLOAD_OVERVIEW_MODEL_NAME, "")
        model_config = None
        if model_settings:
            try:
                model_config = ModelConfig.model_validate(model_settings)
            except ValidationError:
                logger.warning("Optimization %s has a corrupt model_config; trying the model name", optimization_id)
        if model_config is None:
            if model_name_str:
                model_config = ModelConfig(name=model_name_str)
            else:
                raise DomainError("optimization.no_model_config", status=400)

        if model_config.token_source == TOKEN_SOURCE_BYOK:
            engine = getattr(job_store, "engine", None)
            if engine is None:
                raise DomainError("billing.byok_missing_connection", status=400, provider="")
            try:
                model_config = resolve_byok_model_config(
                    model_config,
                    username=current_user.username,
                    vault=ProviderKeyVault(engine=engine),
                )
            except ValueError as exc:
                raise DomainError("billing.byok_missing_connection", status=400, provider=str(exc)) from exc
        lm = build_language_model(model_config)

        if program_type == "baseline":
            signature_code = payload.get("signature_code", "")
            signature_cls = load_signature_from_code(signature_code)
            module_name = payload.get("module_name", "predict")
            module_kwargs = dict(payload.get("module_kwargs", {}))

            try:
                module_factory, auto_signature = resolve_module_factory(module_name)
            except ResolverError as exc:
                raise DomainError("submission.module_resolve_failed", status=400, error=str(exc)) from exc
            if auto_signature or "signature" not in module_kwargs:
                module_kwargs["signature"] = signature_cls
            program = module_factory(**module_kwargs)
        else:
            result_data = job_data.get("result")
            if not result_data:
                raise DomainError("optimization.no_result_for_artifact", status=409)
            result = RunResponse.model_validate(result_data)
            artifact = result.program_artifact
            if not _artifact_has_payload(artifact):
                raise DomainError("optimization.no_program_artifact", status=409)
            if optimization_id not in _program_cache:
                # Pre-migration jobs may not have signature_code in their
                # ``payload_overview``, but the original ``payload`` row
                # always carries it — fall back to that so legacy runs can
                # still be reconstructed.
                effective_overview = {
                    **overview,
                    PAYLOAD_OVERVIEW_SIGNATURE_CODE: (
                        overview.get(PAYLOAD_OVERVIEW_SIGNATURE_CODE) or payload.get("signature_code")
                    ),
                    PAYLOAD_OVERVIEW_MODULE_NAME: (
                        overview.get(PAYLOAD_OVERVIEW_MODULE_NAME) or payload.get("module_name")
                    ),
                    PAYLOAD_OVERVIEW_MODULE_KWARGS: (
                        overview.get(PAYLOAD_OVERVIEW_MODULE_KWARGS) or payload.get("module_kwargs", {})
                    ),
                }
                _program_cache[optimization_id] = _materialize_program(artifact, effective_overview)
            program = _program_cache[optimization_id]

        results = []
        with dspy.context(lm=lm):
            for idx in indices:
                if idx < 0 or idx >= total:
                    continue
                row = dataset[idx]
                example_dict = {}
                for sig_field, col_name in column_mapping.inputs.items():
                    example_dict[sig_field] = row.get(col_name, "")
                for sig_field, col_name in column_mapping.outputs.items():
                    example_dict[sig_field] = row.get(col_name, "")

                example = dspy.Example(**example_dict).with_inputs(*list(column_mapping.inputs.keys()))

                try:
                    prediction = program(**{k: example_dict[k] for k in column_mapping.inputs})
                    outputs = {}
                    for sig_field in column_mapping.outputs:
                        outputs[sig_field] = getattr(prediction, sig_field, None)

                    try:
                        score = metric(example, prediction)
                        score = float(score) if isinstance(score, int | float | bool) else 0.0
                    except Exception:
                        score = 0.0

                    results.append(
                        {
                            "index": idx,
                            "outputs": outputs,
                            "score": score,
                            "pass": score > 0,
                        }
                    )
                except Exception as exc:
                    results.append(
                        {
                            "index": idx,
                            "outputs": {},
                            "score": 0.0,
                            "pass": False,
                            "error": str(exc),
                        }
                    )

        return {"results": results, "program_type": program_type}

    @router.get(
        "/optimizations/{optimization_id}/agent-runs/{run_id}",
        response_model=BlackboxAgentRunResponse,
        summary="One sandboxed agent run of a black-box optimization",
        tags=["agent"],
    )
    def get_agent_run(
        optimization_id: str,
        run_id: int,
        current_user: AuthenticatedUserDep,
        since_transcript: int = Query(0, ge=0),
    ) -> BlackboxAgentRunResponse:
        """Return the answer, transcript and outcome of one sandboxed agent run.

        The run view polls this while the run is live, passing the transcript
        length it already holds so only the new tail travels.

        Args:
            optimization_id: The owning optimization.
            run_id: The run's ordinal within the job.
            current_user: Authenticated caller resolved from the bearer token.
            since_transcript: Transcript characters the caller already has.

        Returns:
            The run's record, with the transcript from ``since_transcript`` on.

        Raises:
            DomainError: 404 when the optimization or the run is unknown or
                inaccessible to the caller.
        """
        load_job_for_user(job_store, optimization_id, current_user)
        get_run = getattr(job_store, "get_agent_run", None)
        run = None if get_run is None else get_run(optimization_id, run_id)
        if run is None:
            raise DomainError(
                "optimization.agent_run_not_found", status=404, optimization_id=optimization_id, run_id=run_id
            )
        transcript = run.get("transcript") or ""
        offset = min(since_transcript, len(transcript))
        return BlackboxAgentRunResponse.model_validate(
            {
                **run,
                "transcript": transcript[offset:],
                "transcript_offset": offset,
                "transcript_length": len(transcript),
            }
        )

    @router.get(
        "/optimizations/{optimization_id}/candidates",
        response_model=RunCandidateList,
        summary="The candidates of a run with state, call count and cost so far",
        tags=["agent"],
    )
    def list_run_candidates(
        optimization_id: str,
        current_user: AuthenticatedUserDep,
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> RunCandidateList:
        """Return one page of a run's candidates with role, state, call count and cost.

        Args:
            optimization_id: The run.
            current_user: Authenticated caller resolved from the bearer token.
            limit: The most candidates to return in this page.
            offset: The number of candidates to skip, in start order.

        Returns:
            One page of the run's candidates in start order, and the next offset.

        Raises:
            DomainError: 404 when the run is unknown or the caller is not its owner.
        """
        job_data = _load_job_for_owner(job_store, optimization_id, current_user)
        job_status = status_to_job_status(job_data.get("status", "pending"))
        list_candidates = getattr(job_store, "list_run_candidates", None)
        candidates = [] if list_candidates is None else list_candidates(optimization_id, limit=limit + 1, offset=offset)
        page = candidates[:limit]
        return RunCandidateList.model_validate(
            {
                "optimization_id": optimization_id,
                "candidates": [{**c, "state": _effective_candidate_state(c["state"], job_status)} for c in page],
                "next_offset": offset + len(page),
                "has_more": len(candidates) > limit,
            }
        )

    @router.get(
        "/optimizations/{optimization_id}/candidates/{candidate_id}/calls",
        response_model=RunCallPage,
        summary="One candidate's relay calls in time order, after a cursor",
        tags=["agent"],
    )
    def list_run_calls(
        optimization_id: str,
        current_user: AuthenticatedUserDep,
        # The store keeps 64 characters of a candidate id; a longer id could
        # never match, so it is refused instead of cut.
        candidate_id: str = Path(max_length=64),
        since: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
    ) -> RunCallPage:
        """Return a candidate's relay calls after the ``since`` cursor.

        A live monitor polls this with the last ``next_since`` it got, so only
        the new calls travel, the same as the agent-run transcript route.

        Args:
            optimization_id: The run.
            candidate_id: The candidate within the run.
            current_user: Authenticated caller resolved from the bearer token.
            since: The last call ``seq`` the caller holds; ``0`` for the start.
            limit: The most calls to return in this page.

        Returns:
            One page of calls, the next cursor, and whether the candidate still runs.

        Raises:
            DomainError: 404 when the run is unknown or the caller is not its owner.
        """
        job_data = _load_job_for_owner(job_store, optimization_id, current_user)
        job_status = status_to_job_status(job_data.get("status", "pending"))
        list_calls = getattr(job_store, "list_run_calls", None)
        get_candidate = getattr(job_store, "get_run_candidate", None)
        calls = [] if list_calls is None else list_calls(optimization_id, candidate_id, since=since, limit=limit + 1)
        candidate = None if get_candidate is None else get_candidate(optimization_id, candidate_id)
        # A candidate with no call yet may still start: it counts as running
        # until the run itself ends, so a monitor does not stop polling early.
        if candidate is None:
            running = job_status not in TERMINAL_STATUSES
        else:
            running = _effective_candidate_state(candidate["state"], job_status) == "running"
        page = calls[:limit]
        return RunCallPage.model_validate(
            {
                "optimization_id": optimization_id,
                "candidate_id": candidate_id,
                "running": running,
                "calls": page,
                "next_since": page[-1]["seq"] if page else since,
                "has_more": len(calls) > limit,
            }
        )

    @router.get(
        "/optimizations/{optimization_id}/test-results",
        summary="Per-example baseline and optimized test scores",
        tags=["agent"],
    )
    def get_test_results(optimization_id: str, current_user: AuthenticatedUserDep) -> dict:
        """Return stored per-example baseline and optimized test scores.

        Sequential test-split indices are remapped to global dataset indices for
        UI use. No inference is executed.

        Args:
            optimization_id: Optimization id whose test results should be returned.
            current_user: Authenticated caller resolved from the bearer token.

        Returns:
            ``{"baseline": [...], "optimized": [...]}`` with global indices.

        Raises:
            DomainError: 404 if unknown or inaccessible, 409 if no result yet.
        """
        job_data = load_job_for_user(job_store, optimization_id, current_user)

        result_data = job_data.get("result")
        if not result_data:
            raise DomainError("optimization.no_result_pending", status=409)

        try:
            result = RunResponse.model_validate(result_data)
        except ValidationError:
            raise DomainError("optimization.corrupt_result", status=500) from None

        payload = job_data.get("payload")
        if not isinstance(payload, dict):
            raise DomainError("optimization.payload_unavailable", status=404)
        dataset = payload.get("dataset", [])
        total = len(dataset)
        fractions_raw = payload.get("split_fractions", {})
        try:
            fractions = SplitFractions.model_validate(fractions_raw)
        except ValidationError:
            fractions = SplitFractions()
        shuffle = payload.get("shuffle", True)
        seed = payload.get("seed")
        effective_seed = seed if seed is not None else stable_seed(optimization_id)

        ordered = list(range(total))
        if shuffle:
            rng = random.Random(effective_seed)
            rng.shuffle(ordered)
        train_end = int(total * fractions.train)
        val_end = train_end + int(total * fractions.val)
        test_indices = ordered[val_end:]

        return {
            "baseline": remap_test_indices(result.baseline_test_results, test_indices),
            "optimized": remap_test_indices(result.optimized_test_results, test_indices),
        }

    @router.get(
        "/optimizations/{optimization_id}/artifact",
        response_model=ProgramArtifactResponse,
        summary="Download the compiled DSPy program artifact",
    )
    def get_job_artifact(optimization_id: str, current_user: AuthenticatedUserDep) -> ProgramArtifactResponse:
        """Return the pickled program artifact for a successful single-run optimization.

        Grid searches 404 here — use ``/grid-result`` instead (one artifact per pair).

        Args:
            optimization_id: Optimization id whose artifact should be returned.
            current_user: Authenticated caller resolved from the bearer token.

        Returns:
            A ``ProgramArtifactResponse`` carrying the pickled program.

        Raises:
            DomainError: 404 (unknown / inaccessible / grid), 409 (not
                success), 500 (corrupt result).
        """

        job_data = load_job_for_user(job_store, optimization_id, current_user)

        overview = parse_overview(job_data)
        optimization_type = overview.get(PAYLOAD_OVERVIEW_OPTIMIZATION_TYPE, OPTIMIZATION_TYPE_RUN)

        if optimization_type == OPTIMIZATION_TYPE_GRID_SEARCH:
            raise DomainError("grid_search.artifact_per_pair_redirect", status=404)

        status = status_to_job_status(job_data.get("status", "pending"))

        if status in {OptimizationStatus.pending, OptimizationStatus.validating, OptimizationStatus.running}:
            raise DomainError("optimization.not_finished", status=409)

        if status == OptimizationStatus.failed:
            error_msg = job_data.get("message") or "unknown error"
            raise DomainError("optimization.failed_no_artifact", status=409, error=error_msg)

        if status == OptimizationStatus.cancelled:
            raise DomainError("optimization.cancelled_no_artifact", status=409)

        if status == OptimizationStatus.success:
            result_data = job_data.get("result")
            if result_data and isinstance(result_data, dict):
                try:
                    result = RunResponse.model_validate(result_data)
                except ValidationError:
                    logger.warning("Optimization %s has corrupted result data", optimization_id)
                    raise DomainError("optimization.corrupt_result", status=500) from None
                return ProgramArtifactResponse(
                    program_artifact=result.program_artifact,
                )

        raise DomainError("optimization.no_artifact_generic", status=409)

    @router.get(
        "/optimizations/{optimization_id}/program-export",
        summary="Download a self-contained, runnable export of the compiled DSPy program",
    )
    def export_job_program(
        optimization_id: str,
        current_user: AuthenticatedUserDep,
        pair_index: Annotated[int | None, Query(ge=0)] = None,
    ) -> StreamingResponse:
        """Stream a zip that reconstructs and runs the program with plain ``dspy``.

        Unlike ``/serve`` (which runs the program on the platform's key), this
        packages the persisted state JSON, the signature source, the module
        recipe, and a standalone loader so the caller owns and runs the program
        themselves — no platform endpoint required. Grid searches export the
        requested pair, or their winning pair when ``pair_index`` is omitted.

        Args:
            optimization_id: Optimization id whose program should be exported.
            current_user: Authenticated caller resolved from the bearer token.
            pair_index: Optional grid-search pair to export instead of the winner.

        Returns:
            A ``StreamingResponse`` carrying the zip as a file attachment.

        Raises:
            DomainError: 404 (unknown / inaccessible / pair), 409 (not success,
                or no reconstructable program), 500 (corrupt result).
        """

        job_data = load_job_for_user(job_store, optimization_id, current_user)

        overview = parse_overview(job_data)
        optimization_type = overview.get(PAYLOAD_OVERVIEW_OPTIMIZATION_TYPE, OPTIMIZATION_TYPE_RUN)

        status = status_to_job_status(job_data.get("status", "pending"))

        if status in {OptimizationStatus.pending, OptimizationStatus.validating, OptimizationStatus.running}:
            raise DomainError("optimization.not_finished", status=409)
        if status == OptimizationStatus.failed:
            error_msg = job_data.get("message") or "unknown error"
            raise DomainError("optimization.failed_no_artifact", status=409, error=error_msg)
        if status == OptimizationStatus.cancelled:
            raise DomainError("optimization.cancelled_no_artifact", status=409)
        if status != OptimizationStatus.success:
            raise DomainError("optimization.no_artifact_generic", status=409)

        result_data = job_data.get("result")
        if not (result_data and isinstance(result_data, dict)):
            raise DomainError("optimization.no_artifact_generic", status=409)
        export_overview = overview
        export_pair_index: int | None = None
        if optimization_type == OPTIMIZATION_TYPE_GRID_SEARCH:
            try:
                grid_result = GridSearchResponse.model_validate(result_data)
            except ValidationError:
                logger.warning("Grid search %s has corrupted result data", optimization_id)
                raise DomainError("grid_search.corrupt_result", status=500) from None
            pair = grid_result.best_pair
            if pair_index is not None:
                pair = next((item for item in grid_result.pair_results if item.pair_index == pair_index), None)
                if pair is None:
                    raise DomainError(
                        "grid_search.pair_position_missing",
                        status=404,
                        pair_index=pair_index,
                    )
            if pair is None:
                raise DomainError("grid_search.no_best_pair", status=409)
            artifact = pair.program_artifact
            export_pair_index = pair.pair_index
            export_overview = {**overview, PAYLOAD_OVERVIEW_MODEL_NAME: pair.generation_model}
            if artifact is None or artifact.program_state_json is None:
                raise DomainError(
                    "grid_search.pair_no_artifact",
                    status=409,
                    pair_index=pair.pair_index,
                )
        else:
            try:
                result = RunResponse.model_validate(result_data)
            except ValidationError:
                logger.warning("Optimization %s has corrupted result data", optimization_id)
                raise DomainError("optimization.corrupt_result", status=500) from None
            artifact = result.program_artifact

        # The export reconstructs from state JSON; a legacy pickle-only artifact
        # carries no signature_code and isn't portably rebuildable, so it's 409.
        if artifact is None or artifact.program_state_json is None:
            raise DomainError("optimization.no_artifact_generic", status=409)
        # A workflow run carries no top-level signature: its program definition
        # is the persisted graph, and each node's signature travels inside it.
        if not (overview.get(PAYLOAD_OVERVIEW_SIGNATURE_CODE) or overview.get(PAYLOAD_OVERVIEW_WORKFLOW)):
            raise DomainError("optimization.no_signature_code_for_reload", status=409)

        zip_bytes = build_program_export_zip(
            optimization_id=optimization_id,
            artifact=artifact,
            overview=export_overview,
        )
        pair_suffix = f"_pair_{export_pair_index}" if export_pair_index is not None else ""
        filename = f"dspy_program_{optimization_id[:8]}{pair_suffix}.zip"
        return StreamingResponse(
            iter([zip_bytes]),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @router.get(
        "/optimizations/{optimization_id}/grid-result",
        response_model=GridSearchResponse,
        summary="Retrieve the full grid-search result with per-pair details",
        tags=["agent"],
    )
    def get_grid_search_result(optimization_id: str, current_user: AuthenticatedUserDep) -> GridSearchResponse:
        """Return all pair results for a finished grid search, including ``best_pair``.

        Only valid after the sweep reaches a terminal status. For live progress
        use ``GET /optimizations/{id}`` whose ``grid_result`` field updates
        in-flight.

        Args:
            optimization_id: Grid-search optimization id.
            current_user: Authenticated caller resolved from the bearer token.

        Returns:
            The full ``GridSearchResponse`` including every pair.

        Raises:
            DomainError: 404 (unknown / inaccessible / not grid / no
                result), 409 (still running / failed without result), 500
                (corrupt result).
        """
        job_data = load_job_for_user(job_store, optimization_id, current_user)

        overview = parse_overview(job_data)
        if overview.get(PAYLOAD_OVERVIEW_OPTIMIZATION_TYPE) != OPTIMIZATION_TYPE_GRID_SEARCH:
            raise DomainError("grid_search.not_a_grid_search", status=404)

        status = status_to_job_status(job_data.get("status", "pending"))
        if status not in TERMINAL_STATUSES:
            raise DomainError("optimization.not_finished", status=409)

        result_data = job_data.get("result")
        if not result_data or not isinstance(result_data, dict):
            if status == OptimizationStatus.failed:
                error_msg = job_data.get("message") or "unknown error"
                raise DomainError("grid_search.failed_no_result", status=409, error=error_msg)
            if status == OptimizationStatus.cancelled:
                raise DomainError("grid_search.cancelled_no_result", status=409)
            raise DomainError("grid_search.no_result_available", status=404)

        try:
            return GridSearchResponse.model_validate(result_data)
        except ValidationError:
            raise DomainError("grid_search.corrupt_result", status=500) from None

    @router.get(
        "/optimizations/{optimization_id}/pair/{pair_index}/test-results",
        summary="Per-example test scores for one grid-search pair",
        tags=["agent"],
    )
    def get_pair_test_results(optimization_id: str, pair_index: int, current_user: AuthenticatedUserDep) -> dict:
        """Per-pair analogue of ``GET /test-results`` with global-index remapping.

        Args:
            optimization_id: Grid-search optimization id.
            pair_index: Index of the pair to score.
            current_user: Authenticated caller resolved from the bearer token.

        Returns:
            ``{"baseline": [...], "optimized": [...]}`` with global indices.

        Raises:
            DomainError: 404 (unknown / inaccessible / pair missing), 409
                (not a grid search, not success, or no stored result).
        """
        job_data = load_job_for_user(job_store, optimization_id, current_user)

        overview = parse_overview(job_data)
        optimization_type = overview.get(PAYLOAD_OVERVIEW_OPTIMIZATION_TYPE, OPTIMIZATION_TYPE_RUN)

        if optimization_type != OPTIMIZATION_TYPE_GRID_SEARCH:
            raise DomainError("grid_search.pair_test_results_grid_only", status=409)

        status = status_to_job_status(job_data.get("status", "pending"))
        if status != OptimizationStatus.success:
            raise DomainError(
                "optimization.not_success_status_for_test_results",
                status=409,
                params={"status": status.value},
            )

        result_data = job_data.get("result")
        if not result_data or not isinstance(result_data, dict):
            raise DomainError("optimization.no_result", status=409)

        try:
            grid_result = GridSearchResponse.model_validate(result_data)
        except ValidationError:
            raise DomainError("grid_search.corrupt_result", status=500) from None

        pair = None
        for pr in grid_result.pair_results:
            if pr.pair_index == pair_index:
                pair = pr
                break
        if pair is None:
            raise DomainError(
                "grid_search.pair_position_missing",
                status=404,
                pair_index=pair_index,
            )

        payload = job_data.get("payload")
        if not isinstance(payload, dict):
            raise DomainError("optimization.payload_unavailable", status=404)
        dataset = payload.get("dataset", [])
        total = len(dataset)
        fractions_raw = payload.get("split_fractions", {})
        try:
            fractions = SplitFractions.model_validate(fractions_raw)
        except ValidationError:
            fractions = SplitFractions()
        shuffle = payload.get("shuffle", True)
        seed = payload.get("seed")
        effective_seed = seed if seed is not None else stable_seed(optimization_id)

        ordered = list(range(total))
        if shuffle:
            rng = random.Random(effective_seed)
            rng.shuffle(ordered)
        train_end = int(total * fractions.train)
        val_end = train_end + int(total * fractions.val)
        test_indices = ordered[val_end:]

        return {
            "baseline": remap_test_indices(pair.baseline_test_results, test_indices),
            "optimized": remap_test_indices(pair.optimized_test_results, test_indices),
        }
