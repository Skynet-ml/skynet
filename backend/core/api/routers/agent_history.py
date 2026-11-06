"""CRUD routes for persisted generalist-agent conversations. [INTERNAL]

Backs the agent panel's history drawer. Each user owns their own threads;
ownership is enforced on every read and write by comparing the
authenticated principal to the row's ``username``. The companion writer
that *populates* these tables lives in :mod:`generalist_agent` — this
router only reads, renames, pins, and archives.

It also lets an owner share a thread by a typed link (SKY-294). A ``view``
link shows the chat read-only to anyone. A ``continue`` link lets a
signed-in user continue the chat as their own copy. A ``continue_data``
link does the same and also grants live, read-only access to the runs,
datasets and files that the owner ticked. Every shared-item read checks
the live link, so a revoke stops it at once. Users other than the owner
never see the owner's private tool results: those become
``shared_chat_item`` rows. Sharing needs the hosted app, so the Mac app
(local mode) cannot create a link.

Hidden from the public Scalar reference — wizard-internal flow.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, load_only

from ...config import settings
from ...models import (
    BulkDeleteByIdsRequest,
    BulkDeleteByIdsResponse,
    BulkDeleteByIdsSkipped,
)
from ...service_gateway.embedding_pipeline.embeddings import get_embedder
from ...storage.dataset_library import DatasetLibraryStore, PostgresDatasetBlobStore
from ...storage.models import (
    AgentChatShareModel,
    AgentConversationModel,
    AgentMessageModel,
    AgentStagedDatasetModel,
    DatasetModel,
    JobModel,
)
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from .agent_settings import MASK, mask_secrets

logger = logging.getLogger(__name__)

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]
MAX_LIST = 100
DEFAULT_LIST = 50
# A shared chat copies at most this many turns into a continued chat.
MAX_SHARED_MESSAGES = 1000
# Each live link is a public page, so one account cannot publish without bound.
MAX_SHARED_CHATS = 200
MAX_SHARE_ITEMS = 50
# A shared-item read goes to the model, so it stays small.
MAX_ITEM_ROWS = 50
MAX_ITEM_CHARS = 20_000
ITEM_KINDS = ("run", "dataset", "file")
# Tool-call keys whose value names one private item. A program is the result
# of a run, so a run grant also covers its program.
_ITEM_ID_KEYS = {
    "optimization_id": "run",
    "job_id": "run",
    "dataset_id": "dataset",
    "source_dataset_id": "dataset",
    "staged_dataset_id": "file",
}
# Catalog tools whose results hold no user data, so other users may see them.
_PUBLIC_TOOLS = frozenset({"list_models_for_agent", "get_registry_snapshot_registry_get"})
_SHARED_HINT = (
    "The owner shares these items read-only. Call read_shared_chat_item with an item's kind and id to read it."
)
_NOT_SHARED_HINT = "Not shared. The owner did not share this result, so its content is not available."
# A tool result (for example the account settings) can carry an email address,
# and another user must not see who owns the chat.
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


class ConversationSummary(BaseModel):
    """List-row projection of an agent conversation."""

    id: str
    title: str
    pinned: bool
    archived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    preview: str | None = Field(default=None, description="Trimmed first user message.")


class ConversationDetail(BaseModel):
    """Full conversation with ordered messages."""

    id: str
    title: str
    pinned: bool
    archived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    messages: list[ConversationMessage]


class ConversationMessage(BaseModel):
    """One persisted turn — mirrors the frontend ``AgentMessage`` shape."""

    role: str
    content: str
    tool_calls: list[dict[str, Any]] | None = None
    model: str | None = None
    # The concrete model behind an auto-routed turn, from router_metadata.
    served_model: str | None = None
    # Token usage, cost and timing of the turn, from router_metadata.
    stats: dict[str, Any] | None = None
    # What the agent said it would do that turn, from router_metadata.
    plan: str | None = None
    # The turn's thinking rows (reasoning, timing, summary), from router_metadata.
    thinking: list[dict[str, Any]] | None = None
    created_at: datetime


class ConversationPatchRequest(BaseModel):
    """Partial update — at least one of ``title`` or ``pinned`` required."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    pinned: bool | None = None


class SharedConversation(BaseModel):
    """Public read-only view of a shared conversation, without owner or cost data."""

    title: str
    created_at: datetime
    updated_at: datetime
    messages: list[ConversationMessage]
    link_type: str = "view"


class SharedItemOption(BaseModel):
    """One private item that a share link may grant, or grants."""

    kind: Literal["run", "dataset", "file"]
    id: str
    label: str
    available: bool = True


class SharedConversationSummary(BaseModel):
    """One of the owner's shared conversations, for the settings list."""

    id: str
    title: str
    shared_at: datetime | None = None
    token: str
    link_type: str = "view"
    items: list[SharedItemOption] = Field(default_factory=list)


class SharedItemRef(BaseModel):
    """One ticked item in a share request."""

    kind: Literal["run", "dataset", "file"]
    id: str = Field(min_length=1, max_length=64)


class ChatShareRequest(BaseModel):
    """The link type and the ticked items of a share link."""

    link_type: Literal["view", "continue", "continue_data"]
    items: list[SharedItemRef] = Field(default_factory=list, max_length=MAX_SHARE_ITEMS)


class ChatShareState(BaseModel):
    """The owner's view of a conversation's share link and the items it may share."""

    token: str | None = None
    link_type: str | None = None
    items: list[SharedItemOption] = Field(default_factory=list)
    candidates: list[SharedItemOption] = Field(default_factory=list)


class SharedChatOpenResponse(BaseModel):
    """What the signed-in caller opens for a share link."""

    mode: Literal["owner", "fork", "view"]
    conversation_id: str | None = None


class SharedItemContent(BaseModel):
    """A capped, masked, read-only view of one shared item."""

    kind: Literal["run", "dataset", "file"]
    id: str
    label: str
    detail: dict[str, Any]


def _row_to_summary(row: AgentConversationModel, preview: str | None) -> ConversationSummary:
    """Project an ORM row + optional preview text into the list-response shape.

    Args:
        row: Loaded ``AgentConversationModel``.
        preview: First user message text (already truncated), or ``None``.

    Returns:
        The serializable summary the route returns.
    """
    return ConversationSummary(
        id=cast(str, row.id),
        title=cast(str, row.title),
        pinned=cast(bool, row.pinned),
        archived_at=cast("datetime | None", row.archived_at),
        created_at=cast(datetime, row.created_at),
        updated_at=cast(datetime, row.updated_at),
        preview=preview,
    )


def create_agent_history_router(*, job_store) -> APIRouter:
    """Build the agent-history router.

    Args:
        job_store: Job-store instance whose ORM session backs the routes.

    Returns:
        A FastAPI ``APIRouter`` exposing list / get / patch / archive routes.
    """
    router = APIRouter()
    dataset_store = DatasetLibraryStore(job_store.engine, PostgresDatasetBlobStore(job_store.engine))

    @router.get(
        "/agent/conversations",
        response_model=list[ConversationSummary],
        summary="List the caller's agent conversations",
    )
    def list_conversations(
        user: AuthenticatedUserDep,
        q: str | None = Query(
            default=None,
            max_length=200,
            description="Case-insensitive substring match against title or message content.",
        ),
        pinned: bool | None = Query(
            default=None,
            description="When set, filter to pinned (true) or unpinned (false) rows only.",
        ),
        limit: int = Query(default=DEFAULT_LIST, ge=1, le=MAX_LIST),
        offset: int = Query(default=0, ge=0),
    ) -> list[ConversationSummary]:
        """Return the caller's conversations, newest activity first.

        Pinned rows sort to the top regardless of ``updated_at`` so they
        stay visible as the user accumulates threads.

        Args:
            user: Authenticated caller; only their conversations are returned.
            q: Optional case-insensitive substring filter on title.
            pinned: Optional pinned/unpinned filter.
            limit: Page size, clamped to ``MAX_LIST``.
            offset: Number of rows to skip for paging.

        Returns:
            A list of ``ConversationSummary`` rows, pinned first then newest.
        """
        with Session(job_store.engine) as session:
            query_clean = (q or "").strip()
            if query_clean and _can_use_semantic_search(session, user.username):
                ranked_ids = _semantic_conversation_ids(
                    session, user.username, query_clean, pinned=pinned, cap=MAX_LIST * 2
                )
                if ranked_ids is not None:
                    page_ids = ranked_ids[offset : offset + limit]
                    if not page_ids:
                        return []
                    rows_by_id = {
                        cast(str, row.id): row
                        for row in session.query(AgentConversationModel)
                        .filter(AgentConversationModel.id.in_(page_ids))
                        .all()
                    }
                    ordered = [rows_by_id[i] for i in page_ids if i in rows_by_id]
                    previews = _fetch_previews(session, list(rows_by_id.keys()))
                    return [
                        _row_to_summary(r, previews.get(cast(str, r.id))) for r in ordered
                    ]
            query = session.query(AgentConversationModel).filter(
                AgentConversationModel.username == user.username
            )
            if pinned is not None:
                query = query.filter(AgentConversationModel.pinned == pinned)
            if query_clean:
                like = f"%{query_clean}%"
                matching_conv_ids = (
                    session.query(AgentMessageModel.conversation_id)
                    .filter(AgentMessageModel.content.ilike(like))
                    .distinct()
                    .subquery()
                )
                query = query.filter(
                    or_(
                        AgentConversationModel.title.ilike(like),
                        AgentConversationModel.id.in_(matching_conv_ids),
                    )
                )
            rows = (
                query.order_by(
                    AgentConversationModel.pinned.desc(),
                    AgentConversationModel.updated_at.desc(),
                )
                .offset(offset)
                .limit(limit)
                .all()
            )
            if not rows:
                return []
            ids = [cast(str, r.id) for r in rows]
            previews = _fetch_previews(session, ids)
            return [_row_to_summary(r, previews.get(cast(str, r.id))) for r in rows]

    @router.get(
        "/agent/conversations/{conversation_id}",
        response_model=ConversationDetail,
        summary="Fetch a single conversation with its full message history",
    )
    def get_conversation(conversation_id: str, user: AuthenticatedUserDep) -> ConversationDetail:
        """Return the named conversation and every persisted turn in order.

        Args:
            conversation_id: UUID of the conversation to load.
            user: Authenticated caller; must own the conversation.

        Returns:
            A ``ConversationDetail`` carrying the thread header plus messages
            ordered ``created_at`` ascending.

        Raises:
            DomainError: 404 when unknown, 403 when the caller does not own it.
        """
        with Session(job_store.engine) as session:
            row = session.get(AgentConversationModel, conversation_id)
            if row is None:
                raise DomainError("agent.conversation.not_found", status=404)
            if row.username != user.username:
                raise DomainError("agent.conversation.forbidden", status=403)
            msgs = (
                session.query(AgentMessageModel)
                .options(
                    load_only(
                        AgentMessageModel.role,
                        AgentMessageModel.content,
                        AgentMessageModel.tool_calls,
                        AgentMessageModel.created_at,
                    )
                )
                .filter(AgentMessageModel.conversation_id == conversation_id)
                .order_by(AgentMessageModel.created_at.asc(), AgentMessageModel.id.asc())
                .all()
            )
            return ConversationDetail(
                id=cast(str, row.id),
                title=cast(str, row.title),
                pinned=cast(bool, row.pinned),
                archived_at=cast("datetime | None", row.archived_at),
                created_at=cast(datetime, row.created_at),
                updated_at=cast(datetime, row.updated_at),
                messages=[
                    ConversationMessage(
                        role=cast(str, m.role),
                        content=cast(str, m.content),
                        tool_calls=cast("list[dict[str, Any]] | None", m.tool_calls),
                        model=cast("str | None", m.model),
                        served_model=(m.router_metadata or {}).get("served_model")
                        if isinstance(m.router_metadata, dict)
                        else None,
                        stats=(m.router_metadata or {}).get("stats") if isinstance(m.router_metadata, dict) else None,
                        plan=(m.router_metadata or {}).get("plan") if isinstance(m.router_metadata, dict) else None,
                        thinking=(m.router_metadata or {}).get("thinking")
                        if isinstance(m.router_metadata, dict)
                        else None,
                        created_at=cast(datetime, m.created_at),
                    )
                    for m in msgs
                ],
            )

    @router.patch(
        "/agent/conversations/{conversation_id}",
        response_model=ConversationSummary,
        summary="Rename or pin/unpin a conversation",
    )
    def patch_conversation(
        conversation_id: str,
        req: ConversationPatchRequest,
        user: AuthenticatedUserDep,
    ) -> ConversationSummary:
        """Patch ``title`` and/or ``pinned`` on an owned conversation.

        Args:
            conversation_id: UUID of the conversation to patch.
            req: Partial update body; at least one field must be supplied.
            user: Authenticated caller; must own the row.

        Returns:
            The updated row projected as ``ConversationSummary``.

        Raises:
            DomainError: 422 when no field supplied, 404 when unknown,
                403 when the caller does not own the row.
        """
        if req.title is None and req.pinned is None:
            raise DomainError("agent.conversation.patch_requires_field", status=422)
        with Session(job_store.engine) as session:
            row = session.get(AgentConversationModel, conversation_id)
            if row is None:
                raise DomainError("agent.conversation.not_found", status=404)
            if row.username != user.username:
                raise DomainError("agent.conversation.forbidden", status=403)
            if req.title is not None:
                row.title = cast(Any, req.title.strip())
            if req.pinned is not None:
                row.pinned = cast(Any, req.pinned)
            row.updated_at = cast(Any, datetime.now(UTC))
            session.commit()
            session.refresh(row)
            return _row_to_summary(row, None)

    @router.delete(
        "/agent/conversations/{conversation_id}",
        status_code=204,
        summary="Permanently delete a conversation and its messages",
    )
    def delete_conversation(conversation_id: str, user: AuthenticatedUserDep) -> None:
        """Delete an owned conversation row; messages cascade via FK.

        Args:
            conversation_id: UUID of the conversation to delete.
            user: Authenticated caller; must own the row.

        Raises:
            DomainError: 404 when unknown, 403 when the caller does not own it.
        """
        with Session(job_store.engine) as session:
            row = session.get(AgentConversationModel, conversation_id)
            if row is None:
                raise DomainError("agent.conversation.not_found", status=404)
            if row.username != user.username:
                raise DomainError("agent.conversation.forbidden", status=403)
            _drop_shares(session, [conversation_id])
            session.delete(row)
            session.commit()

    @router.get(
        "/agent/conversations/{conversation_id}/share",
        response_model=ChatShareState,
        summary="Read the share link of a conversation and the data it can share",
    )
    def get_conversation_share(conversation_id: str, user: AuthenticatedUserDep) -> ChatShareState:
        """Return the conversation's live share link, if any, and the private items it may share.

        Args:
            conversation_id: UUID of the conversation.
            user: Authenticated caller; must own the row.

        Returns:
            The link token, type and ticked items, plus every private item
            (run, dataset, file) that the chat refers to.

        Raises:
            DomainError: 404 when unknown, 403 when the caller does not own it.
        """
        with Session(job_store.engine) as session:
            row = _owned_conversation(session, conversation_id, user.username)
            candidates = _item_candidates(session, row)
            return _share_state(session, _share_of(session, conversation_id), candidates)

    @router.post(
        "/agent/conversations/{conversation_id}/share",
        response_model=ChatShareState,
        summary="Create or change the share link of a conversation",
    )
    def share_conversation(
        conversation_id: str, body: ChatShareRequest, user: AuthenticatedUserDep
    ) -> ChatShareState:
        """Create the conversation's share link, or change its type and items in place.

        The token stays the same while the link lives. Only ``continue_data``
        links keep items, and only items that the chat refers to (or that the
        link already shares) and the caller owns are kept; other items are
        dropped.

        Args:
            conversation_id: UUID of the conversation to share.
            body: The link type and the ticked items.
            user: Authenticated caller; must own the row.

        Returns:
            The new link state.

        Raises:
            DomainError: 404 when unknown (and always in the Mac app, where a
                link cannot work for anyone else), 403 when the caller does not
                own it, 409 ``agent.share.limit_reached`` when a new link would
                pass ``MAX_SHARED_CHATS``, 422 when a ``continue_data`` link
                ticks no item that the chat offers.
        """
        if settings.local_mode:
            raise DomainError("agent.conversation.not_found", status=404)
        with Session(job_store.engine) as session:
            row = _owned_conversation(session, conversation_id, user.username)
            candidates = _item_candidates(session, row)
            share = _share_of(session, conversation_id)
            allowed = {(c.kind, c.id) for c in candidates}
            # A shared item stays shareable after older turns push it out of the candidate window.
            if share is not None:
                allowed |= set(_owned_item_labels(session, user.username, _share_refs(share)))
            items: list[dict[str, str]] = []
            if body.link_type == "continue_data":
                for item in body.items:
                    ref = {"kind": item.kind, "id": item.id}
                    if (item.kind, item.id) in allowed and ref not in items:
                        items.append(ref)
                if not items:
                    raise DomainError("agent_settings.invalid_value", status=422, field="items")
            now = datetime.now(UTC)
            if share is None:
                live = session.query(AgentChatShareModel).filter(AgentChatShareModel.username == user.username).count()
                if live >= MAX_SHARED_CHATS:
                    raise DomainError("agent.share.limit_reached", status=409, limit=MAX_SHARED_CHATS)
                share = AgentChatShareModel(
                    token=secrets.token_urlsafe(24),
                    conversation_id=conversation_id,
                    username=user.username,
                    created_at=now,
                )
                session.add(share)
            share.link_type = body.link_type
            share.items = items
            share.updated_at = now
            try:
                session.commit()
            except IntegrityError:
                # A second request made the link first; change that link instead.
                session.rollback()
                share = _share_of(session, conversation_id)
                if share is None:
                    raise
                share.link_type = body.link_type
                share.items = items
                share.updated_at = now
                session.commit()
            return _share_state(session, share, candidates)

    @router.delete(
        "/agent/conversations/{conversation_id}/share",
        status_code=204,
        summary="Revoke the share link of a conversation",
    )
    def unshare_conversation(conversation_id: str, user: AuthenticatedUserDep) -> None:
        """Delete the conversation's share link, so the link and its data grants stop at once.

        The continued copies that other users made stay theirs, but they
        lose the link, so they can no longer read the shared items.

        Args:
            conversation_id: UUID of the conversation to unshare.
            user: Authenticated caller; must own the row.

        Raises:
            DomainError: 404 when unknown, 403 when the caller does not own it.
        """
        with Session(job_store.engine) as session:
            _owned_conversation(session, conversation_id, user.username)
            share = _share_of(session, conversation_id)
            if share is None:
                return
            session.query(AgentConversationModel).filter(
                AgentConversationModel.source_share_token == share.token
            ).update({AgentConversationModel.source_share_token: None}, synchronize_session=False)
            session.delete(share)
            session.commit()

    @router.get(
        "/agent/shared-conversations",
        response_model=list[SharedConversationSummary],
        summary="List the caller's shared conversations",
    )
    def list_shared_conversations(user: AuthenticatedUserDep) -> list[SharedConversationSummary]:
        """Return the caller's live share links, newest change first.

        Args:
            user: Authenticated caller; only their links are listed.

        Returns:
            The conversation id and title, link token, type and ticked items of
            each link. An item that no longer exists has ``available`` false.
        """
        with Session(job_store.engine) as session:
            rows = (
                session.query(AgentChatShareModel, AgentConversationModel)
                .join(AgentConversationModel, AgentConversationModel.id == AgentChatShareModel.conversation_id)
                .filter(AgentChatShareModel.username == user.username)
                .order_by(AgentChatShareModel.updated_at.desc())
                .limit(MAX_SHARED_CHATS)
                .all()
            )
            summaries = []
            for share, row in rows:
                labels = _owned_item_labels(session, user.username, _share_refs(share))
                summaries.append(
                    SharedConversationSummary(
                        id=cast(str, row.id),
                        title=cast(str, row.title),
                        shared_at=cast("datetime | None", share.created_at),
                        token=cast(str, share.token),
                        link_type=cast(str, share.link_type),
                        items=_item_options(_share_refs(share), labels),
                    )
                )
            return summaries

    # Deliberately unauthenticated: the token is the capability. Every unknown,
    # malformed or revoked token is the same 404 so a probe learns nothing.
    @router.get(
        "/agent/shared-conversations/{token}",
        response_model=SharedConversation,
        summary="Read a shared conversation by its link token",
    )
    def get_shared_conversation(token: str) -> SharedConversation:
        """Return the link type and, for a view-only link, the read-only chat.

        Args:
            token: The share token from the link.

        Returns:
            The link type. A ``view`` link also returns the title and the
            ordered messages, with owner identity, costs, token usage and
            thinking left out, credentials and email addresses masked, and
            every private tool result replaced by a "not shared" row. A
            continue link returns no chat content, because it needs a sign-in.

        Raises:
            DomainError: 404 when no live link carries the token.
        """
        with Session(job_store.engine) as session:
            share = _share_by_token(session, token)
            row = session.get(AgentConversationModel, share.conversation_id)
            if row is None:
                raise DomainError("agent.conversation.not_found", status=404)
            if share.link_type != "view":
                return SharedConversation(
                    title="",
                    created_at=cast(datetime, share.created_at),
                    updated_at=cast(datetime, share.updated_at),
                    messages=[],
                    link_type=cast(str, share.link_type),
                )
            return SharedConversation(
                title=_mask_public(cast(str, row.title)),
                created_at=cast(datetime, row.created_at),
                updated_at=cast(datetime, row.updated_at),
                messages=_recipient_messages(session, share),
                link_type="view",
            )

    @router.post(
        "/agent/shared-conversations/{token}/open",
        response_model=SharedChatOpenResponse,
        summary="Open a shared conversation for the signed-in caller",
    )
    def open_shared_conversation(token: str, user: AuthenticatedUserDep) -> SharedChatOpenResponse:
        """Decide what the caller sees when they open a share link.

        The owner opens the chat itself. Another user gets the read-only view
        for a ``view`` link. For a continue link, the first open makes the
        caller's own copy of the chat and later opens return the same copy,
        with its copied tool calls rebuilt when the owner changed the link.

        Args:
            token: The share token from the link.
            user: Authenticated caller.

        Returns:
            The mode (``owner``, ``fork`` or ``view``) and the conversation to open.

        Raises:
            DomainError: 404 when no live link carries the token.
        """
        with Session(job_store.engine) as session:
            share = _share_by_token(session, token)
            if share.username == user.username:
                return SharedChatOpenResponse(mode="owner", conversation_id=cast(str, share.conversation_id))
            if share.link_type == "view":
                return SharedChatOpenResponse(mode="view", conversation_id=None)
            existing = _fork_of(session, share.token, user.username)
            if existing is not None:
                _refresh_fork(session, share, existing)
                return SharedChatOpenResponse(mode="fork", conversation_id=existing)
            source = session.get(AgentConversationModel, share.conversation_id)
            if source is None:
                raise DomainError("agent.conversation.not_found", status=404)
            messages = _recipient_messages(session, share)
            fork_id = str(uuid.uuid4())
            now = datetime.now(UTC)
            try:
                session.add(
                    AgentConversationModel(
                        id=fork_id,
                        username=user.username,
                        title=_mask_public(cast(str, source.title))[:200],
                        source_share_token=share.token,
                        created_at=now,
                        updated_at=now,
                    )
                )
                # The copy row must exist before its messages reference it.
                session.flush()
                session.add_all(
                    AgentMessageModel(
                        conversation_id=fork_id,
                        role=m.role,
                        content=m.content,
                        tool_calls=m.tool_calls,
                        model=m.model,
                        router_metadata={"served_model": m.served_model, "plan": m.plan}
                        if m.role == "assistant"
                        else None,
                        created_at=m.created_at,
                    )
                    for m in messages
                )
                session.commit()
            except IntegrityError:
                # A second open of the same link made the copy first.
                session.rollback()
                existing = _fork_of(session, share.token, user.username)
                if existing is None:
                    raise
                return SharedChatOpenResponse(mode="fork", conversation_id=existing)
            return SharedChatOpenResponse(mode="fork", conversation_id=fork_id)

    @router.get(
        "/agent/shared-items/{kind}/{item_id}",
        response_model=SharedItemContent,
        operation_id="read_shared_chat_item",
        summary="Read a run, dataset or file that a shared chat grants to the caller",
        tags=["agent"],
    )
    def read_shared_chat_item(
        kind: Literal["run", "dataset", "file"], item_id: str, user: AuthenticatedUserDep
    ) -> SharedItemContent:
        """Read one item that the owner of a shared chat ticked for you, read-only.

        Use it in a chat continued from a share link, for a ``shared_chat_item``
        row whose status is ``shared``: pass that row's item ``kind`` and ``id``.
        The read fails when the item is not shared or the owner revoked the link.

        Args:
            kind: ``run``, ``dataset`` or ``file``.
            item_id: The item id from the shared row.
            user: Authenticated caller; must own a chat continued from a live
                ``continue_data`` link that ticks this item.

        Returns:
            The item label and a capped, masked view of its content.

        Raises:
            DomainError: 403 when no live link grants the item to the caller,
                404 when the item no longer exists.
        """
        with Session(job_store.engine) as session:
            shares = (
                session.query(AgentChatShareModel)
                .join(AgentConversationModel, AgentConversationModel.source_share_token == AgentChatShareModel.token)
                .filter(
                    AgentConversationModel.username == user.username,
                    AgentChatShareModel.link_type == "continue_data",
                )
                .all()
            )
            owner = next(
                (
                    cast(str, share.username)
                    for share in shares
                    if share.username != user.username and (kind, item_id) in _share_refs(share)
                ),
                None,
            )
            if owner is None:
                raise DomainError("agent.conversation.forbidden", status=403)
            content = _item_content(session, dataset_store, owner, kind, item_id)
        if content is None:
            raise DomainError("agent.conversation.not_found", status=404)
        return content

    @router.post(
        "/agent/conversations/bulk-delete",
        response_model=BulkDeleteByIdsResponse,
        summary="Delete many of the caller's conversations in one request",
    )
    def bulk_delete_conversations(
        body: BulkDeleteByIdsRequest, user: AuthenticatedUserDep
    ) -> BulkDeleteByIdsResponse:
        """Delete a batch of the caller's conversations, reporting per-id outcomes.

        Duplicate ids are deduplicated. Ownership is enforced per id by
        ``username``: an id that is unknown or owned by someone else lands in
        ``skipped`` as ``not_found`` (the batch never 403s on one bad id);
        messages cascade via the FK on delete.

        Args:
            body: The bulk-delete request body carrying the conversation ids.
            user: Authenticated caller; only rows they own are deleted.

        Returns:
            A :class:`BulkDeleteByIdsResponse` listing deleted and skipped ids.
        """
        deleted: list[str] = []
        skipped: list[BulkDeleteByIdsSkipped] = []
        seen: set[str] = set()
        ordered_unique: list[str] = []
        for conversation_id in body.ids:
            if conversation_id in seen:
                continue
            seen.add(conversation_id)
            ordered_unique.append(conversation_id)
        if not ordered_unique:
            return BulkDeleteByIdsResponse(deleted=deleted, skipped=skipped)

        with Session(job_store.engine) as session:
            owned = {
                cast(str, row.id): row
                for row in session.query(AgentConversationModel).filter(
                    AgentConversationModel.id.in_(ordered_unique),
                    AgentConversationModel.username == user.username,
                )
            }
            _drop_shares(session, list(owned))
            for conversation_id in ordered_unique:
                row = owned.get(conversation_id)
                if row is None:
                    skipped.append(BulkDeleteByIdsSkipped(id=conversation_id, reason="not_found"))
                    continue
                session.delete(row)
                deleted.append(conversation_id)
            session.commit()
        return BulkDeleteByIdsResponse(deleted=deleted, skipped=skipped)

    return router


def _owned_conversation(session: Session, conversation_id: str, username: str) -> AgentConversationModel:
    """Load a conversation and check that ``username`` owns it.

    Args:
        session: Active SQLAlchemy session.
        conversation_id: UUID of the conversation.
        username: The caller.

    Returns:
        The conversation row.

    Raises:
        DomainError: 404 when unknown, 403 when another user owns it.
    """
    row = session.get(AgentConversationModel, conversation_id)
    if row is None:
        raise DomainError("agent.conversation.not_found", status=404)
    if row.username != username:
        raise DomainError("agent.conversation.forbidden", status=403)
    return row


def _share_of(session: Session, conversation_id: str) -> AgentChatShareModel | None:
    """Return the live share link of a conversation, if any.

    Args:
        session: Active SQLAlchemy session.
        conversation_id: UUID of the conversation.

    Returns:
        The share row, or ``None``.
    """
    return (
        session.query(AgentChatShareModel)
        .filter(AgentChatShareModel.conversation_id == conversation_id)
        .one_or_none()
    )


def _share_by_token(session: Session, token: str) -> AgentChatShareModel:
    """Return the live share link that carries ``token``.

    Args:
        session: Active SQLAlchemy session.
        token: The token from the link.

    Returns:
        The share row.

    Raises:
        DomainError: 404 for an unknown, malformed or revoked token.
    """
    share = session.get(AgentChatShareModel, token) if 0 < len(token) <= 48 else None
    if share is None:
        raise DomainError("agent.conversation.not_found", status=404)
    return share


def _drop_shares(session: Session, conversation_ids: list[str]) -> None:
    """Revoke the share links of conversations that are about to be deleted.

    The database cascade also removes the rows, but the continued copies
    must lose their link token too, and SQLite does not enforce the cascade.

    Args:
        session: Active SQLAlchemy session.
        conversation_ids: The conversations that are deleted.
    """
    shares = (
        session.query(AgentChatShareModel).filter(AgentChatShareModel.conversation_id.in_(conversation_ids)).all()
    )
    for share in shares:
        session.query(AgentConversationModel).filter(
            AgentConversationModel.source_share_token == share.token
        ).update({AgentConversationModel.source_share_token: None}, synchronize_session=False)
        session.delete(share)


def _fork_of(session: Session, token: str, username: str) -> str | None:
    """Return the id of the caller's chat continued from the link ``token``.

    Args:
        session: Active SQLAlchemy session.
        token: The share token.
        username: The caller.

    Returns:
        The conversation id, or ``None`` when the caller has no copy yet.
    """
    row = (
        session.query(AgentConversationModel.id)
        .filter(
            AgentConversationModel.source_share_token == token,
            AgentConversationModel.username == username,
        )
        .first()
    )
    return cast(str, row[0]) if row is not None else None


def _refresh_fork(session: Session, share: AgentChatShareModel, fork_id: str) -> None:
    """Rebuild the copied tool calls of a continued chat after the owner changed the link.

    The copy names the ticked items in its tool-call rows, and the recipient
    and the model learn item ids only from there. A stale copy would hide a
    newly ticked item and still name an unticked one. Copied rows keep the
    source turn's role and time, so they match the source by that key; the
    recipient's own later turns never match and stay as they are.

    Args:
        session: Active SQLAlchemy session.
        share: The live share link.
        fork_id: The recipient's continued chat.
    """
    fork = session.get(AgentConversationModel, fork_id)
    if fork is None or share.updated_at is None or share.updated_at <= fork.created_at:
        return
    rebuilt: dict[tuple[str, Any], list[Any]] = {}
    for m in _recipient_messages(session, share):
        rebuilt.setdefault((m.role, m.created_at), []).append(m.tool_calls)
    changed = False
    for row in (
        session.query(AgentMessageModel)
        .filter(AgentMessageModel.conversation_id == fork_id, AgentMessageModel.created_at <= fork.created_at)
        .order_by(AgentMessageModel.created_at, AgentMessageModel.id)
    ):
        calls = rebuilt.get((cast(str, row.role), row.created_at))
        if calls:
            row.tool_calls = calls.pop(0)
            changed = True
    if changed:
        session.commit()


def _share_refs(share: AgentChatShareModel) -> list[tuple[str, str]]:
    """Return the ticked items of a share link as ``(kind, id)`` pairs.

    Args:
        share: The share row.

    Returns:
        The ticked items; always empty for a link that is not ``continue_data``.
    """
    if share.link_type != "continue_data":
        return []
    return [
        (str(item.get("kind")), str(item.get("id")))
        for item in (share.items or [])
        if isinstance(item, dict) and item.get("kind") in ITEM_KINDS
    ]


def _refs_in(value: Any, found: list[tuple[str, str]]) -> None:
    """Collect the private item ids that a tool call's arguments or result name.

    Args:
        value: Any JSON-like value from a tool call.
        found: The list that receives new ``(kind, id)`` pairs, in order.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            kind = _ITEM_ID_KEYS.get(str(key))
            if kind is not None and isinstance(item, str) and 0 < len(item) <= 64:
                ref = (kind, item)
                if ref not in found:
                    found.append(ref)
            else:
                _refs_in(item, found)
    elif isinstance(value, list):
        for item in value:
            _refs_in(item, found)


def _call_refs(call: Any) -> list[tuple[str, str]]:
    """Return the private items that one stored tool call refers to.

    Args:
        call: One entry of a message's ``tool_calls``.

    Returns:
        The ``(kind, id)`` pairs in the order they appear.
    """
    found: list[tuple[str, str]] = []
    if isinstance(call, dict):
        _refs_in(call.get("payload"), found)
    return found


def _owned_item_labels(
    session: Session, owner: str, refs: list[tuple[str, str]]
) -> dict[tuple[str, str], str]:
    """Return the label of each item in ``refs`` that still exists and ``owner`` owns.

    Args:
        session: Active SQLAlchemy session.
        owner: The share owner.
        refs: Candidate ``(kind, id)`` pairs.

    Returns:
        A map from each owned pair to its display name.
    """
    ids: dict[str, list[str]] = {kind: [] for kind in ITEM_KINDS}
    for kind, item_id in refs:
        ids[kind].append(item_id)
    labels: dict[tuple[str, str], str] = {}
    if ids["run"]:
        for job in session.query(JobModel).filter(
            JobModel.optimization_id.in_(ids["run"]), JobModel.username == owner
        ):
            name = (job.payload_overview or {}).get("name") if isinstance(job.payload_overview, dict) else None
            labels[("run", cast(str, job.optimization_id))] = str(name or job.optimization_id)
    if ids["dataset"]:
        for dataset in session.query(DatasetModel).filter(
            DatasetModel.id.in_(ids["dataset"]), DatasetModel.owner_username == owner
        ):
            labels[("dataset", cast(str, dataset.id))] = cast(str, dataset.name)
    if ids["file"]:
        for staged in session.query(AgentStagedDatasetModel).filter(
            AgentStagedDatasetModel.id.in_(ids["file"]), AgentStagedDatasetModel.username == owner
        ):
            labels[("file", cast(str, staged.id))] = cast(str, staged.dataset_filename)
    return {ref: _mask_public(label) for ref, label in labels.items()}


def _item_options(
    refs: list[tuple[str, str]], labels: dict[tuple[str, str], str]
) -> list[SharedItemOption]:
    """Build the list rows of a set of items.

    Args:
        refs: The ``(kind, id)`` pairs, in order.
        labels: The labels of the items that still exist.

    Returns:
        One row for each pair; a missing item has ``available`` false.
    """
    return [
        SharedItemOption(kind=kind, id=item_id, label=labels.get((kind, item_id), ""), available=(kind, item_id) in labels)
        for kind, item_id in refs
    ]


def _conversation_messages(session: Session, conversation_id: str) -> list[AgentMessageModel]:
    """Load a conversation's newest ``MAX_SHARED_MESSAGES`` turns in order.

    Args:
        session: Active SQLAlchemy session.
        conversation_id: UUID of the conversation.

    Returns:
        The message rows, oldest first.
    """
    newest = (
        session.query(AgentMessageModel)
        .filter(AgentMessageModel.conversation_id == conversation_id)
        .order_by(AgentMessageModel.created_at.desc(), AgentMessageModel.id.desc())
        .limit(MAX_SHARED_MESSAGES)
        .all()
    )
    return newest[::-1]


def _item_candidates(session: Session, row: AgentConversationModel) -> list[SharedItemOption]:
    """Return the private items that a chat refers to and its owner owns.

    Args:
        session: Active SQLAlchemy session.
        row: The conversation, already owner-checked.

    Returns:
        At most ``MAX_SHARE_ITEMS`` items, in the order the chat first names them.
    """
    refs: list[tuple[str, str]] = []
    for m in _conversation_messages(session, cast(str, row.id)):
        for call in m.tool_calls or []:
            for ref in _call_refs(call):
                if ref not in refs:
                    refs.append(ref)
    labels = _owned_item_labels(session, cast(str, row.username), refs)
    owned = [ref for ref in refs if ref in labels][:MAX_SHARE_ITEMS]
    return _item_options(owned, labels)


def _share_state(
    session: Session, share: AgentChatShareModel | None, candidates: list[SharedItemOption]
) -> ChatShareState:
    """Build the owner's view of a conversation's share link.

    Args:
        session: Active SQLAlchemy session.
        share: The live share row, or ``None``.
        candidates: The items that the link may share.

    Returns:
        The link state. A ticked item is ``available`` while it exists, even
        when the chat no longer names it, because the grant still covers it.
    """
    if share is None:
        return ChatShareState(token=None, link_type=None, items=[], candidates=candidates)
    labels = _owned_item_labels(session, cast(str, share.username), _share_refs(share))
    return ChatShareState(
        token=cast(str, share.token),
        link_type=cast(str, share.link_type),
        items=_item_options(_share_refs(share), labels),
        candidates=candidates,
    )


def _redacted_call(call: Any, ticked: list[tuple[str, str]], labels: dict[tuple[str, str], str]) -> Any:
    """Return the form of one tool call that a user other than the owner may see.

    A call on the public list (model and registry catalogs) stays, masked.
    Every other call can carry the owner's private data, so it becomes a
    ``shared_chat_item`` row with no arguments and no result content. The row
    names only the ticked items that the call referred to.

    Args:
        call: One entry of a message's ``tool_calls``.
        ticked: The items the link shares.
        labels: The labels of the ticked items that still exist.

    Returns:
        The redacted call.
    """
    if not isinstance(call, dict):
        return None
    if call.get("tool") in _PUBLIC_TOOLS:
        return _mask_public(call)
    shared = [ref for ref in _call_refs(call) if ref in ticked and ref in labels]
    items = [{"kind": kind, "id": item_id, "label": labels[(kind, item_id)]} for kind, item_id in shared]
    return {
        "id": _mask_public(str(call.get("id") or "")),
        "tool": "shared_chat_item",
        "reason": "",
        "status": "done",
        "payload": {
            "arguments": {},
            "result": {
                "status": "shared" if items else "not_shared",
                "items": items,
                "hint": _SHARED_HINT if items else _NOT_SHARED_HINT,
            },
        },
    }


def _recipient_messages(session: Session, share: AgentChatShareModel) -> list[ConversationMessage]:
    """Return the shared chat in the form that a user other than the owner may see.

    Args:
        session: Active SQLAlchemy session.
        share: The live share link.

    Returns:
        The turns with costs, token usage and thinking left out, credentials
        and email addresses masked, and every private tool call redacted.
    """
    ticked = _share_refs(share)
    labels = _owned_item_labels(session, cast(str, share.username), ticked)
    messages = []
    for m in _conversation_messages(session, cast(str, share.conversation_id)):
        meta = m.router_metadata if isinstance(m.router_metadata, dict) else {}
        calls = [_redacted_call(call, ticked, labels) for call in (m.tool_calls or [])]
        messages.append(
            ConversationMessage(
                role=cast(str, m.role),
                content=_mask_public(cast(str, m.content)),
                tool_calls=[call for call in calls if call is not None] or None,
                model=cast("str | None", m.model),
                served_model=meta.get("served_model"),
                plan=_mask_public(meta.get("plan")),
                created_at=cast(datetime, m.created_at),
            )
        )
    return messages


def _capped(value: Any) -> Any:
    """Cap a value for a shared item read, so one read stays small for the model.

    Args:
        value: Any JSON-like value.

    Returns:
        The masked value, or a truncated JSON text when it is too large.
    """
    masked = _mask_public(value)
    text_value = json.dumps(masked, ensure_ascii=False, default=str)
    if len(text_value) <= MAX_ITEM_CHARS:
        return masked
    return {"truncated": True, "preview": text_value[:MAX_ITEM_CHARS]}


def _item_content(
    session: Session, dataset_store: DatasetLibraryStore, owner: str, kind: str, item_id: str
) -> SharedItemContent | None:
    """Read one shared item of ``owner``, masked and capped.

    Args:
        session: Active SQLAlchemy session.
        dataset_store: Store that reads dataset rows.
        owner: The share owner; the item must still be theirs.
        kind: ``run``, ``dataset`` or ``file``.
        item_id: The item id.

    Returns:
        The item content, or ``None`` when it no longer exists or changed owner.
    """
    if kind == "run":
        job = session.get(JobModel, item_id)
        if job is None or job.username != owner:
            return None
        overview = job.payload_overview if isinstance(job.payload_overview, dict) else {}
        detail = {
            "status": job.status,
            "optimization_type": job.optimization_type,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
            "latest_metrics": _capped(job.latest_metrics),
            "result": _capped(job.result),
        }
        return SharedItemContent(
            kind="run", id=item_id, label=_mask_public(str(overview.get("name") or item_id)), detail=detail
        )
    if kind == "dataset":
        dataset = session.get(DatasetModel, item_id)
        if dataset is None or dataset.owner_username != owner:
            return None
        rows = dataset_store.get_rows(item_id) or []
        detail = {
            "row_count": dataset.row_count,
            "column_count": dataset.column_count,
            "column_schema": _capped(dataset.column_schema),
            "rows": _capped(rows[:MAX_ITEM_ROWS]),
        }
        return SharedItemContent(kind="dataset", id=item_id, label=_mask_public(cast(str, dataset.name)), detail=detail)
    staged = session.get(AgentStagedDatasetModel, item_id)
    if staged is None or staged.username != owner:
        return None
    detail = {"row_count": staged.row_count, "rows": _capped((staged.rows or [])[:MAX_ITEM_ROWS])}
    return SharedItemContent(
        kind="file", id=item_id, label=_mask_public(cast(str, staged.dataset_filename)), detail=detail
    )


def _mask_public(value: Any) -> Any:
    """Mask credentials and email addresses in a value that another user sees.

    Args:
        value: Any JSON-like value from a stored turn.

    Returns:
        The same structure with secrets and email addresses replaced by the mask.
    """
    masked = mask_secrets(value)
    if isinstance(masked, dict):
        return {key: _mask_public(item) for key, item in masked.items()}
    if isinstance(masked, list):
        return [_mask_public(item) for item in masked]
    if isinstance(masked, str):
        return _EMAIL.sub(MASK, masked)
    return masked


def purge_stale_conversations(engine: Engine, *, threshold_days: int) -> int:
    """Delete unpinned conversations whose last activity is older than ``threshold_days``.

    Pinned conversations are preserved regardless of age — the user explicitly
    marked them as worth keeping. Shared conversations are preserved too, so a
    share link keeps working until the owner revokes it. Messages cascade via the ``ON DELETE CASCADE``
    FK on :class:`AgentMessageModel.conversation_id`.

    Args:
        engine: SQLAlchemy engine the conversations table is bound to.
        threshold_days: Days of inactivity (``updated_at``) before a row is
            eligible for deletion.

    Returns:
        The number of conversation rows deleted in this sweep.
    """
    cutoff = datetime.now(UTC) - timedelta(days=threshold_days)
    with Session(engine) as session:
        deleted = (
            session.query(AgentConversationModel)
            .filter(
                AgentConversationModel.pinned.is_(False),
                AgentConversationModel.id.not_in(session.query(AgentChatShareModel.conversation_id)),
                AgentConversationModel.updated_at < cutoff,
            )
            .delete(synchronize_session=False)
        )
        session.commit()
    if deleted:
        logger.info("Purged %d stale agent conversation(s) older than %d days", deleted, threshold_days)
    return int(deleted)


def _vector_literal(vector: list[float]) -> str:
    """Format a Python float list as a pgvector text literal.

    Args:
        vector: The query embedding as a list of floats.

    Returns:
        The pgvector ``"[v1,v2,...]"`` literal — pgvector parses this on input.
    """
    return "[" + ",".join(f"{v:.7f}" for v in vector) + "]"


def _can_use_semantic_search(session: Session, username: str) -> bool:
    """Return whether the caller's owned conversations are fully embedded.

    Matches the dispatch shape in :func:`dashboard.search_optimizations`:
    semantic search is only used when every in-scope row has a vector, so
    a partially-embedded corpus stays fully searchable instead of silently
    dropping unembedded rows.

    Args:
        session: Active SQLAlchemy session.
        username: Authenticated caller; only their conversations are
            considered.

    Returns:
        True when ``settings.embeddings_enabled`` is true and no owned
        conversation lacks a usable embedding row.
    """
    if not settings.embeddings_enabled:
        return False
    try:
        row = session.execute(
            text(
                "SELECT 1 FROM agent_conversations c "
                "LEFT JOIN conversation_embeddings e ON e.conversation_id = c.id "
                "WHERE c.username = :username "
                "  AND (e.conversation_id IS NULL OR e.embedding_summary IS NULL) "
                "LIMIT 1"
            ),
            {"username": username},
        ).first()
        return row is None
    except Exception as exc:
        logger.warning("Conversation semantic-eligibility probe failed: %s", exc)
        return False


def _semantic_conversation_ids(
    session: Session,
    username: str,
    query: str,
    *,
    pinned: bool | None,
    cap: int,
) -> list[str] | None:
    """Rank the caller's conversations by pgvector cosine distance.

    Args:
        session: Active SQLAlchemy session.
        username: Authenticated caller; rows are filtered to this user.
        query: Pre-trimmed free-text query embedded as ``retrieval.query``.
        pinned: Optional pinned filter mirroring the route argument.
        cap: Maximum number of IDs to return (covers paging).

    Returns:
        Ordered conversation IDs (most similar first), or ``None`` when
        the embedder is unavailable or the ranked query fails — the caller
        should fall back to lexical in either case.
    """
    embedder = get_embedder()
    if not embedder.available():
        return None
    vector = embedder.encode(query, task="retrieval.query", user=username)
    if vector is None:
        return None
    params: dict[str, Any] = {
        "username": username,
        "query_vec": _vector_literal(vector),
        "limit": cap,
    }
    where_parts = [
        "c.username = :username",
        "e.embedding_summary IS NOT NULL",
    ]
    if pinned is not None:
        where_parts.append("c.pinned = :pinned")
        params["pinned"] = pinned
    where_sql = " AND ".join(where_parts)
    try:
        rows = (
            session.execute(
                text(
                    "SELECT c.id "
                    "FROM agent_conversations c "
                    "INNER JOIN conversation_embeddings e ON e.conversation_id = c.id "
                    f"WHERE {where_sql} "
                    "ORDER BY e.embedding_summary <=> CAST(:query_vec AS vector) ASC, "
                    "         c.updated_at DESC "
                    "LIMIT :limit"
                ),
                params,
            )
            .mappings()
            .all()
        )
    except Exception as exc:
        logger.warning("Semantic conversation search failed: %s", exc)
        return None
    return [str(row["id"]) for row in rows]


def _fetch_previews(session: Session, conversation_ids: list[str]) -> dict[str, str]:
    """Look up the first user-message text for each conversation, batched.

    The list view shows a short preview next to the title so users can tell
    threads apart before clicking. We pull the *first* user turn per
    conversation in a single query rather than per-row to keep page latency
    flat as the list grows.

    Args:
        session: Active SQLAlchemy session.
        conversation_ids: Conversation IDs whose previews to fetch.

    Returns:
        ``{conversation_id: trimmed_preview}`` — conversations with no
        user turn yet are omitted.
    """
    if not conversation_ids:
        return {}
    rows = (
        session.query(AgentMessageModel)
        .options(load_only(AgentMessageModel.conversation_id, AgentMessageModel.content))
        .filter(
            AgentMessageModel.conversation_id.in_(conversation_ids),
            AgentMessageModel.role == "user",
        )
        .order_by(AgentMessageModel.conversation_id, AgentMessageModel.created_at.asc())
        .all()
    )
    out: dict[str, str] = {}
    for row in rows:
        cid = cast(str, row.conversation_id)
        if cid in out:
            continue
        content = (cast(str, row.content) or "").strip()
        if not content:
            continue
        out[cid] = content[:200]
    return out
