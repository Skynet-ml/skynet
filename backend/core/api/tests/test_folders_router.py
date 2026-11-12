"""Tests for run folders and their Google-Drive-style sharing.

Covers the folder tree (create, rename, move, delete), filing runs, access
inherited from parent folders, the ``editors_can_share`` switch, the rule that a
subfolder cannot give a member less than they inherit, and link claiming.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ...storage.models import Base, JobModel, OptimizationShareGrantModel, RunFolderItemModel, RunFolderModel
from ...storage.remote import RemoteDBJobStore
from ..auth import AuthenticatedUser, get_authenticated_user
from ..routers._helpers import grant_roles_for
from ..routers.folders import create_folders_router
from ..routers.optimizations import create_optimizations_router


class _MemStore(RemoteDBJobStore):
    """In-memory SQLite job store for folder-router tests (skips pgvector bootstrap)."""

    def __init__(self) -> None:
        """Build an in-memory SQLite engine and create the ORM tables."""
        self._engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self._engine)
        self._session_factory = sessionmaker(bind=self._engine)


def _seed_job(store: _MemStore, optimization_id: str, username: str = "alice") -> None:
    """Insert a finished job owned by ``username``.

    Args:
        store: The in-memory store to seed into.
        optimization_id: Optimization id for the seeded job.
        username: Owner username recorded on the job.
    """
    overview = {"optimization_type": "blackbox", "name": optimization_id, "username": username}
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
                payload={"username": username},
                username=username,
            )
        )
        session.commit()


def _client(store: _MemStore, user: str = "alice") -> TestClient:
    """Build a TestClient over the folders router authenticated as ``user``.

    Args:
        store: Job store wired into the router factory.
        user: Username to authenticate as.

    Returns:
        A ``TestClient`` over a minimal app mounting only the folders router.
    """
    app = FastAPI()
    app.include_router(create_folders_router(job_store=store))
    identity = AuthenticatedUser(username=user, role="user", groups=())
    app.dependency_overrides[get_authenticated_user] = lambda: identity
    return TestClient(app, raise_server_exceptions=False)


def _create(client: TestClient, name: str, parent_id: str | None = None) -> str:
    """Create a folder and return its id.

    Args:
        client: Client authenticated as the creator.
        name: Folder name.
        parent_id: Optional parent folder.

    Returns:
        The new folder id.
    """
    resp = client.post("/folders", json={"name": name, "parent_id": parent_id})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _share(client: TestClient, folder_id: str, username: str, role: str) -> None:
    """Invite ``username`` to a folder at ``role``.

    Args:
        client: Client allowed to share the folder.
        folder_id: Folder to share.
        username: Grantee.
        role: ``viewer`` or ``editor``.
    """
    resp = client.post(f"/folders/{folder_id}/sharing/members", json={"username": username, "role": role})
    assert resp.status_code == 200, resp.text


def test_create_rename_and_list_folders() -> None:
    """The owner sees a new folder and its subfolder, and can rename it."""
    store = _MemStore()
    alice = _client(store)
    root = _create(alice, "  Experiments ")
    child = _create(alice, "Sub", parent_id=root)

    resp = alice.patch(f"/folders/{root}", json={"name": "Research"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Research"

    folders = {f["id"]: f for f in alice.get("/folders").json()["folders"]}
    assert folders[root]["name"] == "Research"
    assert folders[root]["role"] == "owner"
    assert folders[child]["parent_id"] == root


def test_blank_name_is_rejected() -> None:
    """A name that is only whitespace returns 400."""
    store = _MemStore()
    resp = _client(store).post("/folders", json={"name": "   "})
    assert resp.status_code == 400


def test_folders_are_private_until_shared() -> None:
    """Another user cannot see or open a folder that was not shared with them."""
    store = _MemStore()
    folder = _create(_client(store), "Private")
    bob = _client(store, "bob")
    assert bob.get("/folders").json()["folders"] == []
    assert bob.get(f"/folders/{folder}/runs").status_code == 404


def test_sharing_a_folder_reaches_subfolders_and_runs() -> None:
    """A viewer grant on a folder covers its subfolders and the runs filed in them."""
    store = _MemStore()
    _seed_job(store, "run-1")
    alice = _client(store)
    root = _create(alice, "Shared")
    child = _create(alice, "Nested", parent_id=root)
    resp = alice.post("/folders/items", json={"optimization_ids": ["run-1"], "folder_id": child})
    assert resp.json()["updated"] == ["run-1"]

    _share(alice, root, "bob", "viewer")
    bob = _client(store, "bob")
    folders = {f["id"]: f for f in bob.get("/folders").json()["folders"]}
    assert set(folders) == {root, child}
    assert folders[child]["role"] == "viewer"
    runs = bob.get(f"/folders/{child}/runs").json()["items"]
    assert [r["optimization_id"] for r in runs] == ["run-1"]
    assert runs[0]["role"] == "viewer"
    assert grant_roles_for(store, ["run-1"], "bob") == {"run-1": "viewer"}


def test_moving_a_run_out_drops_inherited_access() -> None:
    """Unfiling a run removes the access its folder gave."""
    store = _MemStore()
    _seed_job(store, "run-1")
    alice = _client(store)
    folder = _create(alice, "Shared")
    alice.post("/folders/items", json={"optimization_ids": ["run-1"], "folder_id": folder})
    _share(alice, folder, "bob", "editor")
    assert grant_roles_for(store, ["run-1"], "bob") == {"run-1": "editor"}

    resp = alice.post("/folders/items", json={"optimization_ids": ["run-1"], "folder_id": None})
    assert resp.json()["updated"] == ["run-1"]
    assert grant_roles_for(store, ["run-1"], "bob") == {}


def test_folder_owner_is_only_editor_on_other_peoples_runs() -> None:
    """Owning a folder makes you an editor, not the owner, of runs others filed in it."""
    store = _MemStore()
    _seed_job(store, "bob-run", username="bob")
    alice = _client(store)
    folder = _create(alice, "Team")
    _share(alice, folder, "bob", "editor")
    resp = _client(store, "bob").post("/folders/items", json={"optimization_ids": ["bob-run"], "folder_id": folder})
    assert resp.json()["updated"] == ["bob-run"]
    assert grant_roles_for(store, ["bob-run"], "alice") == {"bob-run": "editor"}


def test_viewer_cannot_file_runs_or_create_subfolders() -> None:
    """Filing runs and creating subfolders need editor access."""
    store = _MemStore()
    _seed_job(store, "bob-run", username="bob")
    alice = _client(store)
    folder = _create(alice, "Read only")
    _share(alice, folder, "bob", "viewer")
    bob = _client(store, "bob")
    assert bob.post("/folders/items", json={"optimization_ids": ["bob-run"], "folder_id": folder}).status_code == 403
    assert bob.post("/folders", json={"name": "x", "parent_id": folder}).status_code == 403


def test_cannot_move_someone_elses_unfiled_run() -> None:
    """Only a run's owner can file it; a stranger cannot tell the run from a missing one."""
    store = _MemStore()
    _seed_job(store, "alice-run")
    bob = _client(store, "bob")
    folder = _create(bob, "Mine")
    resp = bob.post("/folders/items", json={"optimization_ids": ["alice-run", "missing"], "folder_id": folder})
    body = resp.json()
    assert body["updated"] == []
    assert {s["optimization_id"]: s["reason"] for s in body["skipped"]} == {
        "alice-run": "not_found",
        "missing": "not_found",
    }


def test_a_run_lives_in_one_folder() -> None:
    """Filing a run into a second folder moves it out of the first."""
    store = _MemStore()
    _seed_job(store, "run-1")
    alice = _client(store)
    first = _create(alice, "A")
    second = _create(alice, "B")
    alice.post("/folders/items", json={"optimization_ids": ["run-1"], "folder_id": first})
    alice.post("/folders/items", json={"optimization_ids": ["run-1"], "folder_id": second})
    assert alice.get(f"/folders/{first}/runs").json()["items"] == []
    assert len(alice.get(f"/folders/{second}/runs").json()["items"]) == 1


def test_owner_keeps_owner_role_on_their_filed_runs_in_listing() -> None:
    """Filing your own run doesn't turn it into a run shared with you."""
    store = _MemStore()
    _seed_job(store, "run-1")
    alice = _client(store)
    folder_id = _create(alice, "A")
    alice.post("/folders/items", json={"optimization_ids": ["run-1"], "folder_id": folder_id})
    app = FastAPI()
    app.include_router(create_optimizations_router(job_store=store, get_worker_ref=lambda: None))
    identity = AuthenticatedUser(username="alice", role="user", groups=())
    app.dependency_overrides[get_authenticated_user] = lambda: identity
    resp = TestClient(app).get("/optimizations", params={"include_shared": True})
    assert resp.status_code == 200, resp.text
    assert [item["role"] for item in resp.json()["items"]] == [None]


def test_delete_removes_subfolders_but_keeps_runs() -> None:
    """Deleting a folder deletes its subfolders and unfiles its runs without deleting them."""
    store = _MemStore()
    _seed_job(store, "run-1")
    alice = _client(store)
    root = _create(alice, "Root")
    child = _create(alice, "Child", parent_id=root)
    alice.post("/folders/items", json={"optimization_ids": ["run-1"], "folder_id": child})

    assert sorted(alice.delete(f"/folders/{root}").json()["deleted"]) == sorted([root, child])
    assert alice.get("/folders").json()["folders"] == []
    with Session(store.engine) as session:
        assert session.get(RunFolderModel, child) is None
        assert session.get(RunFolderItemModel, "run-1") is None
        assert session.get(JobModel, "run-1") is not None


def test_only_the_owner_can_delete_or_move() -> None:
    """Editors cannot delete or move a folder they do not own."""
    store = _MemStore()
    alice = _client(store)
    folder = _create(alice, "Shared")
    _share(alice, folder, "bob", "editor")
    bob = _client(store, "bob")
    assert bob.delete(f"/folders/{folder}").status_code == 403
    assert bob.post(f"/folders/{folder}/move", json={"parent_id": None}).status_code == 403


def test_move_rejects_cycles() -> None:
    """A folder cannot be moved into itself or a descendant."""
    store = _MemStore()
    alice = _client(store)
    root = _create(alice, "Root")
    child = _create(alice, "Child", parent_id=root)
    assert alice.post(f"/folders/{root}/move", json={"parent_id": child}).status_code == 400
    assert alice.post(f"/folders/{root}/move", json={"parent_id": root}).status_code == 400

    other = _create(alice, "Other")
    resp = alice.post(f"/folders/{child}/move", json={"parent_id": other})
    assert resp.status_code == 200
    assert resp.json()["parent_id"] == other


def test_editors_can_share_switch() -> None:
    """Editors may share until the owner turns resharing off; only the owner can flip it."""
    store = _MemStore()
    alice = _client(store)
    folder = _create(alice, "Shared")
    _share(alice, folder, "bob", "editor")
    bob = _client(store, "bob")
    _share(bob, folder, "carol", "viewer")

    assert bob.patch(f"/folders/{folder}", json={"editors_can_share": False}).status_code == 403
    assert alice.patch(f"/folders/{folder}", json={"editors_can_share": False}).status_code == 200
    resp = bob.post(f"/folders/{folder}/sharing/members", json={"username": "dave", "role": "viewer"})
    assert resp.status_code == 403
    assert bob.get(f"/folders/{folder}/sharing").json()["can_manage"] is False


def test_subfolder_cannot_lower_inherited_access() -> None:
    """A subfolder grant below the access inherited from a parent is rejected."""
    store = _MemStore()
    alice = _client(store)
    root = _create(alice, "Root")
    child = _create(alice, "Child", parent_id=root)
    _share(alice, root, "bob", "editor")

    resp = alice.post(f"/folders/{child}/sharing/members", json={"username": "bob", "role": "viewer"})
    assert resp.status_code == 400
    state = alice.get(f"/folders/{child}/sharing").json()
    assert state["inherited"] == [{"username": "bob", "role": "editor", "folder_id": root, "folder_name": "Root"}]


def test_anyone_link_claim_and_restrict() -> None:
    """Claiming an anyone link grants its tier; restricting the link takes it back."""
    store = _MemStore()
    alice = _client(store)
    folder = _create(alice, "Linked")
    resp = alice.put(f"/folders/{folder}/sharing", json={"general_access": "anyone", "general_role": "viewer"})
    token = resp.json()["token"]
    assert resp.json()["share_path"] == f"/folders/share/{token}"

    bob = _client(store, "bob")
    claim = bob.post(f"/folders/share/{token}/claim")
    assert claim.status_code == 200
    assert claim.json() == {"folder_id": folder, "role": "viewer"}
    assert [f["id"] for f in bob.get("/folders").json()["folders"]] == [folder]
    assert alice.get(f"/folders/{folder}/sharing").json()["members"] == []

    alice.put(f"/folders/{folder}/sharing", json={"general_access": "restricted"})
    assert bob.get("/folders").json()["folders"] == []
    assert bob.post(f"/folders/share/{token}/claim").status_code == 404


def test_transfer_ownership_keeps_old_owner_as_editor() -> None:
    """Transferring a folder makes the old owner an editor."""
    store = _MemStore()
    alice = _client(store)
    folder = _create(alice, "Handover")
    _share(alice, folder, "bob", "viewer")
    resp = alice.post(f"/folders/{folder}/sharing/transfer", json={"username": "bob"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["owner"] == "bob"
    assert body["role"] == "editor"
    assert body["members"] == [{"username": "alice", "role": "editor"}]


def test_moving_a_run_to_where_it_already_is_still_checks_access() -> None:
    """A no-op move of someone else's run is refused, so it cannot show the run exists or is unfiled."""
    store = _MemStore()
    _seed_job(store, "alice-run")
    bob = _client(store, "bob")
    body = bob.post("/folders/items", json={"optimization_ids": ["alice-run"], "folder_id": None}).json()
    assert body["updated"] == []
    assert body["skipped"] == [{"optimization_id": "alice-run", "reason": "not_found"}]
    alice = _client(store)
    assert alice.post("/folders/items", json={"optimization_ids": ["alice-run"], "folder_id": None}).json()[
        "updated"
    ] == ["alice-run"]


def test_a_viewer_is_told_a_move_is_forbidden() -> None:
    """A caller who can already see the run gets ``forbidden``, not ``not_found``."""
    store = _MemStore()
    _seed_job(store, "alice-run")
    alice = _client(store)
    folder = _create(alice, "Shared")
    alice.post("/folders/items", json={"optimization_ids": ["alice-run"], "folder_id": folder})
    _share(alice, folder, "bob", "viewer")
    body = (
        _client(store, "bob").post("/folders/items", json={"optimization_ids": ["alice-run"], "folder_id": None}).json()
    )
    assert body["skipped"] == [{"optimization_id": "alice-run", "reason": "forbidden"}]


def test_a_direct_run_grant_does_not_allow_a_move() -> None:
    """A direct grant on the run itself, even editor, lets the caller see it but not move it."""
    store = _MemStore()
    _seed_job(store, "alice-run")
    with Session(store.engine) as session:
        session.add(
            OptimizationShareGrantModel(
                optimization_id="alice-run", grantee_username="bob", role="editor", created_by="alice"
            )
        )
        session.commit()
    bob = _client(store, "bob")
    folder = _create(bob, "Mine")
    for destination in (folder, None):
        body = bob.post("/folders/items", json={"optimization_ids": ["alice-run"], "folder_id": destination}).json()
        assert body["updated"] == []
        assert body["skipped"] == [{"optimization_id": "alice-run", "reason": "forbidden"}]
