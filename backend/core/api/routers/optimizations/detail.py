"""Single-optimization read routes. [MIXED]

Public dev surface (in ``_SCALAR_PUBLIC_PATHS``):
- ``GET /optimizations/{id}`` — full job state.
- ``GET /optimizations/{id}/summary`` — compact metrics summary.

Internal (frontend-only, hidden from public docs):
- ``GET /optimizations/{id}/agent-runs/{run_id}`` — one sandboxed agent run's answer and transcript.
- ``GET /optimizations/{id}/chat`` — the setup chat that created the run, for its owner and admins.
- ``GET /optimizations/{id}/candidates`` — the run's candidates with state, call count and cost (owner only).
- ``GET /optimizations/{id}/candidates/{candidate_id}/calls`` — one candidate's relay calls after a cursor (owner only).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ....config import settings
from ....constants import OPTIMIZATION_TYPE_BLACKBOX, PAYLOAD_OVERVIEW_OPTIMIZATION_TYPE
from ....models import (
    BlackboxAgentRunResponse,
    BlackboxRunResponse,
    JobLogEntry,
    OptimizationStatus,
    OptimizationStatusResponse,
    OptimizationSummaryResponse,
)
from ....models.run_calls import RunCallPage, RunCandidateList
from ....storage.models import AgentConversationModel, JobModel
from ...auth import AuthenticatedUser, get_authenticated_user
from ...converters import (
    compute_elapsed,
    job_owner,
    overview_to_base_fields,
    parse_overview,
    parse_timestamp,
    status_to_job_status,
)
from ...errors import DomainError
from ...sharing_access import ShareRole
from .._helpers import (
    build_summary,
    is_pausable,
    is_resumable,
    load_job_for_user,
    load_job_with_role,
)
from ..agent_history import ConversationDetail, conversation_detail
from ..agent_settings import mask_secrets
from ..constants import TERMINAL_STATUSES
from ._local import execution_budget_view

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
        ``progress_offset`` / ``logs_offset``). Corrupted result data is omitted
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

        optimization_type = overview.get(PAYLOAD_OVERVIEW_OPTIMIZATION_TYPE)

        blackbox_result = None
        result_data = job_data.get("result")
        if (
            result_data
            and isinstance(result_data, dict)
            and status in {OptimizationStatus.success, OptimizationStatus.stopped}
            and optimization_type == OPTIMIZATION_TYPE_BLACKBOX
        ):
            try:
                blackbox_result = BlackboxRunResponse.model_validate(result_data)
            except ValidationError:
                logger.warning("Optimization %s has corrupted result data", optimization_id)

        created_at = parse_timestamp(job_data.get("created_at")) or datetime.now(UTC)
        started_at = parse_timestamp(job_data.get("started_at"))
        completed_at = parse_timestamp(job_data.get("completed_at"))

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
            **overview_to_base_fields(overview),
            message=job_data.get("message"),
            stop_reason=job_data.get("stop_reason"),
            result_availability=job_data.get("result_availability"),
            terminal_evidence=job_data.get("terminal_evidence"),
            execution_budget=execution_budget,
            recovery=job_data.get("recovery"),
            stored_bytes=job_data.get("stored_bytes", 0),
            latest_metrics=latest_metrics,
            progress_events=progress_events,
            progress_offset=progress_offset,
            logs=[JobLogEntry(**log) for log in logs],
            logs_offset=logs_offset,
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
        "/optimizations/{optimization_id}/chat",
        response_model=ConversationDetail,
        summary="The setup chat that created one optimization",
    )
    def get_run_chat(optimization_id: str, current_user: AuthenticatedUserDep) -> ConversationDetail:
        """Return the setup chat bound to a run, to its owner and admins only.

        A run's tools read the owner's runs, datasets, wallet and settings, so
        an editor or a viewer of the run does not get the chat. Only the owner
        reads it unmasked: an admin gets every credential in it masked. The
        mask hides only credential-shaped strings and secret-like keys, so a
        secret typed as plain prose still reaches admins.

        Args:
            optimization_id: The run whose setup chat is read.
            current_user: Authenticated caller resolved from the bearer token.

        Returns:
            The full chat up to this run: every turn before it, including
            earlier setups, and the agent's reply to the turn that started it.
            For an admin who is not the owner, credentials are masked.

        Raises:
            DomainError: 404 when the run is unknown or inaccessible to the
                caller, when the caller is an editor or a viewer of the run,
                or when the run has no bound chat.
        """
        job, role = load_job_with_role(job_store, optimization_id, current_user, include_payload=False)
        engine = getattr(job_store, "engine", None)
        # load_job_with_role gives admins the owner role.
        if engine is None or role != ShareRole.owner:
            raise DomainError("agent.conversation.not_found", status=404)
        with Session(engine) as session:
            bound = session.execute(
                select(JobModel.conversation_id, JobModel.created_at).where(JobModel.optimization_id == optimization_id)
            ).first()
            row = (
                session.get(AgentConversationModel, bound.conversation_id) if bound and bound.conversation_id else None
            )
            if row is None:
                raise DomainError("agent.conversation.not_found", status=404)
            detail = conversation_detail(session, row, run_started_at=bound.created_at)
        if job_owner(job) == current_user.username.strip().lower():
            return detail
        # The owner may paste a key into the chat; an admin must not read it.
        return ConversationDetail.model_validate(mask_secrets(detail.model_dump()))

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

        The list is a snapshot in start order, not in commit order. A candidate
        that commits late can sort before a page the client already holds, so
        an offset page can repeat or skip a candidate during a live run. A
        live poller re-reads from offset 0, or removes duplicates by
        ``candidate_id``.

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
        candidates = job_store.list_run_candidates(optimization_id, limit=limit + 1, offset=offset)
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
        calls = job_store.list_run_calls(optimization_id, candidate_id, since=since, limit=limit + 1)
        candidate = job_store.get_run_candidate(optimization_id, candidate_id)
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
