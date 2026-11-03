"""Streaming endpoint for the submit-wizard AI code agent. [INTERNAL]

All endpoints are hidden from the public Scalar reference (none are in
``_SCALAR_PUBLIC_PATHS``). Used by the wizard UI to author DSPy code
interactively — or, when a request carries ``unified``, the submission
wizard's run code + metric — and to run the wizard's task interview and
inputs inference; not part of the dev integration surface.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse

from ...config import settings
from ...local.cli_catalog import RetiredEffort
from ...models import (
    InputsInferenceRequest,
    InputsInferenceResponse,
    TaskInterviewRequest,
    UnifiedAuthoringContext,
)
from ...models.unified import workspace_files_error
from ...service_gateway.agents.code import run_code_agent
from ...service_gateway.agents.inputs_inference import infer_inputs
from ...service_gateway.agents.intake_interview import MAX_REQUEST_TURNS, intake_turn_stream
from ...service_gateway.agents.kickoff import fits_kickoff_budget, measured_bytes, oversized_kickoff
from ...service_gateway.agents.steering import STEER_KEY_MAX_CHARS, get_steer_store
from ...service_gateway.agents.task_interview import task_interview_turn_stream
from ...storage.models import UserModel
from ..agent_engine import engine_agent_model
from ..agent_turns import AgentTurnRegistry
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..model_catalog import ReasoningEffort, get_catalog_cached
from ..model_router import (
    cli_fast_config,
    effective_reasoning_effort,
    metering_token_source,
    route_interview_model,
    route_local_byok_model,
    route_menu_model,
)
from ..wizard_agent_quota import consume_wizard_agent_turn
from ._helpers import enforce_llm_balance, sse_from_events, stream_with_llm_metering
from .experience import INTAKE_MAX_BYTES

logger = logging.getLogger(__name__)

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]


class ChatTurn(BaseModel):
    """A single prior turn in the agent conversation."""

    role: str = Field(..., description="'user' or 'assistant'.")
    content: str = Field(..., description="Message text.")


def gate_interactive_turn(job_store: Any, username: str, free: bool) -> Any:
    """Admit a wizard turn and pick the store its usage is billed to.

    The submission wizard's agent turns (task interview, inputs inference,
    unified code authoring) are free: they count against the daily cap
    instead of the balance and are never charged. Every other turn keeps the
    balance gate and the charge.

    Args:
        job_store: Job-store whose engine backs billing and the usage cap.
        username: Account starting the turn.
        free: True for a submission-wizard turn.

    Returns:
        The store to meter the turn against; ``None`` streams it unbilled.

    Raises:
        DomainError: 429 at the wizard agent's daily cap; 402 when a billed
            turn's account is below the minimum balance.
    """
    if free:
        consume_wizard_agent_turn(getattr(job_store, "engine", None), username)
        return None
    enforce_llm_balance(job_store, username)
    return job_store


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


def _require_columns_or_unified(dataset_columns: list[str], unified: UnifiedAuthoringContext | None) -> None:
    """Keep the DSPy contract strict: only a unified request may omit the dataset.

    Args:
        dataset_columns: The request's column list.
        unified: The request's unified authoring context, when any.

    Raises:
        ValueError: When the columns are empty and no unified context is set.
    """
    if not dataset_columns and unified is None:
        raise ValueError("dataset_columns must not be empty unless unified is set.")


class CodeAgentRequest(BaseModel):
    """Input for streaming code generation.

    The server only needs the dataset's columns + roles + a small sample
    (not the full dataset); payloads stay under ~20 KB even for wide schemas.
    ``user_message`` toggles mode: empty triggers the non-agentic seed,
    non-empty invokes the ReAct chat agent (which also sees ``chat_history``
    and the current editor contents in ``prior_signature`` / ``prior_metric``).
    """

    dataset_columns: list[str] = Field(
        ...,
        description="All column names in the dataset; may be empty only with ``unified`` set.",
    )
    column_roles: dict[str, str] = Field(..., description="Column → 'input'|'output'|'ignore'.")
    column_kinds: dict[str, str] = Field(
        default_factory=dict,
        description="Input column → 'text'|'image'. Image columns get a dspy.Image typed InputField.",
    )
    sample_rows: list[dict] = Field(default_factory=list, description="Up to 5 sample rows.")
    user_message: str = Field(default="", description="User's latest message. Empty triggers seed mode.")
    chat_history: list[ChatTurn] = Field(
        default_factory=list,
        description="Prior {role, content} turns; seen by the chat agent only.",
    )
    prior_signature: str = Field(default="", description="Current signature code in the editor.")
    prior_metric: str = Field(default="", description="Current metric code in the editor.")
    prior_signature_validation: str = Field(
        default="",
        description=(
            "Short summary of the latest validation result for the current "
            "signature ('OK' / 'errors: ...' / empty). Surfaced to the chat "
            "agent so follow-up edits can target real errors."
        ),
    )
    prior_metric_validation: str = Field(
        default="",
        description=("Short summary of the latest validation result for the current metric."),
    )
    initial_signature: str = Field(
        default="",
        description=(
            "The original signature code from the very first version — used by "
            "the chat agent to honor revert requests. May equal prior_signature "
            "when no edits have happened yet."
        ),
    )
    initial_metric: str = Field(
        default="",
        description=(
            "The original metric code from the very first version — used by "
            "the chat agent to honor revert requests. May equal prior_metric "
            "when no edits have happened yet."
        ),
    )
    prior_workflow: dict | None = Field(
        default=None,
        description=(
            "The workflow graph currently on the canvas (WorkflowSpec wire "
            "shape). Non-None switches both modes to their graph-aware "
            "paths: seed drafts the full DAG, chat gets graph tools."
        ),
    )
    initial_workflow: dict | None = Field(
        default=None,
        description="The original graph from the very first version, for revert support.",
    )
    locale: str | None = Field(
        default=None,
        description=(
            "UI locale code of the client (e.g. 'he', 'en', 'fr-CA'). Sets "
            "the language of the agent's replies and edit rationales; "
            "unknown or missing falls back to Hebrew."
        ),
    )
    interview_brief: list[str] = Field(
        default_factory=list,
        description=(
            "Directives confirmed at the end of the Signature & Metric "
            "interview. The seed authors honor every directive; empty when "
            "no interview happened."
        ),
    )
    model: str | None = Field(
        default=None,
        description=(
            "LiteLLM id of the catalog model that authors the code (the "
            "composer's model menu). Absent runs the catalog's best-value "
            "default model."
        ),
    )
    reasoning_effort: ReasoningEffort | RetiredEffort | None = Field(
        default=None,
        description=(
            "Explicit reasoning-effort level for the chosen model; absent keeps the model's default. "
            "The retired levels ``ultra`` and ``ultracode`` run at the model's top level."
        ),
    )
    fast_mode: bool = Field(
        default=False,
        description=(
            "Run this turn in the CLI's fast mode. Applies only to a local CLI model that takes "
            "fast mode; any other model ignores it."
        ),
    )
    unified: UnifiedAuthoringContext | None = Field(
        default=None,
        description=(
            "Submission-wizard authoring context. Set, the agent drafts/edits "
            "the run code (``prior_signature`` / ``initial_signature`` slots, "
            "``signature_*`` events) and the metric in the shape the chosen "
            "optimizer needs (``prior_metric`` / ``initial_metric`` slots, "
            "``metric_*`` events); ``dataset_columns`` may then be empty."
        ),
    )
    prior_files: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "The wizard workspace's files besides run.py and metric.py, "
            "relative path → text (``unified`` only). The agent reads and "
            "edits them with ``write_file`` / ``delete_file``; ``file_replace`` "
            "/ ``file_delete`` events report its changes."
        ),
    )
    steer_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=STEER_KEY_MAX_CHARS,
        description=(
            "Client-chosen key for this turn. Messages posted to "
            "``/optimizations/agent-steer`` under it while the turn runs reach "
            "the agent at its next step, announced by a ``steer_applied`` event."
        ),
    )

    @model_validator(mode="after")
    def _columns_or_unified(self) -> CodeAgentRequest:
        """Reject an empty dataset unless the request is unified, and unsafe workspace files.

        Returns:
            The validated request.

        Raises:
            ValueError: When ``prior_files`` has an unsafe path or is past the limits.
        """
        _require_columns_or_unified(self.dataset_columns, self.unified)
        error = workspace_files_error(self.prior_files)
        if error is not None:
            raise ValueError(error)
        return self


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


class EditCodeRequest(BaseModel):
    """Compact input for the MCP-exposed ``edit_code`` tool.

    This is a narrower, MCP-friendly projection of :class:`CodeAgentRequest`.
    The generalist agent only needs to state its goal, the current editor
    contents, and enough dataset context to drive the code agent — not the
    full chat history or validation-state scaffolding used by the wizard.
    """

    goal: str = Field(
        ...,
        min_length=1,
        description=("What to change. Non-empty; empty triggers seed mode on the SSE endpoint."),
    )
    current_signature: str = Field(default="", description="Current signature code (may be empty on first call).")
    current_metric: str = Field(default="", description="Current metric code (may be empty on first call).")
    dataset_columns: list[str] = Field(..., min_length=1, description="All column names in the dataset.")
    column_roles: dict[str, str] = Field(..., description="Column → 'input'|'output'|'ignore'.")
    column_kinds: dict[str, str] = Field(
        default_factory=dict,
        description="Input column → 'text'|'image'.",
    )
    sample_rows: list[dict] = Field(default_factory=list, description="Up to 5 sample rows.")


class EditCodeResponse(BaseModel):
    """Final output from a blocking ``edit_code`` call."""

    signature_code: str
    metric_code: str
    assistant_message: str = ""


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
    """Mount the wizard's code-agent and interview SSE endpoints.

    Both streaming routes run their turn through the resumable
    :mod:`~core.api.agent_turns` registry, alongside the resume and cancel
    endpoints that serve it.

    Args:
        job_store: Optional job-store whose engine backs the balance gate and
            per-turn usage metering. When ``None``, turns stream unmetered
            (legacy behavior).

    Returns:
        A configured :class:`APIRouter` with the code-agent endpoints attached.
    """
    router = APIRouter()
    turns = AgentTurnRegistry()
    engine = getattr(job_store, "engine", None) if job_store is not None else None
    if engine is not None:
        turns.bind_engine(engine)
        get_steer_store().bind_engine(engine)

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
        "/optimizations/ai-generate-code",
        summary="Stream AI-generated signature + metric code",
    )
    async def ai_generate_code(req: CodeAgentRequest, current_user: AuthenticatedUserDep) -> StreamingResponse:
        """Stream DSPy code-agent events as SSE.

        Event types:

        * ``signature_patch`` / ``metric_patch`` — ``{"chunk": "<token>"}``
          (seed mode only)
        * ``reasoning_patch`` — ``{"chunk": "<token>", "source": "<stream>"}``
          (both modes; ``source`` separates the parallel seed authors:
          ``signature`` / ``metric`` / ``workflow`` / ``agent``)
        * ``artifact_done`` — ``{"source": "signature" | "metric"}`` (unified
          seed: one parallel author finished, check and repair included)
        * ``tool_start`` — ``{"id", "tool", "reason"}`` (chat mode; the
          unified tools are ``ask_user`` / ``set_brief`` / ``edit_run`` /
          ``edit_metric``)
        * ``code_draft`` — ``{"source": "signature" | "metric", "chunk",
          "reset"}`` (unified chat mode: an ``edit_run`` / ``edit_metric``
          call's code while the model writes it; ``reset`` starts a new draft)
        * ``signature_replace`` / ``metric_replace`` — ``{"code"}`` (in
          unified mode these carry the run code / the metric)
        * ``workflow_replace`` — ``{"workflow", "changed_node_id"}``
          (workflow mode: seed snapshot + one per graph tool op)
        * ``tool_end`` — ``{"id", "tool", "status"}``
        * ``message_patch`` — ``{"chunk": "<token>"}`` (chat mode reply stream)
        * ``done`` — ``{"signature_code", "metric_code", "assistant_message",
          "model", "served_model"}`` (workflow mode carries ``workflow`` +
          ``workflow_valid`` instead of ``signature_code``)
        * ``error`` — ``{"error": "<message>"}``

        The turn runs server-side independent of this connection: the stream
        opens with ``turn_started`` (``{"turn_id"}``), every event carries an
        SSE ``id``, and it closes with ``turn_end``. A response that ends
        without ``turn_end`` is resumed through
        ``GET /optimizations/agent-turns/{turn_id}/stream``.

        Args:
            req: Request body controlling code-agent inputs and chat history.
            current_user: The authenticated caller (required; gates LLM spend).

        Returns:
            A :class:`StreamingResponse` of Server-Sent Events.
        """
        billed_store = await asyncio.to_thread(
            gate_interactive_turn, job_store, current_user.username, req.unified is not None
        )
        picked = await asyncio.to_thread(engine_agent_model, job_store, current_user.username, req.model)
        byok_config = await asyncio.to_thread(
            route_local_byok_model, picked, job_store=job_store, username=current_user.username
        )
        model = byok_config.name if byok_config is not None else route_menu_model(picked)
        effort = effective_reasoning_effort(model, req.reasoning_effort)
        if req.unified is not None and not req.user_message.strip() and not req.reasoning_effort:
            # The wizard's seed lanes write two short functions; at the catalog
            # default effort a reasoning model can deliberate for minutes over
            # edge cases before writing a line, so an unpicked effort runs low.
            catalog_models, _ = await asyncio.to_thread(_intake_catalog)
            effort = _low_effort(model, catalog_models)
        usage_sink: list = []
        source = run_code_agent(
            dataset_columns=req.dataset_columns,
            column_roles=req.column_roles,
            column_kinds=req.column_kinds,
            sample_rows=req.sample_rows,
            user_message=req.user_message,
            chat_history=[t.model_dump() for t in req.chat_history],
            prior_signature=req.prior_signature,
            prior_metric=req.prior_metric,
            prior_signature_validation=req.prior_signature_validation,
            prior_metric_validation=req.prior_metric_validation,
            initial_signature=req.initial_signature,
            initial_metric=req.initial_metric,
            prior_workflow=req.prior_workflow,
            initial_workflow=req.initial_workflow,
            interview_brief=req.interview_brief,
            locale=req.locale,
            model=model,
            reasoning_effort=effort,
            usage_sink=usage_sink,
            unified=req.unified.model_dump() if req.unified else None,
            steer_owner=current_user.username,
            steer_key=req.steer_key,
            prior_files=req.prior_files,
            model_config=byok_config or cli_fast_config(model, req.fast_mode),
        )
        metered = stream_with_llm_metering(
            source,
            job_store=billed_store,
            username=current_user.username,
            description="Code authoring",
            usage_sink=usage_sink,
            token_source=metering_token_source(byok_config),
        )
        return await _turn_response(metered, current_user.username)

    @router.post(
        "/optimizations/task-interview",
        summary="Stream one task interview turn of the submission wizard",
    )
    async def task_interview(req: TaskInterviewRequest, current_user: AuthenticatedUserDep) -> StreamingResponse:
        """Stream one step-1 task interview turn as SSE.

        Event types:

        * ``reasoning_patch`` — ``{"chunk": "<token>"}`` (provider thinking)
        * ``message_patch`` — ``{"chunk": "<token>"}`` (reply stream)
        * ``message_end`` — ``{}`` (the reply is fully streamed; the options
          and the task draft are still generating)
        * ``turn_hint`` — ``{"final": bool}`` (the draft is ready to confirm)
        * ``message_reset`` — ``{}`` (a failed attempt is being retried; the
          client drops any partial reply streamed so far)
        * ``interview_done`` — a :class:`~core.models.TaskInterviewTurnResult`
          (terminal)
        * ``error`` — ``{"error": "<message>", "code": "<i18n key>"}``
        * ``kickoff_oversized`` — ``{"subject": "data", "name": ""}`` (an
          opening turn whose draft and samples are over the opening budget:
          the whole turn, with no model call)

        Framed as a resumable turn exactly like ``ai-generate-code``. Free:
        counted against the wizard agent's daily cap, never charged.

        Args:
            req: The client-owned transcript and the current task draft.
            current_user: The authenticated caller.

        Returns:
            A :class:`StreamingResponse` of Server-Sent Events.
        """
        if not req.turns and not fits_kickoff_budget(
            measured_bytes(req.objective, req.background, req.starting_version or "", req.sample_cases)
        ):
            return await _turn_response(oversized_kickoff("data"), current_user.username)
        billed_store = await asyncio.to_thread(gate_interactive_turn, job_store, current_user.username, True)
        picked = await asyncio.to_thread(engine_agent_model, job_store, current_user.username, req.model)
        byok_config = await asyncio.to_thread(
            route_local_byok_model, picked, job_store=job_store, username=current_user.username, interview=True
        )
        model = byok_config.name if byok_config is not None else await asyncio.to_thread(route_interview_model, picked)
        usage_sink: list = []

        async def source() -> AsyncIterator[dict]:
            """Relay engine events, translating failures into an error event."""
            try:
                async for event in task_interview_turn_stream(
                    turns=[t.model_dump() for t in req.turns],
                    objective=req.objective,
                    background=req.background,
                    artifact_kind=req.artifact_kind,
                    starting_version=req.starting_version,
                    sample_cases=req.sample_cases,
                    locale=req.locale,
                    model=model,
                    reasoning_effort=effective_reasoning_effort(model, req.reasoning_effort),
                    usage_sink=usage_sink,
                    model_config=byok_config or cli_fast_config(model, req.fast_mode),
                ):
                    yield event
            except Exception:
                logger.exception("task interview stream failed")
                yield {
                    "event": "error",
                    "data": {"error": "submit.v2.task.interview_failed", "code": "submit.v2.task.interview_failed"},
                }

        metered = stream_with_llm_metering(
            source(),
            job_store=billed_store,
            username=current_user.username,
            description="Task interview",
            usage_sink=usage_sink,
            token_source=metering_token_source(byok_config),
        )
        return await _turn_response(metered, current_user.username)

    @router.post(
        "/optimizations/infer-inputs",
        response_model=InputsInferenceResponse,
        summary="Infer the submission wizard's inputs from the data",
    )
    async def infer_inputs_route(
        req: InputsInferenceRequest, current_user: AuthenticatedUserDep
    ) -> InputsInferenceResponse:
        """Decide single input vs input/output pairs and pick the columns.

        One non-streamed prediction; the held-out split and its size are
        computed deterministically. Free: counted against the wizard agent's
        daily cap, never charged.

        Args:
            req: The task from step 1 and a sample of the user's data.
            current_user: The authenticated caller.

        Returns:
            The proposal the wizard's step 2 shows for confirmation.
        """
        # A free turn has no billed store, so there is no usage to meter.
        await asyncio.to_thread(gate_interactive_turn, job_store, current_user.username, True)
        picked = await asyncio.to_thread(engine_agent_model, job_store, current_user.username, req.model)
        byok_config = await asyncio.to_thread(
            route_local_byok_model, picked, job_store=job_store, username=current_user.username, interview=True
        )
        model = byok_config.name if byok_config is not None else await asyncio.to_thread(route_interview_model, picked)
        return await asyncio.to_thread(infer_inputs, req.model_copy(update={"model": model}), model_config=byok_config)

    @router.post(
        "/account/intake-interview",
        summary="Stream one onboarding intake interview turn",
    )
    async def intake_interview(req: IntakeInterviewRequest, current_user: AuthenticatedUserDep) -> StreamingResponse:
        """Stream one turn of an intake phase as SSE.

        Same events as ``/optimizations/task-interview`` (``reasoning_patch``,
        ``message_patch``, ``message_end``, ``turn_hint`` with ``final`` =
        phase done, ``message_reset``, ``error``), ending with
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

    @router.post(
        "/optimizations/edit-code",
        response_model=EditCodeResponse,
        summary="Delegate signature + metric editing to the code agent",
        tags=["agent"],
    )
    async def edit_code(req: EditCodeRequest, current_user: AuthenticatedUserDep) -> EditCodeResponse:
        """Run the code agent to completion and return the final code.

        Consumes the same event stream as the SSE endpoint but blocks until
        the ``done`` event, so a ReAct tool call sees a single request /
        response. Streaming updates remain available to human-driven UIs via
        ``POST /optimizations/ai-generate-code``.

        Args:
            req: Compact MCP-friendly input: goal plus current editor contents.
            current_user: The authenticated caller (required; gates LLM spend).

        Returns:
            An :class:`EditCodeResponse` with the final signature, metric, and
            optional assistant message.

        Raises:
            DomainError: 502 when the code agent emits an ``error`` event; 422
                ``models.managed_unavailable_local`` in local mode when the
                caller has no verified OpenRouter key and no subscription
                engine.
        """
        await asyncio.to_thread(enforce_llm_balance, job_store, current_user.username)
        picked = await asyncio.to_thread(engine_agent_model, job_store, current_user.username, None)
        byok_config = await asyncio.to_thread(
            route_local_byok_model, picked, job_store=job_store, username=current_user.username
        )
        # Hosted keeps the code agent's own default (settings.code_agent_model);
        # only the Mac app, which has no managed key, routes the turn here.
        model = None
        if byok_config is not None:
            model = byok_config.name
        elif settings.local_mode:
            model = route_menu_model(picked)
        final_signature = req.current_signature
        final_metric = req.current_metric
        assistant_message = ""

        usage_sink: list = []
        source = run_code_agent(
            dataset_columns=req.dataset_columns,
            column_roles=req.column_roles,
            column_kinds=req.column_kinds,
            sample_rows=req.sample_rows,
            user_message=req.goal,
            chat_history=[],
            prior_signature=req.current_signature,
            prior_metric=req.current_metric,
            model=model,
            usage_sink=usage_sink,
            model_config=byok_config,
        )
        async for event in stream_with_llm_metering(
            source,
            job_store=job_store,
            username=current_user.username,
            description="Code authoring",
            usage_sink=usage_sink,
            token_source=metering_token_source(byok_config),
        ):
            name = event["event"]
            data = event["data"]
            if name == "done":
                final_signature = data.get("signature_code", final_signature)
                final_metric = data.get("metric_code", final_metric)
                assistant_message = data.get("assistant_message", "")
            elif name == "error":
                raise DomainError(
                    "code_agent.upstream_failed",
                    status=502,
                    error=str(data.get("error", "code agent failed")),
                )

        return EditCodeResponse(
            signature_code=final_signature,
            metric_code=final_metric,
            assistant_message=assistant_message,
        )

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
