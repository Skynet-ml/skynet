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
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from functools import partial
from typing import Annotated, Any, Literal, cast
from uuid import uuid4

import anyio
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse

from ...billing.cli_dispatch import CHAT_TITLE_ROLE, NEXT_PROMPT_ROLE
from ...billing.metering import estimate_run_cents, meter_llm_run
from ...config import settings
from ...connectors import github
from ...connectors.vault import ConnectorVault
from ...constants import TOKEN_SOURCE_MANAGED
from ...local.cli_catalog import RetiredEffort
from ...models import ModelConfig
from ...service_gateway.agents import compaction
from ...service_gateway.agents.chat_title import with_title_event, write_chat_title
from ...service_gateway.agents.code import _reply_language
from ...service_gateway.agents.generalist import (
    ThinkingTrail,
    TrustMode,
    WizardState,
    approval_key,
    build_hosted_suggest_lm,
    build_local_suggest_lm,
    build_turn_lm,
    fallback_summary,
    get_approval_registry,
    run_generalist_agent,
)
from ...service_gateway.agents.next_prompt import suggest_next_prompt
from ...service_gateway.agents.steering import (
    STEER_APPLIED_EVENT,
    STEER_KEY_MAX_CHARS,
    STEER_TEXT_MAX_CHARS,
    SteerInputError,
    SteerLimitError,
    get_steer_store,
)
from ...service_gateway.embedding_pipeline import queue_conversation_embed
from ...service_gateway.language_models import usage_by_model_from_history
from ...storage.models import AgentConversationModel, AgentMessageModel
from ..agent_engine import engine_agent_model
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..model_catalog import ReasoningEffort, catalog_snapshot
from ..model_fast_mode import with_fast_mode
from ..model_router import effective_reasoning_effort, metering_token_source, route_agent_model, route_local_byok_model
from ..suggest_model import hosted_suggest_model
from ._helpers import enforce_llm_balance, sse_from_events, stream_with_llm_metering
from .connectors import connector_secret

logger = logging.getLogger(__name__)
# A suggestion has a larger prompt than a thinking label, and on a CLI engine a
# process start alone takes 3-7 s, so the label's 8 s limit made every CLI
# suggestion time out. The CLI side stops its run at 25 s.
# The chat title call uses this timeout too (_ai_title → _next_prompt_lm).
# Keep in sync with TITLE_RELOAD_GAPS_MS in frontend/src/features/agent-panel/lib/title-reload.ts.
_SUGGEST_TIMEOUT_SECONDS = 30.0

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]

TITLE_MAX_CHARS = 40
# Composer blocks for attached files and long pastes (frontend
# ``shared/ui/agent/pasted-text.ts``); their bodies are not the user's words.
_COMPOSER_BLOCK_RE = re.compile(
    r'<pasted_text>.*?</pasted_text>|<attached_file name="([^"]*)">.*?</attached_file>', re.DOTALL
)
# Hidden setup wizard turns (frontend ``features/agent-panel/lib/screen-agent.ts``).
# The kickoff holds no user words; a Back turn carries the agent's request.
_WIZARD_KICKOFF_MARK = "[The wizard opened this screen; I typed nothing.]"
_WIZARD_BACK_MARK = "[The wizard went back to this screen for your request; I typed nothing.]"

NEXT_PROMPT_MAX_TURNS = 100
NEXT_PROMPT_MAX_ACTIONS = 20


# One tool call an earlier assistant turn made, as the panel recorded it.
class ChatToolCall(BaseModel):
    tool: str = Field(..., max_length=200, description="MCP tool name.")
    status: str = Field(default="done", max_length=40, description="'done' or 'error'.")
    result: str | None = Field(
        default=None, description="Tool result text, in full. Compaction clears old results near the context window."
    )


class ChatTurn(BaseModel):
    """A single prior turn in the agent conversation."""

    role: str = Field(..., description="'user' or 'assistant'.")
    content: str = Field(..., description="Message text.")
    id: int | None = Field(
        default=None,
        description="The saved message id, when the chat is saved. A compaction is stored on the user message with this id.",
    )
    tool_calls: list[ChatToolCall] = Field(
        default_factory=list,
        description="Assistant turns only: the tool calls that turn made, oldest first.",
    )


def history_for_agent(turns: list[ChatTurn]) -> list[dict[str, Any]]:
    """Copy each prior turn in full and attach each assistant turn's tool trace.

    No turn is cut here. The history is re-sent in full every turn, and
    ``agents.compaction`` alone bounds it: it clears old tool results first,
    then summarizes old turns.

    Without the trace the model only sees its own prose from earlier turns, so
    it cannot tell which id a past submit returned or why a past call failed.

    Args:
        turns: The client-supplied prior turns, oldest first.

    Returns:
        One ``{role, content}`` dict for each turn, each assistant turn that
        called tools carrying a ``tools`` list.
    """
    history: list[dict[str, Any]] = []
    for turn in turns:
        entry: dict[str, Any] = {"role": turn.role, "content": turn.content}
        if turn.role == "assistant" and turn.tool_calls:
            entry["tools"] = [
                {
                    "tool": call.tool,
                    "status": call.status,
                    "result": call.result or "",
                }
                for call in turn.tool_calls
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
            "Replace the conversation's final user/assistant turn when its user text equals "
            "``user_message``; otherwise append the turn. With ``edit_user_text``, apply only "
            "when the edited turn is not found."
        ),
    )
    edit_user_text: str | None = Field(
        default=None,
        description=(
            "Original text of the user turn that this message edits. The stored copy of that turn and "
            "every later message of the conversation are deleted before the new turn is saved."
        ),
    )
    edit_user_occurrence: int = Field(
        default=0,
        ge=0,
        description=(
            "How many earlier user turns in the chat have the same text as the edited turn. It selects "
            "the edited turn when several stored turns share its text."
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
            "Run this turn in fast mode on a supported local CLI or OpenRouter priority endpoint. "
            "Unsupported models ignore this flag."
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
    persist: bool = Field(
        default=True,
        description=(
            "Save the turn as a conversation. False runs the turn without saving: the server ignores "
            "``conversation_id``, ``regenerate`` and the edit fields, stores no row and emits no "
            "``conversation_meta`` event. The setup wizard's chat sends False."
        ),
    )
    compaction_summary: str | None = Field(
        default=None,
        max_length=compaction.SUMMARY_MAX_CHARS,
        description=(
            "Summary of the turns before ``chat_history`` from the chat's latest compaction. The "
            "agent gets it in place of those turns, and the next compaction updates it."
        ),
    )


# Input for a compaction the user asked for with /compact.
class CompactChatRequest(BaseModel):
    chat_history: list[ChatTurn] = Field(..., description="The turns after the latest compaction, oldest first.")
    compaction_summary: str | None = Field(
        default=None, max_length=compaction.SUMMARY_MAX_CHARS, description="Summary from the chat's latest compaction."
    )
    instructions: str | None = Field(
        default=None, max_length=2000, description="What the summary should focus on (the /compact argument)."
    )
    conversation_id: str | None = Field(default=None, description="Saved conversation that stores the compaction.")
    model: str | None = Field(default=None, description="LiteLLM id of the chat's model; absent = default.")
    reasoning_effort: ReasoningEffort | RetiredEffort | None = Field(
        default=None, description="The chat's reasoning-effort level; absent keeps the model's default."
    )
    fast_mode: bool = Field(default=False, description="The chat's fast mode.")


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


# Delivered ids preserve the outcome when a withdrawal response needs a retry.
class SteerWithdrawResponse(BaseModel):
    """Messages returned to the client, oldest first."""

    ids: list[str]
    delivered: list[str] = Field(default_factory=list, description="Ids already read by the agent, oldest first.")


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


def _next_prompt_lm(
    job_store, username: str, model: str | None, role: str = NEXT_PROMPT_ROLE
) -> tuple[Any | None, str]:
    """Build the small model a suggestion runs on; never the composer, interview or agent model.

    Hosted, it is the scored suggestion model (see
    :func:`~core.api.suggest_model.hosted_suggest_model`). In the Mac app, the
    composer pick only selects the engine; the suggestion runs on that
    engine's smallest model.

    Args:
        job_store: Job store whose engine holds the caller's engine setting and saved keys.
        username: The caller.
        model: The composer's model pick.
        role: The CLI side-call role in the Mac app. The chat title passes
            :data:`CHAT_TITLE_ROLE`, so it does not share the suggestion's pool.

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
    return build_local_suggest_lm(config, _SUGGEST_TIMEOUT_SECONDS, role), metering_token_source(config)


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
    from the first file's name. A setup wizard kickoff gives no title, and a
    wizard Back turn is titled from its request without the marker.

    Args:
        user_message: The first user turn's text.

    Returns:
        A short, single-line title suitable for the conversation header.
    """
    if user_message.lstrip().startswith(_WIZARD_KICKOFF_MARK):
        return ""
    user_message = user_message.lstrip().removeprefix(_WIZARD_BACK_MARK)
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


def _save_ai_title(job_store, conversation_id: str, cut_title: str, title: str) -> bool:
    """Replace the cut title with the model's title, unless the user renamed the chat.

    Args:
        job_store: Job-store instance whose engine backs the DB session.
        conversation_id: The new conversation.
        cut_title: The title cut from the first message, stored at creation.
        title: The model's title.

    Returns:
        Whether the title was saved.
    """
    with Session(job_store.engine) as session:
        # Compare and set in one statement: a rename that lands while the
        # model writes changes the row, so the update matches nothing.
        saved = (
            session.query(AgentConversationModel)
            .filter(AgentConversationModel.id == conversation_id, AgentConversationModel.title == cut_title)
            .update({AgentConversationModel.title: title}, synchronize_session=False)
        )
        session.commit()
    return saved == 1


def _ai_title(
    job_store, username: str, model: str | None, conversation_id: str, user_message: str, cut_title: str
) -> str:
    """Write and save a model title for a new chat; ``""`` on any block or failure.

    It runs on the next-message suggestion's small model, with the same
    balance gate, BYOK routing and metering, so the cut title stays when
    anything blocks it.

    Args:
        job_store: Job store whose engine backs billing and the conversation row.
        username: The caller, who pays for the call.
        model: The composer's model pick; only the Mac app reads it.
        conversation_id: The new conversation.
        user_message: The first user message.
        cut_title: The title cut from the first message.

    Returns:
        The saved title, or ``""`` when the cut title stays.
    """
    typed = " ".join(_COMPOSER_BLOCK_RE.sub(" ", user_message.lstrip().removeprefix(_WIZARD_BACK_MARK)).split())
    if not typed:
        return ""
    try:
        enforce_llm_balance(job_store, username)
        lm, token_source = _next_prompt_lm(job_store, username, model, CHAT_TITLE_ROLE)
    except DomainError:
        return ""
    except Exception:
        logger.warning("chat title model unavailable", exc_info=True)
        return ""
    if lm is None:
        return ""
    try:
        title = write_chat_title(lm, typed)
    except Exception:
        logger.warning("chat title failed", exc_info=True)
        title = ""
    finally:
        meter_llm_run(
            getattr(job_store, "engine", None),
            username,
            [lm],
            description="Agent chat title",
            token_source=token_source,
        )
    if not title or title == cut_title:
        return ""
    try:
        return title if _save_ai_title(job_store, conversation_id, cut_title, title) else ""
    except Exception:
        logger.warning("chat title save failed", exc_info=True)
        return ""


# Holds each running title task so the event loop does not drop it when the
# stream that started it closes first; the thread still saves the title.
_TITLE_TASKS: set[asyncio.Future[str]] = set()


def _start_title_task(
    job_store, username: str, model: str | None, conversation_id: str, user_message: str, cut_title: str
) -> asyncio.Future[str]:
    """Start :func:`_ai_title` in a worker thread, in parallel with the turn.

    Args:
        job_store: Job store whose engine backs billing and the conversation row.
        username: The caller.
        model: The composer's model pick.
        conversation_id: The new conversation.
        user_message: The first user message.
        cut_title: The title cut from the first message.

    Returns:
        The task; it resolves to the saved title or ``""``.
    """
    task = asyncio.ensure_future(
        asyncio.to_thread(_ai_title, job_store, username, model, conversation_id, user_message, cut_title)
    )
    _TITLE_TASKS.add(task)
    task.add_done_callback(_TITLE_TASKS.discard)
    return task


def _persist_user_turn(
    job_store, conversation_id: str, content: str, *, steer: bool = False, compaction: dict[str, Any] | None = None
) -> int:
    """Insert the user's message into ``agent_messages``.

    Args:
        job_store: Job-store instance whose engine backs the DB session.
        conversation_id: Owning conversation id.
        content: User message text exactly as received from the client.
        steer: True for a message sent while a turn runs. A run's setup chat
            keeps a steer as part of that turn, not as a later turn.
        compaction: The compaction entry of the turn this message replaces, if any.

    Returns:
        The id of the new row.
    """
    metadata: dict[str, Any] = {"steer": True} if steer else {}
    if compaction:
        metadata["compaction"] = compaction
    with Session(job_store.engine) as session:
        message = AgentMessageModel(
            conversation_id=conversation_id,
            role="user",
            content=content,
            tool_calls=None,
            model=None,
            router_metadata=metadata or None,
            created_at=datetime.now(UTC),
        )
        session.add(message)
        session.commit()
        return message.id


def _discard_latest_turn(job_store, conversation_id: str, only_if_text: str | None = None) -> dict[str, Any] | None:
    """Delete the final persisted user turn and every response after it.

    The regenerate UI replaces the latest assistant response in memory. Remove
    the matching persisted suffix before saving its replacement so reopening
    the conversation does not reveal duplicate copies of the same turn. A
    user-only suffix is also removed, which covers retrying a failed stream
    before an assistant row was written.

    Args:
        job_store: Job-store instance whose engine backs the DB session.
        conversation_id: Conversation whose latest turn should be replaced.
        only_if_text: When set, delete nothing unless the latest user turn has
            exactly this text.

    Returns:
        The compaction entry stored on the deleted user turn, so its
        replacement keeps the summary and its cost; ``None`` when it had none.
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
        if latest_user is None or (only_if_text is not None and latest_user.content != only_if_text):
            return None
        entry = (latest_user.router_metadata or {}).get("compaction")
        session.query(AgentMessageModel).filter(
            AgentMessageModel.conversation_id == conversation_id,
            AgentMessageModel.id >= latest_user.id,
        ).delete(synchronize_session=False)
        session.commit()
        return entry


def _discard_from_user_turn(
    job_store, conversation_id: str, user_text: str, occurrence: int
) -> tuple[bool, dict[str, Any] | None]:
    """Delete the stored copy of the edited user turn and every message after it.

    Edit-and-resend keeps only the turns before the edited one in memory, so the
    stored history must lose the same suffix; otherwise a reload shows the old
    turn and its replies with the edited copy appended after them. The turn is
    found by its text, not its position: the chat can show turns that were never
    stored (a send refused before it was saved), and the store can hold steered
    turns that never reached the chat. When a missing turn shares the edited
    text, no stored turn matches and nothing is deleted, never a wrong turn.

    Args:
        job_store: Job-store instance whose engine backs the DB session.
        conversation_id: Conversation whose history is cut.
        user_text: Original text of the edited turn.
        occurrence: How many earlier chat user turns have the same text.

    Returns:
        Whether a stored turn matched and was deleted with its suffix, and the
        compaction entry stored on that turn: its summary covers only earlier
        turns, so the edited copy keeps it.
    """
    with Session(job_store.engine) as session:
        edited = (
            session.query(AgentMessageModel.id, AgentMessageModel.router_metadata)
            .filter(
                AgentMessageModel.conversation_id == conversation_id,
                AgentMessageModel.role == "user",
                AgentMessageModel.content == user_text,
            )
            .order_by(AgentMessageModel.id)
            .offset(occurrence)
            .first()
        )
        if edited is None:
            return False, None
        session.query(AgentMessageModel).filter(
            AgentMessageModel.conversation_id == conversation_id,
            AgentMessageModel.id >= edited.id,
        ).delete(synchronize_session=False)
        session.commit()
        return True, (edited.router_metadata or {}).get("compaction")


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
    user_message_id: int | None = None,
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
        user_message_id: The saved row of the turn's user message. The client
            sends it back with that message, so a compaction finds its row.

    Yields:
        The same ``{event, data}`` mappings the SSE serializer expects, with
        the leading ``conversation_meta`` envelope when persistence is active.
    """
    if conversation_id and job_store is not None:
        meta: dict[str, Any] = {"conversation_id": conversation_id, "title": title or ""}
        if user_message_id is not None:
            meta["user_message_id"] = user_message_id
        yield {"event": "conversation_meta", "data": meta}

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
                        steer_id = await asyncio.to_thread(
                            _persist_user_turn, job_store, conversation_id, data["text"], steer=True
                        )
                        # The client sends the id back with the turn, so a later
                        # compaction that keeps from this message can store on it.
                        event = {**event, "data": {**data, "message_id": steer_id}}
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


def _context_window(model_name: str) -> int:
    """Read a model's context window from the model catalog.

    Args:
        model_name: The LiteLLM id the turn runs on.

    Returns:
        The catalog's ``max_input_tokens``, or ``DEFAULT_CONTEXT_WINDOW`` when
        the catalog is not built yet or does not list a window for the model.
    """
    catalog = catalog_snapshot()
    entry = next((m for m in catalog.models if m.value == model_name), None) if catalog else None
    return entry.max_input_tokens if entry and entry.max_input_tokens else compaction.DEFAULT_CONTEXT_WINDOW


def _anchor_by_text(
    turns: list[ChatTurn], cut: int, newest_id: int | None
) -> tuple[str, int | None, int | None, int, int]:
    """Describe the first kept user message for a lookup by text, when the client sent no id for it.

    A message has no id in the browser when its stream stopped before
    ``conversation_meta``, but also when the server never saved it (a refused
    turn, or a stream that failed first). So the search has two bounds: the
    nearest earlier turn with an id, and the nearest later kept user message
    with an id (or the turn's own new row). The browser's messages with the
    same text and no id inside those bounds give the number of rows the
    search must find. A different number means one of them was never saved,
    and the entry is not stored rather than stored on the wrong row.

    Args:
        turns: The client's chat history.
        cut: The index of the first kept turn.
        newest_id: The saved row of the turn's new user message, if any.

    Returns:
        ``(text, after_id, before_id, skip, prior)``: the message text, the id
        the row must be above and the id it must be below (``None`` for no
        bound), how many newer matching rows to skip, and how many older
        matching messages with no id the browser has inside the bounds.
    """
    text = turns[cut].content
    before_id, skip = newest_id, 0
    for turn in turns[cut + 1 :]:
        if turn.role != "user":
            continue
        if turn.id is not None:
            before_id = turn.id
            break
        if turn.content == text:
            skip += 1
    after_id, prior = None, 0
    for turn in reversed(turns[:cut]):
        if turn.id is not None:
            after_id = turn.id
            break
        if turn.role == "user" and turn.content == text:
            prior += 1
    return text, after_id, before_id, skip, prior


def _save_compaction(
    job_store,
    conversation_id: str,
    username: str,
    anchor_id: int | None,
    entry: dict[str, Any],
    by_text: tuple[str, int | None, int | None, int, int] | None = None,
) -> None:
    """Store a compaction on the first user message it keeps.

    A reopened chat then sends history from that message on, with the summary.
    The client sends each saved message's id, so the row is found by id: the
    same text ("yes") can be in the chat more than once. A message with no id
    is found by its text, with ``by_text``.

    Args:
        job_store: Job-store instance whose engine backs the DB session.
        conversation_id: The saved conversation.
        username: The caller; another account's conversation is left alone.
        anchor_id: The id of the first kept user message, or ``None`` when the
            client did not send it.
        entry: The compaction entry, as the ``compaction`` event carries it.
        by_text: The ``_anchor_by_text`` lookup, used when ``anchor_id`` is ``None``.
    """
    with Session(job_store.engine) as session:
        owner = session.get(AgentConversationModel, conversation_id)
        if owner is None or owner.username != username:
            return
        if anchor_id is None and by_text is not None:
            text, after_id, before_id, skip, prior = by_text
            query = session.query(AgentMessageModel.id).filter(
                AgentMessageModel.conversation_id == conversation_id,
                AgentMessageModel.role == "user",
                AgentMessageModel.content == text,
            )
            if after_id is not None:
                query = query.filter(AgentMessageModel.id > after_id)
            if before_id is not None:
                query = query.filter(AgentMessageModel.id < before_id)
            found = [row.id for row in query.order_by(AgentMessageModel.id.desc()).all()]
            # A count that differs from the browser's means a message there was
            # never saved, so no offset names the anchor row with certainty.
            anchor_id = found[skip] if len(found) == prior + 1 + skip else None
        row = session.get(AgentMessageModel, anchor_id) if anchor_id is not None else None
        if row is None or row.conversation_id != conversation_id or row.role != "user":
            logger.warning("No user message %s to store the compaction on in %s", anchor_id, conversation_id)
            return
        row.router_metadata = {**(row.router_metadata or {}), "compaction": entry}
        session.commit()


def _add_side_cents(
    job_store, conversation_id: str, username: str, cents: float, message_id: int | None = None
) -> None:
    """Add the cost of a summary call that stored no compaction to a saved message.

    The chat's cost counts these cents once, from ``side_cents``, so /session
    shows them after a reload.

    Args:
        job_store: Job-store instance whose engine backs the DB session.
        conversation_id: The saved conversation.
        username: The caller; another account's conversation is left alone.
        cents: The call's cost in cents.
        message_id: The message to add them to; ``None`` picks the latest message.
    """
    with Session(job_store.engine) as session:
        owner = session.get(AgentConversationModel, conversation_id)
        if owner is None or owner.username != username:
            return
        query = session.query(AgentMessageModel).filter(AgentMessageModel.conversation_id == conversation_id)
        if message_id is not None:
            query = query.filter(AgentMessageModel.id == message_id)
        row = query.order_by(AgentMessageModel.created_at.desc(), AgentMessageModel.id.desc()).first()
        if row is None:
            return
        meta = row.router_metadata or {}
        row.router_metadata = {**meta, "side_cents": float(meta.get("side_cents") or 0) + cents}
        session.commit()


async def _finish_in_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run ``func`` in a worker thread, and wait for it to end also when the caller is cancelled.

    The stream's metering reads the LM usage when the stream closes. A model
    call still running in its thread then would record its usage after the
    debit, so the user would get the call for free.

    Args:
        func: The blocking call.
        *args: Its positional arguments.
        **kwargs: Its keyword arguments.

    Returns:
        What ``func`` returns.
    """
    future = asyncio.ensure_future(asyncio.to_thread(func, *args, **kwargs))
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        # StreamingResponse cancels an anyio scope on disconnect, and that scope
        # cancels again at every await: without the shield the loop below spins
        # a core until the thread ends. The loop still absorbs plain task.cancel().
        with anyio.CancelScope(shield=True):
            while not future.done():
                try:
                    await asyncio.wait({future})
                except asyncio.CancelledError:
                    continue
        if not future.cancelled():
            future.exception()
        raise


async def _compaction_events(
    *,
    model_config: ModelConfig,
    usage_sink: list,
    turns: list[ChatTurn],
    previous_summary: str | None,
    instructions: str | None,
    window: int,
    tokens_before: int,
    kept_history: list[dict[str, Any]],
    user_message: str,
    reason: str,
    result: dict[str, Any],
    save: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Run one compaction and stream its ``compaction_start`` and ``compaction`` events.

    The summary runs on the turn's own model, and its LM joins the turn's
    usage sink, so ``stream_with_llm_metering`` reserves, debits and reports
    it with the turn, under the same BYOK and subscription rules.

    Args:
        model_config: The turn's resolved model config.
        usage_sink: The turn's usage sink.
        turns: The chat turns to summarize, oldest first.
        previous_summary: The summary from the chat's latest compaction, if any.
        instructions: What the summary should focus on, if anything.
        window: The model's context window.
        tokens_before: The estimated prompt tokens before the compaction.
        kept_history: The history entries that stay after the cut.
        user_message: The new user message, or ``""`` for no new turn.
        reason: ``"threshold"`` or ``"manual"``.
        result: Filled with ``summary`` and ``entry`` on success. A call that
            stores no entry (it failed, or the stream stopped) sets
            ``lost_cents`` to its cost instead.
        save: Stores the entry before the ``compaction`` event goes out, so a
            stop after that event keeps the summary and its cost.

    Yields:
        ``compaction_start``, then ``compaction`` with the entry, or
        ``compaction_error`` with the failed call's ``cents``.
    """
    token_source = metering_token_source(model_config)

    def cost(lm: Any) -> float | None:
        """Price the summary call.

        Args:
            lm: The summary LM.

        Returns:
            The call's cents, or ``None`` when it reported no usage.
        """
        return estimate_run_cents([lm], token_source) if usage_by_model_from_history(lm) else None

    yield {
        "event": "compaction_start",
        "data": {
            "reason": reason,
            "summarized_turns": len(turns),
            "kept_turns": len(kept_history),
            "tokens_before": tokens_before,
        },
    }
    lm: Any = None
    try:
        lm = build_turn_lm(model_config)
        lm.side_call = True
        usage_sink.append(lm)
        summary = await _finish_in_thread(
            compaction.summarize,
            lm,
            [turn.model_dump() for turn in turns],
            previous_summary=previous_summary,
            instructions=instructions,
            window=window,
        )
    except asyncio.CancelledError:
        if lm is not None:
            result["lost_cents"] = cost(lm)
        raise
    except Exception:
        logger.exception("Chat compaction failed")
        cents = cost(lm) if lm is not None else None
        result["lost_cents"] = cents
        yield {"event": "compaction_error", "data": {"reason": reason, "cents": cents}}
        return
    breakdown = usage_by_model_from_history(lm)
    entry = {
        "summary": summary,
        "reason": reason,
        "summarized_turns": len(turns),
        "kept_turns": len(kept_history),
        "tokens_before": tokens_before,
        "tokens_after": compaction.context_tokens(kept_history, user_message, summary),
        "input_tokens": sum(pair[0] for pair in breakdown.values()) if breakdown else None,
        "output_tokens": sum(pair[1] for pair in breakdown.values()) if breakdown else None,
        "cents": cost(lm),
        "created_at": datetime.now(UTC).isoformat(),
    }
    result["summary"] = summary
    result["entry"] = entry
    if save is not None:
        await save(entry)
    yield {"event": "compaction", "data": entry}


def _merge_wizard_patch(state: dict[str, Any], result: Any) -> None:
    """Merge a tool result's ``wizard_state`` patch into the running state.

    Tools that mutate the wizard (``update_wizard_state``,
    ``set_column_roles``, …) echo the validated patch under
    ``result.wizard_state``. We treat the patch as a shallow overlay so the
    persisted ``wizard_state_after`` reflects every change the agent
    successfully made this turn. ``workspace_files`` is the exception: a
    file tool's patch holds only the file it wrote, so it merges into the
    map. Tool results that don't carry the field leave the state untouched.

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
        current = state.get(str(key))
        if key == "workspace_files" and isinstance(value, dict) and isinstance(current, dict):
            state[str(key)] = {**current, **value}
        else:
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

    async def _turn_model_config(
        username: str,
        model: str | None,
        reasoning_effort: str | None,
        fast_mode: bool,
    ) -> ModelConfig:
        """Resolve the model config of a chat turn or a compaction.

        Args:
            username: The caller.
            model: The composer's model pick, or ``None``.
            reasoning_effort: The composer's effort pick, or ``None``.
            fast_mode: The composer's fast-mode switch.

        Returns:
            The routed model config, with effort and fast mode applied.
        """
        picked = await asyncio.to_thread(engine_agent_model, job_store, username, model)
        # A cold model catalog builds on the calling thread, so the catalog
        # reads below run off the event loop (SKY-603).
        model_config = await asyncio.to_thread(
            route_local_byok_model, picked, job_store=job_store, username=username
        ) or await asyncio.to_thread(route_agent_model, picked)
        # A CLI takes its own effort ladder, not the catalog's, and with no
        # pick it runs its own default. The CLI dispatcher drops a level or
        # fast mode the model does not take.
        on_cli = model_config.token_source == "subscription"
        effort = (
            reasoning_effort
            if on_cli
            else await asyncio.to_thread(effective_reasoning_effort, model_config.name, reasoning_effort)
        )
        if effort:
            model_config = model_config.model_copy(update={"extra": {**model_config.extra, "reasoning_effort": effort}})
        return await asyncio.to_thread(with_fast_mode, model_config, fast_mode)

    async def _save_compaction_safely(
        conversation_id: str,
        anchor_id: int | None,
        entry: dict[str, Any],
        username: str,
        by_text: tuple[str, int | None, int | None, int, int] | None = None,
    ) -> None:
        """Store a compaction entry off the event loop; log and go on when the write fails.

        Waits for the write also when the stream is cancelled, so a stop just
        after the summary keeps it.

        Args:
            conversation_id: The saved conversation.
            anchor_id: The id of the first kept user message; ``None`` when the
                client did not send it, so the row is found by ``by_text``.
            entry: The compaction entry.
            username: The caller.
            by_text: The ``_anchor_by_text`` lookup for a message with no id.
        """
        try:
            await _finish_in_thread(_save_compaction, job_store, conversation_id, username, anchor_id, entry, by_text)
        except Exception:
            logger.exception("Failed to save the chat compaction")

    async def _save_side_cents_safely(
        conversation_id: str | None, result: dict[str, Any], username: str, message_id: int | None = None
    ) -> None:
        """Store the cost of a summary call that stored no compaction; log and go on when the write fails.

        Waits for the write also when the stream is cancelled, so a stopped
        turn keeps the cost in /session.

        Args:
            conversation_id: The saved conversation, or ``None`` for an unsaved chat.
            result: The ``_compaction_events`` result; only ``lost_cents`` is read.
            username: The caller.
            message_id: The message to store the cost on; ``None`` picks the latest message.
        """
        cents = result.get("lost_cents")
        if not cents or not conversation_id or job_store is None:
            return
        try:
            await _finish_in_thread(_add_side_cents, job_store, conversation_id, username, cents, message_id)
        except Exception:
            logger.exception("Failed to save the summary call's cost")

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

        def _setup_turn() -> tuple[str | None, str | None, int | None]:
            """Persist the user turn off the event loop.

            Runs the blocking conversation upsert and user-message insert in a
            worker thread so the single event loop is not stalled before the SSE
            stream starts. Auth is already enforced by the route dependency, so
            the turn is always attributed to ``current_user``; a conversation
            ``DomainError`` (403/404) is allowed to propagate.

            Returns:
                ``(conversation_id, title, user_message_id)`` — all ``None`` when
                persistence is off (no ``job_store`` or ``persist`` is False) or
                failed non-fatally.
            """
            if job_store is None or not req.persist:
                return None, None, None
            try:
                cid, ttl = _ensure_conversation(job_store, req.conversation_id, current_user.username, req.user_message)
                carried = None
                if req.edit_user_text is not None:
                    found, carried = _discard_from_user_turn(
                        job_store, cid, req.edit_user_text, req.edit_user_occurrence
                    )
                    # A retry of an edit the server saved, but whose
                    # conversation_meta never reached the client, finds no old
                    # turn. Replace the saved edit only when it is the latest
                    # turn, so a missing target never deletes another turn.
                    if not found and req.regenerate:
                        carried = _discard_latest_turn(job_store, cid, only_if_text=req.user_message)
                elif req.regenerate:
                    # A retry of a send refused before it was stored also
                    # sends regenerate; the text check keeps the previous turn.
                    carried = _discard_latest_turn(job_store, cid, only_if_text=req.user_message)
                user_message_id = _persist_user_turn(job_store, cid, req.user_message, compaction=carried)
            except DomainError:
                raise
            except Exception:
                logger.exception("Failed to persist user turn")
                return None, None, None
            return cid, ttl, user_message_id

        await asyncio.to_thread(enforce_llm_balance, job_store, current_user.username)

        wizard_state: WizardState = {**req.wizard_state}  # type: ignore[typeddict-item]
        model_config = await _turn_model_config(current_user.username, req.model, req.reasoning_effort, req.fast_mode)
        window = await asyncio.to_thread(_context_window, model_config.name)
        # Model routing can refuse the turn with a 4xx. Store nothing before it
        # passes: the client gets no conversation_meta for a refused turn and
        # would resend it as a new edit.
        conversation_id, title, user_message_id = await asyncio.to_thread(_setup_turn)
        # A new chat gets a model title in parallel with the turn. The setup
        # wizard kickoff has no cut title and gets no model title either.
        title_task = (
            _start_title_task(job_store, current_user.username, req.model, conversation_id, req.user_message, title)
            if conversation_id and title and not req.conversation_id
            else None
        )
        usage_sink: list = []
        compaction_cents: float | None = None

        def _github_token() -> str | None:
            """Load the caller's token for the run's repository host, for the coding agent's reads.

            Returns:
                The token, or ``None`` when the host is not linked or the link expired.
            """
            if engine is None:
                return None
            provider = str(wizard_state.get("repo_provider") or github.PROVIDER)
            try:
                return connector_secret(ConnectorVault(engine), current_user.username, provider).access_token
            except DomainError:
                return None

        async def _turn_source() -> AsyncIterator[dict[str, Any]]:
            """Clear old tool results, then compact, when the prompt nears the context window; then run the turn.

            Yields:
                The compaction events, if any, then the agent's events.
            """
            nonlocal compaction_cents
            full_history = history_for_agent(req.chat_history)
            summary = req.compaction_summary
            history = compaction.clear_tool_results(full_history, req.user_message, summary, window)
            tokens = compaction.context_tokens(history, req.user_message, summary)
            cut = 0
            if compaction.should_compact(tokens, window):
                cut = compaction.manual_cut(history, compaction.keep_tokens(window))
                # A summary that frees almost nothing would run again on every
                # turn of a small window; fit_history below drops turns instead.
                if not compaction.shrinks_prompt(history, cut):
                    cut = 0
            if cut > 0:
                result: dict[str, Any] = {}
                anchor_id = req.chat_history[cut].id
                by_text = _anchor_by_text(req.chat_history, cut, user_message_id) if anchor_id is None else None
                try:
                    async for event in _compaction_events(
                        model_config=model_config,
                        usage_sink=usage_sink,
                        turns=req.chat_history[:cut],
                        previous_summary=summary,
                        instructions=None,
                        window=window,
                        tokens_before=tokens,
                        kept_history=history[cut:],
                        user_message=req.user_message,
                        reason="threshold",
                        result=result,
                        save=(
                            partial(
                                _save_compaction_safely,
                                conversation_id,
                                anchor_id,
                                username=current_user.username,
                                by_text=by_text,
                            )
                            if conversation_id and job_store is not None
                            else None
                        ),
                    ):
                        yield event
                finally:
                    # The turn's stats include a failed call too; the chat counts it from side_cents.
                    compaction_cents = result.get("lost_cents")
                    await _save_side_cents_safely(conversation_id, result, current_user.username, user_message_id)
                if "summary" in result:
                    compaction_cents = result["entry"]["cents"]
                    summary = result["summary"]
                    # The summary frees room, so the kept turns may get their cleared results back.
                    history = compaction.clear_tool_results(full_history[cut:], req.user_message, summary, window)
            if compaction.should_compact(compaction.context_tokens(history, req.user_message, summary), window):
                # A failed summary, or one large recent turn, would still
                # overflow the window; the oldest turns go so the turn can run.
                history = compaction.fit_history(history, req.user_message, summary, window)
            async for event in run_generalist_agent(
                wizard_state=wizard_state,
                chat_history=compaction.history_with_summary(summary, history),
                user_message=req.user_message,
                trust_mode=req.trust_mode,
                auth_header=authorization,
                approval_owner=current_user.username,
                locale=req.locale,
                model_config=model_config,
                usage_sink=usage_sink,
                steer_key=req.steer_key,
                github_token=_github_token,
            ):
                yield event

        async def _with_compaction_cents(events: AsyncIterator[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
            """Mark the part of the turn's cost that the compaction's summary call took.

            The turn's stats include that call. The chat's cost then counts each
            compaction once, from its entry, also when the turn stops before
            ``done``.

            Args:
                events: The metered turn events.

            Yields:
                The events; ``done`` and ``turn_stats`` stats gain ``compaction_cents``.
            """
            async for event in events:
                data = event.get("data")
                stats = data.get("stats") if isinstance(data, dict) else None
                if (
                    compaction_cents is not None
                    and event.get("event") in ("done", "turn_stats")
                    and isinstance(stats, dict)
                ):
                    event = {**event, "data": {**data, "stats": {**stats, "compaction_cents": compaction_cents}}}
                yield event

        source = _turn_source()
        metered = stream_with_llm_metering(
            source,
            job_store=job_store,
            username=current_user.username,
            description="Agent chat",
            usage_sink=usage_sink,
            token_source=metering_token_source(model_config),
        )
        wrapped = _wrap_with_persistence(
            _with_compaction_cents(metered),
            job_store=job_store,
            conversation_id=conversation_id,
            title=title,
            wizard_state_before=dict(wizard_state),
            user_message_id=user_message_id,
        )
        if title_task is not None and conversation_id:
            wrapped = with_title_event(wrapped, title_task, conversation_id, title or "")
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
        "/optimizations/generalist-agent/compact",
        summary="Stream the compaction of an agent chat",
    )
    async def compact_chat(req: CompactChatRequest, current_user: AuthenticatedUserDep) -> StreamingResponse:
        """Summarize a chat's older turns on request (``/compact``), as SSE.

        Event types: ``compaction_start``, then ``compaction`` or
        ``compaction_error``, then ``done`` with the call's ``stats``; or one
        ``error`` with code ``nothing_to_compact`` when the chat is too short.

        Args:
            req: The chat history after the latest compaction, its summary,
                the user's focus instructions and the composer's model.
            current_user: The authenticated caller, who pays for the summary call.

        Returns:
            A :class:`StreamingResponse` of Server-Sent Events.
        """
        await asyncio.to_thread(enforce_llm_balance, job_store, current_user.username)
        model_config = await _turn_model_config(current_user.username, req.model, req.reasoning_effort, req.fast_mode)
        window = await asyncio.to_thread(_context_window, model_config.name)
        usage_sink: list = []

        async def _source() -> AsyncIterator[dict[str, Any]]:
            """Run the compaction and end the stream with ``done``.

            Yields:
                The compaction events, then ``done``.
            """
            history = history_for_agent(req.chat_history)
            cut = compaction.manual_cut(history, compaction.keep_tokens(window))
            if cut == 0:
                yield {"event": "error", "data": {"code": "nothing_to_compact"}}
                return
            result: dict[str, Any] = {}
            try:
                async for event in _compaction_events(
                    model_config=model_config,
                    usage_sink=usage_sink,
                    turns=req.chat_history[:cut],
                    previous_summary=req.compaction_summary,
                    instructions=req.instructions,
                    window=window,
                    tokens_before=compaction.context_tokens(history, "", req.compaction_summary),
                    kept_history=history[cut:],
                    user_message="",
                    reason="manual",
                    result=result,
                    save=(
                        partial(
                            _save_compaction_safely,
                            req.conversation_id,
                            req.chat_history[cut].id,
                            username=current_user.username,
                            by_text=(
                                _anchor_by_text(req.chat_history, cut, None)
                                if req.chat_history[cut].id is None
                                else None
                            ),
                        )
                        if req.conversation_id and job_store is not None
                        else None
                    ),
                ):
                    yield event
            finally:
                await _save_side_cents_safely(req.conversation_id, result, current_user.username)
            yield {"event": "done", "data": {}}

        metered = stream_with_llm_metering(
            _source(),
            job_store=job_store,
            username=current_user.username,
            description="Agent chat compaction",
            usage_sink=usage_sink,
            token_source=metering_token_source(model_config),
        )
        return StreamingResponse(
            sse_from_events(metered),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
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

        Raises:
            HTTPException: Text cannot fit the turn (422).
            DomainError: The pending allowance is full (429).
        """
        try:
            message_id = get_steer_store().post(current_user.username, req.steer_key, req.text)
        except SteerInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except SteerLimitError as exc:
            raise DomainError("agent.steer_pending_full", status=429) from exc
        return SteerMessageResponse(id=message_id)

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
            Newly withdrawn ids and ids already read by the agent.
        """
        ids, delivered = get_steer_store().withdraw(current_user.username, req.steer_key, req.ids)
        return SteerWithdrawResponse(ids=ids, delivered=delivered)

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
