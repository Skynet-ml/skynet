"""SSE endpoint for the generalist agent that drives the Skynet wizard. [INTERNAL]

Mirrors the ``code_agent`` router's shape: one streaming POST that emits
reasoning / tool / message events, plus a companion confirm POST so the
client can respond to ``pending_approval`` events (the SSE channel is
server → client only).

Persistence: when the caller is authenticated and ``job_store`` was wired
in at router construction, every turn is mirrored into the
``agent_conversations`` / ``agent_messages`` tables. The first emitted SSE
event is ``conversation_meta`` carrying the canonical conversation id so
new threads materialise without a separate round-trip.

All endpoints are hidden from the public Scalar reference (none are in
``_SCALAR_PUBLIC_PATHS``) — wizard-internal flow.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast, get_args
from uuid import uuid4

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse

from ...billing.cli_dispatch import CLI_FAST_KEY
from ...billing.metering import meter_llm_run
from ...config import settings
from ...constants import TOKEN_SOURCE_MANAGED
from ...local.cli_catalog import CliTopEffort
from ...service_gateway.agents.code import _reply_language
from ...service_gateway.agents.generalist import (
    ThinkingTrail,
    TrustMode,
    WizardState,
    approval_key,
    build_hosted_suggest_lm,
    build_local_suggest_lm,
    fallback_summary,
    get_approval_registry,
    run_generalist_agent,
)
from ...service_gateway.agents.next_prompt import suggest_next_prompt
from ...service_gateway.agents.steering import (
    STEER_APPLIED_EVENT,
    STEER_KEY_MAX_CHARS,
    STEER_TEXT_MAX_CHARS,
    get_steer_store,
)
from ...service_gateway.embedding_pipeline import queue_conversation_embed
from ...storage.models import AgentConversationModel, AgentMessageModel
from ..agent_engine import engine_agent_model
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..model_catalog import ReasoningEffort
from ..model_router import effective_reasoning_effort, metering_token_source, route_agent_model, route_local_byok_model
from ..suggest_model import hosted_suggest_model
from ._helpers import enforce_llm_balance, sse_from_events, stream_with_llm_metering

logger = logging.getLogger(__name__)
# A suggestion has a larger prompt than a thinking label, and on a CLI engine a
# process start alone takes 3-7 s, so the label's 8 s limit made every CLI
# suggestion time out. The CLI side stops its run at 25 s.
_SUGGEST_TIMEOUT_SECONDS = 30.0

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]

TITLE_MAX_CHARS = 40
# Composer blocks for attached files and long pastes (frontend
# ``shared/ui/agent/pasted-text.ts``); their bodies are not the user's words.
_COMPOSER_BLOCK_RE = re.compile(
    r'<pasted_text>.*?</pasted_text>|<attached_file name="([^"]*)">.*?</attached_file>', re.DOTALL
)

# The history is re-sent in full every turn, so an unbounded thread grows the
# prompt without limit. Recent turns carry the working context.
HISTORY_MAX_TURNS = 24
HISTORY_TURN_MAX_CHARS = 4000
HISTORY_MAX_TOOL_CALLS = 8
HISTORY_TOOL_RESULT_MAX_CHARS = 400
NEXT_PROMPT_MAX_TURNS = 100
NEXT_PROMPT_MAX_ACTIONS = 20


# One tool call an earlier assistant turn made, as the panel recorded it.
class ChatToolCall(BaseModel):
    tool: str = Field(..., max_length=200, description="MCP tool name.")
    status: str = Field(default="done", max_length=40, description="'done' or 'error'.")
    result: str | None = Field(default=None, description="Tool result text, clipped client-side.")


class ChatTurn(BaseModel):
    """A single prior turn in the agent conversation."""

    role: str = Field(..., description="'user' or 'assistant'.")
    content: str = Field(..., description="Message text.")
    tool_calls: list[ChatToolCall] = Field(
        default_factory=list,
        description="Assistant turns only: the tool calls that turn made, oldest first.",
    )


def history_for_agent(turns: list[ChatTurn]) -> list[dict[str, Any]]:
    """Bound the prior turns and attach each assistant turn's tool trace.

    Without the trace the model only sees its own prose from earlier turns, so
    it cannot tell which id a past submit returned or why a past call failed.

    Args:
        turns: The client-supplied prior turns, oldest first.

    Returns:
        At most ``HISTORY_MAX_TURNS`` ``{role, content}`` dicts, newest kept,
        each assistant turn that called tools carrying a compact ``tools`` list.
    """
    history: list[dict[str, Any]] = []
    for turn in turns[-HISTORY_MAX_TURNS:]:
        entry: dict[str, Any] = {"role": turn.role, "content": turn.content[:HISTORY_TURN_MAX_CHARS]}
        if turn.role == "assistant" and turn.tool_calls:
            entry["tools"] = [
                {
                    "tool": call.tool,
                    "status": call.status,
                    "result": (call.result or "")[:HISTORY_TOOL_RESULT_MAX_CHARS],
                }
                for call in turn.tool_calls[-HISTORY_MAX_TOOL_CALLS:]
            ]
        history.append(entry)
    return history


class GeneralistAgentRequest(BaseModel):
    """Input for a single generalist-agent turn."""

    user_message: str = Field(..., description="The user's latest message.")
    chat_history: list[ChatTurn] = Field(default_factory=list, description="Prior {role, content} turns.")
    wizard_state: dict = Field(
        default_factory=dict,
        description=(
            "Snapshot of the wizard: ``{dataset_ready, columns_configured, "
            "signature_code, metric_code, model_configured, staged_dataset_id}``."
        ),
    )
    trust_mode: TrustMode = Field(
        default="ask",
        description="'ask' (confirm every mutation), 'auto_safe' (confirm destructive only), 'yolo' (never confirm).",
    )
    conversation_id: str | None = Field(
        default=None,
        description=(
            "Optional id of an existing thread to append to. When absent the "
            "server creates a new conversation and emits its id via the "
            "``conversation_meta`` SSE event before any other event."
        ),
    )
    regenerate: bool = Field(
        default=False,
        description=(
            "Replace the existing conversation's final user/assistant turn instead of appending a duplicate turn."
        ),
    )
    locale: str | None = Field(
        default=None,
        description=(
            "UI locale code of the client (e.g. 'he', 'en', 'fr-CA'). Sets "
            "the language of the agent's replies; unknown or missing falls "
            "back to Hebrew."
        ),
    )
    model: str | None = Field(
        default=None,
        description=(
            "LiteLLM id of the catalog model to run this turn on (the "
            "composer's model menu). Absent runs the catalog's best-value "
            "default model."
        ),
    )
    reasoning_effort: ReasoningEffort | CliTopEffort | None = Field(
        default=None,
        description=(
            "Explicit reasoning-effort level for the chosen model; absent keeps the model's default. "
            "``ultra`` (Codex) and ``ultracode`` (Claude Code) apply only to a local CLI model that "
            "takes them; any other model ignores them."
        ),
    )
    fast_mode: bool = Field(
        default=False,
        description=(
            "Run this turn in the CLI's fast mode. Applies only to a local CLI model that takes "
            "fast mode; any other model ignores it."
        ),
    )
    steer_key: str | None = Field(
        default=None,
        max_length=STEER_KEY_MAX_CHARS,
        description=(
            "Client-chosen key for this turn. Messages posted to "
            "``/optimizations/agent-steer`` under it while the turn runs reach "
            "the agent at its next step, announced by a ``steer_applied`` event."
        ),
    )


class ConfirmApprovalRequest(BaseModel):
    """Client → server reply to a ``pending_approval`` SSE event."""

    call_id: str = Field(..., max_length=24, description="The id carried by the pending_approval event.")
    approved: bool = Field(..., description="True to proceed with the tool, False to decline.")


class ConfirmApprovalResponse(BaseModel):
    """Ack for an approval confirm call."""

    resolved: bool


class SteerMessageRequest(BaseModel):
    """A message for an agent turn that is still running."""

    steer_key: str = Field(..., min_length=1, max_length=STEER_KEY_MAX_CHARS, description="The running turn's key.")
    text: str = Field(..., min_length=1, max_length=STEER_TEXT_MAX_CHARS, description="The message text.")


class SteerMessageResponse(BaseModel):
    """Ack for a steer message."""

    id: str


class SteerWithdrawRequest(BaseModel):
    """Take back the messages a finished turn never read."""

    steer_key: str = Field(..., min_length=1, max_length=STEER_KEY_MAX_CHARS, description="The turn's key.")
    ids: list[str] | None = Field(
        default=None, max_length=100, description="Take back only these messages; omitted takes back every one."
    )


class SteerWithdrawResponse(BaseModel):
    """Messages returned to the client, oldest first."""

    ids: list[str]


class NextPromptTurn(BaseModel):
    """One turn of the conversation a next-message suggestion reads."""

    role: Literal["user", "assistant"] = Field(..., description="'user' or 'assistant'.")
    content: str = Field(..., description="Message text; the server clips long turns.")


class NextPromptRequest(BaseModel):
    """Input for one next-message suggestion."""

    turns: list[NextPromptTurn] = Field(
        ...,
        max_length=NEXT_PROMPT_MAX_TURNS,
        description="The conversation, oldest first; the last turn is the agent's.",
    )
    actions: list[str] = Field(
        default_factory=list,
        max_length=NEXT_PROMPT_MAX_ACTIONS,
        description="Short notes on what just happened (tool results, approvals, uploads), oldest first.",
    )
    locale: str | None = Field(default=None, description="UI locale code; sets the suggestion's language.")
    model: str | None = Field(
        default=None,
        description="The composer's model pick. Only the Mac app reads it, to run the suggestion on the same engine.",
    )


class NextPromptResponse(BaseModel):
    """The suggested next message, or an empty string when there is none."""

    suggestion: str


def _next_prompt_lm(job_store, username: str, model: str | None) -> tuple[Any | None, str]:
    """Build the small model a suggestion runs on; never the composer, interview or agent model.

    Hosted, it is the scored suggestion model (see
    :func:`~core.api.suggest_model.hosted_suggest_model`). In the Mac app, the
    composer pick only selects the engine; the suggestion runs on that
    engine's smallest model.

    Args:
        job_store: Job store whose engine holds the caller's engine setting and saved keys.
        username: The caller.
        model: The composer's model pick.

    Returns:
        ``(lm, token_source)``; ``lm`` is ``None`` when no model can run it.

    Raises:
        DomainError: When the Mac app's pick cannot be routed.
    """
    if not settings.local_mode:
        picked = hosted_suggest_model()
        lm = build_hosted_suggest_lm(picked, _SUGGEST_TIMEOUT_SECONDS) if picked else None
        return lm, TOKEN_SOURCE_MANAGED
    picked = engine_agent_model(job_store, username, model)
    config = route_local_byok_model(picked, job_store=job_store, username=username) or route_agent_model(picked)
    return build_local_suggest_lm(config, _SUGGEST_TIMEOUT_SECONDS), metering_token_source(config)


def next_prompt_suggestion(job_store, username: str, req: NextPromptRequest) -> str:
    """Write the user's likely next message, or ``""`` on any block or failure.

    A suggestion is optional help, so a blocked balance, an unroutable model
    or a failed call returns nothing instead of an error.

    Args:
        job_store: Job store whose engine backs billing.
        username: The caller, who pays for the call.
        req: The conversation and recent actions.

    Returns:
        The suggestion, or ``""``.
    """
    if not req.turns or req.turns[-1].role != "assistant" or not req.turns[-1].content.strip():
        return ""
    try:
        enforce_llm_balance(job_store, username)
        lm, token_source = _next_prompt_lm(job_store, username, req.model)
    except DomainError:
        return ""
    except Exception:
        logger.warning("next-message suggestion model unavailable", exc_info=True)
        return ""
    if lm is None:
        logger.info("next-message suggestion skipped: the engine has no model to run it")
        return ""
    # Subscription calls are metered under the legacy "managed" source at zero
    # cost; the log names who really pays.
    payer = "subscription" if getattr(lm, "subscription", False) else token_source
    logger.info("next-message suggestion on %s (%s)", getattr(lm, "model", "?"), payer)
    try:
        return suggest_next_prompt(
            lm,
            [(turn.role, turn.content) for turn in req.turns],
            req.actions,
            _reply_language(req.locale),
        )
    except Exception:
        logger.warning("next-message suggestion failed", exc_info=True)
        return ""
    finally:
        meter_llm_run(
            getattr(job_store, "engine", None),
            username,
            [lm],
            description="Agent suggestion",
            token_source=token_source,
        )


def _derive_title(user_message: str) -> str:
    """Auto-title a fresh conversation from the user's opening message.

    Truncates whitespace-collapsed text to ``TITLE_MAX_CHARS`` and appends an
    ellipsis when truncation actually loses characters. Attached-file and
    pasted-text blocks are left out; a message with only files is titled
    from the first file's name.

    Args:
        user_message: The first user turn's text.

    Returns:
        A short, single-line title suitable for the conversation header.
    """
    typed = _COMPOSER_BLOCK_RE.sub(" ", user_message)
    if not typed.strip():
        files = [m.group(1) for m in _COMPOSER_BLOCK_RE.finditer(user_message) if m.group(1)]
        typed = files[0] if files else ""
    collapsed = " ".join(typed.split())
    if len(collapsed) <= TITLE_MAX_CHARS:
        return collapsed
    return collapsed[: TITLE_MAX_CHARS - 1].rstrip() + "…"


def _ensure_conversation(job_store, conversation_id: str | None, username: str, user_message: str) -> tuple[str, str]:
    """Create a new conversation row when one isn't supplied; touch existing rows.

    A fresh conversation gets an auto-derived title from the user's first
    message; an existing one keeps its (possibly user-renamed) title and only
    has ``updated_at`` bumped.

    Args:
        job_store: Job-store instance whose engine backs the DB session.
        conversation_id: Optional caller-supplied conversation id.
        username: Authenticated principal that owns the row.
        user_message: First user message (used for auto-title on creation).

    Returns:
        ``(conversation_id, title)`` — the id is freshly generated when input
        was ``None``; ``title`` is the post-state value.

    Raises:
        DomainError: 403 when ``conversation_id`` exists but is owned by a
            different user; 404 when an explicit id is unknown.
    """
    now = datetime.now(UTC)
    with Session(job_store.engine) as session:
        if conversation_id:
            row = session.get(AgentConversationModel, conversation_id)
            if row is None:
                raise DomainError("agent.conversation.not_found", status=404)
            if row.username != username:
                raise DomainError("agent.conversation.forbidden", status=403)
            row.updated_at = cast(Any, now)
            session.commit()
            return cast(str, row.id), cast(str, row.title)
        new_id = str(uuid4())
        title = _derive_title(user_message)
        row = AgentConversationModel(
            id=new_id,
            username=username,
            title=title,
            pinned=False,
            archived_at=None,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.commit()
        return new_id, title


def _persist_user_turn(job_store, conversation_id: str, content: str) -> None:
    """Insert the user's message into ``agent_messages``.

    Args:
        job_store: Job-store instance whose engine backs the DB session.
        conversation_id: Owning conversation id.
        content: User message text exactly as received from the client.
    """
    with Session(job_store.engine) as session:
        session.add(
            AgentMessageModel(
                conversation_id=conversation_id,
                role="user",
                content=content,
                tool_calls=None,
                model=None,
                created_at=datetime.now(UTC),
            )
        )
        session.commit()


def _discard_latest_turn(job_store, conversation_id: str) -> None:
    """Delete the final persisted user turn and every response after it.

    The regenerate UI replaces the latest assistant response in memory. Remove
    the matching persisted suffix before saving its replacement so reopening
    the conversation does not reveal duplicate copies of the same turn. A
    user-only suffix is also removed, which covers retrying a failed stream
    before an assistant row was written.

    Args:
        job_store: Job-store instance whose engine backs the DB session.
        conversation_id: Conversation whose latest turn should be replaced.
    """
    with Session(job_store.engine) as session:
        latest_user = (
            session.query(AgentMessageModel)
            .filter(
                AgentMessageModel.conversation_id == conversation_id,
                AgentMessageModel.role == "user",
            )
            .order_by(AgentMessageModel.id.desc())
            .first()
        )
        if latest_user is None:
            return
        session.query(AgentMessageModel).filter(
            AgentMessageModel.conversation_id == conversation_id,
            AgentMessageModel.id >= latest_user.id,
        ).delete(synchronize_session=False)
        session.commit()


def _persist_assistant_turn(
    job_store,
    conversation_id: str,
    content: str,
    tool_calls: list[dict[str, Any]],
    model: str | None,
    *,
    wizard_state_before: dict[str, Any] | None = None,
    wizard_state_after: dict[str, Any] | None = None,
    allowed_tools: list[str] | None = None,
    tool_schema_hashes: dict[str, str] | None = None,
    router_metadata: dict[str, Any] | None = None,
) -> int:
    """Insert the assistant's completed turn (with tool-call payloads).

    Called once, from the SSE wrapper, when the upstream ``done`` event fires.
    ``tool_calls`` carries the fully-resolved tool history accumulated from
    the ``tool_start`` / ``tool_end`` SSE events so the frontend can rehydrate
    the panel from this row alone.

    Args:
        job_store: Job-store instance whose engine backs the DB session.
        conversation_id: Owning conversation id.
        content: Final assistant message text.
        tool_calls: Accumulated tool-call records (matching ``AgentToolCall``).
        model: Model identifier reported by the agent runtime, when known.
        wizard_state_before: Wizard snapshot at turn start (training metadata).
        wizard_state_after: Wizard snapshot at turn end (training metadata).
        allowed_tools: Tool names exposed to the agent this turn.
        tool_schema_hashes: ``{tool_name: sha256(schema_json)}`` snapshot.
        router_metadata: OpenRouter upstream id + served-by host + latency.
            ``None`` until the runtime captures it (see spec §4).

    Returns:
        The new message's id.
    """
    now = datetime.now(UTC)
    with Session(job_store.engine) as session:
        message = AgentMessageModel(
            conversation_id=conversation_id,
            role="assistant",
            content=content,
            tool_calls=tool_calls or None,
            model=model,
            wizard_state_before=wizard_state_before,
            wizard_state_after=wizard_state_after,
            allowed_tools=allowed_tools,
            tool_schema_hashes=tool_schema_hashes,
            router_metadata=router_metadata,
            created_at=now,
        )
        session.add(message)
        row = session.get(AgentConversationModel, conversation_id)
        if row is not None:
            row.updated_at = cast(Any, now)
        session.commit()
        message_id = message.id
    # Refresh the haystack embedding so the next search hit reflects the
    # turn we just persisted. Runs on a daemon thread and is best-effort —
    # the startup backfill heals any failures on the next deploy.
    queue_conversation_embed(conversation_id, engine=job_store.engine)
    return message_id


def _save_late_metadata(job_store, message_id: int, updates: dict[str, Any]) -> None:
    """Replace keys of a saved turn's metadata, for labels and stats that arrived after its ``done``.

    Args:
        job_store: Job-store instance whose engine backs the DB session.
        message_id: The assistant message the metadata belongs to.
        updates: The keys to replace, e.g. ``thinking`` and ``stats``.
    """
    with Session(job_store.engine) as session:
        message = session.get(AgentMessageModel, message_id)
        if message is None:
            return
        message.router_metadata = {**(message.router_metadata or {}), **updates}
        session.commit()


def _labelled(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Give every row without a label one cut from its reasoning.

    A stream dropped before its last labels arrived would otherwise save
    rows that reopen without one.

    Args:
        rows: The turn's thinking rows.

    Returns:
        The rows, each with a ``summary`` when its reasoning has any text.
    """
    out = []
    for row in rows:
        if not row.get("summary"):
            label = fallback_summary(str(row.get("reasoning") or ""))
            if label:
                row = {**row, "summary": label}
        out.append(row)
    return out


async def _wrap_with_persistence(
    source: AsyncIterator[dict[str, Any]],
    *,
    job_store,
    conversation_id: str | None,
    title: str | None,
    wizard_state_before: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Mirror upstream SSE events, accumulate state, persist on ``done``.

    Prepended with one ``conversation_meta`` event when persistence is on so
    the client can stash the canonical id (especially for newly-created
    threads). Accumulates the assistant text and every settled tool-call so a
    single ``agent_messages`` row captures the whole turn.

    Also captures the four training-metadata fields the optimize CLI needs
    (training_ground_SPEC.md §4): ``wizard_state_before`` from the caller,
    ``allowed_tools`` and ``tool_schema_hashes`` from the runtime's
    ``turn_metadata`` event, and ``wizard_state_after`` by merging each
    tool's ``result.wizard_state`` patch onto the running snapshot. Drops
    the ``turn_metadata`` event before forwarding so the frontend doesn't
    have to learn a new SSE schema.

    Args:
        source: Upstream async event stream from ``run_generalist_agent``.
        job_store: Job-store, or ``None`` when persistence is disabled.
        conversation_id: Persisted conversation id, or ``None`` to passthrough.
        title: Current conversation title (emitted in ``conversation_meta``).
        wizard_state_before: Wizard snapshot the caller handed to the agent
            this turn — persisted verbatim alongside the row.

    Yields:
        The same ``{event, data}`` mappings the SSE serializer expects, with
        the leading ``conversation_meta`` envelope when persistence is active.
    """
    if conversation_id and job_store is not None:
        yield {
            "event": "conversation_meta",
            "data": {"conversation_id": conversation_id, "title": title or ""},
        }

    assistant_buf: list[str] = []
    tool_calls: dict[str, dict[str, Any]] = {}
    tool_order: list[str] = []
    model_used: str | None = None
    served_model_used: str | None = None
    turn_stats: dict[str, Any] | None = None
    plan: str | None = None
    thinking = ThinkingTrail()
    allowed_tools: list[str] | None = None
    tool_schema_hashes: dict[str, str] | None = None
    wizard_state_after: dict[str, Any] = dict(wizard_state_before) if wizard_state_before else {}
    persisted = False
    saved_message_id: int | None = None
    late_labels = False
    late_stats: dict[str, Any] | None = None

    async def _do_persist(content: str) -> None:
        """Write the accumulated turn exactly once; never raise.

        The synchronous psycopg2 write is offloaded to a worker thread so it
        does not block the event loop while the SSE stream is still draining.

        Args:
            content: Final assistant message text for the row.
        """
        nonlocal persisted, saved_message_id
        if persisted or not (conversation_id and job_store is not None):
            return
        ordered_tools = [tool_calls[tid] for tid in tool_order if tid in tool_calls]
        try:
            saved_message_id = await asyncio.to_thread(
                _persist_assistant_turn,
                job_store,
                conversation_id,
                content,
                ordered_tools,
                model_used,
                wizard_state_before=wizard_state_before,
                wizard_state_after=wizard_state_after or None,
                allowed_tools=allowed_tools,
                tool_schema_hashes=tool_schema_hashes,
                router_metadata={
                    key: value
                    for key, value in (
                        ("served_model", served_model_used),
                        ("stats", turn_stats),
                        ("plan", plan),
                        ("thinking", _labelled(thinking.rows())),
                    )
                    if value
                }
                or None,
            )
        except Exception:
            logger.exception("Failed to persist assistant turn")
        persisted = True

    try:
        async for event in source:
            name = event.get("event")
            data = event.get("data") or {}
            thinking.observe(event)
            if name == "message_patch":
                chunk = data.get("chunk")
                if isinstance(chunk, str):
                    assistant_buf.append(chunk)
            elif name == "message_reset":
                # The text so far prefaced a tool call; salvaging it on a
                # dropped stream would persist narration as the reply.
                assistant_buf.clear()
            elif name == "plan":
                raw_plan = data.get("text")
                if isinstance(raw_plan, str) and raw_plan.strip():
                    plan = raw_plan.strip()
            elif name == "turn_metadata":
                raw_allowed = data.get("allowed_tools")
                if isinstance(raw_allowed, list):
                    allowed_tools = [str(t) for t in raw_allowed]
                raw_hashes = data.get("tool_schema_hashes")
                if isinstance(raw_hashes, dict):
                    tool_schema_hashes = {str(k): str(v) for k, v in raw_hashes.items()}
                # Internal envelope — never forward to the frontend.
                continue
            elif name == STEER_APPLIED_EVENT:
                # The steered message is a user turn of this conversation; it
                # goes in now so it sits before the reply written on ``done``.
                if conversation_id and job_store is not None and isinstance(data.get("text"), str):
                    try:
                        await asyncio.to_thread(_persist_user_turn, job_store, conversation_id, data["text"])
                    except Exception:
                        logger.exception("Failed to persist a steered user message")
            elif name == "tool_start":
                tid = str(data.get("id", ""))
                if tid:
                    tool_calls[tid] = {
                        "id": tid,
                        "tool": data.get("tool", ""),
                        "reason": data.get("reason", ""),
                        "status": "running",
                        "startedAt": int(datetime.now(UTC).timestamp() * 1000),
                        "endedAt": None,
                        "payload": {"arguments": data.get("arguments", {})},
                    }
                    tool_order.append(tid)
            elif name == "tool_end":
                tid = str(data.get("id", ""))
                existing = tool_calls.get(tid)
                if existing is not None:
                    existing["status"] = "done" if data.get("status") == "ok" else "error"
                    existing["endedAt"] = int(datetime.now(UTC).timestamp() * 1000)
                    payload = existing.get("payload") or {}
                    result = data.get("result")
                    payload["result"] = result
                    existing["payload"] = payload
                    _merge_wizard_patch(wizard_state_after, result)
            elif name == "done":
                final_text = data.get("assistant_message")
                content = final_text if isinstance(final_text, str) and final_text else "".join(assistant_buf)
                raw_model = data.get("model")
                model_used = raw_model if isinstance(raw_model, str) and raw_model else None
                raw_served = data.get("served_model")
                served_model_used = raw_served if isinstance(raw_served, str) and raw_served else None
                raw_stats = data.get("stats")
                turn_stats = raw_stats if isinstance(raw_stats, dict) else None
                await _do_persist(content)
            elif name == "thinking_summary" and persisted:
                late_labels = True
            elif name == "turn_stats" and persisted and isinstance(data.get("stats"), dict):
                late_stats = data["stats"]
            yield event
    finally:
        late: dict[str, Any] = {}
        if late_labels:
            late["thinking"] = _labelled(thinking.rows())
        if late_stats:
            late["stats"] = late_stats
        if late and saved_message_id is not None and job_store is not None:
            try:
                await asyncio.to_thread(_save_late_metadata, job_store, saved_message_id, late)
            except Exception:
                logger.exception("Failed to save the turn's late labels and stats")
        # The frontend tears down the SSE stream the moment ``submit_job_run_post``
        # succeeds, so ``done`` often never arrives and the wrapped generator is
        # closed (GeneratorExit / CancelledError). Salvage the turn here when it
        # carries real content; an empty greeting turn writes nothing.
        if not persisted and (assistant_buf or any(c["status"] in ("done", "error") for c in tool_calls.values())):
            await _do_persist("".join(assistant_buf))


def _merge_wizard_patch(state: dict[str, Any], result: Any) -> None:
    """Merge a tool result's ``wizard_state`` patch into the running state.

    Tools that mutate the wizard (``update_wizard_state``,
    ``set_column_roles``, …) echo the validated patch under
    ``result.wizard_state``. We treat the patch as a shallow overlay so the
    persisted ``wizard_state_after`` reflects every change the agent
    successfully made this turn. Tool results that don't carry the field
    leave the state untouched.

    Args:
        state: Running ``wizard_state_after`` buffer; mutated in place.
        result: Raw tool result payload from the ``tool_end`` SSE event.
    """
    if not isinstance(result, dict):
        return
    patch = result.get("wizard_state")
    if not isinstance(patch, dict):
        return
    for key, value in patch.items():
        state[str(key)] = value


def create_generalist_agent_router(*, job_store=None) -> APIRouter:
    """Mount the ``/optimizations/generalist-agent`` SSE + confirm endpoints.

    Args:
        job_store: Optional job-store whose engine backs conversation
            persistence. When ``None``, the SSE endpoint streams without
            writing anything (legacy behavior).

    Returns:
        A configured :class:`APIRouter` with the generalist-agent endpoints attached.
    """
    router = APIRouter()

    # The shared DB engine backs cross-replica approval handoff: a confirm
    # that lands on a replica that doesn't hold the stream's in-process future
    # persists the decision for the owning replica's poll loop.
    engine = getattr(job_store, "engine", None) if job_store is not None else None
    if engine is not None:
        get_approval_registry().bind_engine(engine)
        get_steer_store().bind_engine(engine)

    @router.post(
        "/optimizations/generalist-agent",
        summary="Stream generalist-agent events for one user turn",
    )
    async def generalist_agent(
        req: GeneralistAgentRequest,
        current_user: AuthenticatedUserDep,
        authorization: str | None = Header(default=None),
    ) -> StreamingResponse:
        """Stream the generalist agent's reasoning, tool calls, and reply as SSE.

        Event types: ``conversation_meta`` (only when persistence is on),
        ``reasoning_patch``, ``tool_start``, ``tool_end``, ``status_patch``,
        ``pending_approval``, ``approval_resolved``, ``message_patch``,
        ``message_reset``, ``done``, ``error``.

        Args:
            req: Request body with user message, chat history, wizard
                snapshot, trust mode, and optional ``conversation_id``.
            current_user: The authenticated caller. Required — the route 401s
                before any streaming or LLM work when auth is missing/invalid,
                and persisted conversations are attributed to this user.
            authorization: Caller's bearer token, forwarded into the agent's
                MCP session so its tool calls authenticate against
                ``get_authenticated_user`` on the same FastAPI app.

        Returns:
            A :class:`StreamingResponse` of Server-Sent Events.
        """

        def _setup_turn() -> tuple[str | None, str | None]:
            """Persist the user turn off the event loop.

            Runs the blocking conversation upsert and user-message insert in a
            worker thread so the single event loop is not stalled before the SSE
            stream starts. Auth is already enforced by the route dependency, so
            the turn is always attributed to ``current_user``; a conversation
            ``DomainError`` (403/404) is allowed to propagate.

            Returns:
                ``(conversation_id, title)`` — both ``None`` when persistence is
                off (no ``job_store``) or failed non-fatally.
            """
            if job_store is None:
                return None, None
            try:
                cid, ttl = _ensure_conversation(job_store, req.conversation_id, current_user.username, req.user_message)
                if req.regenerate:
                    _discard_latest_turn(job_store, cid)
                _persist_user_turn(job_store, cid, req.user_message)
            except DomainError:
                raise
            except Exception:
                logger.exception("Failed to persist user turn")
                return None, None
            return cid, ttl

        await asyncio.to_thread(enforce_llm_balance, job_store, current_user.username)
        conversation_id, title = await asyncio.to_thread(_setup_turn)

        wizard_state: WizardState = {**req.wizard_state}  # type: ignore[typeddict-item]
        picked = await asyncio.to_thread(engine_agent_model, job_store, current_user.username, req.model)
        model_config = await asyncio.to_thread(
            route_local_byok_model, picked, job_store=job_store, username=current_user.username
        ) or route_agent_model(picked)
        # A CLI takes its own effort ladder, not the catalog's, and with no
        # pick it runs its own default. The CLI dispatcher drops a level or
        # fast mode the model does not take.
        on_cli = model_config.token_source == "subscription"
        if on_cli:
            effort = req.reasoning_effort
        else:
            hosted_pick = None if req.reasoning_effort in get_args(CliTopEffort) else req.reasoning_effort
            effort = effective_reasoning_effort(model_config.name, hosted_pick)
        if effort:
            model_config = model_config.model_copy(update={"extra": {**model_config.extra, "reasoning_effort": effort}})
        if on_cli and req.fast_mode:
            body = model_config.extra.get("extra_body")
            fast_body = {**(body if isinstance(body, dict) else {}), CLI_FAST_KEY: True}
            model_config = model_config.model_copy(update={"extra": {**model_config.extra, "extra_body": fast_body}})
        usage_sink: list = []
        source = run_generalist_agent(
            wizard_state=wizard_state,
            chat_history=history_for_agent(req.chat_history),
            user_message=req.user_message,
            trust_mode=req.trust_mode,
            auth_header=authorization,
            approval_owner=current_user.username,
            locale=req.locale,
            model_config=model_config,
            usage_sink=usage_sink,
            steer_key=req.steer_key,
        )
        metered = stream_with_llm_metering(
            source,
            job_store=job_store,
            username=current_user.username,
            description="Agent chat",
            usage_sink=usage_sink,
            token_source=metering_token_source(model_config),
        )
        wrapped = _wrap_with_persistence(
            metered,
            job_store=job_store,
            conversation_id=conversation_id,
            title=title,
            wizard_state_before=dict(wizard_state),
        )
        return StreamingResponse(
            sse_from_events(wrapped),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @router.post(
        "/optimizations/generalist-agent/confirm",
        response_model=ConfirmApprovalResponse,
        summary="Resolve a pending generalist-agent approval",
    )
    def confirm_approval(req: ConfirmApprovalRequest, current_user: AuthenticatedUserDep) -> ConfirmApprovalResponse:
        """Resolve an outstanding approval from the client.

        Args:
            req: Confirm payload with the ``call_id`` and approval boolean.
            current_user: The authenticated caller. Pending approvals are keyed
                to the stream's owner, so a confirm from any other account
                addresses a different key and cannot resolve this one.

        Returns:
            A :class:`ConfirmApprovalResponse` with ``resolved=True`` on success.

        Raises:
            DomainError: 404 when the call id is unknown locally and no durable
                store is bound (store-less deployments) — the client surfaces
                it as a UI warning. With a store, an unmatched confirm is
                persisted for the replica that owns the stream to pick up.
        """
        resolved = get_approval_registry().resolve_or_persist(
            approval_key(req.call_id, current_user.username), req.approved
        )
        if not resolved:
            raise DomainError("agent.approval.unknown_call_id", status=404)
        return ConfirmApprovalResponse(resolved=True)

    @router.post(
        "/optimizations/agent-steer",
        response_model=SteerMessageResponse,
        summary="Send a message into a running agent turn",
    )
    def post_steer(req: SteerMessageRequest, current_user: AuthenticatedUserDep) -> SteerMessageResponse:
        """Store a message for the caller's turn running under ``steer_key``.

        The turn reads it at its next step and announces that with a
        ``steer_applied`` event. Works for the generalist agent and the code,
        black-box and workflow assistants.

        Args:
            req: The turn's key and the message.
            current_user: The authenticated caller; only their own turns read it.

        Returns:
            The stored message's id.
        """
        return SteerMessageResponse(id=get_steer_store().post(current_user.username, req.steer_key, req.text))

    @router.post(
        "/optimizations/agent-steer/withdraw",
        response_model=SteerWithdrawResponse,
        summary="Take back steer messages a turn never read",
    )
    def withdraw_steer(req: SteerWithdrawRequest, current_user: AuthenticatedUserDep) -> SteerWithdrawResponse:
        """Take back messages the turn under ``steer_key`` has not read.

        Called when the turn ends, and when the user deletes, edits or
        reorders a steered message; each message goes either to the turn or
        back here, never both.

        Args:
            req: The turn's key, and optionally which messages to take back.
            current_user: The authenticated caller.

        Returns:
            The ids of the messages returned, oldest first.
        """
        taken = get_steer_store().take(current_user.username, req.steer_key, req.ids)
        return SteerWithdrawResponse(ids=[message_id for message_id, _ in taken])

    @router.post(
        "/optimizations/agent-suggest",
        response_model=NextPromptResponse,
        summary="Suggest the user's next message to an agent",
    )
    async def suggest_next_message(req: NextPromptRequest, current_user: AuthenticatedUserDep) -> NextPromptResponse:
        """Suggest what the user would likely send next, for the composer's Tab-to-accept hint.

        Works for every agent composer: the generalist agent, the screen
        interviews, the tagger interview and the code assistants. Runs on the
        small model that labels thinking, metered like it, and returns an
        empty suggestion instead of an error when anything blocks it.

        Args:
            req: The conversation, recent actions, locale and model pick.
            current_user: The authenticated caller, who pays for the call.

        Returns:
            The suggestion, or an empty one.
        """
        suggestion = await asyncio.to_thread(next_prompt_suggestion, job_store, current_user.username, req)
        return NextPromptResponse(suggestion=suggestion)

    return router
