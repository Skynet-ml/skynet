"""Google-Drive-style sharing for optimizations. [MIXED]

Owner/editor-gated management endpoints plus the access-gated public surface:

* ``GET    /optimizations/{id}/sharing`` — current sharing config (owner/editor).
* ``PUT    /optimizations/{id}/sharing`` — set the general-access policy.
* ``POST   /optimizations/{id}/sharing/members`` — add/replace a member grant.
* ``PATCH  /optimizations/{id}/sharing/members/{username}`` — change a role.
* ``DELETE /optimizations/{id}/sharing/members/{username}`` — remove a grant.
* ``POST   /optimizations/{id}/sharing/transfer`` — reassign ownership to an
  existing member (the previous owner is demoted to an editor).
* ``GET    /users/search`` — username autocomplete for the invite picker.
* ``GET    /share/{token}`` — **access-gated** composite read of one
  optimization (viewer+ for an invited member or an ``anyone`` link); requires
  a signed-in caller.
* ``POST   /share/{token}/serve`` — one viewer-accessible inference through a
  fresh budget funded by the signed-in caller.

Two sharing modes coexist per :mod:`core.api.sharing_access`: the active link's
``general_access`` (``restricted`` vs ``anyone``) and ``general_role`` (the
``viewer``/``editor`` tier an ``anyone`` link grants a signed-in visitor)
combine with per-user member grants; effective access is the highest the rules
allow, resolved by :func:`resolve_share_access`. Access is login-gated, so the
floor is ``viewer`` (real owner shown). Secrets (API keys, base URLs) never
cross the public boundary.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...billing.credential_safety import scrub_model_config
from ...billing.protected_credentials import scrub_execution_credentials
from ...constants import (
    OPTIMIZATION_TYPE_BLACKBOX,
    PAYLOAD_OVERVIEW_IS_PRIVATE,
    PAYLOAD_OVERVIEW_OPTIMIZATION_TYPE,
    PAYLOAD_OVERVIEW_USERNAME,
)
from ...i18n_keys import I18nKey
from ...models import (
    BlackboxRunResponse,
    JobLogEntry,
    OptimizationStatus,
    OptimizationStatusResponse,
    ServeInfoResponse,
    ServeRequest,
    ServeResponse,
)
from ...notifications import (
    notify_ownership_transfer,
    notify_role_change,
    notify_share_invite,
)
from ...service_gateway.dashboard import invalidate_public_dashboard_cache
from ...service_gateway.embedding_pipeline import set_embedding_privacy
from ...storage.models import (
    AgentConversationModel,
    ApiTokenModel,
    JobModel,
    OptimizationShareGrantModel,
    OptimizationShareLinkModel,
)
from ..auth import AuthenticatedUser, get_authenticated_user, is_admin
from ..converters import (
    compute_elapsed,
    overview_to_base_fields,
    parse_overview,
    parse_timestamp,
    status_to_job_status,
)
from ..errors import DomainError
from ..sharing_access import (
    GENERAL_ACCESS_ANYONE,
    GENERAL_ACCESS_RESTRICTED,
    LINK_GRANT_MARKER,
    LINK_ROLES,
    MEMBER_ROLES,
    ShareRole,
    get_active_link,
    get_grant,
    get_link_by_token,
    list_grants,
    resolve_share_access,
    role_rank,
)
from ._helpers import (
    get_job_no_payload,
    job_owner,
)
from .serve import ServeInferenceEvent, _repo_run, _repo_serve_call, repo_serve_info, require_serving_enabled

logger = logging.getLogger(__name__)

_USER_FACING_OPTIMIZATION_TYPES = frozenset({OPTIMIZATION_TYPE_BLACKBOX})

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]

_INFER_ROLES: frozenset[ShareRole] = frozenset({ShareRole.viewer, ShareRole.editor, ShareRole.owner})
_GENERAL_ACCESS_VALUES = (GENERAL_ACCESS_RESTRICTED, GENERAL_ACCESS_ANYONE)
_LINK_ROLE_VALUES = tuple(sorted(LINK_ROLES))
# Cap for the username-autocomplete result set (contract: at most 10).
_USER_SEARCH_LIMIT = 10
# Synthetic test/load accounts authenticate as email-shaped usernames under the
# reserved ``.local`` TLD (e.g. ``analytics-1-1@s.local``, ``x@sampler.local``).
# ``.local`` is non-routable (RFC 6762) and never a real identity, so we exclude
# such usernames from the people picker — otherwise an integration/E2E harness
# run against a shared backend leaks its fixtures into everyone's invite search.
_SYNTHETIC_USERNAME_PATTERN = "%@%.local%"
# Model-config sub-keys that must never cross the public boundary.
_SECRET_MODEL_FIELDS = ("model_config", "reflection_model_config", "task_model_config")


class SharingMember(BaseModel):
    """One invited member of an optimization (username + tier role)."""

    username: str
    role: str


class SharingState(BaseModel):
    """Owner/editor-facing sharing config for one optimization."""

    general_access: str
    general_role: str = "viewer"
    token: str | None = None
    share_path: str | None = None
    owner: str | None = None
    members: list[SharingMember] = Field(default_factory=list)
    # Explore-corpus visibility — orthogonal to the link's general_access:
    # is_private hides the job from the public /explore search, general_access
    # governs who can reach /share/<token>.
    is_private: bool = False


class PutSharingRequest(BaseModel):
    """Request body for ``PUT /optimizations/{id}/sharing``."""

    general_access: str
    general_role: str | None = None


class SetVisibilityRequest(BaseModel):
    """Request body for ``PUT /optimizations/{id}/visibility`` — the explore-corpus flag."""

    is_private: bool


class AddMemberRequest(BaseModel):
    """Request body for ``POST /optimizations/{id}/sharing/members``."""

    username: str
    role: str


class UpdateMemberRequest(BaseModel):
    """Request body for ``PATCH /optimizations/{id}/sharing/members/{username}``."""

    role: str


class TransferOwnershipRequest(BaseModel):
    """Request body for ``POST /optimizations/{id}/sharing/transfer``."""

    username: str


class UserSearchResponse(BaseModel):
    """Envelope for ``GET /users/search`` — matching distinct usernames."""

    usernames: list[str]


class ClaimShareResponse(BaseModel):
    """Envelope for ``POST /share/{token}/claim`` — where to send the redeemer."""

    optimization_id: str
    role: str


def _strip_model_secrets(cfg: Any) -> Any:
    """Return a copy of a model-config dict without its secret fields.

    Args:
        cfg: A model-config mapping (or anything else, returned unchanged).

    Returns:
        A shallow copy with ``base_url`` removed and ``extra.api_key`` stripped,
        or the input unchanged when it is not a dict.
    """
    if not isinstance(cfg, dict):
        return cfg
    cleaned = scrub_model_config(cfg)
    cleaned.pop("base_url", None)
    return cleaned


def _scrub_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Project the stored payload onto the public, secret-free share view.

    Drops the owner ``username`` and any raw ``dataset`` rows, and strips API
    keys / base URLs from every model config.

    Args:
        payload: The optimization's stored request payload.

    Returns:
        A new dict safe to expose through a share link.
    """
    out: dict[str, Any] = {}
    payload = scrub_execution_credentials(payload)
    for key, value in payload.items():
        if key in ("username", "dataset"):
            continue
        if key in _SECRET_MODEL_FIELDS:
            out[key] = _strip_model_secrets(value)
        elif key in ("generation_models", "reflection_models") and isinstance(value, list):
            out[key] = [_strip_model_secrets(m) for m in value]
        else:
            out[key] = value
    return out


def _build_status_response(job_store, optimization_id: str, job_data: dict[str, Any]) -> OptimizationStatusResponse:
    """Assemble the read-only status response for a shared optimization.

    Mirrors the ``OptimizationStatusResponse`` that ``GET /optimizations/{id}``
    returns (minus its ETag/caching concerns) so the share page can reuse the
    same frontend detail components.

    Args:
        job_store: Job-store the logs/progress are read from.
        optimization_id: Optimization id being rendered.
        job_data: Raw job row.

    Returns:
        A populated :class:`OptimizationStatusResponse`.
    """
    status = status_to_job_status(job_data.get("status", "pending"))
    overview = parse_overview(job_data)
    optimization_type = overview.get(PAYLOAD_OVERVIEW_OPTIMIZATION_TYPE)

    blackbox_result = None
    result_data = job_data.get("result")
    if (
        isinstance(result_data, dict)
        and status == OptimizationStatus.success
        and optimization_type == OPTIMIZATION_TYPE_BLACKBOX
    ):
        try:
            blackbox_result = BlackboxRunResponse.model_validate(result_data)
        except ValidationError:
            logger.warning("Shared optimization %s has corrupted result data", optimization_id)

    created_at = parse_timestamp(job_data.get("created_at")) or datetime.now(UTC)
    started_at = parse_timestamp(job_data.get("started_at"))
    completed_at = parse_timestamp(job_data.get("completed_at"))

    latest_metrics = job_data.get("latest_metrics", {})

    elapsed_str, elapsed_secs = compute_elapsed(
        created_at, started_at, completed_at, job_data.get("accumulated_runtime_seconds") or 0.0
    )
    logs = job_store.get_logs(optimization_id)
    progress_events = job_store.get_progress_events(optimization_id)

    base_fields = overview_to_base_fields(overview)
    # Strip connection secrets from every model config surfaced on this scrubbed
    # composite. ``api_key`` is already dropped when the overview is persisted,
    # but ``base_url`` is not — and this response feeds the public ``/share`` and
    # ``/optimizations/{id}/public`` reads, so an internal endpoint URL must not
    # leak to a non-owner viewer.
    base_fields["model_settings"] = _strip_model_secrets(base_fields.get("model_settings"))
    for _models_key in ("generation_models", "reflection_models"):
        _models = base_fields.get(_models_key)
        if isinstance(_models, list):
            base_fields[_models_key] = [_strip_model_secrets(m) for m in _models]

    return OptimizationStatusResponse(
        optimization_id=optimization_id,
        status=status,
        created_at=created_at,
        started_at=started_at,
        completed_at=completed_at,
        elapsed=elapsed_str,
        elapsed_seconds=elapsed_secs,
        **base_fields,
        message=job_data.get("message"),
        latest_metrics=latest_metrics,
        progress_events=progress_events,
        logs=[JobLogEntry(**log) for log in logs],
        blackbox_result=blackbox_result,
    )


def _serve_info(job_data: dict[str, Any], optimization_id: str) -> ServeInfoResponse | None:
    """Describe a shared repository run for the share page's inference panel.

    Args:
        job_data: Raw job row.
        optimization_id: Optimization whose served version is described.

    Returns:
        A :class:`ServeInfoResponse`, or ``None`` when the run is not a
        finished repository run with a pinned commit.
    """
    try:
        if _repo_run(job_data) is None:
            return None
    except DomainError:
        return None
    return repo_serve_info(job_data, optimization_id, None)


def create_share_router(*, job_store) -> APIRouter:
    """Build the Google-Drive-style optimization sharing router.

    Args:
        job_store: Job-store whose ORM engine backs the share tables and whose
            job rows feed the public composite read and inference path.

    Returns:
        A FastAPI ``APIRouter`` with the owner/editor-gated management routes,
        the user-search autocomplete, and the access-gated public surface.
    """
    router = APIRouter()

    def _require_manage(session: Session, optimization_id: str, user: AuthenticatedUser) -> str | None:
        """Ensure ``user`` may manage sharing for ``optimization_id``.

        Management is **owner-only**: only the job owner (its creator) or an
        admin may invite people, change roles, transfer ownership, and set
        general access. Editors and viewers — like strangers — 404 here
        (existence is never leaked, so non-managers can't even confirm the
        optimization exists).

        Args:
            session: Open DB session.
            optimization_id: Optimization being managed.
            user: Authenticated caller.

        Returns:
            The job owner username (lowercased), or ``None`` when the job
            carries no owner.

        Raises:
            DomainError: 404 when the optimization is unknown or the caller is
                not the owner/admin (owner existence is not leaked).
        """
        try:
            job_data = get_job_no_payload(job_store, optimization_id)
        except KeyError:
            raise DomainError("optimization.not_found", status=404, optimization_id=optimization_id) from None
        owner = job_owner(job_data)
        if (owner is not None and owner == user.username) or is_admin(user):
            return owner
        raise DomainError("optimization.not_found", status=404, optimization_id=optimization_id)

    def _sharing_state(session: Session, optimization_id: str, owner: str | None) -> SharingState:
        """Assemble the current :class:`SharingState` for an optimization.

        Args:
            session: Open DB session.
            optimization_id: Optimization to describe.
            owner: The job owner username (shown to managers).

        Returns:
            The populated :class:`SharingState`.
        """
        link = get_active_link(session, optimization_id)
        general_access = link.general_access if link is not None else GENERAL_ACCESS_RESTRICTED
        general_role = link.general_role if link is not None else str(ShareRole.viewer)
        token = link.token if link is not None else None
        # Only named invites appear in the people list; link-derived memberships
        # (created_by == LINK_GRANT_MARKER) are covered by the general-access link
        # row, exactly like Google Drive keeps "anyone with the link" users out of
        # the explicit people-with-access list.
        members = [
            SharingMember(username=g.grantee_username, role=g.role)
            for g in list_grants(session, optimization_id)
            if g.created_by != LINK_GRANT_MARKER
        ]
        # Existence already validated by _require_manage; read the overview for
        # the current explore-corpus visibility flag.
        overview = parse_overview(get_job_no_payload(job_store, optimization_id))
        return SharingState(
            general_access=general_access,
            general_role=general_role,
            token=token,
            share_path=f"/share/{token}" if token else None,
            owner=owner,
            members=members,
            is_private=bool(overview.get(PAYLOAD_OVERVIEW_IS_PRIVATE, False)),
        )

    def _ensure_link(session: Session, optimization_id: str, created_by: str) -> OptimizationShareLinkModel:
        """Return the active link, minting one if none exists.

        Args:
            session: Open DB session (caller commits).
            optimization_id: Optimization the link belongs to.
            created_by: Username recorded as the link creator.

        Returns:
            The active :class:`OptimizationShareLinkModel`.
        """
        link = get_active_link(session, optimization_id)
        if link is None:
            link = OptimizationShareLinkModel(
                token=secrets.token_urlsafe(24),
                optimization_id=optimization_id,
                created_by=created_by,
                created_at=datetime.now(UTC),
                general_access=GENERAL_ACCESS_RESTRICTED,
                general_role=str(ShareRole.viewer),
            )
            session.add(link)
        return link

    def _sync_link_memberships(session: Session, optimization_id: str, link: OptimizationShareLinkModel) -> None:
        """Reconcile link-derived memberships with the link's current policy.

        Drive-style live propagation: when the link is ``anyone`` every existing
        link membership is re-pointed at the link's current tier (so an
        editor→viewer flip downgrades them immediately); when the link is
        ``restricted`` (the "turn the link off" action) every link membership is
        deleted, which both revokes access and drops the run from those users'
        tables. Named invites (``created_by != LINK_GRANT_MARKER``) are never
        touched — they are authoritative. Caller commits.

        Args:
            session: Open DB session.
            optimization_id: Optimization whose link memberships are reconciled.
            link: The just-updated active link row.
        """
        markers = session.scalars(
            select(OptimizationShareGrantModel).where(
                OptimizationShareGrantModel.optimization_id == optimization_id,
                OptimizationShareGrantModel.created_by == LINK_GRANT_MARKER,
            )
        )
        if link.general_access == GENERAL_ACCESS_ANYONE and link.general_role in MEMBER_ROLES:
            for grant in markers:
                grant.role = link.general_role
        else:
            for grant in markers:
                session.delete(grant)

    def _reassign_job_owner(optimization_id: str, new_owner: str) -> None:
        """Rewrite a job's stored owner to ``new_owner`` everywhere it lives.

        The owner identity is denormalized three ways — ``payload['username']``
        (what :func:`job_owner` reads first), ``payload_overview['username']``
        (its fallback), and the indexed ``username`` column (what the
        "my optimizations" list query filters on) — so all three are rewritten
        together to keep ownership consistent across the read paths.

        Args:
            optimization_id: Optimization whose owner is reassigned.
            new_owner: The new owner's lowercased username.
        """
        job_data = job_store.get_job(optimization_id)
        overview = parse_overview(job_data)
        overview[PAYLOAD_OVERVIEW_USERNAME] = new_owner
        updates: dict[str, Any] = {"payload_overview": overview, "username": new_owner}
        payload = job_data.get("payload")
        if isinstance(payload, dict):
            updates["payload"] = {**payload, "username": new_owner}
        job_store.update_job(optimization_id, **updates)

    @router.get(
        "/optimizations/{optimization_id}/sharing",
        response_model=SharingState,
        summary="Get the sharing config (general access + members) for an optimization",
    )
    def get_sharing(optimization_id: str, current_user: AuthenticatedUserDep) -> SharingState:
        """Return the optimization's sharing config for an owner/editor.

        Args:
            optimization_id: Optimization to inspect.
            current_user: Authenticated owner/editor.

        Returns:
            The current :class:`SharingState`.

        Raises:
            DomainError: 404 when the optimization is unknown/inaccessible;
                403 when the caller may not manage sharing.
        """
        with Session(job_store.engine) as session:
            owner = _require_manage(session, optimization_id, current_user)
            return _sharing_state(session, optimization_id, owner)

    @router.put(
        "/optimizations/{optimization_id}/sharing",
        response_model=SharingState,
        summary="Set the general-access policy (restricted vs anyone-with-link)",
    )
    def put_sharing(optimization_id: str, req: PutSharingRequest, current_user: AuthenticatedUserDep) -> SharingState:
        """Set the link's general-access policy and tier, minting a link if needed.

        ``general_role`` is the tier an ``anyone`` link grants a signed-in
        visitor (``viewer``/``editor``); omit it to leave the current tier
        unchanged. It is persisted regardless of ``general_access`` so toggling
        back to ``anyone`` later keeps the chosen tier.

        Args:
            optimization_id: Optimization to update.
            req: Body carrying the new ``general_access`` policy and optional
                ``general_role`` tier.
            current_user: Authenticated owner/editor.

        Returns:
            The updated :class:`SharingState`.

        Raises:
            DomainError: 404 when unknown/inaccessible; 403 when the caller may
                not manage sharing; 400 when ``general_access`` or
                ``general_role`` is invalid.
        """
        if req.general_access not in _GENERAL_ACCESS_VALUES:
            raise DomainError("share.invalid_general_access", status=400, allowed=", ".join(_GENERAL_ACCESS_VALUES))
        if req.general_role is not None and req.general_role not in LINK_ROLES:
            raise DomainError(
                "share.invalid_role",
                status=400,
                role=req.general_role,
                allowed=", ".join(_LINK_ROLE_VALUES),
            )
        with Session(job_store.engine) as session:
            owner = _require_manage(session, optimization_id, current_user)
            link = _ensure_link(session, optimization_id, current_user.username)
            link.general_access = req.general_access
            if req.general_role is not None:
                link.general_role = req.general_role
            _sync_link_memberships(session, optimization_id, link)
            session.commit()
            return _sharing_state(session, optimization_id, owner)

    @router.put(
        "/optimizations/{optimization_id}/visibility",
        response_model=SharingState,
        summary="Set whether an optimization is private (hidden from the public Explore corpus)",
    )
    def put_visibility(
        optimization_id: str, req: SetVisibilityRequest, current_user: AuthenticatedUserDep
    ) -> SharingState:
        """Flip an optimization's public-Explore visibility (owner-only).

        Toggles the ``is_private`` flag the Explore public corpus filters on.
        Writes it to ``payload_overview`` **and** the denormalized
        ``job_embeddings.is_private`` column (what the corpus query actually
        reads), then invalidates the cached public dashboard so the change shows
        immediately. Independent of the share link's ``general_access`` — corpus
        discoverability and link access are separate axes.

        Args:
            optimization_id: Optimization to update.
            req: Body carrying the new ``is_private`` flag.
            current_user: Authenticated owner/admin.

        Returns:
            The updated :class:`SharingState`.

        Raises:
            DomainError: 404 when unknown/inaccessible; 403 when the caller may
                not manage sharing.
        """
        with Session(job_store.engine) as session:
            owner = _require_manage(session, optimization_id, current_user)
            overview = parse_overview(get_job_no_payload(job_store, optimization_id))
            overview[PAYLOAD_OVERVIEW_IS_PRIVATE] = req.is_private
            job_store.set_payload_overview(optimization_id, overview)
            set_embedding_privacy(job_store, optimization_id, req.is_private)
            invalidate_public_dashboard_cache()
            return _sharing_state(session, optimization_id, owner)

    @router.post(
        "/optimizations/{optimization_id}/sharing/members",
        response_model=SharingState,
        summary="Invite a user (add or replace a member grant)",
    )
    def add_member(optimization_id: str, req: AddMemberRequest, current_user: AuthenticatedUserDep) -> SharingState:
        """Add or replace a member grant on the optimization.

        Args:
            optimization_id: Optimization to share.
            req: Body carrying the grantee ``username`` and tier ``role``.
            current_user: Authenticated owner/editor.

        Returns:
            The updated :class:`SharingState`.

        Raises:
            DomainError: 404 when unknown/inaccessible or the caller is not an
                owner/admin; 400 when the role is invalid or the caller tries to
                grant the owner themselves.
        """
        if req.role not in MEMBER_ROLES:
            raise DomainError("share.invalid_role", status=400, role=req.role, allowed=", ".join(sorted(MEMBER_ROLES)))
        grantee = req.username.strip().lower()
        with Session(job_store.engine) as session:
            owner = _require_manage(session, optimization_id, current_user)
            if owner is not None and grantee == owner:
                raise DomainError("share.cannot_grant_self", status=400)
            existing = get_grant(session, optimization_id, grantee)
            if existing is not None:
                existing.role = req.role
                # Promote a link-derived membership to a named invite so it becomes
                # authoritative — it must no longer track (or be revoked with) the
                # link once the owner has invited the person by name.
                existing.created_by = current_user.username
            else:
                session.add(
                    OptimizationShareGrantModel(
                        optimization_id=optimization_id,
                        grantee_username=grantee,
                        role=req.role,
                        created_by=current_user.username,
                        created_at=datetime.now(UTC),
                    )
                )
            session.commit()
            state = _sharing_state(session, optimization_id, owner)
        # Notify outside the session so a slow Outlook send never holds the
        # transaction open; only fires once the grant is durably committed.
        notify_share_invite(optimization_id, grantee, current_user.username, req.role)
        return state

    @router.patch(
        "/optimizations/{optimization_id}/sharing/members/{username}",
        response_model=SharingState,
        summary="Change an existing member's role",
    )
    def update_member(
        optimization_id: str,
        username: str,
        req: UpdateMemberRequest,
        current_user: AuthenticatedUserDep,
    ) -> SharingState:
        """Change an existing member's tier role.

        Args:
            optimization_id: Optimization to update.
            username: Grantee whose role changes.
            req: Body carrying the new ``role``.
            current_user: Authenticated owner/editor.

        Returns:
            The updated :class:`SharingState`.

        Raises:
            DomainError: 404 when the optimization is unknown/inaccessible, the
                caller is not an owner/admin, or the member grant does not exist;
                400 when the role is invalid or the caller targets their own grant.
        """
        if req.role not in MEMBER_ROLES:
            raise DomainError("share.invalid_role", status=400, role=req.role, allowed=", ".join(sorted(MEMBER_ROLES)))
        grantee = username.strip().lower()
        with Session(job_store.engine) as session:
            owner = _require_manage(session, optimization_id, current_user)
            if grantee == current_user.username.strip().lower():
                raise DomainError("share.cannot_modify_self", status=400)
            grant = get_grant(session, optimization_id, grantee)
            if grant is None:
                raise DomainError("share.member_not_found", status=404, username=grantee)
            grant.role = req.role
            session.commit()
            state = _sharing_state(session, optimization_id, owner)
        notify_role_change(optimization_id, grantee, current_user.username, req.role)
        return state

    @router.delete(
        "/optimizations/{optimization_id}/sharing/members/{username}",
        response_model=SharingState,
        summary="Remove a member's grant",
    )
    def remove_member(optimization_id: str, username: str, current_user: AuthenticatedUserDep) -> SharingState:
        """Remove a member's grant from the optimization.

        Args:
            optimization_id: Optimization to update.
            username: Grantee whose grant is removed.
            current_user: Authenticated owner/editor.

        Returns:
            The updated :class:`SharingState`.

        Raises:
            DomainError: 404 when the optimization is unknown/inaccessible, the
                caller is not an owner/admin, or the member grant does not exist;
                400 when the caller targets their own grant.
        """
        grantee = username.strip().lower()
        with Session(job_store.engine) as session:
            owner = _require_manage(session, optimization_id, current_user)
            if grantee == current_user.username.strip().lower():
                raise DomainError("share.cannot_modify_self", status=400)
            grant = get_grant(session, optimization_id, grantee)
            if grant is None:
                raise DomainError("share.member_not_found", status=404, username=grantee)
            session.delete(grant)
            session.commit()
            return _sharing_state(session, optimization_id, owner)

    @router.post(
        "/optimizations/{optimization_id}/sharing/transfer",
        response_model=SharingState,
        summary="Transfer ownership to an existing member (old owner becomes an editor)",
    )
    def transfer_ownership(
        optimization_id: str, req: TransferOwnershipRequest, current_user: AuthenticatedUserDep
    ) -> SharingState:
        """Hand a single optimization's ownership to an existing member.

        Mirrors Google Drive's My-Drive transfer: an optimization has exactly
        one owner, so ownership is reassigned outright — the previous owner is
        demoted to an ``editor`` grant (keeping edit/run access), and the new
        owner's member grant is dropped (the owner is never also a grantee). The
        serving key is baked into the optimization's stored ``model_config``, so
        it is unchanged: transfer moves control, not billing. The new owner must
        already be a member (Drive: you can only transfer to someone you shared
        with).

        Args:
            optimization_id: Optimization whose ownership moves.
            req: Body carrying the new owner ``username`` (an existing member).
            current_user: Authenticated current owner/admin.

        Returns:
            The updated :class:`SharingState` — ``owner`` is the new owner and
            the previous owner now appears as an ``editor`` member.

        Raises:
            DomainError: 404 when unknown/inaccessible or the caller may not
                manage sharing; 400 when transferring to the current owner;
                404 (``share.member_not_found``) when the target is not a member.
        """
        new_owner = req.username.strip().lower()
        with Session(job_store.engine) as session:
            owner = _require_manage(session, optimization_id, current_user)
            if owner is not None and new_owner == owner:
                raise DomainError("share.cannot_grant_self", status=400)
            grant = get_grant(session, optimization_id, new_owner)
            if grant is None:
                raise DomainError("share.member_not_found", status=404, username=new_owner)
            # Flip the structural owner first so the transfer itself is durable
            # even if the grant bookkeeping below fails (recoverable: the new
            # owner now controls the share and can re-add the old one).
            _reassign_job_owner(optimization_id, new_owner)
            session.delete(grant)
            if owner is not None:
                demoted = get_grant(session, optimization_id, owner)
                if demoted is not None:
                    demoted.role = str(ShareRole.editor)
                else:
                    session.add(
                        OptimizationShareGrantModel(
                            optimization_id=optimization_id,
                            grantee_username=owner,
                            role=str(ShareRole.editor),
                            created_by=current_user.username,
                            created_at=datetime.now(UTC),
                        )
                    )
            session.commit()
            state = _sharing_state(session, optimization_id, new_owner)
        notify_ownership_transfer(optimization_id, new_owner, current_user.username)
        return state

    @router.get(
        "/users/search",
        response_model=UserSearchResponse,
        summary="Autocomplete distinct known usernames by prefix",
    )
    def search_users(q: str, current_user: AuthenticatedUserDep) -> UserSearchResponse:
        """Return up to 10 distinct known usernames matching a prefix.

        Backs the invite autocomplete. Searches the union of known username
        sources (``jobs.username``, ``agent_conversations.username``,
        ``api_tokens.username``); a blank query returns nothing. Synthetic
        ``.local`` test/load accounts are excluded so harness fixtures never
        surface in the people picker (see ``_SYNTHETIC_USERNAME_PATTERN``).

        Args:
            q: Case-insensitive username prefix.
            current_user: Any authenticated caller.

        Returns:
            A :class:`UserSearchResponse` with the matching usernames.
        """
        prefix = q.strip().lower()
        if not prefix:
            return UserSearchResponse(usernames=[])
        pattern = f"{prefix}%"
        found: set[str] = set()
        with Session(job_store.engine) as session:
            for column in (
                JobModel.username,
                AgentConversationModel.username,
                ApiTokenModel.username,
            ):
                rows = session.scalars(
                    select(column)
                    .where(column.isnot(None))
                    .where(column.ilike(pattern))
                    .where(~column.ilike(_SYNTHETIC_USERNAME_PATTERN))
                    .distinct()
                )
                for value in rows:
                    if value:
                        found.add(str(value).strip().lower())
        return UserSearchResponse(usernames=sorted(found)[:_USER_SEARCH_LIMIT])

    @router.get(
        "/share/{token}",
        summary="Access-gated composite snapshot of a shared optimization (viewer+)",
    )
    def get_shared_optimization(token: str, current_user: AuthenticatedUserDep) -> dict[str, Any]:
        """Return the access-gated composite view of a shared optimization.

        Requires a signed-in caller; the floor is ``viewer`` (real owner shown).
        ``serve_info`` (the field schema behind the inference panel) is available
        to every viewer because each call spends a fresh caller-owned budget.
        Secrets (API keys, base URLs) are stripped from the payload.

        Args:
            token: The public share token from the URL.
            current_user: Authenticated caller.

        Returns:
            ``{optimization_id, role, owner, status, payload, serve_info}``.

        Raises:
            DomainError: 404 when the token is unknown/revoked, the caller has
                no access, or the underlying optimization no longer exists.
        """
        with Session(job_store.engine) as session:
            role = resolve_share_access(session, token, current_user)
            if role is None:
                raise DomainError("share.not_found", status=404)
            link = get_link_by_token(session, token)
            optimization_id = link.optimization_id

        try:
            job_data = job_store.get_job(optimization_id)
        except KeyError:
            raise DomainError("share.not_found", status=404) from None

        owner = job_owner(job_data)
        can_serve = role_rank(role) >= role_rank(ShareRole.viewer)

        status = _build_status_response(job_store, optimization_id, job_data)
        raw_payload = job_data.get("payload")
        payload = _scrub_payload(raw_payload) if isinstance(raw_payload, dict) else {}
        serve_info = None
        if can_serve and owner is not None:
            info = _serve_info(job_data, optimization_id)
            serve_info = info.model_dump(mode="json") if info is not None else None

        return {
            "optimization_id": optimization_id,
            "role": role.value,
            "owner": owner,
            "status": status.model_dump(mode="json"),
            "payload": payload,
            "serve_info": serve_info,
        }

    @router.get(
        "/optimizations/{optimization_id}/public",
        summary="Scrubbed read-only view of a public (Explore-corpus) optimization",
    )
    def get_public_optimization(optimization_id: str) -> dict[str, Any]:
        """Return the scrubbed, read-only composite for a PUBLIC optimization.

        Mirrors the access-gated ``GET /share/{token}`` composite but is keyed by
        optimization id and gated on the Explore-corpus ``is_private`` flag rather
        than a share token: a public, successful user optimization grants every caller the
        ``viewer`` tier (read + clone) — the owner is shown for attribution,
        secrets (API keys, base URLs) are stripped from the payload, and inference
        is disabled (``serve_info`` is ``null``, so no caller can spend the
        owner's key). Internal tagger jobs, non-successful jobs, and private
        optimizations 404, exactly as if they were unlisted.
        This backs the Explore "public" tab so that a *listed* run is also
        *openable* and *forkable* — public discoverability and view access stay in
        sync.

        Args:
            optimization_id: The optimization to read.

        Returns:
            ``{optimization_id, role, owner, status, payload, serve_info}`` —
            the same shape as the share composite.

        Raises:
            DomainError: 404 when the optimization is unknown or private.
        """
        try:
            job_data = job_store.get_job(optimization_id)
        except KeyError:
            raise DomainError("share.not_found", status=404) from None
        overview = parse_overview(job_data)
        optimization_type = overview.get(PAYLOAD_OVERVIEW_OPTIMIZATION_TYPE) or job_data.get("optimization_type")
        if (
            optimization_type not in _USER_FACING_OPTIMIZATION_TYPES
            or status_to_job_status(job_data.get("status", "pending")) != OptimizationStatus.success
        ):
            raise DomainError("share.not_found", status=404)
        if bool(overview.get(PAYLOAD_OVERVIEW_IS_PRIVATE, False)):
            raise DomainError("share.not_found", status=404)

        status = _build_status_response(job_store, optimization_id, job_data)
        raw_payload = job_data.get("payload")
        payload = _scrub_payload(raw_payload) if isinstance(raw_payload, dict) else {}
        return {
            "optimization_id": optimization_id,
            # ``viewer`` (not the anonymous ``view`` floor): a public run grants
            # read + clone to every signed-in user — the point of the showcase is
            # to fork others' work. Inference stays editor+ (``serve_info`` is
            # null below), so a viewer still can't spend the owner's key.
            "role": ShareRole.viewer.value,
            "owner": job_owner(job_data),
            "status": status.model_dump(mode="json"),
            "payload": payload,
            "serve_info": None,
        }

    @router.post(
        "/share/{token}/claim",
        response_model=ClaimShareResponse,
        summary="Redeem a share link: record a link membership and return the target optimization",
    )
    def claim_shared_optimization(token: str, current_user: AuthenticatedUserDep) -> ClaimShareResponse:
        """Redeem a share link, joining the caller to it, then point them at it.

        Google-Drive link semantics: opening an ``anyone``-with-link URL records a
        *link membership* for the signed-in caller at the link's current tier — so
        the run lists in their account (``/optimizations/shared-with-me`` and the
        unified table) and the normal ``/optimizations/{id}`` routes resolve them
        to that tier. The membership is **not** frozen: it tracks the link, so a
        later editor→viewer change downgrades them and restricting/revoking the
        link removes their access entirely (see :func:`_sync_link_memberships`). A
        ``restricted`` link grants nothing: the caller must already be the owner or
        a named invitee, else 404. The owner/admin needs no membership, and a
        caller who already holds a named invite keeps it untouched (invites are
        authoritative and independent of the link). Authentication is required —
        the whole app is login-gated, so there is no anonymous tier to redeem.

        Args:
            token: The public share token from the ``/share/<token>`` URL.
            current_user: Authenticated caller redeeming the link.

        Returns:
            A :class:`ClaimShareResponse` with the target ``optimization_id`` and
            the caller's effective ``role`` after redemption.

        Raises:
            DomainError: 404 when the token is unknown/revoked, the underlying
                optimization is gone, or the caller has no access under the
                link's policy.
        """
        with Session(job_store.engine) as session:
            role = resolve_share_access(session, token, current_user)
            if role is None:
                raise DomainError("share.not_found", status=404)
            link = get_link_by_token(session, token)
            optimization_id = link.optimization_id
            if not job_store.job_exists(optimization_id):
                raise DomainError("share.not_found", status=404)
            # Record a link-derived membership so the run lists in the caller's
            # table, carrying the link's *current* tier. This is not a frozen
            # grant: it is re-synced here on every open and by put_sharing on any
            # link change, and deleted when the link is restricted — so link
            # access tracks the link (Drive semantics). The owner/admin
            # (role==owner) needs no membership; a caller who already holds a
            # named invite keeps it untouched (invites are authoritative).
            if (
                role != ShareRole.owner
                and link.general_access == GENERAL_ACCESS_ANYONE
                and link.general_role in MEMBER_ROLES
            ):
                username = current_user.username.strip().lower()
                existing = get_grant(session, optimization_id, username)
                if existing is None:
                    session.add(
                        OptimizationShareGrantModel(
                            optimization_id=optimization_id,
                            grantee_username=username,
                            role=link.general_role,
                            created_by=LINK_GRANT_MARKER,
                            created_at=datetime.now(UTC),
                        )
                    )
                    session.commit()
                elif existing.created_by == LINK_GRANT_MARKER and existing.role != link.general_role:
                    existing.role = link.general_role
                    session.commit()
        return ClaimShareResponse(optimization_id=optimization_id, role=str(role))

    @router.post(
        "/share/{token}/serve",
        response_model=ServeResponse,
        summary="Run one inference on a shared optimization (viewer+ only)",
    )
    def serve_shared_optimization(
        token: str,
        req: ServeRequest,
        current_user: AuthenticatedUserDep,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ServeResponse:
        """Run one shared inference using a fresh caller-owned request budget.

        Every signed-in viewer funds their own sandbox and model route. A shared
        caller never spends the run owner's wallet or BYOK model connection.

        Args:
            token: The public share token from the URL.
            req: Inference request carrying the inputs.
            current_user: Authenticated caller.
            idempotency_key: Transport replay identity.

        Returns:
            A :class:`ServeResponse` with the predicted outputs and resolved
            model identifier.

        Raises:
            DomainError: 404 when the token is unknown/revoked; 400 without a
                replay key; 409 when the run is not serveable or its owner did
                not turn serving on; 410 for a DSPy program run; 502 when the
                sandbox call fails.
        """
        with Session(job_store.engine) as session:
            role = resolve_share_access(session, token, current_user)
            if role is None:
                raise DomainError("share.not_found", status=404)
            if role not in _INFER_ROLES:
                raise DomainError("share.inference_forbidden", status=403)
            link = get_link_by_token(session, token)
            optimization_id = link.optimization_id
        # Tracking starts once the link resolves: an unknown token names no
        # program to credit.
        with ServeInferenceEvent(
            job_store,
            username=current_user.username,
            optimization_id=optimization_id,
            channel="share_link",
            route="serve",
        ):
            key = (idempotency_key or "").strip()
            if not key:
                raise DomainError("budget.idempotency_required", status=400)

            try:
                job_data = job_store.get_job(optimization_id)
            except KeyError:
                raise DomainError("share.not_found", status=404) from None
            owner = job_owner(job_data)
            if owner is None:
                raise DomainError("share.not_found", status=404)
            if _repo_run(job_data) is None:
                raise DomainError(I18nKey.OPTIMIZATION_DSPY_RUN_RETIRED, status=410)
            require_serving_enabled(job_data)
            return ServeResponse.model_validate(
                _repo_serve_call(
                    job_store=job_store,
                    job_data=job_data,
                    optimization_id=optimization_id,
                    req=req,
                    current_user=current_user,
                    idempotency_key=key,
                    kind="shared_serve",
                )
            )

    return router
