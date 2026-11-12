"""Tests for the optimization⇄dataset links.

Runs against an in-memory SQLite store (the sibling routers' pattern: a
``RemoteDBJobStore`` subclass that skips the pgvector bootstrap so
``Base.metadata.create_all`` stands up every table). Stored runs are seeded
directly, the way earlier submissions left them, so the dataset→optimizations
reverse link and saving a run's dataset round-trip through real rows.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ...constants import (
    PAYLOAD_OVERVIEW_DATASET_FILENAME,
    PAYLOAD_OVERVIEW_NAME,
    PAYLOAD_OVERVIEW_OPTIMIZATION_TYPE,
    PAYLOAD_OVERVIEW_SOURCE_DATASET_ID,
    PAYLOAD_OVERVIEW_USERNAME,
)
from ...storage.models import Base
from ...storage.remote import RemoteDBJobStore
from ..auth import AuthenticatedUser, get_authenticated_user
from ..converters import overview_to_base_fields
from ..errors import DomainError
from ..routers.dataset_library import create_dataset_library_router

_ALICE = AuthenticatedUser(username="alice", role="user", groups=())
_BOB = AuthenticatedUser(username="bob", role="user", groups=())

_ROWS = [{"question": "2+2", "answer": "4"}, {"question": "3+3", "answer": "6"}]
_SCHEMA = {
    "column_order": ["question", "answer"],
    "column_roles": {"question": "input", "answer": "output"},
    "column_kinds": {"question": "text", "answer": "text"},
}
_MAPPING = {"inputs": {"q": "question"}, "outputs": {"a": "answer"}}


class _MemStore(RemoteDBJobStore):
    """In-memory SQLite job store for the link tests (no pgvector)."""

    def __init__(self) -> None:
        """Build an in-memory SQLite engine and create the ORM tables."""
        self._engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self._engine)
        self._session_factory = sessionmaker(bind=self._engine)


def _app_for(store: _MemStore, user: AuthenticatedUser) -> FastAPI:
    """Mount the library router on a store, authed as ``user``.

    Args:
        store: Backing store the router reads runs and datasets from.
        user: Identity the auth dependency resolves to for every request.

    Returns:
        A FastAPI app whose ``DomainError``s render the production envelope.
    """
    app = FastAPI()
    app.include_router(create_dataset_library_router(job_store=store))
    app.dependency_overrides[get_authenticated_user] = lambda: user

    @app.exception_handler(DomainError)
    async def _domain_error_handler(_request, exc: DomainError) -> JSONResponse:
        """Mirror the app-level envelope so tests can assert on ``code``."""
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "code": exc.code, "params": exc.params},
        )

    return app


def _client(store: _MemStore, user: AuthenticatedUser) -> TestClient:
    """Build a test client over the library router authed as ``user``.

    Args:
        store: Backing store the client shares.
        user: Identity the auth dependency resolves to.

    Returns:
        A ``TestClient`` driving the mounted app.
    """
    return TestClient(_app_for(store, user), raise_server_exceptions=False)


def _save(client: TestClient, *, name: str = "Math", rows: list[dict[str, Any]] = _ROWS) -> str:
    """Save a dataset to the caller's library and return its id.

    Args:
        client: Authenticated test client.
        name: Display name for the entry.
        rows: Dataset rows to save.

    Returns:
        The new dataset's id.
    """
    resp = client.post(
        "/datasets/library",
        json={"name": name, "source": "upload", "dataset": rows, "column_schema": _SCHEMA},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["dataset"]["id"]


def _seed_run(
    store: _MemStore,
    *,
    username: str = "alice",
    name: str = "inline-run",
    source_dataset_id: str | None = None,
) -> str:
    """Store a finished run the way an earlier submission recorded it.

    Args:
        store: Backing store the run is written to.
        username: Owner of the run.
        name: Display name recorded in the overview.
        source_dataset_id: Library dataset the run was submitted from, if any.

    Returns:
        The new run's id.
    """
    optimization_id = str(uuid4())
    store.create_job(optimization_id, username=username)
    overview: dict[str, Any] = {
        PAYLOAD_OVERVIEW_OPTIMIZATION_TYPE: "blackbox",
        PAYLOAD_OVERVIEW_NAME: name,
        PAYLOAD_OVERVIEW_USERNAME: username,
        PAYLOAD_OVERVIEW_DATASET_FILENAME: "math.csv",
    }
    if source_dataset_id is not None:
        overview[PAYLOAD_OVERVIEW_SOURCE_DATASET_ID] = source_dataset_id
    store.set_payload_overview(optimization_id, overview)
    store.update_job(
        optimization_id,
        payload={"dataset": _ROWS, "column_mapping": _MAPPING, "column_order": ["question", "answer"]},
    )
    return optimization_id


def test_reverse_link_lists_runs_that_used_dataset() -> None:
    """The dataset→optimizations endpoint lists a run submitted from the dataset."""
    store = _MemStore()
    client = _client(store, _ALICE)
    dataset_id = _save(client)
    optimization_id = _seed_run(store, name="ref-run", source_dataset_id=dataset_id)

    listing = client.get(f"/datasets/library/{dataset_id}/optimizations")
    assert listing.status_code == 200, listing.text
    refs = listing.json()["optimizations"]
    assert [r["optimization_id"] for r in refs] == [optimization_id]
    assert refs[0]["name"] == "ref-run"
    assert refs[0]["optimization_type"] == "blackbox"


def test_reverse_link_excludes_runs_that_used_other_datasets() -> None:
    """A run with no source dataset never appears under any dataset's link."""
    store = _MemStore()
    client = _client(store, _ALICE)
    dataset_id = _save(client)
    _seed_run(store)

    listing = client.get(f"/datasets/library/{dataset_id}/optimizations").json()
    assert listing["optimizations"] == []


def test_save_run_dataset_round_trips_into_library() -> None:
    """Saving a run's dataset stores its rows and a schema from the run mapping."""
    store = _MemStore()
    client = _client(store, _ALICE)
    optimization_id = _seed_run(store)

    saved = client.post(f"/datasets/library/from-optimization/{optimization_id}")
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["dataset"]["source"] == "optimization"
    assert body["dataset"]["name"] == "math.csv"
    dataset_id = body["dataset"]["id"]

    rows = client.get(f"/datasets/library/{dataset_id}/rows").json()
    assert rows["rows"] == _ROWS
    assert rows["columns"] == ["question", "answer"]
    assert rows["column_schema"]["column_roles"] == {"question": "input", "answer": "output"}
    assert rows["column_schema"]["column_kinds"] == {"question": "text", "answer": "text"}


def test_save_run_dataset_accepts_name_override() -> None:
    """An explicit name overrides the run-derived default on the saved entry."""
    store = _MemStore()
    client = _client(store, _ALICE)
    optimization_id = _seed_run(store)

    saved = client.post(f"/datasets/library/from-optimization/{optimization_id}", json={"name": "Renamed"})
    assert saved.status_code == 200, saved.text
    assert saved.json()["dataset"]["name"] == "Renamed"


def test_save_run_dataset_requires_run_access() -> None:
    """A caller who cannot see the run cannot save its dataset (404)."""
    store = _MemStore()
    optimization_id = _seed_run(store)

    bob = _client(store, _BOB)
    resp = bob.post(f"/datasets/library/from-optimization/{optimization_id}")
    assert resp.status_code == 404


def test_overview_to_base_fields_surfaces_source_dataset_id() -> None:
    """The converter maps the stored source id onto the response field."""
    fields = overview_to_base_fields({PAYLOAD_OVERVIEW_SOURCE_DATASET_ID: "ds-123"})
    assert fields["source_dataset_id"] == "ds-123"
    assert overview_to_base_fields({})["source_dataset_id"] is None
