"""Run folders with Google-Drive-style sharing.

Folders group optimizations in the sidebar and share them as a unit. The rules
follow Drive's "My Drive" model (see :mod:`core.api.folder_access`):

* ``GET    /folders`` — every folder the caller can reach (owned, shared, and
  everything nested beneath those).
* ``POST   /folders`` — create a folder, optionally inside another (editor+ on
  the parent).
* ``PATCH  /folders/{id}`` — rename (editor+) or flip ``editors_can_share``
  (owner).
* ``DELETE /folders/{id}`` — delete a folder and its subfolders (owner). Runs
  inside are never deleted; they return to the unfiled list.
* ``POST   /folders/{id}/move`` — move a folder (owner, editor+ on the target).
* ``GET    /folders/{id}/runs`` — the runs filed directly in a folder (viewer+).
* ``GET    /folders/runs`` — the caller's own runs with their folders, compact,
  for an agent that sorts runs into folders.
* ``POST   /folders/items`` — file runs into a folder, or unfile them.
* ``GET/PUT /folders/{id}/sharing`` plus ``members`` and ``transfer`` — the
  same sharing surface as datasets, gated by ``editors_can_share``.
* ``POST   /folders/share/{token}/claim`` — redeem an ``anyone`` link.

The tree, filing and listing routes carry the ``agent`` tag, so the MCP server
exposes them as tools. The sharing routes stay REST-only.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from ...constants import (
    PAYLOAD_OVERVIEW_DATASET_FILENAME,
    PAYLOAD_OVERVIEW_DESCRIPTION,
    PAYLOAD_OVERVIEW_MODEL_NAME,
    PAYLOAD_OVERVIEW_MODULE_NAME,
    PAYLOAD_OVERVIEW_NAME,
    PAYLOAD_OVERVIEW_OPTIMIZER_NAME,
)
from ...storage.models import (
    RunFolderItemModel,
    RunFolderModel,
    RunFolderShareGrantModel,
    RunFolderShareLinkModel,
)
from ..auth import AuthenticatedUser, get_authenticated_user, is_admin
from ..converters import job_owner, parse_overview, parse_timestamp
from ..errors import DomainError
from ..folder_access import (
    can_share,
    folder_chain,
    folder_of_run,
    get_active_link,
    get_folder,
    get_grant,
    get_link_by_token,
    list_grants,
    require_folder_role,
    resolve_folder_role,
    resolve_share_access,
    subtree_ids,
)
from ..sharing_access import (
    GENERAL_ACCESS_ANYONE,
    GENERAL_ACCESS_RESTRICTED,
    LINK_GRANT_MARKER,
    LINK_ROLES,
    MEMBER_ROLES,
    ShareRole,
    _normalize_username,
    role_rank,
)
from ._helpers import get_job_no_payload, grant_roles_for, pausable_id_flags, resumable_id_flags, run_folder_ids
from .constants import VALID_STATUSES
from .optimizations.listing import build_sidebar_item
from .optimizations.schemas import SidebarJobItem

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]

_GENERAL_ACCESS_VALUES = (GENERAL_ACCESS_RESTRICTED, GENERAL_ACCESS_ANYONE)
_LINK_ROLE_VALUES = tuple(sorted(LINK_ROLES))
_MAX_NAME_LENGTH = 120
_MAX_MOVE_BATCH = 200
_AGENT_TAG = "agent"
# The organize listing filters in Python, so it reads every owned run from the
# store in pages of this size to keep each query and folder lookup bounded.
_ORGANIZE_SCAN_PAGE = 500
_ORGANIZE_DEFAULT_LIMIT = 100
_ORGANIZE_MAX_LIMIT = 200
_TASK_PREVIEW_CHARS = 160


class FolderItem(BaseModel):
    """One folder as the sidebar tree sees it."""

    id: str
    name: str
    parent_id: str | None = None
    owner: str
    role: str
    shared: bool = False
    editors_can_share: bool = True
    run_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class FoldersResponse(BaseModel):
    """Envelope for ``GET /folders``."""

    folders: list[FolderItem]


class CreateFolderRequest(BaseModel):
    """Request body for ``POST /folders``."""

    name: str = Field(min_length=1, max_length=_MAX_NAME_LENGTH)
    parent_id: str | None = None
    reuse_existing: bool = Field(
        default=False,
        description=(
            "Return the caller's folder with the same name (case-insensitive) under the same parent "
            "instead of creating a duplicate; makes retries safe"
        ),
    )


class UpdateFolderRequest(BaseModel):
    """Request body for ``PATCH /folders/{id}``."""

    name: str | None = Field(default=None, min_length=1, max_length=_MAX_NAME_LENGTH)
    editors_can_share: bool | None = None


class DeleteFolderResponse(BaseModel):
    """Response for ``DELETE /folders/{id}``."""

    deleted: list[str]


class MoveFolderRequest(BaseModel):
    """Request body for ``POST /folders/{id}/move``; ``None`` moves to the top level."""

    parent_id: str | None = None


class FolderRunsResponse(BaseModel):
    """Envelope for ``GET /folders/{id}/runs``."""

    items: list[SidebarJobItem]


class MoveRunsRequest(BaseModel):
    """Request body for ``POST /folders/items``; ``folder_id=None`` unfiles the runs."""

    optimization_ids: list[str] = Field(min_length=1, max_length=_MAX_MOVE_BATCH)
    folder_id: str | None = None


class OrganizeRunItem(BaseModel):
    """One run as the organize listing shows it."""

    optimization_id: str
    name: str | None = None
    task: str | None = None
    dataset: str | None = None
    model: str | None = None
    status: str
    folder_id: str | None = None
    folder: str | None = None
    created_at: datetime | None = None


class OrganizeRunsResponse(BaseModel):
    """Envelope for ``GET /folders/runs``."""

    items: list[OrganizeRunItem]
    total_runs: int
    unfiled_runs: int
    matched: int
    next_offset: int | None = None
    hint: str


class MoveRunsSkipped(BaseModel):
    """A run left where it was, with the reason."""

    optimization_id: str
    reason: str


class MoveRunsResponse(BaseModel):
    """Response for ``POST /folders/items``."""

    updated: list[str]
    skipped: list[MoveRunsSkipped]


class FolderSharingMember(BaseModel):
    """One member of a folder (username + tier role)."""

    username: str
    role: str


class InheritedFolderMember(BaseModel):
    """Access a member holds through a parent folder, shown read-only."""

    username: str
    role: str
    folder_id: str
    folder_name: str


class FolderSharingState(BaseModel):
    """Sharing config for one folder, as its managers see it."""

    general_access: str
    general_role: str = "viewer"
    token: str | None = None
    share_path: str | None = None
    owner: str | None = None
    role: str
    can_manage: bool = False
    editors_can_share: bool = True
    members: list[FolderSharingMember] = Field(default_factory=list)
    inherited: list[InheritedFolderMember] = Field(default_factory=list)


class PutFolderSharingRequest(BaseModel):
    """Request body for ``PUT /folders/{id}/sharing``."""

    general_access: str
    general_role: str | None = None


class AddFolderMemberRequest(BaseModel):
    """Request body for ``POST /folders/{id}/sharing/members``."""

    username: str
    role: str


class UpdateFolderMemberRequest(BaseModel):
    """Request body for ``PATCH /folders/{id}/sharing/members/{username}``."""

    role: str


class TransferFolderOwnershipRequest(BaseModel):
    """Request body for ``POST /folders/{id}/sharing/transfer``."""

    username: str


class ClaimFolderResponse(BaseModel):
    """Envelope for ``POST /folders/share/{token}/claim``."""

    folder_id: str
    role: str


def _clean_name(name: str) -> str:
    """Return a trimmed folder name, rejecting one that is blank once trimmed.

    Args:
        name: Raw name from the request.

    Returns:
        The trimmed name.

    Raises:
        DomainError: 400 ``folder.invalid_name`` when only whitespace remains.
    """
    cleaned = name.strip()
    if not cleaned:
        raise DomainError("folder.invalid_name", status=400)
    return cleaned


def _inherited_members(session: Session, folder: RunFolderModel) -> list[InheritedFolderMember]:
    """Return the best grant each member holds on the folder's ancestors.

    Args:
        session: Open DB session.
        folder: Folder whose parents are inspected.

    Returns:
        One entry per grantee, carrying the ancestor that gives the best role.
    """
    best: dict[str, InheritedFolderMember] = {}
    for ancestor in folder_chain(session, folder.id)[1:]:
        for grant in list_grants(session, ancestor.id):
            if grant.role not in MEMBER_ROLES:
                continue
            current = best.get(grant.grantee_username)
            if current is None or role_rank(grant.role) > role_rank(current.role):
                best[grant.grantee_username] = InheritedFolderMember(
                    username=grant.grantee_username,
                    role=grant.role,
                    folder_id=ancestor.id,
                    folder_name=ancestor.name,
                )
    return sorted(best.values(), key=lambda m: m.username)


def _inherited_role(session: Session, folder: RunFolderModel, username: str) -> str | None:
    """Return the best role ``username`` already holds through the folder's parents.

    Args:
        session: Open DB session.
        folder: Folder being shared.
        username: Normalized grantee.

    Returns:
        ``editor`` when they own a parent, else their best parent grant, or ``None``.
    """
    ancestors = folder_chain(session, folder.id)[1:]
    if any(_normalize_username(a.owner_username) == username for a in ancestors):
        return str(ShareRole.editor)
    roles = [m.role for m in _inherited_members(session, folder) if m.username == username]
    return roles[0] if roles else None


def _find_owned_folder(session: Session, owner: str, name: str, parent_id: str | None) -> RunFolderModel | None:
    """Return the owner's folder named ``name`` under ``parent_id``, ignoring case.

    Args:
        session: Open DB session.
        owner: Normalized owner username.
        name: Trimmed folder name to match.
        parent_id: Parent folder id, or ``None`` for the top level.

    Returns:
        The oldest matching folder, or ``None``.
    """
    parent_clause = RunFolderModel.parent_id.is_(None) if parent_id is None else RunFolderModel.parent_id == parent_id
    candidates = session.scalars(
        select(RunFolderModel)
        .where(RunFolderModel.owner_username == owner, parent_clause)
        .order_by(RunFolderModel.created_at)
    ).all()
    wanted = name.casefold()
    return next((f for f in candidates if f.name.strip().casefold() == wanted), None)


def _organize_item(row: dict, folder_id: str | None, folder_name: str | None) -> OrganizeRunItem:
    """Build the compact organize-listing entry for one job row.

    Args:
        row: Raw job row from the store.
        folder_id: Folder the run is filed in, if any.
        folder_name: That folder's name when the caller can see it.

    Returns:
        The populated :class:`OrganizeRunItem`.
    """
    overview = parse_overview(row)
    task = (overview.get(PAYLOAD_OVERVIEW_DESCRIPTION) or "").strip() or None
    if task and len(task) > _TASK_PREVIEW_CHARS:
        task = task[: _TASK_PREVIEW_CHARS - 1].rstrip() + "…"
    return OrganizeRunItem(
        optimization_id=row["optimization_id"],
        name=overview.get(PAYLOAD_OVERVIEW_NAME),
        task=task,
        dataset=overview.get(PAYLOAD_OVERVIEW_DATASET_FILENAME),
        model=overview.get(PAYLOAD_OVERVIEW_MODEL_NAME),
        status=row.get("status", "pending"),
        folder_id=folder_id,
        folder=folder_name,
        created_at=parse_timestamp(row.get("created_at")),
    )


def _matches(item: OrganizeRunItem, row: dict, needle: str) -> bool:
    """Return whether a run's text fields contain ``needle``.

    Args:
        item: The run's compact entry.
        row: The raw job row, for the module and optimizer names.
        needle: Lower-cased search text.

    Returns:
        ``True`` when any searchable field contains ``needle``.
    """
    overview = parse_overview(row)
    fields = (
        item.name,
        overview.get(PAYLOAD_OVERVIEW_DESCRIPTION),
        item.dataset,
        item.model,
        item.folder,
        overview.get(PAYLOAD_OVERVIEW_MODULE_NAME),
        overview.get(PAYLOAD_OVERVIEW_OPTIMIZER_NAME),
        item.optimization_id,
    )
    return any(needle in str(value).lower() for value in fields if value)


def create_folders_router(*, job_store) -> APIRouter:
    """Build the run-folder router.

    Args:
        job_store: Storage backend whose ``engine`` carries the jobs and folder
            tables.

    Returns:
        A FastAPI ``APIRouter`` with the folder, filing and sharing routes.
    """
    router = APIRouter()

    def _engine():
        """Return the job store's engine, or fail when folders are unsupported.

        Returns:
            The SQLAlchemy engine.

        Raises:
            DomainError: 404 ``folder.not_found`` on stores without an engine.
        """
        engine = getattr(job_store, "engine", None)
        if engine is None:
            raise DomainError("folder.not_found", status=404)
        return engine

    def _sharing_state(session: Session, folder: RunFolderModel, role: ShareRole) -> FolderSharingState:
        """Assemble the :class:`FolderSharingState` for one folder.

        Args:
            session: Open DB session.
            folder: Folder to describe.
            role: Caller's effective role on it.

        Returns:
            The populated state.
        """
        link = get_active_link(session, folder.id)
        token = link.token if link is not None else None
        return FolderSharingState(
            general_access=link.general_access if link is not None else GENERAL_ACCESS_RESTRICTED,
            general_role=link.general_role if link is not None else str(ShareRole.viewer),
            token=token,
            share_path=f"/folders/share/{token}" if token else None,
            owner=folder.owner_username,
            role=str(role),
            can_manage=can_share(folder, role),
            editors_can_share=bool(folder.editors_can_share),
            members=[
                FolderSharingMember(username=g.grantee_username, role=g.role)
                for g in list_grants(session, folder.id)
                if g.created_by != LINK_GRANT_MARKER
            ],
            inherited=_inherited_members(session, folder),
        )

    def _require_share(session: Session, folder_id: str, user: AuthenticatedUser) -> tuple[RunFolderModel, ShareRole]:
        """Require that ``user`` may change who has access to a folder.

        Args:
            session: Open DB session.
            folder_id: Folder being shared.
            user: Authenticated caller.

        Returns:
            ``(folder, role)``.

        Raises:
            DomainError: 404 when inaccessible; 403 ``folder.share.forbidden``
                when the caller is a viewer, or an editor while the owner has
                turned resharing off.
        """
        folder, role = require_folder_role(session, folder_id, user, ShareRole.viewer)
        if not can_share(folder, role):
            raise DomainError("folder.share.forbidden", status=403)
        return folder, role

    def _check_not_below_inherited(session: Session, folder: RunFolderModel, username: str, role: str) -> None:
        """Reject a grant lower than the access the member inherits.

        Drive does not let a subfolder give someone less access than they
        already hold on its parent.

        Args:
            session: Open DB session.
            folder: Folder being shared.
            username: Normalized grantee.
            role: Requested role.

        Raises:
            DomainError: 400 ``folder.share.below_inherited``.
        """
        inherited = _inherited_role(session, folder, username)
        if inherited is not None and role_rank(role) < role_rank(inherited):
            raise DomainError("folder.share.below_inherited", status=400, username=username, role=inherited)

    def _sync_link_memberships(session: Session, folder_id: str, link: RunFolderShareLinkModel) -> None:
        """Reconcile link-derived memberships with the link's current policy.

        Args:
            session: Open DB session (caller commits).
            folder_id: Folder whose link memberships are reconciled.
            link: The just-updated active link row.
        """
        markers = session.scalars(
            select(RunFolderShareGrantModel).where(
                RunFolderShareGrantModel.folder_id == folder_id,
                RunFolderShareGrantModel.created_by == LINK_GRANT_MARKER,
            )
        )
        if link.general_access == GENERAL_ACCESS_ANYONE and link.general_role in MEMBER_ROLES:
            for grant in markers:
                grant.role = link.general_role
        else:
            for grant in markers:
                session.delete(grant)

    def _upsert_grant(session: Session, folder_id: str, grantee: str, role: str, created_by: str) -> None:
        """Add or replace a named member grant.

        Args:
            session: Open DB session (caller commits).
            folder_id: Folder being shared.
            grantee: Normalized grantee.
            role: Tier to grant.
            created_by: Username recorded as the inviter.
        """
        existing = get_grant(session, folder_id, grantee)
        if existing is not None:
            existing.role = role
            # A named invite outranks a link membership and stops tracking the link.
            existing.created_by = created_by
            return
        session.add(
            RunFolderShareGrantModel(
                folder_id=folder_id,
                grantee_username=grantee,
                role=role,
                created_by=created_by,
                created_at=datetime.now(UTC),
            )
        )

    @router.get(
        "/folders",
        response_model=FoldersResponse,
        summary="List every folder the caller can reach",
        tags=[_AGENT_TAG],
        operation_id="list_folders",
    )
    def list_folders(current_user: AuthenticatedUserDep) -> FoldersResponse:
        """Return owned folders, shared folders, and everything nested in them.

        A folder whose parent the caller cannot see is reported at the top level
        (``parent_id`` is ``None``), the way Drive lists a shared subfolder in
        "Shared with me" without its parent.

        Args:
            current_user: Authenticated caller.

        Returns:
            A :class:`FoldersResponse`.
        """
        engine = getattr(job_store, "engine", None)
        if engine is None:
            return FoldersResponse(folders=[])
        username = _normalize_username(current_user.username)
        with Session(engine) as session:
            roots = set(
                session.scalars(select(RunFolderModel.id).where(RunFolderModel.owner_username == username)).all()
            )
            roots.update(
                session.scalars(
                    select(RunFolderShareGrantModel.folder_id).where(
                        RunFolderShareGrantModel.grantee_username == username,
                        RunFolderShareGrantModel.hidden_at.is_(None),
                    )
                ).all()
            )
            visible = subtree_ids(session, roots)
            if not visible:
                return FoldersResponse(folders=[])
            folders = session.scalars(select(RunFolderModel).where(RunFolderModel.id.in_(visible))).all()
            counts = dict(
                session.execute(
                    select(RunFolderItemModel.folder_id, func.count())
                    .where(RunFolderItemModel.folder_id.in_(visible))
                    .group_by(RunFolderItemModel.folder_id)
                ).all()
            )
            shared_ids = set(
                session.scalars(
                    select(RunFolderShareGrantModel.folder_id).where(RunFolderShareGrantModel.folder_id.in_(visible))
                ).all()
            )
            shared_ids.update(
                session.scalars(
                    select(RunFolderShareLinkModel.folder_id).where(
                        RunFolderShareLinkModel.folder_id.in_(visible),
                        RunFolderShareLinkModel.revoked_at.is_(None),
                        RunFolderShareLinkModel.general_access == GENERAL_ACCESS_ANYONE,
                    )
                ).all()
            )
            items: list[FolderItem] = []
            for folder in folders:
                role = resolve_folder_role(session, folder.id, current_user)
                if role is None:
                    continue
                items.append(
                    FolderItem(
                        id=folder.id,
                        name=folder.name,
                        parent_id=folder.parent_id if folder.parent_id in visible else None,
                        owner=folder.owner_username,
                        role=str(role),
                        shared=folder.id in shared_ids,
                        editors_can_share=bool(folder.editors_can_share),
                        run_count=int(counts.get(folder.id, 0)),
                        created_at=folder.created_at,
                        updated_at=folder.updated_at,
                    )
                )
        items.sort(key=lambda f: f.name.lower())
        return FoldersResponse(folders=items)

    def _folder_item(session: Session, folder: RunFolderModel, role: ShareRole) -> FolderItem:
        """Build the :class:`FolderItem` returned by the mutation routes.

        Args:
            session: Open DB session.
            folder: Folder to describe.
            role: Caller's effective role on it.

        Returns:
            The populated item.
        """
        run_count = session.scalar(
            select(func.count()).select_from(RunFolderItemModel).where(RunFolderItemModel.folder_id == folder.id)
        )
        link = get_active_link(session, folder.id)
        shared = bool(list_grants(session, folder.id)) or (
            link is not None and link.general_access == GENERAL_ACCESS_ANYONE
        )
        return FolderItem(
            id=folder.id,
            name=folder.name,
            parent_id=folder.parent_id,
            owner=folder.owner_username,
            role=str(role),
            shared=shared,
            editors_can_share=bool(folder.editors_can_share),
            run_count=int(run_count or 0),
            created_at=folder.created_at,
            updated_at=folder.updated_at,
        )

    @router.post(
        "/folders",
        response_model=FolderItem,
        status_code=201,
        summary="Create a folder",
        tags=[_AGENT_TAG],
        operation_id="create_folder",
    )
    def create_folder(req: CreateFolderRequest, response: Response, current_user: AuthenticatedUserDep) -> FolderItem:
        """Create a folder owned by the caller.

        A subfolder inherits its parent's access, so creating one inside a
        shared folder needs editor access on that parent. With
        ``reuse_existing`` the caller's folder of the same name under the same
        parent comes back with status 200 instead of a duplicate.

        Args:
            req: Body carrying the ``name``, optional ``parent_id`` and ``reuse_existing``.
            response: Outgoing response, set to 200 when an existing folder is reused.
            current_user: Authenticated caller.

        Returns:
            The new (or reused) :class:`FolderItem`.

        Raises:
            DomainError: 400 on a blank name; 404/403 on the parent.
        """
        name = _clean_name(req.name)
        with Session(_engine()) as session:
            if req.parent_id:
                require_folder_role(session, req.parent_id, current_user, ShareRole.editor)
            owner = _normalize_username(current_user.username)
            if req.reuse_existing:
                # No unique constraint backs (owner, parent, name), so overlapping
                # retries would both miss the lookup and insert; serialize them.
                bind = session.get_bind()
                if bind.dialect.name == "postgresql":
                    key = f"run-folder:{owner}:{req.parent_id or ''}:{name.casefold()}"
                    session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})
                existing = _find_owned_folder(session, owner, name, req.parent_id or None)
                if existing is not None:
                    response.status_code = 200
                    return _folder_item(session, existing, ShareRole.owner)
            now = datetime.now(UTC)
            folder = RunFolderModel(
                owner_username=owner,
                name=name,
                parent_id=req.parent_id or None,
                editors_can_share=True,
                created_at=now,
                updated_at=now,
            )
            session.add(folder)
            session.commit()
            return _folder_item(session, folder, ShareRole.owner)

    @router.patch(
        "/folders/{folder_id}",
        response_model=FolderItem,
        summary="Rename a folder or change who can share it",
        tags=[_AGENT_TAG],
        operation_id="update_folder",
    )
    def update_folder(folder_id: str, req: UpdateFolderRequest, current_user: AuthenticatedUserDep) -> FolderItem:
        """Rename a folder (editor+) or set ``editors_can_share`` (owner).

        Args:
            folder_id: Folder to update.
            req: Body with the optional new ``name`` and ``editors_can_share``.
            current_user: Authenticated caller.

        Returns:
            The updated :class:`FolderItem`.

        Raises:
            DomainError: 404/403 on access; 400 on a blank name.
        """
        with Session(_engine()) as session:
            folder, role = require_folder_role(session, folder_id, current_user, ShareRole.editor)
            if req.editors_can_share is not None and req.editors_can_share != folder.editors_can_share:
                if role != ShareRole.owner:
                    raise DomainError("folder.forbidden", status=403)
                folder.editors_can_share = req.editors_can_share
            if req.name is not None:
                folder.name = _clean_name(req.name)
            folder.updated_at = datetime.now(UTC)
            session.commit()
            return _folder_item(session, folder, role)

    @router.delete(
        "/folders/{folder_id}",
        response_model=DeleteFolderResponse,
        summary="Delete a folder and its subfolders",
        tags=[_AGENT_TAG],
        operation_id="delete_folder",
    )
    def delete_folder(folder_id: str, current_user: AuthenticatedUserDep) -> DeleteFolderResponse:
        """Delete a folder and its subfolders; the runs inside are kept and become unfiled.

        Runs inside are never deleted: they return to their owners' unfiled
        lists, and whatever access they inherited from the folder goes with it.

        Args:
            folder_id: Folder to delete.
            current_user: Authenticated caller.

        Returns:
            The ids of every folder removed.

        Raises:
            DomainError: 404 when inaccessible; 403 for a non-owner.
        """
        with Session(_engine()) as session:
            require_folder_role(session, folder_id, current_user, ShareRole.owner)
            ids = list(subtree_ids(session, [folder_id]))
            session.execute(delete(RunFolderItemModel).where(RunFolderItemModel.folder_id.in_(ids)))
            session.execute(delete(RunFolderShareGrantModel).where(RunFolderShareGrantModel.folder_id.in_(ids)))
            session.execute(delete(RunFolderShareLinkModel).where(RunFolderShareLinkModel.folder_id.in_(ids)))
            session.execute(delete(RunFolderModel).where(RunFolderModel.id.in_(ids)))
            session.commit()
        return DeleteFolderResponse(deleted=sorted(ids))

    @router.post(
        "/folders/{folder_id}/move",
        response_model=FolderItem,
        summary="Move a folder",
        tags=[_AGENT_TAG],
        operation_id="move_folder",
    )
    def move_folder(folder_id: str, req: MoveFolderRequest, current_user: AuthenticatedUserDep) -> FolderItem:
        """Move a folder under another folder, or to the top level.

        Only the owner may move a folder, and they need editor access on the
        destination. The folder then inherits the destination's access and
        drops what it inherited from its old parent.

        Args:
            folder_id: Folder to move.
            req: Body carrying the destination ``parent_id`` (``None`` for top level).
            current_user: Authenticated caller.

        Returns:
            The moved :class:`FolderItem`.

        Raises:
            DomainError: 404/403 on access; 400 ``folder.invalid_move`` when the
                destination is the folder itself or one of its subfolders.
        """
        with Session(_engine()) as session:
            folder, role = require_folder_role(session, folder_id, current_user, ShareRole.owner)
            target = req.parent_id or None
            if target is not None:
                if target in subtree_ids(session, [folder_id]):
                    raise DomainError("folder.invalid_move", status=400)
                require_folder_role(session, target, current_user, ShareRole.editor)
            folder.parent_id = target
            folder.updated_at = datetime.now(UTC)
            session.commit()
            return _folder_item(session, folder, role)

    @router.get(
        "/folders/{folder_id}/runs",
        response_model=FolderRunsResponse,
        summary="List the runs in a folder",
        tags=[_AGENT_TAG],
        operation_id="list_folder_runs",
    )
    def list_folder_runs(folder_id: str, current_user: AuthenticatedUserDep) -> FolderRunsResponse:
        """Return the runs filed directly in a folder, newest first.

        Each item carries the caller's ``role`` on the run (``owner`` for their
        own runs) so the sidebar can gate actions.

        Args:
            folder_id: Folder to list.
            current_user: Authenticated caller.

        Returns:
            A :class:`FolderRunsResponse`.

        Raises:
            DomainError: 404 when inaccessible.
        """
        with Session(_engine()) as session:
            require_folder_role(session, folder_id, current_user, ShareRole.viewer)
            ids = list(
                session.scalars(
                    select(RunFolderItemModel.optimization_id).where(RunFolderItemModel.folder_id == folder_id)
                ).all()
            )
        rows = job_store.list_jobs_by_ids(ids) if ids else []
        username = _normalize_username(current_user.username)
        roles = grant_roles_for(job_store, [row["optimization_id"] for row in rows], username)
        resumable_ids = resumable_id_flags(job_store, rows)
        pausable_ids = pausable_id_flags(job_store, rows)
        items = []
        for row in rows:
            oid = row["optimization_id"]
            role = str(ShareRole.owner) if is_admin(current_user) or job_owner(row) == username else roles.get(oid)
            items.append(build_sidebar_item(row, resumable_ids, pausable_ids, role=role, folder_id=folder_id))
        return FolderRunsResponse(items=items)

    @router.post(
        "/folders/items",
        response_model=MoveRunsResponse,
        summary="File runs into a folder or unfile them",
        tags=[_AGENT_TAG],
        operation_id="move_runs_to_folder",
    )
    def move_runs(req: MoveRunsRequest, current_user: AuthenticatedUserDep) -> MoveRunsResponse:
        """File up to 200 runs into ``folder_id`` in one call, or unfile them when ``folder_id`` is null.

        A run lives in at most one folder, so filing it moves it. Taking a run
        out of a folder needs the run's owner or an editor of that folder;
        filing it somewhere new also needs editor access on the destination.

        Args:
            req: Body carrying the ``optimization_ids`` and destination ``folder_id``.
            current_user: Authenticated caller.

        Returns:
            The runs that moved and the ones skipped, with reasons.

        Raises:
            DomainError: 404/403 on the destination folder.
        """
        username = _normalize_username(current_user.username)
        admin = is_admin(current_user)
        updated: list[str] = []
        skipped: list[MoveRunsSkipped] = []
        with Session(_engine()) as session:
            if req.folder_id:
                require_folder_role(session, req.folder_id, current_user, ShareRole.editor)
            folder_roles: dict[str, ShareRole | None] = {}
            ids = list(dict.fromkeys(req.optimization_ids))
            visible = grant_roles_for(job_store, ids, username)
            for oid in ids:
                try:
                    job = get_job_no_payload(job_store, oid)
                except KeyError:
                    skipped.append(MoveRunsSkipped(optimization_id=oid, reason="not_found"))
                    continue
                current = folder_of_run(session, oid)
                allowed = admin or job_owner(job) == username
                if not allowed and current is not None:
                    if current not in folder_roles:
                        folder_roles[current] = resolve_folder_role(session, current, current_user)
                    current_role = folder_roles[current]
                    allowed = current_role is not None and role_rank(current_role) >= role_rank(ShareRole.editor)
                if not allowed:
                    # A caller with no role on the run sees it as missing, like a 404 elsewhere.
                    reason = "forbidden" if oid in visible else "not_found"
                    skipped.append(MoveRunsSkipped(optimization_id=oid, reason=reason))
                    continue
                # Checked after access, so a no-op cannot tell a stranger the run exists or where it is.
                if current == (req.folder_id or None):
                    updated.append(oid)
                    continue
                item = session.get(RunFolderItemModel, oid)
                if req.folder_id:
                    if item is None:
                        session.add(
                            RunFolderItemModel(
                                optimization_id=oid,
                                folder_id=req.folder_id,
                                added_by=username,
                                added_at=datetime.now(UTC),
                            )
                        )
                    else:
                        item.folder_id = req.folder_id
                        item.added_by = username
                        item.added_at = datetime.now(UTC)
                elif item is not None:
                    session.delete(item)
                updated.append(oid)
            session.commit()
        return MoveRunsResponse(updated=updated, skipped=skipped)

    @router.get(
        "/folders/runs",
        response_model=OrganizeRunsResponse,
        summary="List your runs with their folders, to organize them",
        tags=[_AGENT_TAG],
        operation_id="list_runs_for_organizing",
    )
    def list_runs_for_organizing(
        current_user: AuthenticatedUserDep,
        q: str | None = Query(
            default=None,
            description="Case-insensitive text match on name, task, dataset, model, module, optimizer or folder",
        ),
        status: str | None = Query(
            default=None, description="Exact status filter: pending, validating, running, success, failed, cancelled"
        ),
        unfiled_only: bool = Query(default=False, description="Only runs that are not in any folder"),
        limit: int = Query(default=_ORGANIZE_DEFAULT_LIMIT, ge=1, le=_ORGANIZE_MAX_LIMIT, description="Page size"),
        offset: int = Query(default=0, ge=0, description="Matches to skip; pass the previous next_offset"),
    ) -> OrganizeRunsResponse:
        """List your own runs, newest first, with name, task, dataset, model, status and folder.

        Covers the runs the caller owns, which are the runs they can always
        file. ``total_runs`` and ``unfiled_runs`` count every owned run;
        ``matched`` counts the runs that pass the filters.

        Args:
            current_user: Authenticated caller.
            q: Optional search text.
            status: Optional exact status filter.
            unfiled_only: Keep only runs outside every folder.
            limit: Page size.
            offset: Number of matches to skip.

        Returns:
            An :class:`OrganizeRunsResponse`.

        Raises:
            DomainError: 422 ``filter.invalid_status`` for an unknown status.
        """
        if status is not None and status not in VALID_STATUSES:
            raise DomainError("filter.invalid_status", status=422, value=status, allowed=sorted(VALID_STATUSES))
        # Status is filtered below, not in the store query, so the totals and
        # the empty-account hint cover every owned run.
        rows: list[dict] = []
        folder_ids: dict[str, str] = {}
        seen: set[str] = set()
        store_offset = 0
        while True:
            batch = job_store.list_jobs(
                username=current_user.username, limit=_ORGANIZE_SCAN_PAGE, offset=store_offset, with_counts=False
            )
            # A run created during the scan shifts the offsets by one, so the
            # next page can repeat a row; count each run once.
            fresh = [row for row in batch if row["optimization_id"] not in seen]
            seen.update(row["optimization_id"] for row in fresh)
            rows.extend(fresh)
            folder_ids.update(run_folder_ids(job_store, [row["optimization_id"] for row in fresh]))
            if len(batch) < _ORGANIZE_SCAN_PAGE:
                break
            store_offset += _ORGANIZE_SCAN_PAGE
        folder_names: dict[str, str] = {}
        engine = getattr(job_store, "engine", None)
        if engine is not None and folder_ids:
            with Session(engine) as session:
                for fid in set(folder_ids.values()):
                    folder = get_folder(session, fid)
                    # A run can sit in a folder its owner lost access to; keep
                    # the id (the sidebar shows it too) but not the name.
                    if folder is not None and resolve_folder_role(session, fid, current_user) is not None:
                        folder_names[fid] = folder.name
        needle = (q or "").strip().lower()
        entries: list[OrganizeRunItem] = []
        unfiled = 0
        for row in rows:
            fid = folder_ids.get(row["optimization_id"])
            if fid is None:
                unfiled += 1
            item = _organize_item(row, fid, folder_names.get(fid) if fid else None)
            if unfiled_only and fid is not None:
                continue
            if status is not None and item.status != status:
                continue
            if needle and not _matches(item, row, needle):
                continue
            entries.append(item)
        page = entries[offset : offset + limit]
        next_offset = offset + limit if offset + limit < len(entries) else None
        if not rows:
            hint = "0 runs found for this account."
        elif not entries:
            hint = "0 runs match these filters. Drop q, status or unfiled_only to see more."
        else:
            hint = (
                "Group related runs: create_folder with reuse_existing=true, then move_runs_to_folder "
                "with up to 200 optimization_ids."
            )
        return OrganizeRunsResponse(
            items=page,
            total_runs=len(rows),
            unfiled_runs=unfiled,
            matched=len(entries),
            next_offset=next_offset,
            hint=hint,
        )

    @router.get("/folders/{folder_id}/sharing", response_model=FolderSharingState, summary="Get a folder's sharing")
    def get_sharing(folder_id: str, current_user: AuthenticatedUserDep) -> FolderSharingState:
        """Return a folder's sharing config to anyone who can see the folder.

        ``can_manage`` tells the client whether the caller may change it.

        Args:
            folder_id: Folder to inspect.
            current_user: Authenticated caller.

        Returns:
            The current :class:`FolderSharingState`.

        Raises:
            DomainError: 404 when inaccessible.
        """
        with Session(_engine()) as session:
            folder, role = require_folder_role(session, folder_id, current_user, ShareRole.viewer)
            return _sharing_state(session, folder, role)

    @router.put("/folders/{folder_id}/sharing", response_model=FolderSharingState, summary="Set general access")
    def put_sharing(
        folder_id: str, req: PutFolderSharingRequest, current_user: AuthenticatedUserDep
    ) -> FolderSharingState:
        """Set the link's general-access policy and tier, minting a link if needed.

        Args:
            folder_id: Folder to update.
            req: Body with ``general_access`` and optional ``general_role``.
            current_user: Authenticated caller allowed to share.

        Returns:
            The updated :class:`FolderSharingState`.

        Raises:
            DomainError: 404/403 on access; 400 on invalid values.
        """
        if req.general_access not in _GENERAL_ACCESS_VALUES:
            raise DomainError("share.invalid_general_access", status=400, allowed=", ".join(_GENERAL_ACCESS_VALUES))
        if req.general_role is not None and req.general_role not in LINK_ROLES:
            raise DomainError(
                "share.invalid_role", status=400, role=req.general_role, allowed=", ".join(_LINK_ROLE_VALUES)
            )
        with Session(_engine()) as session:
            folder, role = _require_share(session, folder_id, current_user)
            link = get_active_link(session, folder_id)
            if link is None:
                link = RunFolderShareLinkModel(
                    token=secrets.token_urlsafe(24),
                    folder_id=folder_id,
                    created_by=current_user.username,
                    created_at=datetime.now(UTC),
                    general_access=GENERAL_ACCESS_RESTRICTED,
                    general_role=str(ShareRole.viewer),
                )
                session.add(link)
            link.general_access = req.general_access
            if req.general_role is not None:
                link.general_role = req.general_role
            _sync_link_memberships(session, folder_id, link)
            session.commit()
            return _sharing_state(session, folder, role)

    @router.post(
        "/folders/{folder_id}/sharing/members", response_model=FolderSharingState, summary="Invite a user to a folder"
    )
    def add_member(
        folder_id: str, req: AddFolderMemberRequest, current_user: AuthenticatedUserDep
    ) -> FolderSharingState:
        """Add or replace a member grant on the folder.

        Args:
            folder_id: Folder to share.
            req: Body carrying the grantee ``username`` and ``role``.
            current_user: Authenticated caller allowed to share.

        Returns:
            The updated :class:`FolderSharingState`.

        Raises:
            DomainError: 404/403 on access; 400 on an invalid role, a self or
                owner invite, or a role below what the member inherits.
        """
        if req.role not in MEMBER_ROLES:
            raise DomainError("share.invalid_role", status=400, role=req.role, allowed=", ".join(sorted(MEMBER_ROLES)))
        grantee = _normalize_username(req.username)
        with Session(_engine()) as session:
            folder, role = _require_share(session, folder_id, current_user)
            if grantee in (_normalize_username(folder.owner_username), _normalize_username(current_user.username)):
                raise DomainError("folder.share.cannot_grant_self", status=400)
            _check_not_below_inherited(session, folder, grantee, req.role)
            _upsert_grant(session, folder_id, grantee, req.role, current_user.username)
            session.commit()
            return _sharing_state(session, folder, role)

    @router.patch(
        "/folders/{folder_id}/sharing/members/{username}",
        response_model=FolderSharingState,
        summary="Change a folder member's role",
    )
    def update_member(
        folder_id: str, username: str, req: UpdateFolderMemberRequest, current_user: AuthenticatedUserDep
    ) -> FolderSharingState:
        """Change an existing member's role.

        Args:
            folder_id: Folder to update.
            username: Member whose role changes.
            req: Body carrying the new ``role``.
            current_user: Authenticated caller allowed to share.

        Returns:
            The updated :class:`FolderSharingState`.

        Raises:
            DomainError: 404/403 on access; 404 when the member has no grant; 400
                on an invalid role, a self edit, or a role below what they inherit.
        """
        if req.role not in MEMBER_ROLES:
            raise DomainError("share.invalid_role", status=400, role=req.role, allowed=", ".join(sorted(MEMBER_ROLES)))
        grantee = _normalize_username(username)
        with Session(_engine()) as session:
            folder, role = _require_share(session, folder_id, current_user)
            if grantee == _normalize_username(current_user.username):
                raise DomainError("folder.share.cannot_modify_self", status=400)
            grant = get_grant(session, folder_id, grantee)
            if grant is None:
                raise DomainError("folder.share.member_not_found", status=404, username=grantee)
            _check_not_below_inherited(session, folder, grantee, req.role)
            grant.role = req.role
            session.commit()
            return _sharing_state(session, folder, role)

    @router.delete(
        "/folders/{folder_id}/sharing/members/{username}",
        response_model=FolderSharingState,
        summary="Remove a folder member",
    )
    def remove_member(folder_id: str, username: str, current_user: AuthenticatedUserDep) -> FolderSharingState:
        """Remove a member's grant from the folder.

        Access the member holds through a parent folder is unaffected, as in
        Drive, where inherited access is managed on the parent.

        Args:
            folder_id: Folder to update.
            username: Member whose grant is removed.
            current_user: Authenticated caller allowed to share.

        Returns:
            The updated :class:`FolderSharingState`.

        Raises:
            DomainError: 404/403 on access; 404 when the member has no grant; 400
                on a self removal.
        """
        grantee = _normalize_username(username)
        with Session(_engine()) as session:
            folder, role = _require_share(session, folder_id, current_user)
            if grantee == _normalize_username(current_user.username):
                raise DomainError("folder.share.cannot_modify_self", status=400)
            grant = get_grant(session, folder_id, grantee)
            if grant is None:
                raise DomainError("folder.share.member_not_found", status=404, username=grantee)
            session.delete(grant)
            session.commit()
            return _sharing_state(session, folder, role)

    @router.post(
        "/folders/{folder_id}/sharing/transfer",
        response_model=FolderSharingState,
        summary="Transfer folder ownership to an existing member",
    )
    def transfer_ownership(
        folder_id: str, req: TransferFolderOwnershipRequest, current_user: AuthenticatedUserDep
    ) -> FolderSharingState:
        """Hand a folder to an existing member; the old owner becomes an editor.

        Only the folder changes hands. The runs inside keep their own owners,
        as Drive keeps file ownership apart from folder ownership.

        Args:
            folder_id: Folder whose ownership moves.
            req: Body carrying the new owner ``username``.
            current_user: Authenticated owner or admin.

        Returns:
            The updated :class:`FolderSharingState`.

        Raises:
            DomainError: 404/403 on access; 400 when targeting the current owner;
                404 when the target is not a member.
        """
        new_owner = _normalize_username(req.username)
        with Session(_engine()) as session:
            folder, _ = require_folder_role(session, folder_id, current_user, ShareRole.owner)
            old_owner = _normalize_username(folder.owner_username)
            if new_owner == old_owner:
                raise DomainError("folder.share.cannot_grant_self", status=400)
            grant = get_grant(session, folder_id, new_owner)
            if grant is None:
                raise DomainError("folder.share.member_not_found", status=404, username=new_owner)
            session.delete(grant)
            folder.owner_username = new_owner
            folder.updated_at = datetime.now(UTC)
            session.flush()
            _upsert_grant(session, folder_id, old_owner, str(ShareRole.editor), current_user.username)
            session.commit()
            caller_role = resolve_folder_role(session, folder_id, current_user) or ShareRole.editor
            return _sharing_state(session, folder, caller_role)

    @router.post(
        "/folders/share/{token}/claim",
        response_model=ClaimFolderResponse,
        summary="Redeem a folder share link",
    )
    def claim_shared_folder(token: str, current_user: AuthenticatedUserDep) -> ClaimFolderResponse:
        """Redeem a folder link so the folder lists in the caller's sidebar.

        Under an ``anyone`` link the signed-in caller gets a link membership at
        the link's tier; it follows later link changes. A ``restricted`` link
        grants nothing on its own.

        Args:
            token: The public share token from the URL.
            current_user: Authenticated caller.

        Returns:
            The target ``folder_id`` and the caller's role after redemption.

        Raises:
            DomainError: 404 ``folder.share.not_found`` when the token is unknown
                or revoked, the folder is gone, or the caller has no access.
        """
        with Session(_engine()) as session:
            role = resolve_share_access(session, token, current_user)
            link = get_link_by_token(session, token)
            if role is None or link is None or get_folder(session, link.folder_id) is None:
                raise DomainError("folder.share.not_found", status=404)
            folder_id = link.folder_id
            own_role = resolve_folder_role(session, folder_id, current_user)
            if (
                link.general_access == GENERAL_ACCESS_ANYONE
                and link.general_role in MEMBER_ROLES
                and (own_role is None or role_rank(own_role) < role_rank(link.general_role))
            ):
                username = _normalize_username(current_user.username)
                existing = get_grant(session, folder_id, username)
                if existing is None:
                    session.add(
                        RunFolderShareGrantModel(
                            folder_id=folder_id,
                            grantee_username=username,
                            role=link.general_role,
                            created_by=LINK_GRANT_MARKER,
                            created_at=datetime.now(UTC),
                        )
                    )
                elif existing.created_by == LINK_GRANT_MARKER:
                    existing.role = link.general_role
                session.commit()
        return ClaimFolderResponse(folder_id=folder_id, role=str(role))

    return router
