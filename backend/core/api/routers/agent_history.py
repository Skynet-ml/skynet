"""CRUD routes for persisted generalist-agent conversations. [INTERNAL]

Backs the agent panel's history drawer. Each user owns their own threads;
ownership is enforced on every read and write by comparing the
authenticated principal to the row's ``username``. The companion writer
that *populates* these tables lives in :mod:`generalist_agent` — this
router only reads, renames, pins, and archives. It also lets an owner
share a thread as a public read-only link: the link reads the live
conversation by an unguessable token until the owner revokes it.

In the Mac app (local mode) a link on this computer cannot work for anyone
else, so sharing publishes a masked snapshot of the local chat to the linked
hosted account through the ``/desktop/v1/shared-chats`` routes. Each share
click uploads the latest messages again and keeps the same link.

Hidden from the public Scalar reference — wizard-internal flow.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import or_, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, load_only

from ...config import settings
from ...local import hosted_link
from ...models import (
    BulkDeleteByIdsRequest,
    BulkDeleteByIdsResponse,
    BulkDeleteByIdsSkipped,
)
from ...service_gateway.embedding_pipeline.embeddings import get_embedder
from ...storage.models import AgentConversationModel, AgentMessageModel
from ..auth import AuthenticatedUser, get_authenticated_user
from ..desktop_auth import DesktopPrincipal, get_desktop_principal
from ..errors import DomainError
from .agent_settings import MASK, mask_secrets

logger = logging.getLogger(__name__)

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]
# Module-level so tests can override the dependency by identity.
desktop_sync_principal = get_desktop_principal("sync")
DesktopSyncDep = Annotated[DesktopPrincipal, Depends(desktop_sync_principal)]

MAX_LIST = 100
DEFAULT_LIST = 50
# Caps on a chat snapshot that the Mac app publishes to hosted.
MAX_SHARED_MESSAGES = 1000
MAX_SHARED_BYTES = 2_000_000
# Fixed namespace: the hosted id of a published Mac chat is uuid5 of the hosted
# username and the local chat id, so a re-share updates the same row and link.
_SHARED_CHAT_NAMESPACE = uuid.UUID("6f1d2c4e-8a3b-5d7f-9e1a-2b4c6d8e0f13")
_DESKTOP_SHARED_CHATS = "/desktop/v1/shared-chats"
# A tool result (for example the account settings) can carry an email address,
# and the public link must not show who owns the chat.
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


class ConversationShareResponse(BaseModel):
    """The public read-only link token for a shared conversation."""

    token: str


class SharedConversation(BaseModel):
    """Public read-only view of a shared conversation, without owner or cost data."""

    title: str
    created_at: datetime
    updated_at: datetime
    messages: list[ConversationMessage]


class SharedConversationSummary(BaseModel):
    """One of the owner's shared conversations, for the settings list."""

    id: str
    title: str
    shared_at: datetime | None = None
    token: str


class SharedChatMessage(BaseModel):
    """One turn of a chat snapshot published from the Mac app."""

    role: str = Field(min_length=1, max_length=16)
    content: str = ""
    tool_calls: list[dict[str, Any]] | None = None
    model: str | None = Field(default=None, max_length=128)
    served_model: str | None = Field(default=None, max_length=128)
    plan: str | None = None
    thinking: list[dict[str, Any]] | None = None
    created_at: datetime


class SharedChatImportRequest(BaseModel):
    """A masked chat snapshot that the linked Mac app publishes as a public link."""

    local_id: str = Field(min_length=1, max_length=64, description="Stable id of the chat on the Mac.")
    title: str = Field(default="", max_length=200)
    messages: list[SharedChatMessage] = Field(min_length=1, max_length=MAX_SHARED_MESSAGES)


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
            session.delete(row)
            session.commit()

    @router.post(
        "/agent/conversations/{conversation_id}/share",
        response_model=ConversationShareResponse,
        summary="Create (or return) the public read-only link of a conversation",
    )
    def share_conversation(conversation_id: str, user: AuthenticatedUserDep) -> ConversationShareResponse:
        """Return the conversation's share token, minting one on the first call.

        Args:
            conversation_id: UUID of the conversation to share.
            user: Authenticated caller; must own the row.

        In the Mac app the token belongs to the linked hosted account: the
        local chat is published there as a masked snapshot, again on each call.

        Returns:
            The token; repeated calls return the same token until revoked.

        Raises:
            DomainError: 404 when unknown, 403 when the caller does not own it,
                413 when a Mac snapshot is too large, plus the hosted-link
                errors (409 not linked, 502, 503) in the Mac app.
        """
        with Session(job_store.engine) as session:
            row = session.get(AgentConversationModel, conversation_id)
            if row is None:
                raise DomainError("agent.conversation.not_found", status=404)
            if row.username != user.username:
                raise DomainError("agent.conversation.forbidden", status=403)
            if settings.local_mode:
                snapshot = _local_snapshot(session, row)
            else:
                if not row.share_token:
                    row.share_token = cast(Any, secrets.token_urlsafe(24))
                    row.shared_at = cast(Any, datetime.now(UTC))
                    session.commit()
                    session.refresh(row)
                return ConversationShareResponse(token=cast(str, row.share_token))
        published = hosted_link.hosted_request(
            job_store.engine, user.username, "POST", _DESKTOP_SHARED_CHATS, snapshot.model_dump(mode="json")
        )
        token = published.get("token") if isinstance(published, dict) else None
        if not isinstance(token, str) or not token:
            raise DomainError("hosted_link.failed", status=502)
        return ConversationShareResponse(token=token)

    @router.delete(
        "/agent/conversations/{conversation_id}/share",
        status_code=204,
        summary="Revoke the public read-only link of a conversation",
    )
    def unshare_conversation(conversation_id: str, user: AuthenticatedUserDep) -> None:
        """Clear the conversation's share token so its link stops working.

        In the Mac app the id is a local chat id or a hosted id from the
        shared-chats list, and the linked hosted account revokes the link.

        Args:
            conversation_id: UUID of the conversation to unshare.
            user: Authenticated caller; must own the row.

        Raises:
            DomainError: 404 when unknown, 403 when the caller does not own it,
                plus the hosted-link errors in the Mac app.
        """
        if settings.local_mode:
            hosted_link.hosted_request(
                job_store.engine, user.username, "DELETE", f"{_DESKTOP_SHARED_CHATS}/{conversation_id}"
            )
            return
        with Session(job_store.engine) as session:
            row = session.get(AgentConversationModel, conversation_id)
            if row is None:
                raise DomainError("agent.conversation.not_found", status=404)
            if row.username != user.username:
                raise DomainError("agent.conversation.forbidden", status=403)
            row.share_token = cast(Any, None)
            row.shared_at = cast(Any, None)
            session.commit()

    @router.get(
        "/agent/shared-conversations",
        response_model=list[SharedConversationSummary],
        summary="List the caller's shared conversations",
    )
    def list_shared_conversations(user: AuthenticatedUserDep) -> list[SharedConversationSummary]:
        """Return the caller's conversations that have a live public link, newest share first.

        In the Mac app the list comes from the linked hosted account.

        Args:
            user: Authenticated caller; only their conversations are listed.

        Returns:
            The id, title, share time and link token of each shared conversation.

        Raises:
            DomainError: The hosted-link errors in the Mac app.
        """
        if settings.local_mode:
            rows = hosted_link.hosted_request(job_store.engine, user.username, "GET", _DESKTOP_SHARED_CHATS)
            return [SharedConversationSummary.model_validate(item) for item in rows or []]
        with Session(job_store.engine) as session:
            return _shared_summaries(session, user.username)

    # The body is read by hand, so the byte cap applies before the JSON parse.
    # A declared body parameter would make FastAPI buffer and parse it first.
    @router.post(
        _DESKTOP_SHARED_CHATS,
        response_model=ConversationShareResponse,
        include_in_schema=False,
        summary="Publish a Mac app chat snapshot as a public read-only link",
    )
    async def import_shared_chat(request: Request, principal: DesktopSyncDep) -> ConversationShareResponse:
        """Store the Mac chat snapshot as a shared conversation of the linked account.

        The hosted row id comes from the account and ``local_id``, so sharing the
        same Mac chat again replaces its messages and keeps the same token.

        Args:
            request: The request whose body is the ``SharedChatImportRequest`` JSON.
            principal: Desktop principal with the ``sync`` scope.

        Returns:
            The share token of the hosted copy.

        Raises:
            DomainError: 413 when the body is larger than ``MAX_SHARED_BYTES``.
            RequestValidationError: 422 when the body is not a valid snapshot.
        """
        raw = await _read_capped_snapshot(request)
        try:
            body = SharedChatImportRequest.model_validate_json(raw)
        except ValidationError as exc:
            raise RequestValidationError(exc.errors(include_url=False, include_context=False)) from None
        return await run_in_threadpool(_store_snapshot, principal.username, body)

    def _store_snapshot(username: str, body: SharedChatImportRequest) -> ConversationShareResponse:
        """Write a parsed Mac chat snapshot as the account's shared conversation.

        Args:
            username: The hosted account behind the desktop token.
            body: The snapshot; it is masked again here.

        Returns:
            The share token of the hosted copy.

        Raises:
            DomainError: 403 when the derived row belongs to another account.
        """
        conversation_id = _published_chat_id(username, body.local_id)
        now = datetime.now(UTC)
        with Session(job_store.engine) as session:
            row = session.get(AgentConversationModel, conversation_id)
            if row is None:
                row = AgentConversationModel(id=conversation_id, username=username, created_at=now)
                session.add(row)
            elif row.username != username:
                raise DomainError("agent.conversation.forbidden", status=403)
            row.title = cast(Any, mask_secrets(body.title.strip())[:200])
            row.updated_at = cast(Any, now)
            if not row.share_token:
                row.share_token = cast(Any, secrets.token_urlsafe(24))
                row.shared_at = cast(Any, now)
            session.query(AgentMessageModel).filter(AgentMessageModel.conversation_id == conversation_id).delete(
                synchronize_session=False
            )
            session.add_all(_imported_message(conversation_id, m) for m in body.messages)
            session.commit()
            return ConversationShareResponse(token=cast(str, row.share_token))

    @router.get(
        _DESKTOP_SHARED_CHATS,
        response_model=list[SharedConversationSummary],
        summary="List the linked account's shared conversations",
    )
    def desktop_list_shared_chats(principal: DesktopSyncDep) -> list[SharedConversationSummary]:
        """Return the linked account's shared conversations for the Mac settings list.

        Args:
            principal: Desktop principal with the ``sync`` scope.

        Returns:
            The same rows as ``GET /agent/shared-conversations``.
        """
        with Session(job_store.engine) as session:
            return _shared_summaries(session, principal.username)

    @router.delete(
        _DESKTOP_SHARED_CHATS + "/{chat_id}",
        status_code=204,
        summary="Revoke the public link of one of the linked account's chats",
    )
    def desktop_revoke_shared_chat(chat_id: str, principal: DesktopSyncDep) -> None:
        """Clear the share token of the account's chat named by ``chat_id``.

        ``chat_id`` is a hosted conversation id (from the list) or a Mac chat id
        (from the chat's own menu). An unknown or unshared id is a no-op.

        Args:
            chat_id: Hosted conversation id or Mac chat id.
            principal: Desktop principal with the ``sync`` scope.
        """
        candidates = [chat_id[:36], _published_chat_id(principal.username, chat_id)]
        with Session(job_store.engine) as session:
            for row in session.query(AgentConversationModel).filter(
                AgentConversationModel.id.in_(candidates),
                AgentConversationModel.username == principal.username,
            ):
                row.share_token = cast(Any, None)
                row.shared_at = cast(Any, None)
            session.commit()

    # Deliberately unauthenticated: the token is the capability. Every unknown,
    # malformed or revoked token is the same 404 so a probe learns nothing.
    @router.get(
        "/agent/shared-conversations/{token}",
        response_model=SharedConversation,
        summary="Read a shared conversation by its public link token",
    )
    def get_shared_conversation(token: str) -> SharedConversation:
        """Return the live, read-only, secret-masked view of a shared conversation.

        Args:
            token: The share token from the public link.

        Returns:
            The title and ordered messages. Owner identity, costs and token
            usage are left out, and credentials and email addresses in text and
            tool data are masked. At most ``MAX_SHARED_MESSAGES`` turns are returned.

        Raises:
            DomainError: 404 when no conversation carries the token.
        """
        if not token or len(token) > 64:
            raise DomainError("agent.conversation.not_found", status=404)
        with Session(job_store.engine) as session:
            row = session.query(AgentConversationModel).filter(AgentConversationModel.share_token == token).first()
            if row is None:
                raise DomainError("agent.conversation.not_found", status=404)
            msgs = (
                session.query(AgentMessageModel)
                .filter(AgentMessageModel.conversation_id == row.id)
                .order_by(AgentMessageModel.created_at.asc(), AgentMessageModel.id.asc())
                .limit(MAX_SHARED_MESSAGES)
                .all()
            )
            return SharedConversation(
                title=_mask_public(cast(str, row.title)),
                created_at=cast(datetime, row.created_at),
                updated_at=cast(datetime, row.updated_at),
                messages=[_shared_message(m) for m in msgs],
            )

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


def _shared_message(m: AgentMessageModel) -> ConversationMessage:
    """Project one persisted turn into its public, secret-masked shape.

    Args:
        m: The loaded message row.

    Returns:
        The turn without its token usage or cost (``stats``), with every
        credential and email address in its text, tool calls, plan and
        thinking masked.
    """
    meta = m.router_metadata if isinstance(m.router_metadata, dict) else {}
    return ConversationMessage(
        role=cast(str, m.role),
        content=_mask_public(cast(str, m.content)),
        tool_calls=_mask_public(m.tool_calls) if m.tool_calls else None,
        model=cast("str | None", m.model),
        served_model=meta.get("served_model"),
        plan=_mask_public(meta.get("plan")),
        thinking=_mask_public(meta.get("thinking")),
        created_at=cast(datetime, m.created_at),
    )


def _mask_public(value: Any) -> Any:
    """Mask credentials and email addresses in a value that a public link shows.

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


def _shared_summaries(session: Session, username: str) -> list[SharedConversationSummary]:
    """List the user's conversations that have a live share token.

    Args:
        session: Active SQLAlchemy session.
        username: The owner.

    Returns:
        The rows, newest share first.
    """
    rows = (
        session.query(AgentConversationModel)
        .filter(
            AgentConversationModel.username == username,
            AgentConversationModel.share_token.is_not(None),
        )
        .order_by(AgentConversationModel.shared_at.desc(), AgentConversationModel.updated_at.desc())
        .limit(MAX_LIST)
        .all()
    )
    return [
        SharedConversationSummary(
            id=cast(str, row.id),
            title=cast(str, row.title),
            shared_at=cast("datetime | None", row.shared_at),
            token=cast(str, row.share_token),
        )
        for row in rows
    ]


def _published_chat_id(username: str, local_id: str) -> str:
    """Return the hosted conversation id of a Mac chat published by ``username``.

    Args:
        username: The hosted account.
        local_id: The chat id on the Mac.

    Returns:
        A UUID string that is the same for the same account and Mac chat.
    """
    return str(uuid.uuid5(_SHARED_CHAT_NAMESPACE, f"{username}\n{local_id}"))


async def _read_capped_snapshot(request: Request) -> bytes:
    """Read a snapshot body, stopping as soon as it passes ``MAX_SHARED_BYTES``.

    The declared length is checked first and the stream is counted as it
    arrives, so an oversized or lying upload is refused before it is buffered
    whole or parsed.

    Args:
        request: The incoming import request.

    Returns:
        The raw body bytes.

    Raises:
        DomainError: 413 ``agent.share.too_large``.
    """
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_SHARED_BYTES:
        raise DomainError("agent.share.too_large", status=413, limit=MAX_SHARED_BYTES)
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > MAX_SHARED_BYTES:
            raise DomainError("agent.share.too_large", status=413, limit=MAX_SHARED_BYTES)
        chunks.append(chunk)
    return b"".join(chunks)


def _check_snapshot_size(snapshot: SharedChatImportRequest) -> None:
    """Refuse a chat snapshot whose JSON is larger than ``MAX_SHARED_BYTES``.

    Args:
        snapshot: The snapshot to measure.

    Raises:
        DomainError: 413 ``agent.share.too_large``.
    """
    size = len(json.dumps(snapshot.model_dump(mode="json"), ensure_ascii=False).encode("utf-8"))
    if size > MAX_SHARED_BYTES:
        raise DomainError("agent.share.too_large", status=413, limit=MAX_SHARED_BYTES)


def _local_snapshot(session: Session, row: AgentConversationModel) -> SharedChatImportRequest:
    """Build the masked snapshot of a Mac chat that hosted publishes.

    Args:
        session: Active SQLAlchemy session.
        row: The local conversation, already owner-checked.

    Returns:
        The title and the public, masked shape of each message.

    Raises:
        DomainError: 404 when the chat has no messages, 413 when it is too large.
    """
    msgs = (
        session.query(AgentMessageModel)
        .filter(AgentMessageModel.conversation_id == row.id)
        .order_by(AgentMessageModel.created_at.asc(), AgentMessageModel.id.asc())
        .limit(MAX_SHARED_MESSAGES + 1)
        .all()
    )
    if not msgs:
        raise DomainError("agent.conversation.not_found", status=404)
    if len(msgs) > MAX_SHARED_MESSAGES:
        raise DomainError("agent.share.too_large", status=413, limit=MAX_SHARED_MESSAGES)
    snapshot = SharedChatImportRequest(
        local_id=cast(str, row.id),
        title=mask_secrets(cast(str, row.title))[:200],
        messages=[SharedChatMessage.model_validate(_shared_message(m).model_dump(exclude={"stats"})) for m in msgs],
    )
    _check_snapshot_size(snapshot)
    return snapshot


def _imported_message(conversation_id: str, m: SharedChatMessage) -> AgentMessageModel:
    """Turn one snapshot turn into a masked hosted message row.

    Args:
        conversation_id: The hosted conversation id.
        m: The snapshot turn.

    Returns:
        The new row; plan, thinking and the served model go to ``router_metadata``
        where the public read finds them.
    """
    created_at = m.created_at if m.created_at.tzinfo else m.created_at.replace(tzinfo=UTC)
    return AgentMessageModel(
        conversation_id=conversation_id,
        role=m.role,
        content=mask_secrets(m.content),
        tool_calls=mask_secrets(m.tool_calls) if m.tool_calls else None,
        model=m.model,
        router_metadata={
            "served_model": m.served_model,
            "plan": mask_secrets(m.plan),
            "thinking": mask_secrets(m.thinking),
        },
        created_at=created_at,
    )


def purge_stale_conversations(engine: Engine, *, threshold_days: int) -> int:
    """Delete unpinned conversations whose last activity is older than ``threshold_days``.

    Pinned conversations are preserved regardless of age — the user explicitly
    marked them as worth keeping. Shared conversations are preserved too, so a
    public link keeps working until the owner stops sharing. Messages cascade via the ``ON DELETE CASCADE``
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
                AgentConversationModel.share_token.is_(None),
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
