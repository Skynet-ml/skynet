"""Streaming endpoints for the onboarding intake interview and resumable agent turns. [INTERNAL]

All endpoints are hidden from the public Scalar reference (none are in
``_SCALAR_PUBLIC_PATHS``). The onboarding screen runs its intake interview
here; the resume and cancel endpoints serve the turns the interview starts.
Not part of the dev integration surface.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse

from ...service_gateway.agents.intake_interview import MAX_REQUEST_TURNS, intake_turn_stream
from ...storage.models import UserModel
from ..agent_engine import engine_agent_model
from ..agent_turns import AgentTurnRegistry
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..model_catalog import ReasoningEffort, get_catalog_cached
from ..model_router import (
    effective_reasoning_effort,
    metering_token_source,
    route_interview_model,
    route_local_byok_model,
)
from ..wizard_agent_quota import consume_wizard_agent_turn
from ._helpers import enforce_llm_balance, sse_from_events, stream_with_llm_metering
from .experience import INTAKE_MAX_BYTES

logger = logging.getLogger(__name__)

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]


def _intake_completed(engine: Any, username: str) -> bool:
    """Report whether the caller already finished the onboarding intake.

    Args:
        engine: SQLAlchemy engine holding the accounts, or ``None``.
        username: Account starting the turn.

    Returns:
        ``True`` only for an account row stamped as having finished the intake.
    """
    if engine is None:
        return False
    with Session(engine) as session:
        row = session.get(UserModel, username)
        return row is not None and row.intake_completed_at is not None


def gate_intake_turn(job_store: Any, username: str) -> tuple[Any, bool]:
    """Admit an onboarding intake turn and decide who pays for it.

    The intake runs on a brand-new account's first login, usually with no
    credits and no key. An account that can pay is billed like any other
    interview turn. One that cannot, and has not finished the intake yet,
    runs on the platform: the turn is unbilled, counts against the free
    wizard agent's daily cap, and the caller forces the cheap intake model.

    Args:
        job_store: Job-store whose engine backs billing and the usage cap.
        username: Account starting the turn.

    Returns:
        ``(store to meter against or None, whether the platform pays)``.

    Raises:
        DomainError: 402 when the account cannot pay and already finished
            the intake; 429 at the free agent's daily cap.
    """
    try:
        enforce_llm_balance(job_store, username)
        return job_store, False
    except DomainError as exc:
        if exc.status_code != 402:
            raise
        engine = getattr(job_store, "engine", None)
        if _intake_completed(engine, username):
            raise
        consume_wizard_agent_turn(engine, username)
        return None, True


def _intake_catalog() -> tuple[list[Any], list[str]]:
    """Read the catalog's models and provider slugs for an intake turn.

    Returns:
        ``(models, provider slugs)``; both empty when the catalog is unavailable,
        which leaves the turn on the server default model with no BYOK provider to name.
    """
    try:
        catalog = get_catalog_cached()
    except Exception:
        logger.warning("model catalog unavailable for the intake interview", exc_info=True)
        return [], []
    slugs = {p.slug for p in catalog.providers} | {m.provider for m in catalog.models if getattr(m, "provider", None)}
    return list(catalog.models), sorted(slugs)


def _low_effort(model: str | None, catalog_models: list[Any]) -> str | None:
    """Pick the low reasoning effort for a turn's model, when it takes one.

    Args:
        model: The model the turn runs on.
        catalog_models: Catalog entries.

    Returns:
        ``"low"`` snapped onto the model's ladder, or ``None`` for a model
        that declares no reasoning control (sending one could be rejected).
    """
    entry = next((m for m in catalog_models if m.value == model), None)
    if entry is None or not (entry.reasoning_efforts or getattr(entry, "supports_thinking", False)):
        return None
    return effective_reasoning_effort(model, "low")


class IntakeTurn(BaseModel):
    """A single prior turn of one intake phase."""

    role: Literal["user", "assistant"] = Field(..., description="'user' or 'assistant'.")
    content: str = Field(..., max_length=4000, description="Message text.")


class IntakeInterviewRequest(BaseModel):
    """Request body for ``POST /account/intake-interview``."""

    phase: Literal["billing", "privacy", "emails", "trust"] = Field(..., description="The intake phase this turn runs.")
    turns: list[IntakeTurn] = Field(
        default_factory=list,
        max_length=MAX_REQUEST_TURNS,
        description="This phase's prior turns, oldest first; empty asks the phase's opening question.",
    )
    profile: dict[str, Any] = Field(
        default_factory=dict,
        description="The partial intake profile so far; the interviewer never asks about what it holds.",
    )
    locale: str | None = Field(default=None, description="UI locale code; sets the reply language.")
    model: str | None = Field(
        default=None,
        description=(
            "LiteLLM id of a catalog model to conduct the turn. Absent runs the cheapest featured model; "
            "ignored when the platform pays for a new account's turn."
        ),
    )
    reasoning_effort: ReasoningEffort | None = Field(
        default=None, description="Explicit reasoning effort; absent runs low effort when the model takes one."
    )

    @field_validator("profile")
    @classmethod
    def _cap_profile_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Reject a profile larger than the stored intake cap once serialized.

        Args:
            value: Profile from the request.

        Returns:
            The unchanged profile.

        Raises:
            ValueError: When the serialized profile exceeds the cap.
        """
        if len(json.dumps(value, ensure_ascii=False, default=str).encode()) > INTAKE_MAX_BYTES:
            raise ValueError(f"profile must serialize to at most {INTAKE_MAX_BYTES} bytes")
        return value


class CancelAgentTurnResponse(BaseModel):
    """Envelope for ``POST /optimizations/agent-turns/{turn_id}/cancel``."""

    cancelled: bool


_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _parse_last_event_id(value: str | None) -> int:
    """Read a resumable stream's ``Last-Event-ID`` header as a sequence number.

    Args:
        value: Raw header value, if the client sent one.

    Returns:
        The sequence number, or 0 when absent or not a non-negative integer.
    """
    if value is None or not value.strip().isdigit():
        return 0
    return int(value.strip())


def create_code_agent_router(*, job_store=None) -> APIRouter:
    """Mount the onboarding intake interview and the agent-turn endpoints.

    The intake route runs its turn through the resumable
    :mod:`~core.api.agent_turns` registry, alongside the resume and cancel
    endpoints that serve it.

    Args:
        job_store: Optional job-store whose engine backs the balance gate and
            per-turn usage metering. When ``None``, turns stream unmetered
            (legacy behavior).

    Returns:
        A configured :class:`APIRouter` with the intake and agent-turn endpoints attached.
    """
    router = APIRouter()
    turns = AgentTurnRegistry()
    engine = getattr(job_store, "engine", None) if job_store is not None else None
    if engine is not None:
        turns.bind_engine(engine)

    async def _turn_response(source: AsyncIterator[dict], username: str) -> StreamingResponse:
        """Start ``source`` as a resumable turn and stream its first response window.

        Args:
            source: The turn's metered event stream.
            username: Owner of the turn.

        Returns:
            A :class:`StreamingResponse` of Server-Sent Events whose first
            event is ``turn_started``.
        """
        turn_id = await turns.start(source, owner=username)
        return StreamingResponse(
            sse_from_events(turns.stream(turn_id)), media_type="text/event-stream", headers=_SSE_HEADERS
        )

    @router.post(
        "/account/intake-interview",
        summary="Stream one onboarding intake interview turn",
    )
    async def intake_interview(req: IntakeInterviewRequest, current_user: AuthenticatedUserDep) -> StreamingResponse:
        """Stream one turn of an intake phase as SSE.

        Events: ``reasoning_patch``, ``message_patch``, ``message_end``,
        ``turn_hint`` with ``final`` = phase done, ``message_reset`` and
        ``error``, ending with
        ``interview_done`` — ``{"message", "options", "phase_done",
        "profile_patch", "skip_phases", "skip_rest", "model",
        "served_model"}``. Framed as a resumable turn.

        An account below the turn balance that has not finished the intake
        runs on the platform's cheap intake model, unbilled and under the
        free agent's daily cap (see :func:`gate_intake_turn`).

        Args:
            req: Phase, this phase's transcript, the profile so far, locale.
            current_user: The authenticated caller.

        Returns:
            A :class:`StreamingResponse` of Server-Sent Events.
        """
        billed_store, platform_paid = await asyncio.to_thread(gate_intake_turn, job_store, current_user.username)
        catalog_models, provider_slugs = await asyncio.to_thread(_intake_catalog)
        picked = await asyncio.to_thread(
            engine_agent_model, job_store, current_user.username, None if platform_paid else req.model
        )
        byok_config = await asyncio.to_thread(
            route_local_byok_model, picked, job_store=job_store, username=current_user.username, interview=True
        )
        model = byok_config.name if byok_config is not None else await asyncio.to_thread(route_interview_model, picked)
        if req.reasoning_effort and not platform_paid:
            effort = effective_reasoning_effort(model, req.reasoning_effort)
        else:
            effort = _low_effort(model, catalog_models)
        usage_sink: list = []

        async def source() -> AsyncIterator[dict]:
            """Relay engine events, translating failures into an error event."""
            try:
                async for event in intake_turn_stream(
                    phase=req.phase,
                    turns=[t.model_dump() for t in req.turns],
                    profile=req.profile,
                    provider_slugs=provider_slugs,
                    locale=req.locale,
                    model=model,
                    reasoning_effort=effort,
                    usage_sink=usage_sink,
                    model_config=byok_config,
                ):
                    yield event
            except Exception:
                logger.exception("intake interview stream failed")
                yield {
                    "event": "error",
                    "data": {"error": "intake.interview.llm_failed", "code": "intake.interview.llm_failed"},
                }

        metered = stream_with_llm_metering(
            source(),
            job_store=billed_store,
            username=current_user.username,
            description="Onboarding interview",
            usage_sink=usage_sink,
            token_source=metering_token_source(byok_config),
        )
        return await _turn_response(metered, current_user.username)

    @router.get(
        "/optimizations/agent-turns/{turn_id}/stream",
        summary="Resume a wizard agent turn's event stream",
    )
    async def resume_agent_turn(
        turn_id: str,
        current_user: AuthenticatedUserDep,
        after: Annotated[int | None, Query(ge=0, description="Last event id already received.")] = None,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        """Replay a turn's events after the client's last one, then tail it.

        The hosting edge cuts any request at 15 minutes, so each response
        ends after a window and the client reconnects here until it receives
        ``turn_end``. Works from any replica: events of a turn owned by
        another replica are read from the shared store.

        Args:
            turn_id: Turn returned by the ``turn_started`` event.
            current_user: The authenticated caller; must own the turn.
            after: Last event id the client holds; wins over the header.
            last_event_id: Standard SSE resume header, used when ``after`` is absent.

        Returns:
            A :class:`StreamingResponse` of Server-Sent Events.

        Raises:
            DomainError: 404 when the turn is unknown, expired or not the caller's.
        """
        await turns.authorize(turn_id, current_user.username)
        after_seq = after if after is not None else _parse_last_event_id(last_event_id)
        return StreamingResponse(
            sse_from_events(turns.stream(turn_id, after_seq=after_seq)),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    @router.post(
        "/optimizations/agent-turns/{turn_id}/cancel",
        response_model=CancelAgentTurnResponse,
        summary="Stop a running wizard agent turn",
    )
    async def cancel_agent_turn(turn_id: str, current_user: AuthenticatedUserDep) -> CancelAgentTurnResponse:
        """Stop a turn the caller started (the wizard's explicit Stop).

        Idempotent: stopping a finished, unknown or foreign turn is a no-op
        reported as ``cancelled: false``.

        Args:
            turn_id: Turn to stop.
            current_user: The authenticated caller; must own the turn.

        Returns:
            A :class:`CancelAgentTurnResponse`; ``cancelled`` is true when a
            running turn was stopped or flagged for its owning replica.
        """
        return CancelAgentTurnResponse(cancelled=await turns.cancel(turn_id, current_user.username))

    return router
