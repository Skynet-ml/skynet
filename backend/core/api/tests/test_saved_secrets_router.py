"""Tests for the saved secrets router.

Runs against an in-memory SQLite store with a throwaway vault key, so the
tests cover the ciphertext-only storage, value-free responses, owner scoping,
replace-by-name, and the decrypt path a repository run uses.
"""

from __future__ import annotations

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

from ...connectors.saved_secrets import SavedSecretStore
from ...storage.models import UserSecretModel
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
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
