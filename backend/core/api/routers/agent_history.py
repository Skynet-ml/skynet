"""CRUD routes for persisted generalist-agent conversations. [INTERNAL]

Backs the agent panel's history drawer. Each user owns their own threads;
ownership is enforced on every read and write by comparing the
authenticated principal to the row's ``username``. The companion writer
that *populates* these tables lives in :mod:`generalist_agent` — this
router only reads, renames, pins, and archives.

A run keeps the setup chat that created it (``jobs.conversation_id``), and
:func:`conversation_detail` also serves that chat to the run's editors.

Hidden from the public Scalar reference — wizard-internal flow.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, load_only

from ...config import settings
from ...models import (
    BulkDeleteByIdsRequest,
    BulkDeleteByIdsResponse,
    BulkDeleteByIdsSkipped,
)
from ...service_gateway.embedding_pipeline.embeddings import get_embedder
from ...storage.models import (
    AgentConversationModel,
    AgentMessageModel,
    JobModel,
)
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError

logger = logging.getLogger(__name__)

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]
MAX_LIST = 100
DEFAULT_LIST = 50


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
    # The chat compaction that starts at this user turn, from router_metadata.
    compaction: dict[str, Any] | None = None
    # The cost of summary calls that stored no compaction, from router_metadata.
    side_cents: float | None = None
    # The row id; a compaction names the user message it starts at by this id.
    id: int | None = None
    created_at: datetime


class ConversationPatchRequest(BaseModel):
    """Partial update — at least one of ``title`` or ``pinned`` required."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    pinned: bool | None = None


# Fork keeps every turn before the user turn with this 0-based number.
class ConversationForkRequest(BaseModel):
    user_turns: int = Field(ge=0)


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
            return conversation_detail(session, row)

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
        "/agent/conversations/{conversation_id}/fork",
        response_model=ConversationSummary,
        summary="Copy the start of a conversation into a new conversation",
    )
    def fork_conversation(
        conversation_id: str,
        req: ConversationForkRequest,
        user: AuthenticatedUserDep,
    ) -> ConversationSummary:
        """Save a new conversation with copies of the turns before one user turn.

        Args:
            conversation_id: UUID of the source conversation.
            req: How many user turns, from the start, the fork keeps.
            user: Authenticated caller; must own the source conversation.

        Returns:
            The new conversation projected as ``ConversationSummary``.

        Raises:
            DomainError: 404 when unknown, 403 when the caller does not own it.
        """
        with Session(job_store.engine) as session:
            source = session.get(AgentConversationModel, conversation_id)
            if source is None:
                raise DomainError("agent.conversation.not_found", status=404)
            if source.username != user.username:
                raise DomainError("agent.conversation.forbidden", status=403)
            msgs = (
                session.query(AgentMessageModel)
                .filter(AgentMessageModel.conversation_id == source.id)
                .order_by(AgentMessageModel.created_at.asc(), AgentMessageModel.id.asc())
                .all()
            )
            kept = _turns_before_user_turn(msgs, req.user_turns)
            now = datetime.now(UTC)
            row = AgentConversationModel(
                id=str(uuid4()),
                username=user.username,
                title=source.title,
                pinned=False,
                archived_at=None,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            # The wizard state stays on the source rows, so one turn never
            # counts twice in the training-ground export. The source chat paid
            # for its turns, so the copies carry no cost.
            session.add_all(
                AgentMessageModel(
                    conversation_id=row.id,
                    role=m.role,
                    content=m.content,
                    tool_calls=m.tool_calls,
                    model=m.model,
                    router_metadata=_fork_metadata(m.router_metadata),
                    created_at=m.created_at,
                )
                for m in kept
            )
            session.commit()
            session.refresh(row)
            return _row_to_summary(row, _fetch_previews(session, [row.id]).get(row.id))

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


def conversation_detail(
    session: Session,
    row: AgentConversationModel,
    *,
    run_started_at: datetime | None = None,
) -> ConversationDetail:
    """Serialize one conversation and every persisted turn in order.

    Args:
        session: Active SQLAlchemy session.
        row: The conversation, already access-checked by the caller.
        run_started_at: When a run's setup chat is read, the run's creation
            time. The chat then ends with the turn that started the run,
            including the agent's reply saved after it; later turns are left
            out.

    Returns:
        The ``ConversationDetail`` with messages ordered ``created_at`` ascending.
    """
    msgs = (
        session.query(AgentMessageModel)
        .filter(AgentMessageModel.conversation_id == row.id)
        .order_by(AgentMessageModel.created_at.asc(), AgentMessageModel.id.asc())
        .all()
    )
    if run_started_at is not None:
        msgs = msgs[: _setup_end(msgs, _aware(run_started_at))]
    messages = []
    for m in msgs:
        meta = m.router_metadata if isinstance(m.router_metadata, dict) else {}
        messages.append(
            ConversationMessage(
                role=cast(str, m.role),
                content=cast(str, m.content),
                tool_calls=cast("list[dict[str, Any]] | None", m.tool_calls),
                model=cast("str | None", m.model),
                served_model=meta.get("served_model"),
                stats=meta.get("stats"),
                plan=meta.get("plan"),
                thinking=meta.get("thinking"),
                compaction=meta.get("compaction"),
                side_cents=meta.get("side_cents"),
                id=cast(int, m.id),
                created_at=cast(datetime, m.created_at),
            )
        )
    if run_started_at is not None:
        # The owner's later pins, archives and turns are not part of this run's setup.
        return ConversationDetail(
            id=cast(str, row.id),
            title=cast(str, row.title),
            pinned=False,
            archived_at=None,
            created_at=cast(datetime, row.created_at),
            updated_at=messages[-1].created_at if messages else cast(datetime, row.created_at),
            messages=messages,
        )
    return ConversationDetail(
        id=cast(str, row.id),
        title=cast(str, row.title),
        pinned=cast(bool, row.pinned),
        archived_at=cast("datetime | None", row.archived_at),
        created_at=cast(datetime, row.created_at),
        updated_at=cast(datetime, row.updated_at),
        messages=messages,
    )


def _fork_metadata(meta: Any) -> dict[str, Any] | None:
    """Copy one row's ``router_metadata`` for a fork, without the turn's cost.

    The compaction summary stays, because the fork's next turn needs it; only
    its cost goes.

    Args:
        meta: The source row's ``router_metadata``.

    Returns:
        The copy, or ``None`` when the source row has none.
    """
    if not isinstance(meta, dict):
        return None
    copy = {key: value for key, value in meta.items() if key not in ("stats", "side_cents")}
    if isinstance(copy.get("compaction"), dict):
        copy["compaction"] = {**copy["compaction"], "cents": None}
    return copy


def _turns_before_user_turn(msgs: list[AgentMessageModel], user_turns: int) -> list[AgentMessageModel]:
    """Return the ordered turns before the user turn with 0-based number ``user_turns``.

    Args:
        msgs: A conversation's turns, ordered oldest first.
        user_turns: How many user turns the result keeps.

    Returns:
        Every turn before that user turn, or all turns when it does not exist.
    """
    seen = 0
    for index, m in enumerate(msgs):
        if m.role != "user":
            continue
        if seen == user_turns:
            return msgs[:index]
        seen += 1
    return msgs


def _setup_end(msgs: list[AgentMessageModel], started: datetime) -> int:
    """Return how many of a chat's ordered turns belong to the run started at ``started``.

    The agent may save its reply to the turn that started the run after the
    run, so the chat ends at the first new user turn after the start. A steer
    sent while that turn runs is part of it, not a new turn.

    Args:
        msgs: The chat's turns, ordered by ``created_at``.
        started: The run's creation time, timezone-aware.

    Returns:
        The count of leading turns to keep.
    """
    return next(
        (
            i
            for i, m in enumerate(msgs)
            if m.role == "user"
            and _aware(cast(datetime, m.created_at)) > started
            and not (isinstance(m.router_metadata, dict) and m.router_metadata.get("steer"))
        ),
        len(msgs),
    )


def _aware(moment: datetime) -> datetime:
    """Read a naive timestamp as UTC, so SQLite and Postgres rows compare alike."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def purge_stale_conversations(engine: Engine, *, threshold_days: int) -> int:
    """Delete unpinned conversations whose last activity is older than ``threshold_days``.

    Pinned conversations are preserved regardless of age — the user explicitly
    marked them as worth keeping. The setup chat of a run is preserved too,
    because the run's owner, admins and editors read that chat. Messages cascade
    via the ``ON DELETE CASCADE`` FK on :class:`AgentMessageModel.conversation_id`.

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
                # NOT IN over a NULL matches nothing, so unbound runs stay out of the subquery.
                AgentConversationModel.id.not_in(
                    session.query(JobModel.conversation_id).filter(JobModel.conversation_id.isnot(None))
                ),
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
