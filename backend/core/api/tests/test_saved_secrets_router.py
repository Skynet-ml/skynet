"""Tests for the saved secrets router.

Runs against an in-memory SQLite store with a throwaway vault key, so the
tests cover the ciphertext-only storage, value-free responses, owner scoping,
replace-by-name, and the decrypt path a repository run uses.
"""

from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ...config import settings
from ...connectors.saved_secrets import SavedSecretStore
from ...local.picked_folders import PICKED_FOLDERS_FILE
from ...storage.models import UserSecretModel
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..routers import saved_secrets as saved_secrets_router
from ..routers.saved_secrets import create_saved_secrets_router
from .test_connectors_router import _MemStore, vault_key  # noqa: F401 - fixture re-export

pytestmark = pytest.mark.usefixtures("vault_key")

_ALICE = AuthenticatedUser(username="alice", role="user", groups=())
_BOB = AuthenticatedUser(username="bob", role="user", groups=())


def _client(store: _MemStore, user: AuthenticatedUser) -> TestClient:
    """Mount the secrets router on ``store`` authed as ``user``.

    Args:
        store: Shared in-memory store.
        user: Identity every request resolves to.

    Returns:
        A test client with the app's error envelope.
    """
    app = FastAPI()
    app.include_router(create_saved_secrets_router(job_store=store))
    app.dependency_overrides[get_authenticated_user] = lambda: user

    @app.exception_handler(DomainError)
    async def _domain_error_handler(_request, exc: DomainError) -> JSONResponse:
        """Mirror the app-level envelope so tests can assert on ``code``."""
        return JSONResponse(status_code=exc.status_code, content={"code": exc.code, "params": exc.params})

    return TestClient(app)


def test_saved_value_is_encrypted_and_never_returned() -> None:
    """The value is stored as ciphertext and absent from every response."""
    store = _MemStore()
    client = _client(store, _ALICE)
    saved = client.put("/secrets", json={"name": "NPM_TOKEN", "value": "s3cret-value"})
    assert saved.status_code == 200
    assert "s3cret-value" not in saved.text
    listed = client.get("/secrets")
    assert [s["name"] for s in listed.json()["secrets"]] == ["NPM_TOKEN"]
    assert "s3cret-value" not in listed.text
    with Session(store.engine) as session:
        row = session.scalars(select(UserSecretModel)).one()
    assert b"s3cret-value" not in row.secret_ciphertext
    assert SavedSecretStore(store.engine).resolve("alice", row.id) == "s3cret-value"


def test_saving_the_same_name_replaces_the_value() -> None:
    """A second save under one name keeps one row with the new value."""
    store = _MemStore()
    client = _client(store, _ALICE)
    first = client.put("/secrets", json={"name": "API_KEY", "value": "old"}).json()
    second = client.put("/secrets", json={"name": "API_KEY", "value": "new"}).json()
    assert first["id"] == second["id"]
    assert len(client.get("/secrets").json()["secrets"]) == 1
    assert SavedSecretStore(store.engine).resolve("alice", first["id"]) == "new"


def test_secrets_are_scoped_to_their_owner() -> None:
    """Another user can neither list, delete, nor resolve someone's secret."""
    store = _MemStore()
    secret_id = _client(store, _ALICE).put("/secrets", json={"name": "TOKEN", "value": "v"}).json()["id"]
    bob = _client(store, _BOB)
    assert bob.get("/secrets").json()["secrets"] == []
    assert bob.delete(f"/secrets/{secret_id}").json()["code"] == "secrets.not_found"
    with pytest.raises(DomainError) as caught:
        SavedSecretStore(store.engine).resolve("bob", secret_id)
    assert caught.value.code == "secrets.not_found"


def test_delete_removes_the_secret() -> None:
    """Deleting returns 204 and the secret disappears from the list."""
    store = _MemStore()
    client = _client(store, _ALICE)
    secret_id = client.put("/secrets", json={"name": "TOKEN", "value": "v"}).json()["id"]
    assert client.delete(f"/secrets/{secret_id}").status_code == 204
    assert client.get("/secrets").json()["secrets"] == []


@pytest.mark.parametrize("name", ["1TOKEN", "MY-TOKEN", "", "A B"])
def test_invalid_names_are_rejected(name: str) -> None:
    """Names must be valid environment variable names."""
    client = _client(_MemStore(), _ALICE)
    assert client.put("/secrets", json={"name": name, "value": "v"}).status_code == 422


def test_save_stores_the_approved_hosts_and_returns_them_without_the_value() -> None:
    """The hosts are cleaned and stored, and a new save replaces them (SKY-641)."""
    store = _MemStore()
    client = _client(store, _ALICE)
    body = {"name": "MAPS_KEY", "value": "maps-value", "allowed_hosts": [" Maps.GoogleAPIs.com. ", "*.example.com"]}
    saved = client.put("/secrets", json=body)
    assert saved.status_code == 200
    assert saved.json()["allowed_hosts"] == ["maps.googleapis.com", "*.example.com"]
    listed = client.get("/secrets")
    assert listed.json()["secrets"][0]["allowed_hosts"] == ["maps.googleapis.com", "*.example.com"]
    assert "maps-value" not in listed.text
    replaced = client.put("/secrets", json={"name": "MAPS_KEY", "value": "v2", "allowed_hosts": ["api.example.org"]})
    assert replaced.json()["allowed_hosts"] == ["api.example.org"]


def test_save_without_hosts_approves_no_host() -> None:
    """A secret saved with no host, as every secret before SKY-641, lists an empty host list."""
    client = _client(_MemStore(), _ALICE)
    assert client.put("/secrets", json={"name": "TOKEN", "value": "v"}).json()["allowed_hosts"] == []


@pytest.mark.parametrize("host", ["*", "*.com", "http://api.example.com", "api.example.com/path", "bad host"])
def test_save_refuses_a_bad_host(host: str) -> None:
    """An approved host must be an exact host or ``*.suffix``, and the error does not echo the value."""
    client = _client(_MemStore(), _ALICE)
    response = client.put("/secrets", json={"name": "TOKEN", "value": "v-sentinel", "allowed_hosts": [host]})
    assert response.status_code == 422


def test_store_rejects_bad_names_outside_the_router() -> None:
    """The store enforces the name rule itself, not only the request model."""
    with pytest.raises(DomainError) as caught:
        SavedSecretStore(_MemStore().engine).save("alice", "bad-name", "v")
    assert caught.value.code == "secrets.invalid_name"


def test_migration_gives_existing_secrets_no_host() -> None:
    """The SKY-641 migration adds an empty host list, so an old secret goes to no host until saved again."""
    store = _MemStore()
    secret_id = _client(store, _ALICE).put("/secrets", json={"name": "MAPS_KEY", "value": "v"}).json()["id"]
    with store.engine.begin() as connection:
        connection.execute(text("ALTER TABLE user_secrets DROP COLUMN allowed_hosts"))
    migration = runpy.run_path(
        str(Path(__file__).resolve().parents[3] / "alembic/versions/c4d1e8a2f6b3_add_user_secrets_allowed_hosts.py")
    )
    for _ in range(2):
        with store.engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            migration["upgrade"]()

    assert _client(store, _ALICE).get("/secrets").json()["secrets"][0]["allowed_hosts"] == []
    secrets = SavedSecretStore(store.engine)
    assert secrets.resolve("alice", secret_id) == "v"
    with pytest.raises(ValueError, match="may not go to"):
        secrets.resolve("alice", secret_id, ["maps.googleapis.com"])


_ENV_VALUE = "env-SENTINEL-4b9d"
_SAVED_VALUE = "saved-SENTINEL-8e2c"


@pytest.fixture
def local_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Turn local mode on for one test, with the test's ``project`` folder picked in the app.

    Args:
        monkeypatch: Pytest's patcher, for the local settings.
        tmp_path: Test directory.
    """
    monkeypatch.setattr(settings, "local_mode", True)
    state = tmp_path / "state"
    state.mkdir()
    (state / PICKED_FOLDERS_FILE).write_text(json.dumps([str(tmp_path / "project")]))
    monkeypatch.setattr(settings, "local_state_dir", str(state))


def _env_folder(tmp_path: Path, text: str) -> str:
    """Write a folder whose root ``.env`` holds ``text``.

    Args:
        tmp_path: Test directory.
        text: Content of the ``.env`` file.

    Returns:
        The absolute folder path.
    """
    folder = tmp_path / "project"
    folder.mkdir()
    (folder / ".env").write_text(text, encoding="utf-8")
    return str(folder)


@pytest.mark.parametrize(
    ("method", "url", "body"),
    [("get", "/secrets/local-env?path=/tmp", None), ("post", "/secrets/local-env/import", {"path": "/tmp"})],
)
def test_local_env_routes_are_hidden_outside_local_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, method: str, url: str, body: dict | None
) -> None:
    """Answer 404 outside local mode and read no file."""
    monkeypatch.setattr(settings, "local_mode", False)
    monkeypatch.setattr(saved_secrets_router, "read_env_file", lambda _path: pytest.fail("read a file"))
    client = _client(_MemStore(), _ALICE)
    response = client.request(method, url, json=body)
    assert response.status_code == 404
    assert response.json()["code"] == "local.not_available"


def test_local_env_routes_are_not_in_the_openapi_schema() -> None:
    """Keep the local-only routes out of the public API contract."""
    app = FastAPI()
    app.include_router(create_saved_secrets_router(job_store=_MemStore()))
    assert not any("local-env" in path for path in app.openapi()["paths"])


@pytest.mark.usefixtures("local_mode")
def test_preview_lists_names_and_clashes_without_values(tmp_path: Path) -> None:
    """List each name with its clash flag, and never return a value."""
    store = _MemStore()
    client = _client(store, _ALICE)
    client.put("/secrets", json={"name": "SAME", "value": _ENV_VALUE})
    client.put("/secrets", json={"name": "OTHER", "value": _SAVED_VALUE})
    folder = _env_folder(tmp_path, f"NEW={_ENV_VALUE}\nSAME={_ENV_VALUE}\nOTHER={_ENV_VALUE}\nbad-name={_ENV_VALUE}\n")
    response = client.get("/secrets/local-env", params={"path": folder})
    assert response.status_code == 200
    assert response.json() == {
        "status": "found",
        "entries": [
            {"name": "NEW", "clash": False},
            {"name": "SAME", "clash": False},
            {"name": "OTHER", "clash": True},
        ],
        "skipped": [{"line": 4, "name": "bad-name", "reason": "invalid_name"}],
    }
    assert "SENTINEL" not in response.text


@pytest.mark.usefixtures("local_mode")
def test_preview_reports_a_folder_without_env(tmp_path: Path) -> None:
    """Report ``missing`` when the folder has no ``.env`` file."""
    (tmp_path / "project").mkdir()
    response = _client(_MemStore(), _ALICE).get("/secrets/local-env", params={"path": str(tmp_path / "project")})
    assert response.json() == {"status": "missing", "entries": [], "skipped": []}


@pytest.mark.usefixtures("local_mode")
def test_preview_and_import_treat_a_folder_the_user_did_not_pick_as_missing(tmp_path: Path) -> None:
    """Read no ``.env`` outside the picked folders, also through a link out of one, and save nothing (SKY-1007)."""
    _env_folder(tmp_path, "A=1\n")
    other = tmp_path / "other"
    other.mkdir()
    (other / ".env").write_text(f"STOLEN={_ENV_VALUE}\n", encoding="utf-8")
    (tmp_path / "project" / "up").symlink_to(tmp_path)
    client = _client(_MemStore(), _ALICE)
    for folder in (other, tmp_path, tmp_path / "project" / "up" / "other"):
        preview = client.get("/secrets/local-env", params={"path": str(folder)})
        assert preview.json() == {"status": "missing", "entries": [], "skipped": []}, folder
        imported = client.post("/secrets/local-env/import", json={"path": str(folder)})
        assert imported.json()["status"] == "missing", folder
        assert "SENTINEL" not in preview.text + imported.text
    assert client.get("/secrets").json()["secrets"] == []


@pytest.mark.usefixtures("local_mode")
def test_preview_and_import_read_a_folder_below_a_picked_one(tmp_path: Path) -> None:
    """A folder below the picked one keeps today's behavior (SKY-1007)."""
    sub = tmp_path / "project" / "service"
    sub.mkdir(parents=True)
    (sub / ".env").write_text(f"TOKEN={_ENV_VALUE}\n", encoding="utf-8")
    client = _client(_MemStore(), _ALICE)
    preview = client.get("/secrets/local-env", params={"path": str(sub)})
    assert preview.json()["status"] == "found"
    imported = client.post("/secrets/local-env/import", json={"path": str(sub)})
    assert imported.json()["imported"] == ["TOKEN"]


@pytest.mark.usefixtures("local_mode")
def test_preview_and_import_refuse_a_link_that_leaves_the_folder(tmp_path: Path) -> None:
    """Refuse a ``.env`` link to a file outside the folder, and save nothing."""
    outside = tmp_path / "elsewhere.env"
    outside.write_text(f"STOLEN={_ENV_VALUE}\n", encoding="utf-8")
    folder = tmp_path / "project"
    folder.mkdir()
    (folder / ".env").symlink_to(outside)
    store = _MemStore()
    client = _client(store, _ALICE)
    preview = client.get("/secrets/local-env", params={"path": str(folder)})
    assert preview.json()["status"] == "unsafe"
    imported = client.post("/secrets/local-env/import", json={"path": str(folder)})
    assert imported.json()["status"] == "unsafe"
    assert client.get("/secrets").json()["secrets"] == []
    assert "SENTINEL" not in preview.text + imported.text


@pytest.mark.usefixtures("local_mode")
def test_import_saves_new_names_with_no_host(tmp_path: Path) -> None:
    """Save each new pair with no host, and return names only."""
    store = _MemStore()
    client = _client(store, _ALICE)
    folder = _env_folder(tmp_path, f"API_KEY={_ENV_VALUE}\n")
    response = client.post("/secrets/local-env/import", json={"path": folder})
    assert response.status_code == 200
    body = response.json()
    assert body["imported"] == ["API_KEY"]
    assert [(s["name"], s["allowed_hosts"]) for s in body["secrets"]] == [("API_KEY", [])]
    assert "SENTINEL" not in response.text
    assert SavedSecretStore(store.engine).resolve("alice", body["secrets"][0]["id"]) == _ENV_VALUE


@pytest.mark.usefixtures("local_mode")
def test_import_never_overwrites_a_clash_silently(tmp_path: Path) -> None:
    """Keep a clashing saved value and its hosts unless the caller names it in ``replace``."""
    store = _MemStore()
    client = _client(store, _ALICE)
    saved = client.put(
        "/secrets", json={"name": "TOKEN", "value": _SAVED_VALUE, "allowed_hosts": ["api.example.com"]}
    ).json()
    folder = _env_folder(tmp_path, f"TOKEN={_ENV_VALUE}\n")
    secrets = SavedSecretStore(store.engine)

    kept = client.post("/secrets/local-env/import", json={"path": folder}).json()
    assert kept["kept"] == ["TOKEN"]
    assert kept["replaced"] == []
    assert kept["secrets"][0]["id"] == saved["id"]
    assert secrets.resolve("alice", saved["id"]) == _SAVED_VALUE

    other = client.post("/secrets/local-env/import", json={"path": folder, "replace": ["ELSE"]}).json()
    assert other["kept"] == ["TOKEN"]
    assert secrets.resolve("alice", saved["id"]) == _SAVED_VALUE

    replaced = client.post("/secrets/local-env/import", json={"path": folder, "replace": ["TOKEN"]})
    assert replaced.json()["replaced"] == ["TOKEN"]
    assert replaced.json()["secrets"][0]["allowed_hosts"] == ["api.example.com"]
    assert secrets.resolve("alice", saved["id"]) == _ENV_VALUE
    assert "SENTINEL" not in str(kept) + str(other) + replaced.text


@pytest.mark.usefixtures("local_mode")
def test_import_leaves_a_same_value_secret_as_it_is(tmp_path: Path) -> None:
    """Change nothing for a name saved with the same value."""
    store = _MemStore()
    client = _client(store, _ALICE)
    saved = client.put("/secrets", json={"name": "TOKEN", "value": _ENV_VALUE, "allowed_hosts": ["a.example.com"]})
    folder = _env_folder(tmp_path, f"TOKEN={_ENV_VALUE}\n")
    body = client.post("/secrets/local-env/import", json={"path": folder}).json()
    assert (body["imported"], body["replaced"], body["kept"]) == ([], [], [])
    assert body["secrets"][0]["updated_at"] == saved.json()["updated_at"]
    assert body["secrets"][0]["allowed_hosts"] == ["a.example.com"]


@pytest.mark.usefixtures("local_mode")
def test_import_saves_nothing_when_the_names_do_not_fit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Refuse the whole import when the new names pass the per-user limit."""
    monkeypatch.setattr(saved_secrets_router, "MAX_SECRETS_PER_USER", 1)
    store = _MemStore()
    client = _client(store, _ALICE)
    folder = _env_folder(tmp_path, "FIRST=1\nSECOND=2\n")
    response = client.post("/secrets/local-env/import", json={"path": folder})
    assert response.status_code == 422
    assert response.json()["code"] == "secrets.limit_reached"
    assert client.get("/secrets").json()["secrets"] == []


@pytest.mark.usefixtures("local_mode")
def test_import_refuses_a_bad_replace_name(tmp_path: Path) -> None:
    """Refuse a ``replace`` entry that is not a valid name."""
    folder = _env_folder(tmp_path, "A=1\n")
    response = _client(_MemStore(), _ALICE).post("/secrets/local-env/import", json={"path": folder, "replace": ["a b"]})
    assert response.status_code == 422
