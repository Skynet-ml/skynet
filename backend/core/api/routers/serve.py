"""Routes that serve the best version of a finished repository run.

Public dev surface (in ``_SCALAR_PUBLIC_PATHS``):
- ``POST /serve/{id}`` — run one input through the repository's ``serve.py``.
- ``GET /serve/{id}/info`` — describe the served repository run.

Skynet optimizes repositories only. The DSPy program routes that the in-app
agents still list as tools return 410 for every call.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterable, AsyncIterator
from functools import partial
from types import TracebackType
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field

from ...constants import PAYLOAD_OVERVIEW_COLUMN_MAPPING
from ...i18n_keys import I18nKey
from ...models import OptimizationStatus, ServeInfoResponse, ServeRequest, ServeResponse
from ...models.serve import ServingEnabledRequest, ServingEnabledResponse
from ...service_gateway.optimization.blackbox.repo_serving import INTERACTION_KIND as REPO_SERVE_KIND
from ...service_gateway.optimization.blackbox.repo_serving import SERVE_FILE, serve_payload, staged_snapshot
from ...telemetry.server_events import record_server_event
from ...worker.repo_staging import improved_patch, is_repo_payload
from ..auth import AuthenticatedUser, get_authenticated_user
from ..converters import job_owner, parse_overview, status_to_job_status
from ..errors import DomainError
from ..protected_interaction import run_protected_interaction
from ..sharing_access import ShareRole
from ._helpers import load_job_for_user, require_role_at_least

logger = logging.getLogger(__name__)

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]

SERVE_INFERENCE_EVENT = "serve_inference"

ServeChannel = Literal["api_token", "share_link", "ui"]
ServeRoute = Literal["serve", "serve_stream", "pair", "pair_stream", "chat"]


def serve_channel(request: Request) -> ServeChannel:
    """Classify how a serve call was authenticated.

    The channel comes only from the auth path (``get_authenticated_user``
    stamps PAT requests), never from the request body, so a caller cannot pass
    a playground click off as a production integration.

    Args:
        request: Incoming request after authentication ran.

    Returns:
        ``"api_token"`` for a personal-access-token call, else ``"ui"``.
    """
    channel = getattr(getattr(request, "state", None), "auth_channel", None)
    return "api_token" if channel == "api_token" else "ui"


class ServeInferenceEvent:
    """Record exactly one ``serve_inference`` telemetry row for one serve call.

    Used as a sync or async context manager around a serve handler: an
    exception records ``error``, a normal exit records ``ok``. A streaming
    handler hands its event source to :meth:`stream` instead, which defers the
    row until the stream ends so the status reflects how the stream finished.
    """

    def __init__(
        self,
        job_store: Any,
        *,
        username: str,
        optimization_id: str,
        channel: ServeChannel,
        route: ServeRoute,
        pair_index: int | None = None,
    ) -> None:
        """Capture the structural, PII-free facts of one serve call.

        Args:
            job_store: Store whose engine backs ``telemetry_events``.
            username: Server-trusted caller identity.
            optimization_id: Served job id; the metric joins it to the owner.
            channel: How the caller authenticated.
            route: Which serve endpoint handled the call.
            pair_index: Grid pair index for pair routes.
        """
        self._engine = getattr(job_store, "engine", None)
        self._username = username
        self._properties: dict[str, Any] = {
            "optimization_id": optimization_id,
            "channel": channel,
            "route": route,
        }
        if pair_index is not None:
            self._properties["pair_index"] = pair_index
        self._deferred = False
        self._recorded = False

    def record(self, status: Literal["ok", "error"]) -> None:
        """Write the row once; later calls and telemetry failures are no-ops.

        Args:
            status: ``"ok"`` or ``"error"``.
        """
        if self._recorded:
            return
        self._recorded = True
        try:
            record_server_event(
                self._engine,
                username=self._username,
                name=SERVE_INFERENCE_EVENT,
                properties={**self._properties, "status": status},
            )
        except Exception:
            logger.warning("telemetry: serve_inference not recorded", exc_info=True)

    def stream(self, source: AsyncIterable[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
        """Defer the row to the end of a streamed response.

        Args:
            source: The handler's ``{event, data}`` stream.

        Returns:
            The same events, recording the row when the stream ends.
        """
        self._deferred = True
        return self._recorded_stream(source)

    async def _recorded_stream(self, source: AsyncIterable[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
        """Mirror ``source`` and record its outcome on teardown.

        A stream that the client drops before it completes counts as an error:
        the caller never received a result.

        Args:
            source: Upstream event stream.

        Yields:
            The upstream events unchanged.
        """
        status: Literal["ok", "error"] = "error"
        failed = False
        try:
            async for event in source:
                if event.get("event") == "error":
                    failed = True
                yield event
            status = "error" if failed else "ok"
        finally:
            await asyncio.to_thread(self.record, status)

    def __enter__(self) -> ServeInferenceEvent:
        """Enter the tracked handler body.

        Returns:
            This event, so a streaming handler can call :meth:`stream`.
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Record the handler's outcome unless a stream took it over.

        Args:
            exc_type: Exception type raised by the handler, if any.
            exc: Exception raised by the handler, if any.
            traceback: Traceback of that exception.
        """
        if exc_type is not None:
            self.record("error")
        elif not self._deferred:
            self.record("ok")

    async def __aenter__(self) -> ServeInferenceEvent:
        """Enter the tracked async handler body.

        Returns:
            This event, so a streaming handler can call :meth:`stream`.
        """
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Record the outcome off the event loop, since the write is blocking.

        Args:
            exc_type: Exception type raised by the handler, if any.
            exc: Exception raised by the handler, if any.
            traceback: Traceback of that exception.
        """
        await asyncio.to_thread(self.__exit__, exc_type, exc, traceback)


class RequestUserInferenceRequest(BaseModel):
    """Request body for ``POST /serve/{id}/request-form``."""

    prompt: str = Field(
        default="",
        max_length=400,
        description="Short sentence, in the user's reply language, explaining why inference is being offered.",
    )


class RequestUserInferenceResponse(BaseModel):
    """Envelope for ``POST /serve/{id}/request-form`` — UI-trigger marker."""

    optimization_id: str
    awaiting_inputs: bool
    prompt: str


class RequestUserPairInferenceResponse(BaseModel):
    """Envelope for ``POST /serve/{id}/pair/{index}/request-form`` — UI-trigger marker."""

    optimization_id: str
    pair_index: int
    awaiting_inputs: bool
    prompt: str


def _interaction_authority(max_cost_cents: int | None, idempotency_key: str | None) -> tuple[int | None, str]:
    """Require a replay key; a one-request maximum stays optional.

    Args:
        max_cost_cents: Caller-selected maximum in cents, or ``None`` to draw on the account.
        idempotency_key: Transport replay identity.

    Returns:
        Optional maximum and trimmed idempotency key.

    Raises:
        DomainError: When the replay key is missing.
    """
    key = (idempotency_key or "").strip()
    if not key:
        raise DomainError("budget.idempotency_required", status=400)
    return max_cost_cents, key


def _repo_run(job_data: dict[str, Any]) -> dict[str, Any] | None:
    """Return the repository target of a finished, serveable repository run.

    Args:
        job_data: Stored job row the caller may read.

    Returns:
        The ``target.repo`` section, or ``None`` when the run is not a repository run.

    Raises:
        DomainError: 409 when the run did not succeed or has no pinned commit.
    """
    payload = job_data.get("payload")
    if not isinstance(payload, dict) or not is_repo_payload(payload):
        return None
    status = status_to_job_status(job_data.get("status", "pending"))
    if status != OptimizationStatus.success:
        raise DomainError(
            "optimization.not_success_status_for_serve",
            status=409,
            params={"status": status.value},
        )
    repo = payload["target"]["repo"]
    if not isinstance(repo.get("commit"), str) or not repo["commit"]:
        raise DomainError("optimization.no_result_pending", status=409)
    return repo


def _repo_serve_call(
    *,
    job_store: Any,
    job_data: dict[str, Any],
    optimization_id: str,
    req: ServeRequest,
    current_user: AuthenticatedUser,
    idempotency_key: str | None,
) -> dict[str, Any]:
    """Serve one input through the best version of a repository run.

    The guest applies the best patch to the pinned commit and calls the root
    ``serve.py``'s ``serve(input)`` with ``req.inputs``. A run with no patch
    that beats the baseline serves its starting code.

    Args:
        job_store: Store backing the run and the budget ledger.
        job_data: Stored repository run.
        optimization_id: The run being served.
        req: Caller inputs and optional cost maximum.
        current_user: Authenticated caller who funds this call.
        idempotency_key: Transport replay identity.

    Returns:
        ``ServeResponse``-shaped result with the settled charge.

    Raises:
        DomainError: 400 without a replay key, 502 when the sandbox call fails.
    """
    max_cost_cents, key = _interaction_authority(req.max_cost_cents, idempotency_key)
    stored = job_data["payload"]
    result_data = job_data.get("result")
    owner = job_owner(job_data)
    result = run_protected_interaction(
        serve_payload(
            job_data,
            patch=improved_patch(result_data if isinstance(result_data, dict) else {}) or "",
            inputs=req.inputs,
            optimization_id=optimization_id,
        ),
        kind=REPO_SERVE_KIND,
        max_cost_cents=max_cost_cents,
        idempotency_key=key,
        user=current_user,
        job_store=job_store,
        credential_owner=owner,
        credential_binding_id=job_data.get("execution_budget_id") or stored.get("execution_budget_id"),
        stage_payload=partial(staged_snapshot, engine=getattr(job_store, "engine", None), owner=owner),
    )
    if result.get("error"):
        raise DomainError("serve.protected_interaction_failed", status=502, error=result["error"])
    return result


def _artifact_prompt_fields(artifact: Any) -> tuple[list[str], list[str], str | None, int]:
    """Read prompt metadata from a ``ProgramArtifact`` with ``None``-safe fallbacks.

    ``artifact.optimized_prompt`` may be unset for legacy results, so this
    helper centralises the empty-default behaviour the routes rely on.

    Args:
        artifact: A ``ProgramArtifact`` (or grid-pair equivalent).

    Returns:
        ``(input_fields, output_fields, instructions, demo_count)``.
    """
    prompt = artifact.get("optimized_prompt") if isinstance(artifact, dict) else artifact.optimized_prompt
    if prompt is None:
        return [], [], None, 0
    if isinstance(prompt, dict):
        raw_inputs = prompt.get("input_fields")
        raw_outputs = prompt.get("output_fields")
        raw_instructions = prompt.get("instructions")
        raw_demos = prompt.get("demos")
        return (
            [value for value in raw_inputs if isinstance(value, str)] if isinstance(raw_inputs, list) else [],
            [value for value in raw_outputs if isinstance(value, str)] if isinstance(raw_outputs, list) else [],
            raw_instructions if isinstance(raw_instructions, str) else None,
            len(raw_demos) if isinstance(raw_demos, list) else 0,
        )
    return (
        list(prompt.input_fields),
        list(prompt.output_fields),
        prompt.instructions,
        len(prompt.demos),
    )


def _column_mapping_fields(overview: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Read signature field names from the submission's persisted column mapping.

    Flex programs run optimizer-authored code in an interpreter and expose no
    named predictor, so their artifact carries no ``optimized_prompt``. The
    column mapping still names every signature input and output field.

    Args:
        overview: Persisted payload overview.

    Returns:
        ``(input_fields, output_fields)``, both empty when no mapping exists.
    """
    mapping = overview.get(PAYLOAD_OVERVIEW_COLUMN_MAPPING)
    if not isinstance(mapping, dict):
        return [], []

    def _names(kind: str) -> list[str]:
        """Return the signature field names mapped under ``kind``.

        Args:
            kind: Mapping side, either ``inputs`` or ``outputs``.

        Returns:
            Field names in their persisted order.
        """
        raw = mapping.get(kind)
        return [name for name in raw if isinstance(name, str)] if isinstance(raw, dict) else []

    return _names("inputs"), _names("outputs")


def serving_enabled(job_data: dict[str, Any]) -> bool:
    """Tell whether the owner turned serving on for a run.

    Args:
        job_data: Stored job row.

    Returns:
        ``True`` only when the run's overview stores ``serving_enabled`` as true.
    """
    return parse_overview(job_data).get("serving_enabled") is True


def require_serving_enabled(job_data: dict[str, Any]) -> None:
    """Refuse a serve call before any billing or sandbox work when serving is off.

    Args:
        job_data: Stored job row of a serveable repository run.

    Raises:
        DomainError: 409 ``serve.not_enabled`` when the owner did not turn serving on.
    """
    if not serving_enabled(job_data):
        raise DomainError("serve.not_enabled", status=409)


def _dspy_serve_retired() -> DomainError:
    """Build the refusal for a DSPy program serve call.

    Returns:
        A 410 error that says Skynet now serves repository runs only.
    """
    return DomainError(I18nKey.OPTIMIZATION_DSPY_RUN_RETIRED, status=410)


def create_serve_router(*, job_store) -> APIRouter:
    """Build the serve router.

    Args:
        job_store: Job-store instance backing the load/inference helpers.

    Returns:
        A FastAPI ``APIRouter`` exposing the repository serve routes.
    """
    router = APIRouter()

    # The DSPy-only routes below stay registered because the in-app agents list
    # them as tools (``test_generalist_tool_coverage``); they refuse with 410.

    @router.get(
        "/serve/{optimization_id}/info",
        response_model=ServeInfoResponse,
        summary="Describe an optimized program without running it",
        tags=["agent"],
    )
    def serve_info(optimization_id: str, current_user: AuthenticatedUserDep) -> ServeInfoResponse:
        """Describe a finished repository run without running it.

        Metadata-only: no sandbox or LLM calls.

        Args:
            optimization_id: Optimization id whose program should be described.
            current_user: Authenticated caller resolved from the bearer token.

        Returns:
            A ``ServeInfoResponse`` that names the served file, engine and scorer model.

        Raises:
            DomainError: 404 if unknown or inaccessible, 409 if the run is not
                in a serveable state, 410 for a DSPy program run.
        """
        job_data = load_job_for_user(job_store, optimization_id, current_user)
        if _repo_run(job_data) is None:
            raise _dspy_serve_retired()
        stored = job_data["payload"]
        scorer = stored.get("scorer")
        model = scorer.get("model") if isinstance(scorer, dict) else None
        return ServeInfoResponse(
            optimization_id=optimization_id,
            module_name=SERVE_FILE,
            optimizer_name=str((stored.get("strategy") or {}).get("engine") or ""),
            model_name=str(model.get("name") or "") if isinstance(model, dict) else "",
            input_fields=[],
            output_fields=[],
            serving_enabled=serving_enabled(job_data),
        )

    @router.put(
        "/serve/{optimization_id}/enabled",
        response_model=ServingEnabledResponse,
        summary="Turn serving on or off for a finished repository run",
    )
    def set_serving_enabled(
        optimization_id: str,
        req: ServingEnabledRequest,
        current_user: AuthenticatedUserDep,
    ) -> ServingEnabledResponse:
        """Store the owner's serving switch for a finished repository run.

        Setting the same value again is a no-op that returns the same reply.

        Args:
            optimization_id: Optimization id whose switch changes.
            req: Body with the wanted switch state.
            current_user: Authenticated caller resolved from the bearer token.

        Returns:
            The stored switch state.

        Raises:
            DomainError: 404 if unknown or inaccessible, 403 for a caller below
                owner, 409 if the run did not succeed, 410 for a DSPy program run.
        """
        job_data, _role = require_role_at_least(job_store, optimization_id, current_user, ShareRole.owner)
        if _repo_run(job_data) is None:
            raise _dspy_serve_retired()
        overview = parse_overview(job_data)
        if overview.get("serving_enabled") is not req.enabled:
            overview["serving_enabled"] = req.enabled
            job_store.set_payload_overview(optimization_id, overview)
        return ServingEnabledResponse(optimization_id=optimization_id, serving_enabled=req.enabled)

    @router.post(
        "/serve/{optimization_id}/request-form",
        response_model=RequestUserInferenceResponse,
        operation_id="request_user_inference",
        summary="Ask the user to fill an inference form; the chat panel renders an input card",
        tags=["agent"],
    )
    def request_user_inference(
        optimization_id: str,
        req: RequestUserInferenceRequest,
        current_user: AuthenticatedUserDep,
    ) -> RequestUserInferenceResponse:
        """Signal the chat UI to render an inline inference-input card.

        Stateless: the endpoint exists only so the agent can call a named
        tool that the frontend recognizes via its ``tool_start`` SSE event.
        The card itself hits ``/serve/{id}/info`` for the schema and
        ``/serve/{id}`` for the actual call.

        Args:
            optimization_id: Optimization id whose form should be rendered.
            req: Optional prompt describing why inference is being offered.
            current_user: Authenticated caller resolved from the bearer token.

        Returns:
            A :class:`RequestUserInferenceResponse` carrying the prompt back
            so the card can display it.

        Raises:
            DomainError: 404 if unknown or inaccessible, 409 if the run is not
                in a serveable state or serving is off, 410 for a DSPy program run.
        """
        job_data = load_job_for_user(job_store, optimization_id, current_user)
        if _repo_run(job_data) is None:
            raise _dspy_serve_retired()
        require_serving_enabled(job_data)
        return RequestUserInferenceResponse(
            optimization_id=optimization_id,
            awaiting_inputs=True,
            prompt=req.prompt.strip(),
        )

    @router.post(
        "/serve/{optimization_id}/pair/{pair_index}/request-form",
        response_model=RequestUserPairInferenceResponse,
        operation_id="request_user_pair_inference",
        summary="Ask the user to run inference through one grid-search pair; the chat renders an input card",
        tags=["agent"],
    )
    def request_user_pair_inference(
        optimization_id: str,
        pair_index: int,
        req: RequestUserInferenceRequest,
        current_user: AuthenticatedUserDep,
    ) -> RequestUserPairInferenceResponse:
        """Refuse a grid-search pair inference card; grid pairs are DSPy programs.

        Args:
            optimization_id: Grid-search optimization id.
            pair_index: Index of the pair to serve.
            req: Optional prompt describing why inference is being offered.
            current_user: Authenticated caller resolved from the bearer token.

        Raises:
            DomainError: 404 if unknown or inaccessible, else 410.
        """
        load_job_for_user(job_store, optimization_id, current_user)
        raise _dspy_serve_retired()

    @router.post(
        "/serve/{optimization_id}",
        response_model=ServeResponse,
        summary="Run a single inference through an optimized program",
        tags=["agent"],
    )
    def serve_program(
        optimization_id: str,
        req: ServeRequest,
        request: Request,
        current_user: AuthenticatedUserDep,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ServeResponse:
        """Run one input through the best version of a repository run.

        Args:
            optimization_id: Optimization id whose program should run.
            req: Inference request carrying inputs.
            request: Incoming request; its auth channel tags the telemetry row.
            current_user: Authenticated caller resolved from the bearer token.
            idempotency_key: Transport replay identity.

        Returns:
            A ``ServeResponse`` with the outputs and the settled charge.

        Raises:
            DomainError: 400 without a replay key, 404 if unknown or
                inaccessible, 409 if not serveable or serving is off, 410 for a
                DSPy program run, 502 when the sandbox call fails.
        """
        with ServeInferenceEvent(
            job_store,
            username=current_user.username,
            optimization_id=optimization_id,
            channel=serve_channel(request),
            route="serve",
        ):
            job_data = load_job_for_user(job_store, optimization_id, current_user)
            if _repo_run(job_data) is None:
                raise _dspy_serve_retired()
            require_serving_enabled(job_data)
            return ServeResponse.model_validate(
                _repo_serve_call(
                    job_store=job_store,
                    job_data=job_data,
                    optimization_id=optimization_id,
                    req=req,
                    current_user=current_user,
                    idempotency_key=idempotency_key,
                )
            )

    @router.get(
        "/serve/{optimization_id}/pair/{pair_index}/info",
        response_model=ServeInfoResponse,
        summary="Describe the program for one grid-search pair",
        tags=["agent"],
    )
    def serve_pair_info(optimization_id: str, pair_index: int, current_user: AuthenticatedUserDep) -> ServeInfoResponse:
        """Refuse to describe a grid-search pair; grid pairs are DSPy programs.

        Args:
            optimization_id: Grid-search optimization id.
            pair_index: Index of the pair in the grid-search result.
            current_user: Authenticated caller resolved from the bearer token.

        Raises:
            DomainError: 404 if unknown or inaccessible, else 410.
        """
        load_job_for_user(job_store, optimization_id, current_user)
        raise _dspy_serve_retired()

    return router
