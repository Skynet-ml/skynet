"""Grantee controls for items another user shared with the caller.

Google Drive lets a person manage what others share with them without touching
the owner's copy. These routes do the same for runs, datasets, tagging sessions
and folders. Each one acts only on the caller's own grant row:

* ``POST   /shared-with-me/{kind}/{item_id}/hide`` — remove the item from the
  caller's "Shared with me" view. Access does not change.
* ``DELETE /shared-with-me/{kind}/{item_id}/hide`` — show it again (Undo, and
  when the caller opens the item). A no-op when nothing is hidden.
* ``PATCH  /shared-with-me/{kind}/{item_id}`` — lower the caller's own role.
  A caller can never raise it.
* ``DELETE /shared-with-me/{kind}/{item_id}`` — remove the caller's own access.

A run or subfolder can also give access through a shared parent folder. That
access has no grant row of its own, so the routes answer 409
``sharing.access_inherited`` with the folder name; the caller changes it on that
folder. An owner holds no grant row, so these routes can never remove an owner.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ...storage.models import (
    DatasetShareGrantModel,
    OptimizationShareGrantModel,
    RunFolderShareGrantModel,
    TaggingSessionShareGrantModel,
)
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..folder_access import folder_chain, folder_of_run
from ..folder_access import get_grant as get_folder_grant
from ..sharing_access import _normalize_username, role_rank

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]

SharedKind = Literal["optimizations", "datasets", "tagging-sessions", "folders"]

_GRANT_TABLES: dict[str, tuple[Any, str]] = {
    "optimizations": (OptimizationShareGrantModel, "optimization_id"),
    "datasets": (DatasetShareGrantModel, "dataset_id"),
    "tagging-sessions": (TaggingSessionShareGrantModel, "session_id"),
    "folders": (RunFolderShareGrantModel, "folder_id"),
}


class SharedRoleRequest(BaseModel):
    """Request body for ``PATCH /shared-with-me/{kind}/{item_id}``."""

    role: Literal["viewer", "editor"]


def _inheriting_folder_name(session: Session, kind: str, item_id: str, username: str) -> str | None:
    """Return the name of the folder that gives the caller inherited access.

    Args:
        session: Open DB session.
        kind: Item kind from the route.
        item_id: Item the caller tries to change.
        username: Normalized caller username.

    Returns:
        The nearest parent folder the caller owns or holds a grant on, or
        ``None`` when no parent folder gives access (or the kind has no
        folders).
    """
    if kind == "optimizations":
        folder_id = folder_of_run(session, item_id)
        chain = folder_chain(session, folder_id) if folder_id else []
    elif kind == "folders":
        chain = folder_chain(session, item_id)[1:]
    else:
        return None
    for folder in chain:
        if _normalize_username(folder.owner_username) == username:
            return folder.name
        if get_folder_grant(session, folder.id, username) is not None:
            return folder.name
    return None


def _own_grant(session: Session, kind: str, item_id: str, username: str) -> Any:
    """Load the caller's own grant row on an item.

    Args:
        session: Open DB session.
        kind: Item kind from the route.
        item_id: Item id.
        username: Normalized caller username.

    Returns:
        The caller's grant row.

    Raises:
        DomainError: 409 ``sharing.access_inherited`` when access comes only
            from a parent folder; 404 ``sharing.grant_not_found`` otherwise.
    """
    model, column = _GRANT_TABLES[kind]
    grant = session.get(model, {column: item_id, "grantee_username": username})
    if grant is not None:
        return grant
    folder_name = _inheriting_folder_name(session, kind, item_id, username)
    if folder_name is not None:
        raise DomainError("sharing.access_inherited", status=409, folder=folder_name)
    raise DomainError("sharing.grant_not_found", status=404)


def create_shared_with_me_router(*, job_store) -> APIRouter:
    """Build the router for the caller's controls on items shared with them.

    Args:
        job_store: Job store whose ``engine`` backs the grant tables.

    Returns:
        The configured :class:`APIRouter`.
    """
    router = APIRouter()

    def _engine() -> Any:
        """Return the grant-bearing engine.

        Returns:
            The SQLAlchemy engine of the job store.

        Raises:
            DomainError: 404 ``sharing.grant_not_found`` when the store has no
                engine (no sharing tables).
        """
        engine = getattr(job_store, "engine", None)
        if engine is None:
            raise DomainError("sharing.grant_not_found", status=404)
        return engine

    @router.post(
        "/shared-with-me/{kind}/{item_id}/hide",
        status_code=204,
        response_class=Response,
        summary="Hide an item from the caller's Shared with me view",
    )
    def hide_shared_item(kind: SharedKind, item_id: str, current_user: AuthenticatedUserDep) -> Response:
        """Hide an item from the caller's own view; access does not change.

        Args:
            kind: Item kind.
            item_id: Item id.
            current_user: Authenticated caller.

        Returns:
            An empty 204 response.
        """
        username = _normalize_username(current_user.username)
        with Session(_engine()) as session:
            grant = _own_grant(session, kind, item_id, username)
            grant.hidden_at = datetime.now(UTC)
            session.commit()
        return Response(status_code=204)

    @router.delete(
        "/shared-with-me/{kind}/{item_id}/hide",
        status_code=204,
        response_class=Response,
        summary="Show a hidden item in the caller's Shared with me view again",
    )
    def unhide_shared_item(kind: SharedKind, item_id: str, current_user: AuthenticatedUserDep) -> Response:
        """Clear the caller's hidden mark on an item, if one is set.

        The app calls this when the caller opens a shared item, so it is a
        no-op (still 204) when the caller holds no grant or nothing is hidden.

        Args:
            kind: Item kind.
            item_id: Item id.
            current_user: Authenticated caller.

        Returns:
            An empty 204 response.
        """
        username = _normalize_username(current_user.username)
        engine = getattr(job_store, "engine", None)
        if engine is None:
            return Response(status_code=204)
        model, column = _GRANT_TABLES[kind]
        with Session(engine) as session:
            grant = session.get(model, {column: item_id, "grantee_username": username})
            if grant is not None and grant.hidden_at is not None:
                grant.hidden_at = None
                session.commit()
        return Response(status_code=204)

    @router.patch(
        "/shared-with-me/{kind}/{item_id}",
        status_code=204,
        response_class=Response,
        summary="Lower the caller's own role on a shared item",
    )
    def lower_shared_role(
        kind: SharedKind, item_id: str, body: SharedRoleRequest, current_user: AuthenticatedUserDep
    ) -> Response:
        """Set the caller's own grant to an equal or lower role.

        Args:
            kind: Item kind.
            item_id: Item id.
            body: The new role.
            current_user: Authenticated caller.

        Returns:
            An empty 204 response.

        Raises:
            DomainError: 403 ``sharing.role_raise_forbidden`` when the new role
                is higher than the caller's current role.
        """
        username = _normalize_username(current_user.username)
        with Session(_engine()) as session:
            grant = _own_grant(session, kind, item_id, username)
            if role_rank(body.role) > role_rank(grant.role):
                raise DomainError("sharing.role_raise_forbidden", status=403, role=grant.role)
            grant.role = body.role
            session.commit()
        return Response(status_code=204)

    @router.delete(
        "/shared-with-me/{kind}/{item_id}",
        status_code=204,
        response_class=Response,
        summary="Remove the caller's own access to a shared item",
    )
    def remove_shared_access(kind: SharedKind, item_id: str, current_user: AuthenticatedUserDep) -> Response:
        """Delete the caller's own grant on an item.

        Args:
            kind: Item kind.
            item_id: Item id.
            current_user: Authenticated caller.

        Returns:
            An empty 204 response.
        """
        username = _normalize_username(current_user.username)
        with Session(_engine()) as session:
            session.delete(_own_grant(session, kind, item_id, username))
            session.commit()
        return Response(status_code=204)

    return router
