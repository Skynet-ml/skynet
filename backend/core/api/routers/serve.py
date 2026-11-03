"""Routes for inference on optimized programs (single runs and grid-search pairs). [MIXED]

Public dev surface (in ``_SCALAR_PUBLIC_PATHS``):
- ``POST /serve/{id}`` — invoke the trained program.
- ``GET /serve/{id}/info`` — input/output schema for the program.

Internal (frontend-only, hidden from public docs):
- ``POST /serve/{id}/stream`` and per-pair variants under
  ``/serve/{id}/pair/{idx}/...`` — too granular for dev integration.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterable, AsyncIterator, Callable
from types import TracebackType
from typing import Annotated, Any, Literal

import dspy
from dspy.streaming import StreamListener, StreamResponse
from fastapi import APIRouter, Depends, Header, Request
from pydantic import AliasChoices, BaseModel, Field, model_validator
from starlette.responses import StreamingResponse

from ...billing import ProviderKeyVault, resolve_byok_model_config
from ...billing.budget_amounts import MAX_CENTS
from ...billing.metering import meter_llm_run
from ...config import settings
from ...constants import (
    PAYLOAD_OVERVIEW_COLUMN_MAPPING,
    PAYLOAD_OVERVIEW_GENERATION_MODELS,
    PAYLOAD_OVERVIEW_MODEL_NAME,
    PAYLOAD_OVERVIEW_MODEL_SETTINGS,
    PAYLOAD_OVERVIEW_MODULE_NAME,
    PAYLOAD_OVERVIEW_OPTIMIZER_NAME,
    PAYLOAD_OVERVIEW_SIGNATURE_CODE,
    PAYLOAD_OVERVIEW_TOOL_SOURCE,
    PAYLOAD_OVERVIEW_WORKFLOW,
    TOKEN_SOURCE_BYOK,
    TOKEN_SOURCE_MANAGED,
)
from ...models import ModelConfig, ServeInfoResponse, ServeRequest, ServeResponse
from ...models.serve import reject_model_override
from ...service_gateway.agents.generalist import TrustMode, get_approval_registry
from ...service_gateway.agents.react_serve import run_react_chat
from ...service_gateway.agents.steering import STEER_KEY_MAX_CHARS
from ...service_gateway.language_models import build_language_model
from ...service_gateway.optimization.workflow import capture_node_traces
from ...telemetry.server_events import record_server_event
from ..auth import AuthenticatedUser, get_authenticated_user
from ..converters import job_owner
from ..errors import DomainError
from ..protected_interaction import run_protected_interaction
from ..response_limits import AGENT_MAX_INSTRUCTIONS, AGENT_MAX_TEXT, truncate_text
from ..sharing_access import ShareRole
from ._helpers import (
    _protected_api_runtime,
    enforce_llm_balance,
    load_job_for_user,
    load_pair_program,
    load_pair_program_metadata,
    load_program,
    load_program_metadata,
    load_react_chat_inputs,
    require_role_at_least,
    sanitize_node_traces,
    sse_from_events,
    stream_with_llm_metering,
    workflow_spec_from_overview,
)

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


def _resolve_inference_model_config(job_store, username: str, model_config: ModelConfig) -> ModelConfig:
    """Resolve a caller's stored BYOK connection for interactive inference.

    Args:
        job_store: Store whose engine backs provider connections.
        username: Authenticated caller who pays for the inference.
        model_config: Requested or stored model config.

    Returns:
        Managed config unchanged, or a BYOK copy carrying runtime credentials.

    Raises:
        DomainError: 400 when the caller lacks a verified matching connection.
    """
    if model_config.token_source != TOKEN_SOURCE_BYOK:
        return model_config
    engine = getattr(job_store, "engine", None)
    if engine is None:
        raise DomainError("billing.byok_missing_connection", status=400, provider="")
    try:
        return resolve_byok_model_config(
            model_config,
            username=username,
            vault=ProviderKeyVault(engine=engine),
        )
    except ValueError as exc:
        raise DomainError("billing.byok_missing_connection", status=400, provider=str(exc)) from exc


def _pair_model_config(model_name: str, overview: dict[str, Any]) -> ModelConfig:
    """Resolve a grid pair's persisted model config.

    Args:
        model_name: Pair generation-model identifier.
        overview: Parent grid payload overview.

    Returns:
        The matching persisted config, or a legacy name-only config.
    """
    for raw_config in overview.get(PAYLOAD_OVERVIEW_GENERATION_MODELS, []):
        if not isinstance(raw_config, dict):
            continue
        config = ModelConfig.model_validate(raw_config)
        if config.normalized_identifier() == model_name.strip("/"):
            return config
    return ModelConfig(name=model_name)


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


class ServeChatTurn(BaseModel):
    """One prior chat turn carried by the react-serve chat request."""

    role: str = Field(default="user")
    content: str = Field(default="", max_length=AGENT_MAX_TEXT)


class ServeChatRequest(BaseModel):
    """Request body for ``POST /serve/{id}/chat`` (live ReAct chat turn)."""

    user_message: str = Field(..., max_length=AGENT_MAX_TEXT)
    steer_key: str | None = Field(
        default=None,
        max_length=STEER_KEY_MAX_CHARS,
        description=(
            "Client-chosen key for this turn. Messages posted to "
            "``/optimizations/agent-steer`` under it reach the non-protected agent "
            "at its next step, announced by a ``steer_applied`` event. "
            "Protected turns ignore this key and never announce ``steer_ready``."
        ),
    )
    chat_history: list[ServeChatTurn] = Field(default_factory=list)
    trust_mode: TrustMode = Field(
        default="ask",
        description="'ask'/'auto_safe' confirm every tool, 'yolo' confirms none.",
    )
    max_cost_cents: int | None = Field(
        validation_alias=AliasChoices("max_cost_cents", "max_cost_credits"),
        default=None,
        ge=1,
        le=MAX_CENTS,
        strict=True,
        description="Optional cap in cents for this one chat turn; omitted, the turn draws on the account balance.",
    )

    @model_validator(mode="before")
    @classmethod
    def _locked_model(cls, data: Any) -> Any:
        """Refuse a caller-chosen model; the program runs on the one it was optimized with.

        Args:
            data: Raw request body.

        Returns:
            The unchanged body.
        """
        return reject_model_override(data)


class ServeChatConfirmRequest(BaseModel):
    """Confirm payload for resolving a pending react-serve tool approval."""

    call_id: str
    approved: bool


class ServeChatConfirmResponse(BaseModel):
    """Ack for a react-serve approval confirm call."""

    resolved: bool


def _cap_serve_outputs(outputs: dict[str, Any]) -> dict[str, Any]:
    """Truncate string fields in a serve-response ``outputs`` dict.

    LLM predictions can run to many KB; echoing the raw output into the
    agent's context blows the window after one or two calls. Non-string
    values pass through unchanged.

    Args:
        outputs: Mapping of output-field name to predicted value.

    Returns:
        A new dict with long string values truncated.
    """
    return {
        key: truncate_text(value, AGENT_MAX_TEXT) if isinstance(value, str) else value for key, value in outputs.items()
    }


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


def _protected_payload(
    job_data: dict[str, Any],
    artifact: Any,
    overview: dict[str, Any],
    model_config: ModelConfig,
    inputs: dict[str, Any],
    interaction: dict[str, Any],
) -> dict[str, Any]:
    """Build the smallest secret-free guest payload for one interaction.

    Args:
        job_data: Persisted protected optimization row.
        artifact: Selected optimized program artifact.
        overview: Scrubbed program reconstruction metadata.
        model_config: Caller-selected task model without resolved credentials.
        inputs: Validated program inputs.
        interaction: Guest operation descriptor.

    Returns:
        Payload ready for trusted credential resolution and sandbox routing.
    """
    stored = job_data.get("payload") if isinstance(job_data.get("payload"), dict) else {}
    effective_overview = dict(overview)
    effective_overview[PAYLOAD_OVERVIEW_SIGNATURE_CODE] = effective_overview.get(
        PAYLOAD_OVERVIEW_SIGNATURE_CODE
    ) or stored.get("signature_code")
    tool_source = stored.get("tool_source") or effective_overview.get(PAYLOAD_OVERVIEW_TOOL_SOURCE)
    payload = {
        "model_config": model_config.model_dump(mode="json"),
        "token_source": model_config.token_source or TOKEN_SOURCE_MANAGED,
        "program_artifact": (artifact.model_dump(mode="json") if hasattr(artifact, "model_dump") else artifact),
        "payload_overview": effective_overview,
        "inputs": inputs,
        "_interaction": interaction,
    }
    if tool_source is not None:
        payload["tool_source"] = tool_source
    return payload


def _protected_result(
    *,
    job_store: Any,
    job_data: dict[str, Any],
    artifact: Any,
    overview: dict[str, Any],
    model_config: ModelConfig,
    inputs: dict[str, Any],
    interaction: dict[str, Any],
    max_cost_cents: int | None,
    idempotency_key: str,
    current_user: AuthenticatedUser,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    require_tool_approval: bool = False,
) -> dict[str, Any]:
    """Execute one protected program call under caller-owned authority.

    Args:
        job_store: Store backing the program and budget ledger.
        job_data: Persisted protected optimization row.
        artifact: Selected program artifact.
        overview: Program reconstruction metadata.
        model_config: Requested task model.
        inputs: Validated program inputs.
        interaction: Guest operation descriptor.
        max_cost_cents: Optional request maximum accepted by the caller.
        idempotency_key: Stable request replay identity.
        current_user: Authenticated caller who funds this interaction.
        on_event: Optional live event receiver.
        require_tool_approval: Whether live tools require an explicit decision.

    Returns:
        Completed isolated interaction response.

    Raises:
        DomainError: When the isolated interaction fails.
    """
    stored = job_data.get("payload") if isinstance(job_data.get("payload"), dict) else {}
    result = run_protected_interaction(
        _protected_payload(job_data, artifact, overview, model_config, inputs, interaction),
        kind=str(interaction["kind"]),
        max_cost_cents=max_cost_cents,
        idempotency_key=idempotency_key,
        user=current_user,
        job_store=job_store,
        credential_owner=job_owner(job_data),
        credential_binding_id=job_data.get("execution_budget_id") or stored.get("execution_budget_id"),
        on_event=on_event,
        require_tool_approval=require_tool_approval,
    )
    if result.get("error"):
        raise DomainError("serve.protected_interaction_failed", status=502, error=result["error"])
    return result


def _protected_program_call(
    *,
    job_store: Any,
    job_data: dict[str, Any],
    optimization_id: str,
    req: ServeRequest,
    current_user: AuthenticatedUser,
    idempotency_key: str | None,
    pair_index: int | None = None,
    stream: bool = False,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Validate and execute one protected single-program or pair invocation.

    Args:
        job_store: Store backing artifacts and billing.
        job_data: Persisted protected optimization row.
        optimization_id: Program identity.
        req: Caller inputs and optional one-request maximum.
        current_user: Authenticated spending owner.
        idempotency_key: Transport replay identity.
        pair_index: Optional grid pair selection.
        stream: Whether guest token events should be emitted.
        on_event: Optional live event receiver.

    Returns:
        Completed isolated serve response.
    """
    maximum, key = _interaction_authority(req.max_cost_cents, idempotency_key)
    if pair_index is None:
        artifact, overview, model_name = load_program_metadata(job_store, optimization_id, current_user)
        model_settings = overview.get(PAYLOAD_OVERVIEW_MODEL_SETTINGS, {})
        if model_settings:
            model_config = ModelConfig.model_validate(model_settings)
        elif model_name:
            model_config = ModelConfig(name=model_name)
        else:
            raise DomainError("serve.no_model_config", status=400)
    else:
        artifact, pair, overview = load_pair_program_metadata(job_store, optimization_id, pair_index, current_user)
        pair_model = pair.get("generation_model", "") if isinstance(pair, dict) else pair.generation_model
        model_config = _pair_model_config(pair_model, overview)
    input_fields, output_fields, _instructions, _demo_count = _artifact_prompt_fields(artifact)
    workflow = workflow_spec_from_overview(overview)
    if workflow is not None:
        input_fields = workflow.input_field_names()
        output_fields = workflow.output_field_names()
    if not input_fields:
        input_fields, output_fields = _column_mapping_fields(overview)
    if not input_fields:
        raise DomainError("serve.no_declared_inputs", status=400)
    missing = [field for field in input_fields if field not in req.inputs]
    if missing:
        raise DomainError(
            "serve.missing_inputs",
            status=400,
            missing=missing,
            input_fields=input_fields,
        )
    inputs = {field: req.inputs[field] for field in input_fields}
    return _protected_result(
        job_store=job_store,
        job_data=job_data,
        artifact=artifact,
        overview=overview,
        model_config=model_config,
        inputs=inputs,
        interaction={
            "kind": "serve",
            "optimization_id": optimization_id,
            "input_fields": input_fields,
            "output_fields": output_fields,
            "stream": stream,
        },
        max_cost_cents=maximum,
        idempotency_key=key,
        current_user=current_user,
        on_event=on_event,
    )


async def _protected_sse(run: Callable[[Callable[[dict[str, Any]], None]], dict[str, Any]], *, final_event: str):
    """Bridge blocking sandbox execution and tool approvals into an SSE source.

    Args:
        run: Blocking protected interaction accepting a thread-safe event sink.
        final_event: Terminal event name expected by the frontend.

    Yields:
        Live guest events followed by the reconciled terminal result.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def emit(event: dict[str, Any]) -> None:
        """Move one parent or guest event onto the request loop.

        Args:
            event: SSE-shaped interaction event.
        """
        loop.call_soon_threadsafe(queue.put_nowait, event)

    task = asyncio.create_task(asyncio.to_thread(run, emit))
    try:
        while not task.done() or not queue.empty():
            getter = asyncio.create_task(queue.get())
            done, _pending = await asyncio.wait({task, getter}, return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                yield getter.result()
            else:
                getter.cancel()
            if task in done and queue.empty():
                break
        result = await task
        yield {"event": final_event, "data": result}
    except asyncio.CancelledError:
        raise
    except DomainError as error:
        # The stream already answered 200, so send the same code and params as the
        # HTTP error envelope for the client to translate.
        if error.status_code >= 500:
            # The 200 stream hides a server failure from access logs, so keep the trace here.
            logger.exception("Protected interaction stream failed")
        yield {"event": "error", "data": {"error": error.detail, "code": error.code, "params": error.params}}
    except Exception as error:
        logger.exception("Protected interaction stream failed")
        yield {"event": "error", "data": {"error": str(error)}}


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


def _workflow_anchor_fields(overview: dict[str, Any]) -> tuple[list[str], list[str]] | None:
    """Read workflow anchor fields without parsing any authored node code.

    Args:
        overview: Persisted payload overview containing an optional workflow.

    Returns:
        ``(input_fields, output_fields)`` for a workflow, or ``None`` when the
        optimization is not a workflow.
    """
    workflow = overview.get(PAYLOAD_OVERVIEW_WORKFLOW)
    if not isinstance(workflow, dict):
        return None
    raw_nodes = workflow.get("nodes")
    nodes = raw_nodes if isinstance(raw_nodes, list) else []

    def _anchor_names(kind: str) -> list[str]:
        """Return safe field names from one persisted workflow anchor.

        Args:
            kind: Anchor kind, either ``input`` or ``output``.

        Returns:
            Persisted string field names in their original order.
        """
        anchor = next((node for node in nodes if isinstance(node, dict) and node.get("kind") == kind), None)
        if not isinstance(anchor, dict):
            return []
        raw_fields = anchor.get("fields")
        fields = raw_fields if isinstance(raw_fields, list) else []
        return [field["name"] for field in fields if isinstance(field, dict) and isinstance(field.get("name"), str)]

    return _anchor_names("input"), _anchor_names("output")


# Long or multi-line example values make the usage snippet unwieldy and can
# break single-line shells, so the sample helpers drop them in favour of a
# ``<field>`` placeholder the frontend renders.
_MAX_SAMPLE_LEN = 200


def _coerce_sample_value(value: Any) -> str | None:
    """Return a snippet-safe string for a sample value, or ``None`` to skip it.

    Args:
        value: A raw input value from a demo or a dataset row.

    Returns:
        The trimmed string for short single-line strings (and stringified
        numbers/bools), or ``None`` when the value is unsuitable for inlining.
    """
    if isinstance(value, str):
        trimmed = value.strip()
        if trimmed and len(trimmed) <= _MAX_SAMPLE_LEN and "\n" not in trimmed:
            return trimmed
        return None
    if isinstance(value, (int, float)):  # bool is an int subclass; str() is fine
        return str(value)
    return None


def _collect_sample(
    source: dict[str, Any], input_fields: list[str], mapping: dict[str, Any] | None = None
) -> dict[str, str]:
    """Pick snippet-safe example values for ``input_fields`` from one row/demo.

    Reads each field directly, falling back to the column-mapping (in either
    direction) when the signature field name differs from the dataset column.

    Args:
        source: A single demo's ``inputs`` dict or a dataset row.
        input_fields: Declared signature input field names.
        mapping: Optional ``column_mapping['inputs']`` for field↔column lookup.

    Returns:
        A ``{field: value}`` map containing only the fields with usable values.
    """
    result: dict[str, str] = {}
    for field in input_fields:
        raw = source.get(field)
        if raw is None and mapping:
            for key, val in mapping.items():
                if val == field and key in source:
                    raw = source[key]
                    break
                if key == field and isinstance(val, str) and val in source:
                    raw = source[val]
                    break
        coerced = _coerce_sample_value(raw)
        if coerced is not None:
            result[field] = coerced
    return result


def _sample_inputs(
    job_store: Any,
    optimization_id: str,
    user: AuthenticatedUser,
    artifact: Any,
    input_fields: list[str],
) -> dict[str, str]:
    """Build example input values to prefill the integration snippet.

    Prefers a real example baked into the program (a demo). Legacy artifacts
    may fall back to the first dataset row so optimizers that carry no demos
    still yield copy-paste-ready values. Protected artifacts never read the
    unsanitized submission payload from this metadata-only route.

    Args:
        job_store: Store used to read the dataset for the fallback path.
        optimization_id: Optimization whose dataset is the fallback source.
        user: Authenticated caller; ownership is re-checked on the dataset read.
        artifact: Program artifact whose demos are the preferred source.
        input_fields: Declared signature input field names.

    Returns:
        A ``{field: value}`` map (possibly partial or empty).
    """
    prompt = (
        artifact.get("optimized_prompt") if isinstance(artifact, dict) else getattr(artifact, "optimized_prompt", None)
    )
    if isinstance(prompt, dict):
        raw_demos = prompt.get("demos")
        demos = raw_demos if isinstance(raw_demos, list) else []
    else:
        demos = list(getattr(prompt, "demos", []) or []) if prompt is not None else []
    if demos:
        first_demo = demos[0]
        demo_inputs = (
            first_demo.get("inputs", {}) if isinstance(first_demo, dict) else getattr(first_demo, "inputs", None) or {}
        )
        if not isinstance(demo_inputs, dict):
            demo_inputs = {}
        sample = _collect_sample(demo_inputs, input_fields)
        if sample:
            return sample
    if isinstance(artifact, dict):
        return {}
    job_data = load_job_for_user(job_store, optimization_id, user)
    payload = job_data.get("payload") or {}
    dataset = payload.get("dataset") or []
    if not dataset or not isinstance(dataset[0], dict):
        return {}
    mapping = (payload.get("column_mapping") or {}).get("inputs") or {}
    return _collect_sample(dataset[0], input_fields, mapping)


async def _stream_program_inference(
    *,
    program: Any,
    lm: Any,
    filtered_inputs: dict[str, Any],
    input_fields: list[str],
    output_fields: list[str],
    listeners: list[StreamListener],
    model_used: str,
    error_log_context: str,
) -> Any:
    """Yield ``{event, data}`` dicts for streaming a DSPy program's inference.

    Tries ``dspy.streamify`` first and falls back to a synchronous call when
    the program / output fields aren't streamable. Emits ``token`` events
    per chunk and a terminal ``final`` event, or a single ``error`` event on
    failure. Re-raises ``asyncio.CancelledError`` so the caller can tear
    the generator down cleanly.

    Args:
        program: Compiled DSPy program to invoke.
        lm: DSPy language model context to bind during inference.
        filtered_inputs: Caller-provided inputs filtered to declared fields.
        input_fields: Declared input field names from the optimized prompt.
        output_fields: Declared output field names from the optimized prompt.
        listeners: Stream listeners (one per output field) for token fan-out.
        model_used: Identifier for the LM, surfaced in the ``final`` event.
        error_log_context: Tag included in the failure log for debuggability.

    Yields:
        ``{"event": "token"|"final"|"error", "data": ...}`` dicts.

    Raises:
        asyncio.CancelledError: Re-raised so the caller can tear the
            generator down cleanly.
    """
    try:
        final_outputs: dict[str, Any] = {}
        try:
            stream_program = dspy.streamify(
                program,
                stream_listeners=listeners,
                async_streaming=True,
            )
            with dspy.context(lm=lm):
                output_stream = stream_program(**filtered_inputs)
                async for item in output_stream:
                    if isinstance(item, StreamResponse):
                        yield {
                            "event": "token",
                            "data": {"field": item.signature_field_name, "chunk": item.chunk},
                        }
                    elif isinstance(item, dspy.Prediction):
                        if output_fields:
                            for field in output_fields:
                                final_outputs[field] = getattr(item, field, None)
                        else:
                            final_outputs |= {
                                key: val for key, val in item.toDict().items() if key not in filtered_inputs
                            }
            yield {
                "event": "final",
                "data": {
                    "outputs": final_outputs,
                    "input_fields": input_fields,
                    "output_fields": output_fields,
                    "model_used": model_used,
                },
            }
            return
        except Exception as stream_exc:
            with dspy.context(lm=lm):
                prediction = await asyncio.to_thread(lambda: program(**filtered_inputs))
            if output_fields:
                for field in output_fields:
                    final_outputs[field] = getattr(prediction, field, None)
            else:
                final_outputs |= {key: val for key, val in prediction.toDict().items() if key not in filtered_inputs}
            yield {
                "event": "final",
                "data": {
                    "outputs": final_outputs,
                    "input_fields": input_fields,
                    "output_fields": output_fields,
                    "model_used": model_used,
                    "streaming_fallback": True,
                    "fallback_reason": str(stream_exc),
                },
            }
            return
    except asyncio.CancelledError:
        raise
    except Exception:
        yield {"event": "error", "data": {"error": "streaming failed"}}
        logger.exception("Serve stream failed for %s", error_log_context)
        return


def create_serve_router(*, job_store) -> APIRouter:
    """Build the serve router.

    Args:
        job_store: Job-store instance backing the load/inference helpers.

    Returns:
        A FastAPI ``APIRouter`` exposing single-run and grid-pair serve routes.
    """
    router = APIRouter()

    @router.get(
        "/serve/{optimization_id}/info",
        response_model=ServeInfoResponse,
        summary="Describe an optimized program without running it",
        tags=["agent"],
    )
    def serve_info(optimization_id: str, current_user: AuthenticatedUserDep) -> ServeInfoResponse:
        """Describe an optimized program without running it.

        Metadata-only — no LLM calls. Protected jobs are described from their
        persisted scrubbed prompt metadata without loading authored code. 404
        if unknown or inaccessible to the caller, 409 if not finished.

        Args:
            optimization_id: Optimization id whose artifact should be described.
            current_user: Authenticated caller resolved from the bearer token.

        Returns:
            A ``ServeInfoResponse`` listing the program's I/O fields,
            instructions, and demo count.

        Raises:
            DomainError: 404 if unknown or inaccessible, 409 if the
                optimization is not in a serveable state.
        """
        artifact, overview, model_name = load_program_metadata(job_store, optimization_id, current_user)
        input_fields, output_fields, instructions, demo_count = _artifact_prompt_fields(artifact)
        workflow_fields = _workflow_anchor_fields(overview)
        if workflow_fields is not None:
            input_fields, output_fields = workflow_fields
        if not input_fields:
            input_fields, output_fields = _column_mapping_fields(overview)

        return ServeInfoResponse(
            optimization_id=optimization_id,
            module_name=overview.get(PAYLOAD_OVERVIEW_MODULE_NAME, ""),
            optimizer_name=overview.get(PAYLOAD_OVERVIEW_OPTIMIZER_NAME, ""),
            model_name=model_name,
            input_fields=input_fields,
            output_fields=output_fields,
            instructions=truncate_text(instructions, AGENT_MAX_INSTRUCTIONS),
            demo_count=demo_count,
            sample_inputs=_sample_inputs(job_store, optimization_id, current_user, artifact, input_fields),
        )

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
        Access and artifact readiness are gated by ``load_program_metadata``
        so the agent can't render a form for an inaccessible optimization.
        Protected jobs stay metadata-only in the API process; their inference
        route remains unavailable until it has a metered interactive sandbox.
        The card itself hits ``/serve/{id}/info`` for the field schema and
        ``/serve/{id}`` for the actual inference call.

        Args:
            optimization_id: Optimization id whose form should be rendered.
            req: Optional prompt describing why inference is being offered.
            current_user: Authenticated caller resolved from the bearer token.

        Returns:
            A :class:`RequestUserInferenceResponse` carrying the prompt back
            so the upload card can display it.

        Raises:
            DomainError: 404 if unknown or inaccessible, 409 if the
                optimization is not in a serveable state.
        """
        load_program_metadata(job_store, optimization_id, current_user)
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
        """Signal the chat UI to render an inline inference card for one pair.

        The grid-search twin of :func:`request_user_inference`: it targets a
        single generation×reflection pair by ``pair_index`` (the agent picks
        one after reading ``serve_pair_info`` / ``get_grid_search_result``).
        Access, pair existence, and artifact readiness are gated by
        ``load_pair_program_metadata`` so the agent can't render a form for an
        inaccessible run or nonexistent pair. Protected jobs stay
        metadata-only in the API process. The card fetches the field schema
        from ``/serve/{id}/pair/{index}/info`` and runs inference via
        ``/serve/{id}/pair/{index}``.

        Args:
            optimization_id: Grid-search optimization id.
            pair_index: Index of the pair to serve.
            req: Optional prompt describing why inference is being offered.
            current_user: Authenticated caller resolved from the bearer token.

        Returns:
            A :class:`RequestUserPairInferenceResponse` echoing the target pair
            and prompt so the card can render them.

        Raises:
            DomainError: 404 if unknown/inaccessible, 409 if not in a
                serveable state, 400 if the pair index is out of range.
        """
        load_pair_program_metadata(job_store, optimization_id, pair_index, current_user)
        return RequestUserPairInferenceResponse(
            optimization_id=optimization_id,
            pair_index=pair_index,
            awaiting_inputs=True,
            prompt=req.prompt.strip(),
        )

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
        """Run a blocking inference call through the compiled program.

        The program always runs on the model settings it was optimized with:
        stored job settings, else the stored model name. All ``input_fields`` must be supplied; extras are
        ignored.

        Args:
            optimization_id: Optimization id whose program should run.
            req: Inference request carrying inputs.
            request: Incoming request; its auth channel tags the telemetry row.
            current_user: Authenticated caller resolved from the bearer token.

        Returns:
            A ``ServeResponse`` with the predicted outputs and resolved model.

        Raises:
            DomainError: 400 (bad inputs / no model), 402 (caller has no
                spendable balance), 404 (unknown or inaccessible), 409 (not
                in a serveable state).
        """
        with ServeInferenceEvent(
            job_store,
            username=current_user.username,
            optimization_id=optimization_id,
            channel=serve_channel(request),
            route="serve",
        ):
            job_data = load_job_for_user(job_store, optimization_id, current_user)
            if _protected_api_runtime(job_data) is not None:
                return ServeResponse.model_validate(
                    _protected_program_call(
                        job_store=job_store,
                        job_data=job_data,
                        optimization_id=optimization_id,
                        req=req,
                        current_user=current_user,
                        idempotency_key=idempotency_key,
                    )
                )
            program, result, overview = load_program(job_store, optimization_id, current_user)
            artifact = result.program_artifact

            model_settings = overview.get(PAYLOAD_OVERVIEW_MODEL_SETTINGS, {})
            model_name = overview.get(PAYLOAD_OVERVIEW_MODEL_NAME, "")
            if model_settings:
                model_config = ModelConfig.model_validate(model_settings)
            elif model_name:
                model_config = ModelConfig(name=model_name)
            else:
                raise DomainError("serve.no_model_config", status=400)

            input_fields, output_fields, _instructions, _demo_count = _artifact_prompt_fields(artifact)
            workflow_spec = workflow_spec_from_overview(overview)
            if workflow_spec is not None:
                input_fields = workflow_spec.input_field_names()
                output_fields = workflow_spec.output_field_names()

            if not input_fields:
                input_fields, output_fields = _column_mapping_fields(overview)
            if not input_fields:
                raise DomainError("serve.no_declared_inputs", status=400)
            missing = [f for f in input_fields if f not in req.inputs]
            if missing:
                raise DomainError(
                    "serve.missing_inputs",
                    status=400,
                    missing=missing,
                    input_fields=input_fields,
                )
            filtered_inputs = {f: req.inputs[f] for f in input_fields}

            enforce_llm_balance(job_store, current_user.username)
            model_config = _resolve_inference_model_config(job_store, current_user.username, model_config)
            lm = build_language_model(model_config)

            node_traces = None
            try:
                with dspy.context(lm=lm):
                    if workflow_spec is not None:
                        with capture_node_traces() as raw_traces:
                            prediction = program(**filtered_inputs)
                        node_traces = sanitize_node_traces(raw_traces)
                    else:
                        prediction = program(**filtered_inputs)
            finally:
                # A failed inference still consumed provider tokens (dspy retries
                # under the hood), so the debit rides ``finally``.
                meter_llm_run(
                    getattr(job_store, "engine", None),
                    current_user.username,
                    [lm],
                    description="Serve inference",
                    token_source=model_config.token_source or TOKEN_SOURCE_MANAGED,
                )

            outputs: dict[str, Any] = {}
            if output_fields:
                for field in output_fields:
                    outputs[field] = getattr(prediction, field, None)
            else:
                outputs = {key: val for key, val in prediction.toDict().items() if key not in req.inputs}

            return ServeResponse(
                optimization_id=optimization_id,
                outputs=_cap_serve_outputs(outputs),
                input_fields=input_fields,
                output_fields=output_fields,
                model_used=model_config.normalized_identifier(),
                node_traces=node_traces,
            )

    @router.post(
        "/serve/{optimization_id}/stream",
        summary="Run inference and stream partial outputs as SSE",
    )
    async def serve_program_stream(
        optimization_id: str,
        req: ServeRequest,
        request: Request,
        current_user: AuthenticatedUserDep,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        """Run inference and stream partial outputs as Server-Sent Events.

        Emits one ``token`` event per chunk, keyed by output field, then a
        terminal ``final`` event. Falls back to blocking inference with
        ``streaming_fallback=true`` if ``dspy.streamify`` can't set up
        listeners. Same input validation and model resolution as the
        non-streaming endpoint.

        Args:
            optimization_id: Optimization id whose program should run.
            req: Inference request carrying inputs.
            request: Incoming request; its auth channel tags the telemetry row.
            current_user: Authenticated caller resolved from the bearer token.

        Returns:
            A streaming ``StreamingResponse`` with ``text/event-stream`` body.

        Raises:
            DomainError: 400, 402, 404 (including inaccessible to caller),
                or 409 mirroring the non-streaming route.
        """
        async with ServeInferenceEvent(
            job_store,
            username=current_user.username,
            optimization_id=optimization_id,
            channel=serve_channel(request),
            route="serve_stream",
        ) as inference:
            try:
                job_data = await asyncio.to_thread(load_job_for_user, job_store, optimization_id, current_user)
            except DomainError as error:
                if error.status_code != 404:
                    raise
                job_data = None
            if job_data is not None and _protected_api_runtime(job_data) is not None:
                _interaction_authority(req.max_cost_cents, idempotency_key)
                source = _protected_sse(
                    lambda emit: _protected_program_call(
                        job_store=job_store,
                        job_data=job_data,
                        optimization_id=optimization_id,
                        req=req,
                        current_user=current_user,
                        idempotency_key=idempotency_key,
                        stream=True,
                        on_event=emit,
                    ),
                    final_event="final",
                )
                return StreamingResponse(
                    sse_from_events(inference.stream(source)),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",
                    },
                )
            # Offload the synchronous DB read + program deserialization so it does
            # not block the single event loop (and every other in-flight request /
            # SSE stream) before the first byte is produced.
            program, result, overview = await asyncio.to_thread(load_program, job_store, optimization_id, current_user)
            artifact = result.program_artifact

            model_settings = overview.get(PAYLOAD_OVERVIEW_MODEL_SETTINGS, {})
            model_name = overview.get(PAYLOAD_OVERVIEW_MODEL_NAME, "")
            if model_settings:
                model_config = ModelConfig.model_validate(model_settings)
            elif model_name:
                model_config = ModelConfig(name=model_name)
            else:
                raise DomainError("serve.no_model_config", status=400)

            input_fields, output_fields, _instructions, _demo_count = _artifact_prompt_fields(artifact)
            workflow_spec = workflow_spec_from_overview(overview)
            if workflow_spec is not None:
                # Workflows serve their anchor fields. Per-output-field token
                # listeners still attach; if streamify can't wire them onto the
                # composed program the generator's fallback runs blocking
                # inference and emits a single final event.
                input_fields = workflow_spec.input_field_names()
                output_fields = workflow_spec.output_field_names()

            if not input_fields:
                input_fields, output_fields = _column_mapping_fields(overview)
            if not input_fields:
                raise DomainError("serve.no_declared_inputs", status=400)
            missing = [f for f in input_fields if f not in req.inputs]
            if missing:
                raise DomainError(
                    "serve.missing_inputs",
                    status=400,
                    missing=missing,
                    input_fields=input_fields,
                )
            filtered_inputs = {f: req.inputs[f] for f in input_fields}

            await asyncio.to_thread(enforce_llm_balance, job_store, current_user.username)
            model_config = _resolve_inference_model_config(job_store, current_user.username, model_config)
            lm = build_language_model(model_config)
            model_used = model_config.normalized_identifier()
            listeners = [StreamListener(signature_field_name=f) for f in output_fields]
            source = _stream_program_inference(
                program=program,
                lm=lm,
                filtered_inputs=filtered_inputs,
                input_fields=input_fields,
                output_fields=output_fields,
                listeners=listeners,
                model_used=model_used,
                error_log_context=f"job {optimization_id}",
            )
            metered = stream_with_llm_metering(
                source,
                job_store=job_store,
                username=current_user.username,
                description="Serve inference",
                usage_sink=[lm],
                token_source=model_config.token_source or TOKEN_SOURCE_MANAGED,
            )
            return StreamingResponse(
                sse_from_events(inference.stream(metered)),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

    @router.get(
        "/serve/{optimization_id}/pair/{pair_index}/info",
        response_model=ServeInfoResponse,
        summary="Describe the program for one grid-search pair",
        tags=["agent"],
    )
    def serve_pair_info(optimization_id: str, pair_index: int, current_user: AuthenticatedUserDep) -> ServeInfoResponse:
        """Describe the program for one grid-search pair without running it.

        Same shape as ``GET /serve/{id}/info`` but scoped to a specific
        grid-search pair; ``model_name`` is the pair's generation model.

        Args:
            optimization_id: Grid-search optimization id.
            pair_index: Index of the pair in the grid-search result.
            current_user: Authenticated caller resolved from the bearer token.

        Returns:
            A ``ServeInfoResponse`` describing the pair's compiled program.

        Raises:
            DomainError: 404 (unknown / inaccessible), 409 (not finished or
                pair failed).
        """
        artifact, pair, overview = load_pair_program_metadata(job_store, optimization_id, pair_index, current_user)
        input_fields, output_fields, instructions, demo_count = _artifact_prompt_fields(artifact)
        if not input_fields:
            input_fields, output_fields = _column_mapping_fields(overview)
        model_name = pair.get("generation_model", "") if isinstance(pair, dict) else pair.generation_model

        return ServeInfoResponse(
            optimization_id=optimization_id,
            module_name=overview.get(PAYLOAD_OVERVIEW_MODULE_NAME, ""),
            optimizer_name=overview.get(PAYLOAD_OVERVIEW_OPTIMIZER_NAME, ""),
            model_name=model_name,
            input_fields=input_fields,
            output_fields=output_fields,
            instructions=truncate_text(instructions, AGENT_MAX_INSTRUCTIONS),
            demo_count=demo_count,
            sample_inputs=_sample_inputs(job_store, optimization_id, current_user, artifact, input_fields),
        )

    @router.post(
        "/serve/{optimization_id}/pair/{pair_index}",
        response_model=ServeResponse,
        summary="Run inference through one grid-search pair",
    )
    def serve_pair_program(
        optimization_id: str,
        pair_index: int,
        req: ServeRequest,
        request: Request,
        current_user: AuthenticatedUserDep,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ServeResponse:
        """Run inference through one grid-search pair's compiled program.

        The program always runs on the pair's generation model. All ``input_fields`` must be supplied;
        extras are ignored.

        Args:
            optimization_id: Grid-search optimization id.
            pair_index: Index of the pair in the grid-search result.
            req: Inference request carrying inputs.
            request: Incoming request; its auth channel tags the telemetry row.
            current_user: Authenticated caller resolved from the bearer token.

        Returns:
            A ``ServeResponse`` with the predicted outputs and resolved model.

        Raises:
            DomainError: 400 (bad inputs), 402 (caller has no spendable
                balance), 404 (unknown / inaccessible), 409 (not finished
                or pair failed).
        """
        with ServeInferenceEvent(
            job_store,
            username=current_user.username,
            optimization_id=optimization_id,
            channel=serve_channel(request),
            route="pair",
            pair_index=pair_index,
        ):
            job_data = load_job_for_user(job_store, optimization_id, current_user)
            if _protected_api_runtime(job_data) is not None:
                return ServeResponse.model_validate(
                    _protected_program_call(
                        job_store=job_store,
                        job_data=job_data,
                        optimization_id=optimization_id,
                        req=req,
                        current_user=current_user,
                        idempotency_key=idempotency_key,
                        pair_index=pair_index,
                    )
                )
            program, pair, overview = load_pair_program(job_store, optimization_id, pair_index, current_user)
            artifact = pair.program_artifact

            model_config = _pair_model_config(pair.generation_model, overview)

            input_fields, output_fields, _instructions, _demo_count = _artifact_prompt_fields(artifact)

            if not input_fields:
                raise DomainError("serve.no_declared_inputs", status=400)
            missing = [f for f in input_fields if f not in req.inputs]
            if missing:
                raise DomainError(
                    "serve.missing_inputs",
                    status=400,
                    missing=missing,
                    input_fields=input_fields,
                )
            filtered_inputs = {f: req.inputs[f] for f in input_fields}

            enforce_llm_balance(job_store, current_user.username)
            model_config = _resolve_inference_model_config(job_store, current_user.username, model_config)
            lm = build_language_model(model_config)

            try:
                with dspy.context(lm=lm):
                    prediction = program(**filtered_inputs)
            finally:
                # A failed inference still consumed provider tokens (dspy retries
                # under the hood), so the debit rides ``finally``.
                meter_llm_run(
                    getattr(job_store, "engine", None),
                    current_user.username,
                    [lm],
                    description="Serve inference",
                    token_source=model_config.token_source or TOKEN_SOURCE_MANAGED,
                )

            outputs: dict[str, Any] = {}
            if output_fields:
                for field in output_fields:
                    outputs[field] = getattr(prediction, field, None)
            else:
                outputs = {key: val for key, val in prediction.toDict().items() if key not in req.inputs}

            return ServeResponse(
                optimization_id=optimization_id,
                outputs=_cap_serve_outputs(outputs),
                input_fields=input_fields,
                output_fields=output_fields,
                model_used=model_config.normalized_identifier(),
            )

    @router.post(
        "/serve/{optimization_id}/pair/{pair_index}/stream",
        summary="Stream inference from one grid-search pair as SSE",
    )
    async def serve_pair_program_stream(
        optimization_id: str,
        pair_index: int,
        req: ServeRequest,
        request: Request,
        current_user: AuthenticatedUserDep,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        """Stream inference from one grid-search pair as Server-Sent Events.

        Same ``token`` -> ``final`` event shape as the single-run stream
        endpoint, same blocking fallback behavior when ``dspy.streamify``
        can't set up listeners.

        Args:
            optimization_id: Grid-search optimization id.
            pair_index: Index of the pair in the grid-search result.
            req: Inference request carrying inputs.
            request: Incoming request; its auth channel tags the telemetry row.
            current_user: Authenticated caller resolved from the bearer token.

        Returns:
            A streaming ``StreamingResponse`` with ``text/event-stream`` body.

        Raises:
            DomainError: 400 (bad inputs), 402 (caller has no spendable
                balance), 404 (unknown / inaccessible), 409 (not finished
                or pair failed).
        """
        async with ServeInferenceEvent(
            job_store,
            username=current_user.username,
            optimization_id=optimization_id,
            channel=serve_channel(request),
            route="pair_stream",
            pair_index=pair_index,
        ) as inference:
            try:
                job_data = await asyncio.to_thread(load_job_for_user, job_store, optimization_id, current_user)
            except DomainError as error:
                if error.status_code != 404:
                    raise
                job_data = None
            if job_data is not None and _protected_api_runtime(job_data) is not None:
                _interaction_authority(req.max_cost_cents, idempotency_key)
                source = _protected_sse(
                    lambda emit: _protected_program_call(
                        job_store=job_store,
                        job_data=job_data,
                        optimization_id=optimization_id,
                        req=req,
                        current_user=current_user,
                        idempotency_key=idempotency_key,
                        pair_index=pair_index,
                        stream=True,
                        on_event=emit,
                    ),
                    final_event="final",
                )
                return StreamingResponse(
                    sse_from_events(inference.stream(source)),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",
                    },
                )
            # Offload the synchronous DB read + program deserialization so it does
            # not block the single event loop before the first byte is produced.
            program, pair, overview = await asyncio.to_thread(
                load_pair_program, job_store, optimization_id, pair_index, current_user
            )
            artifact = pair.program_artifact

            model_config = _pair_model_config(pair.generation_model, overview)

            input_fields, output_fields, _instructions, _demo_count = _artifact_prompt_fields(artifact)

            if not input_fields:
                raise DomainError("serve.no_declared_inputs", status=400)
            missing = [f for f in input_fields if f not in req.inputs]
            if missing:
                raise DomainError(
                    "serve.missing_inputs",
                    status=400,
                    missing=missing,
                    input_fields=input_fields,
                )
            filtered_inputs = {f: req.inputs[f] for f in input_fields}

            await asyncio.to_thread(enforce_llm_balance, job_store, current_user.username)
            model_config = _resolve_inference_model_config(job_store, current_user.username, model_config)
            lm = build_language_model(model_config)
            model_used = model_config.normalized_identifier()
            listeners = [StreamListener(signature_field_name=f) for f in output_fields]
            source = _stream_program_inference(
                program=program,
                lm=lm,
                filtered_inputs=filtered_inputs,
                input_fields=input_fields,
                output_fields=output_fields,
                listeners=listeners,
                model_used=model_used,
                error_log_context=f"job {optimization_id} pair {pair_index}",
            )
            metered = stream_with_llm_metering(
                source,
                job_store=job_store,
                username=current_user.username,
                description="Serve inference",
                usage_sink=[lm],
                token_source=model_config.token_source or TOKEN_SOURCE_MANAGED,
            )
            return StreamingResponse(
                sse_from_events(inference.stream(metered)),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

    @router.post(
        "/serve/{optimization_id}/chat",
        summary="Stream a live ReAct chat turn against an optimized agent",
    )
    async def serve_chat_stream(
        optimization_id: str,
        req: ServeChatRequest,
        request: Request,
        current_user: AuthenticatedUserDep,
        authorization: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> StreamingResponse:
        """Stream one chat turn against a served, optimized ReActV2 agent as SSE.

        The agent's tools execute live against the MCP server the run was
        optimized against; each call is approval-gated by ``trust_mode`` (every
        tool except in ``yolo``). Emits the generalist SSE envelope
        (``reasoning_patch``, ``tool_start`` / ``tool_end``, ``pending_approval``
        / ``approval_resolved``, ``message_patch``, ``done``, ``error``).

        Args:
            optimization_id: The react run to chat against.
            req: Chat request with the user's message, prior turns and trust mode.
            request: Incoming request; its auth channel tags the telemetry row.
            current_user: Authenticated caller; non-admins are restricted to
                their own runs.
            authorization: Caller's bearer token, forwarded into the agent's MCP
                session so its tool calls authenticate as the same user.

        Returns:
            A streaming ``text/event-stream`` response.

        Raises:
            DomainError: 404 (unknown/inaccessible), 409 (not a success react
                run, or not served from a live-MCP source), 400 (no model),
                402 (caller has no spendable balance).
        """
        async with ServeInferenceEvent(
            job_store,
            username=current_user.username,
            optimization_id=optimization_id,
            channel=serve_channel(request),
            route="chat",
        ) as inference:
            try:
                job_data = await asyncio.to_thread(load_job_for_user, job_store, optimization_id, current_user)
            except DomainError as error:
                if error.status_code != 404:
                    raise
                job_data = None
            if job_data is not None and _protected_api_runtime(job_data) is not None:
                maximum, key = _interaction_authority(req.max_cost_cents, idempotency_key)
                artifact, overview, model_name = await asyncio.to_thread(
                    load_program_metadata, job_store, optimization_id, current_user
                )
                raw_artifact = artifact if isinstance(artifact, dict) else artifact.model_dump(mode="json")
                overlay = raw_artifact.get("react_overlay")
                if not isinstance(overlay, dict):
                    raise DomainError("serve.chat_not_react", status=409)
                stored = job_data.get("payload") if isinstance(job_data.get("payload"), dict) else {}
                tool_source = stored.get("tool_source") or overlay.get("tool_source")
                if not isinstance(tool_source, dict) or tool_source.get("kind") != "live_mcp":
                    raise DomainError("serve.chat_requires_live_mcp", status=409)
                model_settings = overview.get(PAYLOAD_OVERVIEW_MODEL_SETTINGS, {})
                if model_settings:
                    model_config = ModelConfig.model_validate(model_settings)
                elif model_name:
                    model_config = ModelConfig(name=model_name)
                else:
                    raise DomainError("serve.no_model_config", status=400)
                input_fields, output_fields, _instructions, _demo_count = _artifact_prompt_fields(artifact)
                if not input_fields:
                    raise DomainError("serve.no_declared_inputs", status=400)
                inputs = dict.fromkeys(input_fields, "")
                inputs[input_fields[0]] = req.user_message
                source = _protected_sse(
                    lambda emit: _protected_result(
                        job_store=job_store,
                        job_data=job_data,
                        artifact=artifact,
                        overview=overview,
                        model_config=model_config,
                        inputs=inputs,
                        interaction={
                            "kind": "react_chat",
                            "optimization_id": optimization_id,
                            "input_fields": input_fields,
                            "output_fields": output_fields,
                            "stream": True,
                        },
                        max_cost_cents=maximum,
                        idempotency_key=key,
                        current_user=current_user,
                        on_event=emit,
                        require_tool_approval=req.trust_mode != "yolo",
                    ),
                    final_event="done",
                )
                return StreamingResponse(
                    sse_from_events(inference.stream(source)),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",
                    },
                )
            # Offload the synchronous DB read + program deserialization so it does
            # not block the single event loop before the first byte is produced.
            signature_cls, program_state_json, react_overlay, overview = await asyncio.to_thread(
                load_react_chat_inputs, job_store, optimization_id, current_user
            )

            model_settings = overview.get(PAYLOAD_OVERVIEW_MODEL_SETTINGS, {})
            model_name = overview.get(PAYLOAD_OVERVIEW_MODEL_NAME, "")
            if model_settings:
                model_config = ModelConfig.model_validate(model_settings)
            elif model_name:
                model_config = ModelConfig(name=model_name)
            else:
                raise DomainError("serve.no_model_config", status=400)

            await asyncio.to_thread(enforce_llm_balance, job_store, current_user.username)
            model_config = _resolve_inference_model_config(job_store, current_user.username, model_config)
            lm = build_language_model(model_config)
            tool_source = react_overlay.tool_source or {}
            mcp_url = tool_source.get("mcp_url") or settings.generalist_agent_mcp_url

            source = run_react_chat(
                signature_cls=signature_cls,
                program_state_json=program_state_json,
                react_overlay=react_overlay,
                user_message=req.user_message,
                trust_mode=req.trust_mode,
                lm=lm,
                model_name=model_config.normalized_identifier(),
                mcp_url=mcp_url,
                auth_header=authorization,
                steer_key=req.steer_key,
                approval_owner=current_user.username,
            )
            metered = stream_with_llm_metering(
                source,
                job_store=job_store,
                username=current_user.username,
                description="Serve chat",
                usage_sink=[lm],
                token_source=model_config.token_source or TOKEN_SOURCE_MANAGED,
            )
            return StreamingResponse(
                sse_from_events(inference.stream(metered)),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

    @router.post(
        "/serve/{optimization_id}/chat/confirm",
        response_model=ServeChatConfirmResponse,
        summary="Resolve a pending react-serve chat tool approval",
    )
    def serve_chat_confirm(
        optimization_id: str,
        req: ServeChatConfirmRequest,
        current_user: AuthenticatedUserDep,
    ) -> ServeChatConfirmResponse:
        """Resolve an outstanding tool approval from the react-serve chat.

        Approval call-ids are process-unique, so this shares the same global
        registry the generalist agent uses; ``optimization_id`` scopes the
        route and enforces the caller holds editor-tier access (chat spends the
        owner's key, so it is editor+ like the rest of the serve surface).

        Args:
            optimization_id: The react run the pending call belongs to.
            req: Confirm payload with the ``call_id`` and approval boolean.
            current_user: Authenticated caller; must hold editor-tier access.

        Returns:
            A :class:`ServeChatConfirmResponse` with ``resolved=True`` on success.

        Raises:
            DomainError: 404 when the run is unknown/inaccessible; 403 when the
                caller's role is below editor; 404 when the call id is unknown
                or already resolved.
        """
        job_data = load_job_for_user(job_store, optimization_id, current_user)
        if _protected_api_runtime(job_data) is None:
            require_role_at_least(job_store, optimization_id, current_user, ShareRole.editor)
        resolved = get_approval_registry().resolve_or_persist(req.call_id, req.approved)
        if not resolved:
            raise DomainError("agent.approval.unknown_call_id", status=404)
        return ServeChatConfirmResponse(resolved=True)

    return router
