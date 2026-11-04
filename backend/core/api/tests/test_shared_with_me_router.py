"""Tests for the caller's controls on items other users shared with them.

Covers hide and unhide, removing one's own access, refusal when access comes
from a shared folder, lowering one's own role, isolation between users, and
that shared items never count toward the grantee's storage.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ...storage.models import (
    Base,
    DatasetModel,
    DatasetShareGrantModel,
    JobModel,
    OptimizationShareGrantModel,
    RunFolderItemModel,
    RunFolderModel,
    RunFolderShareGrantModel,
    TaggingSessionShareGrantModel,
)
from ...storage.remote import RemoteDBJobStore
from ...storage.usage import compute_user_storage
from ..auth import AuthenticatedUser, get_authenticated_user
from ..dataset_access import list_grants_for_user_all as dataset_grants_for
from ..routers.folders import create_folders_router
from ..routers.shared_with_me import create_shared_with_me_router
from ..sharing_access import resolve_effective_role
from ..tagging_session_access import list_grants_for_user_all as session_grants_for


class _MemStore(RemoteDBJobStore):
    """In-memory SQLite job store (skips the pgvector bootstrap)."""

    def __init__(self) -> None:
        """Build an in-memory SQLite engine and create the ORM tables."""
        self._engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self._engine)
        self._session_factory = sessionmaker(bind=self._engine)


def _user(name: str) -> AuthenticatedUser:
    """Return an ordinary authenticated user.

    Args:
        name: Username.

    Returns:
        The user identity.
    """
    return AuthenticatedUser(username=name, role="user", groups=())


def _client(store: _MemStore, user: str) -> TestClient:
    """Build a client over the folders and shared-with-me routers.

    Args:
        store: Job store wired into the routers.
        user: Username to authenticate as.

    Returns:
        A ``TestClient`` authenticated as ``user``.
    """
    app = FastAPI()
    app.include_router(create_folders_router(job_store=store))
    app.include_router(create_shared_with_me_router(job_store=store))
    identity = _user(user)
    app.dependency_overrides[get_authenticated_user] = lambda: identity
    return TestClient(app, raise_server_exceptions=False)


def _seed_run(store: _MemStore, optimization_id: str, owner: str = "alice") -> None:
    """Insert a finished run owned by ``owner``.

    Args:
        store: Store to seed.
        optimization_id: Run id.
        owner: Owner username.
    """
    overview = {"optimization_type": "run", "name": optimization_id, "username": owner}
    with Session(store.engine) as session:
        session.add(
            JobModel(
                optimization_id=optimization_id,
                status="success",
                created_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
                latest_metrics={},
                result=None,
                payload_overview=overview,
                payload={"username": owner},
                username=owner,
            )
        )
        session.commit()


def _add(store: _MemStore, *rows: object) -> None:
    """Insert ORM rows and commit.

    Args:
        store: Store to write to.
        *rows: Rows to insert.
    """
    with Session(store.engine) as session:
        session.add_all(list(rows))
        session.commit()


def _run_grant(optimization_id: str, grantee: str, role: str = "editor") -> OptimizationShareGrantModel:
    """Build a direct grant on a run.

    Args:
        optimization_id: Run id.
        grantee: Grantee username.
        role: Granted role.

    Returns:
        The unsaved grant row.
    """
    return OptimizationShareGrantModel(
        optimization_id=optimization_id, grantee_username=grantee, role=role, created_by="alice"
    )


def _shared_ids(store: _MemStore, user: str) -> list[str]:
    """Return the run ids in a user's Shared with me list.

    Args:
        store: Store to read.
        user: Grantee username.

    Returns:
        The listed run ids.
    """
    return [row["optimization_id"] for row in store.list_jobs_shared_with(user)]


def _role(store: _MemStore, optimization_id: str, user: str) -> str | None:
    """Resolve a user's effective role on a run.

    Args:
        store: Store to read.
        optimization_id: Run id.
        user: Username.

    Returns:
        The role value, or ``None`` when the user has no access.
    """
    with Session(store.engine) as session:
        role = resolve_effective_role(session, optimization_id, _user(user))
    return None if role is None else str(role)


def test_hide_keeps_access_and_unhide_lists_again() -> None:
    """Hiding drops the run from my list only; my access and other lists stay."""
    store = _MemStore()
    _seed_run(store, "run-1")
    _add(store, _run_grant("run-1", "bob"), _run_grant("run-1", "carol"))
    bob = _client(store, "bob")

    assert bob.post("/shared-with-me/optimizations/run-1/hide").status_code == 204
    assert _shared_ids(store, "bob") == []
    assert store.count_jobs_shared_with("bob") == 0
    assert store.count_jobs_visible_to("bob") == 0
    assert _role(store, "run-1", "bob") == "editor"
    assert _shared_ids(store, "carol") == ["run-1"]

    assert bob.delete("/shared-with-me/optimizations/run-1/hide").status_code == 204
    assert _shared_ids(store, "bob") == ["run-1"]


def test_unhide_without_a_grant_is_a_no_op() -> None:
    """Opening an item the caller owns or cannot reach still answers 204."""
    store = _MemStore()
    _seed_run(store, "run-1")
    assert _client(store, "alice").delete("/shared-with-me/optimizations/run-1/hide").status_code == 204


def test_remove_access_deletes_only_my_grant() -> None:
    """Removing my access ends it and leaves other members untouched."""
    store = _MemStore()
    _seed_run(store, "run-1")
    _add(store, _run_grant("run-1", "bob"), _run_grant("run-1", "carol"))

    assert _client(store, "bob").delete("/shared-with-me/optimizations/run-1").status_code == 204
    assert _role(store, "run-1", "bob") is None
    assert _role(store, "run-1", "carol") == "editor"
    assert _client(store, "bob").delete("/shared-with-me/optimizations/run-1").status_code == 404


def test_owner_cannot_remove_themselves() -> None:
    """The owner holds no grant, so the route refuses and ownership stays."""
    store = _MemStore()
    _seed_run(store, "run-1")
    assert _client(store, "alice").delete("/shared-with-me/optimizations/run-1").status_code == 404
    assert _role(store, "run-1", "alice") == "owner"


def test_inherited_run_access_is_refused_with_the_folder_name() -> None:
    """A run reached only through a shared folder must be changed on that folder."""
    store = _MemStore()
    _seed_run(store, "run-1")
    _add(
        store,
        RunFolderModel(id="f-1", owner_username="alice", name="Research"),
        RunFolderItemModel(optimization_id="run-1", folder_id="f-1", added_by="alice"),
        RunFolderShareGrantModel(folder_id="f-1", grantee_username="bob", role="viewer", created_by="alice"),
    )
    bob = _client(store, "bob")
    for resp in (
        bob.delete("/shared-with-me/optimizations/run-1"),
        bob.post("/shared-with-me/optimizations/run-1/hide"),
        bob.patch("/shared-with-me/optimizations/run-1", json={"role": "viewer"}),
    ):
        assert resp.status_code == 409
        assert "Research" in resp.text


def test_lower_role_only() -> None:
    """An editor may become a viewer; a viewer may never become an editor."""
    store = _MemStore()
    _seed_run(store, "run-1")
    _add(store, _run_grant("run-1", "bob", "editor"))
    bob = _client(store, "bob")

    assert bob.patch("/shared-with-me/optimizations/run-1", json={"role": "viewer"}).status_code == 204
    assert _role(store, "run-1", "bob") == "viewer"
    assert bob.patch("/shared-with-me/optimizations/run-1", json={"role": "editor"}).status_code == 403
    assert _role(store, "run-1", "bob") == "viewer"
    assert bob.patch("/shared-with-me/optimizations/run-1", json={"role": "owner"}).status_code == 422


def test_folder_hide_remove_and_inherited_subfolder() -> None:
    """A shared folder hides from my tree; its subfolder points to the parent."""
    store = _MemStore()
    alice = _client(store, "alice")
    root = alice.post("/folders", json={"name": "Team"}).json()["id"]
    child = alice.post("/folders", json={"name": "Sub", "parent_id": root}).json()["id"]
    assert alice.post(f"/folders/{root}/sharing/members", json={"username": "bob", "role": "editor"}).status_code == 200
    bob = _client(store, "bob")

    def bob_folders() -> set[str]:
        """Return the folder ids bob's tree lists."""
        return {f["id"] for f in bob.get("/folders").json()["folders"]}

    assert bob_folders() == {root, child}
    resp = bob.delete(f"/shared-with-me/folders/{child}")
    assert resp.status_code == 409
    assert "Team" in resp.text

    assert bob.post(f"/shared-with-me/folders/{root}/hide").status_code == 204
    assert bob_folders() == set()
    assert bob.get(f"/folders/{root}/runs").status_code == 200
    assert bob.delete(f"/shared-with-me/folders/{root}/hide").status_code == 204
    assert bob_folders() == {root, child}

    assert bob.delete(f"/shared-with-me/folders/{root}").status_code == 204
    assert bob_folders() == set()
    assert bob.get(f"/folders/{root}/runs").status_code == 404


def test_dataset_and_tagging_session_hide() -> None:
    """Hidden datasets and sessions leave the listing grant maps until unhidden."""
    store = _MemStore()
    _add(
        store,
        DatasetShareGrantModel(dataset_id="ds-1", grantee_username="bob", role="viewer", created_by="alice"),
        TaggingSessionShareGrantModel(session_id="ts-1", grantee_username="bob", role="editor", created_by="alice"),
    )
    bob = _client(store, "bob")
    assert bob.post("/shared-with-me/datasets/ds-1/hide").status_code == 204
    assert bob.post("/shared-with-me/tagging-sessions/ts-1/hide").status_code == 204
    with Session(store.engine) as session:
        assert dataset_grants_for(session, "bob") == {}
        assert session_grants_for(session, "bob") == {}
    assert bob.delete("/shared-with-me/datasets/ds-1/hide").status_code == 204
    assert bob.patch("/shared-with-me/tagging-sessions/ts-1", json={"role": "viewer"}).status_code == 204
    assert bob.delete("/shared-with-me/tagging-sessions/ts-1/hide").status_code == 204
    with Session(store.engine) as session:
        assert dataset_grants_for(session, "bob") == {"ds-1": "viewer"}
        assert session_grants_for(session, "bob") == {"ts-1": "viewer"}


def test_unknown_kind_is_rejected() -> None:
    """Only the four shareable kinds are accepted."""
    store = _MemStore()
    assert _client(store, "bob").post("/shared-with-me/agents/x/hide").status_code == 422


def test_shared_dataset_costs_only_the_owner() -> None:
    """A dataset shared with me does not count toward my storage."""
    store = _MemStore()
    _add(
        store,
        DatasetModel(
            id="ds-1",
            owner_username="alice",
            name="rows",
            source="upload",
            row_count=1,
            column_count=1,
            byte_size=4096,
            stored_bytes=4096,
            content_hash="ds-1",
            column_schema={},
        ),
        DatasetShareGrantModel(dataset_id="ds-1", grantee_username="bob", role="editor", created_by="alice"),
    )
    assert compute_user_storage(store.engine, "alice").total >= 4096
    assert compute_user_storage(store.engine, "bob").total == 0
