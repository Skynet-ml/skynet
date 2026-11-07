"""The caller's left run-wizard setup, kept until submit or Start new. [INTERNAL]

Each account has one draft slot with a revision that the server raises on
each write. A save or a delete names the revision it read and changes the
slot only when that revision is still current, so the order never depends
on device clocks. A stale call gets the current slot back and changes
nothing. A delete leaves the row marked deleted, so a save sent before it
cannot land after it. The browser saves the setup while the user works and
when they leave, reads it when the wizard opens on any device, and deletes
it on submit or on Start new. The setup chat travels inside the draft, so it
never becomes a saved conversation.

A page that closes may never read the answer to its last save, so the browser
keeps a copy and sends it again on the next load. Each save names its page
and a count, and the row keeps the latest count of recent pages, so that
second send is refused when the save already landed or when a delete came
after the revision the copy builds on. A page that leaves the list leaves
its last revision behind, and a copy from an unknown page that builds on an
older revision is refused, as its save may have landed.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...storage.models import SetupDraftModel
from ...storage.usage import json_byte_size
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ._helpers import enforce_storage_quota

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]

# Above the browser's own chat cap (300k characters), so a normal setup never trims.
SETUP_DRAFT_MAX_BYTES = 10_000_000
# Each page counts as one entry however often it saves, and a page that only
# opens a setup saves nothing, so this covers many tabs and devices.
SETUP_DRAFT_KEPT_WRITERS = 50


# left_at is stored as BIGINT, so a larger value must fail validation, not the write.
class SetupDraft(BaseModel):
    """A run-wizard setup the user left, with its setup chat."""

    left_at: int = Field(
        ge=0, le=2**63 - 1, description="When the user last changed or left the setup, in epoch milliseconds."
    )
    values: dict[str, str] = Field(description="Raw text of the tab's setup keys, by key, without the setup chat.")
    chat: dict[str, Any] | None = Field(
        default=None, description="The setup chat as id, kicked screens and messages; oldest messages go first."
    )
    staged_dataset_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=36,
        description="The staged upload the setup uses; the staged-dataset sweep keeps it while the draft exists.",
    )

    @model_validator(mode="after")
    def refuse_unstorable(self) -> SetupDraft:
        """Refuse text and numbers the database cannot store, so they fail as a 422, not the write.

        Returns:
            The draft unchanged.

        Raises:
            ValueError: When a string holds a NUL or a lone surrogate, or a number is not finite.
        """
        if not _storable([self.values, self.chat, self.staged_dataset_id]):
            raise ValueError("setup draft holds a NUL, a lone surrogate or a non-finite number")
        return self


# The response puts the draft beside its revision, so a stale caller can
# learn the current revision and send again.
class SetupDraftSlot(BaseModel):
    """The caller's setup draft slot: its revision, the draft it holds, and whether this call changed it."""

    revision: int = Field(ge=0, description="The slot's revision; a save or delete must name it to change the slot.")
    changed: bool = Field(description="True when this call wrote the draft or the delete; false when it was stale.")
    draft: SetupDraft | None = Field(description="The stored draft, or null when there is none or it was deleted.")
    # A page tells its own earlier save from another page's setup by these two.
    writer: str | None = Field(
        default=None, description="The page whose save made this revision, or null when unknown or deleted."
    )
    seq: int | None = Field(default=None, description="That page's count for the save, or null when unknown.")


def _storable(value: Any) -> bool:
    """Tell whether Postgres JSONB and VARCHAR can hold a value.

    Args:
        value: A JSON value from the request.

    Returns:
        False when a string or key holds a NUL or a lone surrogate, or a number is NaN or infinite.
    """
    if isinstance(value, str):
        if "\x00" in value:
            return False
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return False
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_storable(key) and _storable(item) for key, item in value.items())
    if isinstance(value, list):
        return all(_storable(item) for item in value)
    return True


def _size(value: Any) -> int:
    """Return the size of ``value`` as compact UTF-8 JSON, in bytes.

    Args:
        value: Any JSON-serializable value.

    Returns:
        The encoded size in bytes.
    """
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def _fit_chat(values: dict[str, str], chat: dict[str, Any] | None) -> dict[str, Any] | None:
    """Cut the chat so the draft fits ``SETUP_DRAFT_MAX_BYTES``, newest messages kept.

    Args:
        values: The setup's keys, which are never cut.
        chat: The setup chat, or ``None``.

    Returns:
        The chat, whole or without its oldest messages.

    Raises:
        DomainError: 413 when the draft is too large even with no chat messages.
    """
    room = SETUP_DRAFT_MAX_BYTES - _size({"values": values, "chat": None})
    if chat is None or _size(chat) - _size(None) <= room:
        if room < 0:
            raise DomainError("wizard.setup_draft.too_large", status=413)
        return chat
    messages = chat.get("messages")
    messages = messages if isinstance(messages, list) else []
    room -= _size({**chat, "messages": []}) - _size(None)
    if room < 0:
        raise DomainError("wizard.setup_draft.too_large", status=413)
    start = len(messages)
    while start > 0:
        cost = _size(messages[start - 1]) + 1
        if cost > room:
            break
        room -= cost
        start -= 1
    return {**chat, "messages": messages[start:]}


def _stored_size(values: dict[str, str], chat: dict[str, Any] | None) -> int:
    """Return the bytes a draft counts in the storage budget.

    Args:
        values: The setup's keys.
        chat: The setup chat, or ``None``.

    Returns:
        The size of both, as compact UTF-8 JSON.
    """
    return json_byte_size(values) + json_byte_size(chat)


def _slot(row: SetupDraftModel | None, *, changed: bool) -> SetupDraftSlot:
    """Render the caller's slot.

    Args:
        row: The stored row, or ``None``.
        changed: Whether this call wrote the row.

    Returns:
        The slot as the browser reads it, with the page and count of the save
        that made the current revision when the row still lists it.
    """
    if row is None:
        return SetupDraftSlot(revision=0, changed=changed, draft=None)
    draft = (
        None
        if row.deleted
        else SetupDraft(
            left_at=row.left_at, values=row.draft_values, chat=row.chat, staged_dataset_id=row.staged_dataset_id
        )
    )
    made = next((entry for entry in row.saves or [] if entry[2] == row.revision), None) if draft else None
    return SetupDraftSlot(
        revision=row.revision,
        changed=changed,
        draft=draft,
        writer=made[0] if made else None,
        seq=made[1] if made else None,
    )


def _revision(row: SetupDraftModel | None) -> int:
    """Return the slot's current revision; a missing row is revision 0.

    Args:
        row: The stored row, or ``None``.

    Returns:
        The revision.
    """
    return 0 if row is None else row.revision


def _copy_is_stale(row: SetupDraftModel | None, writer: str | None, seq: int | None, copy_base: int) -> bool:
    """Tell whether a browser copy sent again must not change the slot.

    Args:
        row: The stored row, or ``None``.
        writer: The page that made the copy's save, or ``None``.
        seq: The page's count for that save, or ``None``.
        copy_base: The revision the copy builds on.

    Returns:
        True when the copy's save already landed, or may have landed and left
        the list, or a delete came after its base.
    """
    if row is None:
        return False
    counts = [s for w, s, _ in row.saves if w == writer]
    if not counts:
        # The page left the list or never saved; a copy older than the last page
        # that left may hold a save that landed, so it never undoes later edits.
        return copy_base < row.evicted_revision or row.deleted_revision > copy_base
    landed = seq is not None and max(counts) >= seq
    return landed or row.deleted_revision > copy_base


def _with_save(saves: list[list[Any]] | None, writer: str, seq: int, revision: int) -> tuple[list[list[Any]], int]:
    """Record a page's save, keeping only the latest pages.

    Args:
        saves: The row's [writer, seq, revision] entries, oldest first, or ``None`` on a new row.
        writer: The page that sent the save.
        seq: The page's count for the save.
        revision: The revision the save makes.

    Returns:
        The new entries, with this page last, and the highest revision of a
        page that left the list, or 0.
    """
    saves = saves or []
    # A page's saves can land out of order; the higher count already covers the lower.
    last = max((s for w, s, _ in saves if w == writer), default=0)
    kept = [*[entry for entry in saves if entry[0] != writer], [writer, max(seq, last), revision]]
    dropped = kept[:-SETUP_DRAFT_KEPT_WRITERS]
    return kept[-SETUP_DRAFT_KEPT_WRITERS:], max((r for _, _, r in dropped), default=0)


def create_setup_draft_router(*, job_store) -> APIRouter:
    """Build the caller-scoped setup draft router.

    Args:
        job_store: Job-store instance whose ORM engine keeps the drafts and
            that resolves the caller's storage budget.

    Returns:
        A router with get, put and delete operations for the caller's draft.
    """
    router = APIRouter()

    @router.get(
        "/wizard/setup-draft",
        response_model=SetupDraftSlot,
        operation_id="get_setup_draft",
        summary="Read the caller's left run-wizard setup",
    )
    def get_setup_draft(user: AuthenticatedUserDep) -> SetupDraftSlot:
        """Return the caller's slot; its draft is null when there is none.

        Args:
            user: Authenticated caller.

        Returns:
            The slot.
        """
        with Session(job_store.engine) as session:
            return _slot(session.get(SetupDraftModel, user.username), changed=False)

    @router.put(
        "/wizard/setup-draft",
        response_model=SetupDraftSlot,
        operation_id="put_setup_draft",
        summary="Replace the caller's left run-wizard setup",
    )
    def put_setup_draft(
        body: SetupDraft,
        user: AuthenticatedUserDep,
        base_revision: Annotated[int, Query(ge=0, description="The slot revision the caller last read.")],
        writer: Annotated[
            str | None, Query(min_length=1, max_length=64, description="A random id of the browser page that saves.")
        ] = None,
        seq: Annotated[
            int | None, Query(ge=1, description="The page's count of saves; a later save counts higher.")
        ] = None,
        copy_base: Annotated[
            int | None,
            Query(ge=0, description="Set when the save is a browser copy sent again: the revision the copy builds on."),
        ] = None,
    ) -> SetupDraftSlot:
        """Store the draft when ``base_revision`` is still current.

        Args:
            body: The setup to keep.
            user: Authenticated caller.
            base_revision: The revision the caller last read.
            writer: The page that saves, or ``None``.
            seq: The page's count for this save, or ``None``.
            copy_base: The revision a browser copy builds on, or ``None`` for a normal save.

        Returns:
            The slot after the call; ``changed`` is false when the revision
            was stale or a copy sent again was stale, and the slot is then the
            current one.

        Raises:
            DomainError: ``user.storage.quota_exceeded`` (409) when a larger
                draft would push the caller past their storage budget.
        """
        chat = _fit_chat(body.values, body.chat)
        new_size = _stored_size(body.values, chat)

        def write(session: Session) -> tuple[SetupDraftModel | None, bool]:
            """Apply the body to the caller's row when the revision still matches.

            The row lock holds from the revision check to the commit, so two
            calls from the same revision cannot both write, and the budget
            check sees the size the write replaces.

            Args:
                session: Open ORM session.

            Returns:
                The row, not yet committed, and whether it changed.
            """
            row = session.get(SetupDraftModel, user.username, with_for_update=True)
            if _revision(row) != base_revision:
                return row, False
            if copy_base is not None and _copy_is_stale(row, writer, seq, copy_base):
                return row, False
            old_size = 0 if row is None or row.deleted else _stored_size(row.draft_values, row.chat)
            if new_size > old_size:
                enforce_storage_quota(job_store, user.username, incoming_bytes=new_size - old_size)
            if row is None:
                row = SetupDraftModel(username=user.username, revision=0)
                session.add(row)
            row.revision = base_revision + 1
            row.left_at = body.left_at
            row.draft_values = body.values
            row.chat = chat
            row.staged_dataset_id = body.staged_dataset_id
            row.deleted = False
            if writer is not None and seq is not None:
                row.saves, evicted = _with_save(row.saves, writer, seq, row.revision)
                row.evicted_revision = max(row.evicted_revision or 0, evicted)
            row.updated_at = datetime.now(UTC)
            return row, True

        return _commit(job_store.engine, write)

    @router.delete(
        "/wizard/setup-draft",
        response_model=SetupDraftSlot,
        operation_id="delete_setup_draft",
        summary="Delete the caller's left run-wizard setup",
    )
    def delete_setup_draft(
        user: AuthenticatedUserDep,
        revision: Annotated[int, Query(ge=0, description="The slot revision the caller last read.")],
    ) -> SetupDraftSlot:
        """Delete the caller's draft when ``revision`` is still current.

        A draft another device saved after the caller's read stays. The row
        stays as a deleted mark with a new revision, so a save sent before
        the delete is refused. The next sweep may then purge the draft's upload.

        Args:
            user: Authenticated caller.
            revision: The revision the caller last read.

        Returns:
            The slot after the call; ``changed`` is false when it was stale.
        """

        def write(session: Session) -> tuple[SetupDraftModel | None, bool]:
            """Turn the caller's row into a deleted mark when the revision still matches.

            Args:
                session: Open ORM session.

            Returns:
                The row, not yet committed, and whether it changed.
            """
            row = session.get(SetupDraftModel, user.username, with_for_update=True)
            if _revision(row) != revision:
                return row, False
            if row is None:
                row = SetupDraftModel(username=user.username, left_at=0)
                session.add(row)
            row.revision = revision + 1
            row.draft_values = {}
            row.chat = None
            row.staged_dataset_id = None
            row.deleted = True
            row.deleted_revision = row.revision
            row.updated_at = datetime.now(UTC)
            return row, True

        return _commit(job_store.engine, write)

    return router


def _commit(engine, write) -> SetupDraftSlot:
    """Run ``write`` in a session and commit, once more when a first write raced.

    Args:
        engine: The ORM engine that keeps the drafts.
        write: Applies the call to the row and returns it and whether it changed.

    Returns:
        The slot after the commit.
    """
    with Session(engine) as session:
        row, changed = write(session)
        try:
            session.commit()
        except IntegrityError:
            # A missing row has nothing to lock, so two first writes can race to
            # insert it; the later one then sees the row and is stale.
            session.rollback()
            row, changed = write(session)
            session.commit()
        if row is not None:
            session.refresh(row)
        return _slot(row, changed=changed)
