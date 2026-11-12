"""Tests for the connectors router (Hugging Face).

Runs the router against an in-memory SQLite store with every Hugging Face
call mocked at the :mod:`httpx` boundary, so the tests cover the vault
round-trip (ciphertext only, masked views), the token fallback, the OAuth
start/callback/finish steps, and the parquet import path through the library's gated
save.
"""

from __future__ import annotations

import io
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ...config import settings
from ...connectors import huggingface as hf
from ...connectors.vault import ConnectorVault
from ...storage.dataset_library import DatasetLibraryStore, PostgresDatasetBlobStore
from ...storage.models import Base, UserConnectorModel
from ...storage.remote import RemoteDBJobStore
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..routers.connectors import create_connectors_router

_ALICE = AuthenticatedUser(username="alice", role="user", groups=())


class _MemStore(RemoteDBJobStore):
    """In-memory SQLite job store for connector tests (no pgvector)."""

    def __init__(self) -> None:
        """Build an in-memory SQLite engine and create the ORM tables."""
        self._engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self._engine)
        self._session_factory = sessionmaker(bind=self._engine)


def _make_client(user: AuthenticatedUser = _ALICE) -> tuple[TestClient, _MemStore]:
    """Mount the connectors router authed as ``user`` with the error envelope.

    Args:
        user: Identity the auth dependency resolves to for every request.

    Returns:
        A ``(client, store)`` pair sharing one in-memory store.
    """
    store = _MemStore()
    app = FastAPI()
    app.include_router(create_connectors_router(job_store=store))
    app.dependency_overrides[get_authenticated_user] = lambda: user

    @app.exception_handler(DomainError)
    async def _domain_error_handler(_request, exc: DomainError) -> JSONResponse:
        """Mirror the app-level envelope so tests can assert on ``code``."""
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "code": exc.code, "params": exc.params},
        )

    return TestClient(app), store


@pytest.fixture
def vault_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """Configure a throwaway Fernet key for the duration of a test."""
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setattr(settings, "byok_vault_key", SecretStr(key))
    monkeypatch.setattr(settings, "hf_oauth_client_id", None)
    monkeypatch.setattr(settings, "hf_oauth_client_secret", None)
    monkeypatch.setattr(settings, "hf_oauth_redirect_uri", None)
    return key


def _response(status: int, body: Any = None, content: bytes | None = None) -> httpx.Response:
    """Build an httpx response with a request attached (so ``.json()`` works).

    Args:
        status: HTTP status.
        body: JSON body, if any.
        content: Raw bytes body, if any.

    Returns:
        The response.
    """
    request = httpx.Request("GET", "https://huggingface.co/")
    if content is not None:
        return httpx.Response(status, content=content, request=request)
    return httpx.Response(status, json=body, request=request)


def test_list_is_empty_and_reports_oauth_unavailable(vault_key: str) -> None:
    """A fresh user sees the HF provider unlinked with no OAuth button."""
    client, _ = _make_client()
    body = client.get("/connectors").json()
    assert body["connectors"][0] == {
        "provider": "huggingface",
        "connected": False,
        "status": None,
        "account_label": None,
        "auth_method": None,
        "oauth_available": False,
        "picker_available": False,
        "connected_at": None,
    }


def test_save_token_verifies_and_stores_ciphertext(vault_key: str) -> None:
    """A pasted token is checked against whoami and never stored in the clear."""
    client, store = _make_client()
    with patch("core.connectors.huggingface.httpx.get", return_value=_response(200, {"name": "alice-hf"})) as get:
        response = client.put("/connectors/huggingface/token", json={"token": "hf_secret"})
    assert response.status_code == 200
    entry = response.json()["connectors"][0]
    assert entry["connected"] is True
    assert entry["account_label"] == "alice-hf"
    assert entry["auth_method"] == "token"
    assert entry["status"] == "connected"
    assert get.call_args.kwargs["headers"]["Authorization"] == "Bearer hf_secret"
    with Session(store.engine) as session:
        row = session.execute(select(UserConnectorModel)).scalar_one()
    assert b"hf_secret" not in row.secret_ciphertext
    assert ConnectorVault(store.engine).resolve("alice", "huggingface").access_token == "hf_secret"


def test_save_token_rejected_by_hub(vault_key: str) -> None:
    """A 401 from whoami surfaces as an invalid-token error and stores nothing."""
    client, _ = _make_client()
    with patch("core.connectors.huggingface.httpx.get", return_value=_response(401, {"error": "bad"})):
        response = client.put("/connectors/huggingface/token", json={"token": "nope"})
    assert response.status_code == 400
    assert response.json()["code"] == "connectors.hf_invalid_token"
    assert client.get("/connectors").json()["connectors"][0]["connected"] is False


def test_save_token_requires_vault_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a vault key the link is refused with 503 before calling HF."""
    monkeypatch.setattr(settings, "byok_vault_key", None)
    client, _ = _make_client()
    with patch("core.connectors.huggingface.httpx.get", return_value=_response(200, {"name": "x"})):
        response = client.put("/connectors/huggingface/token", json={"token": "hf_secret"})
    assert response.status_code == 503
    assert response.json()["code"] == "connectors.vault_not_configured"


def test_delete_is_idempotent(vault_key: str) -> None:
    """Disconnecting twice succeeds both times and leaves the user unlinked."""
    client, _ = _make_client()
    with patch("core.connectors.huggingface.httpx.get", return_value=_response(200, {"name": "alice-hf"})):
        client.put("/connectors/huggingface/token", json={"token": "hf_secret"})
    first = client.delete("/connectors/huggingface")
    second = client.delete("/connectors/huggingface")
    assert first.status_code == second.status_code == 200
    assert second.json()["connectors"][0]["connected"] is False


def test_oauth_start_503_when_unconfigured(vault_key: str) -> None:
    """Starting OAuth without a client id is refused."""
    client, _ = _make_client()
    response = client.post("/connectors/huggingface/oauth/start")
    assert response.status_code == 503
    assert response.json()["code"] == "connectors.hf_oauth_not_configured"


_BOB = AuthenticatedUser(username="bob", role="user", groups=())


def oauth_fragment(response: httpx.Response) -> dict[str, str]:
    """Assert a callback redirect that carries the finish values in the fragment.

    Args:
        response: The callback's response.

    Returns:
        The fragment values, one per key.
    """
    assert response.status_code == 303
    target = urlparse(response.headers["location"])
    assert "error" not in target.query, target.query
    return {key: values[0] for key, values in parse_qs(target.fragment).items()}


def _hf_start(client: TestClient) -> str:
    """Start the Hugging Face sign-in and return the state.

    Args:
        client: The test client.

    Returns:
        The encrypted state from the authorize URL.
    """
    start = client.post("/connectors/huggingface/oauth/start")
    assert start.status_code == 200
    return parse_qs(urlparse(start.json()["authorize_url"]).query)["state"][0]


def _hf_finish(client: TestClient, state: str, code: str = "code-xyz") -> tuple[httpx.Response, Any]:
    """Call the finish route with Hugging Face's token and userinfo calls mocked.

    Args:
        client: The test client.
        state: The state from the callback.
        code: The code from the callback.

    Returns:
        The finish response and the mock of the token call.
    """
    token_body = {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 28800, "scope": "openid profile"}
    with (
        patch("core.connectors.huggingface.httpx.post", return_value=_response(200, token_body)) as post,
        patch(
            "core.connectors.huggingface.httpx.get",
            return_value=_response(200, {"preferred_username": "alice-hf"}),
        ),
    ):
        response = client.post("/connectors/huggingface/oauth/finish", json={"state": state, "code": code})
    return response, post


def test_oauth_start_and_finish_link_the_account(vault_key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The callback only forwards the code; the signed-in finish stores the tokens."""
    monkeypatch.setattr(settings, "hf_oauth_client_id", "client-123")
    monkeypatch.setattr(settings, "app_public_url", "https://app.example")
    client, store = _make_client()
    assert client.get("/connectors").json()["connectors"][0]["oauth_available"] is True

    start = client.post("/connectors/huggingface/oauth/start")
    assert start.status_code == 200
    url = urlparse(start.json()["authorize_url"])
    assert url.netloc == "huggingface.co"
    query = parse_qs(url.query)
    assert query["client_id"] == ["client-123"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == ["http://testserver/connectors/huggingface/oauth/callback"]
    state = query["state"][0]
    payload = hf.parse_state(state)
    assert payload["u"] == "alice"

    with patch("core.connectors.huggingface.httpx.post") as never:
        callback = client.get(
            "/connectors/huggingface/oauth/callback",
            params={"code": "code-xyz", "state": state},
            follow_redirects=False,
        )
    never.assert_not_called()
    assert callback.headers["location"].startswith("https://app.example/?settings=connectors#")
    assert oauth_fragment(callback) == {
        "oauth_flow": "connector:huggingface",
        "oauth_state": state,
        "oauth_code": "code-xyz",
    }
    assert client.get("/connectors").json()["connectors"][0]["connected"] is False
    assert ConnectorVault(store.engine).resolve("alice", "huggingface") is None

    finish, post = _hf_finish(client, state)
    assert finish.status_code == 200, finish.text
    assert finish.json()["connectors"][0]["connected"] is True
    form = post.call_args.kwargs["data"]
    assert form["code"] == "code-xyz"
    assert form["code_verifier"] == payload["v"]
    assert form["client_id"] == "client-123"
    assert "client_secret" not in form

    entry = client.get("/connectors").json()["connectors"][0]
    assert entry["connected"] is True
    assert entry["auth_method"] == "oauth"
    assert entry["account_label"] == "alice-hf"
    secret = ConnectorVault(store.engine).resolve("alice", "huggingface")
    assert secret.access_token == "at-1"
    assert secret.refresh_token == "rt-1"
    assert secret.expires_at is not None


def test_oauth_finish_by_another_user_is_refused(vault_key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A state that Alice started cannot link a Hugging Face account to Bob."""
    monkeypatch.setattr(settings, "hf_oauth_client_id", "client-123")
    client, store = _make_client()
    state = _hf_start(client)
    client.app.dependency_overrides[get_authenticated_user] = lambda: _BOB
    finish, post = _hf_finish(client, state)
    assert finish.status_code == 403
    assert finish.json()["code"] == "connectors.hf_oauth_state_invalid"
    post.assert_not_called()
    vault = ConnectorVault(store.engine)
    assert vault.resolve("bob", "huggingface") is None
    assert vault.resolve("alice", "huggingface") is None


def test_oauth_finish_needs_a_signed_in_caller(vault_key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a bearer token the finish route answers 401 and trades nothing."""
    monkeypatch.setattr(settings, "hf_oauth_client_id", "client-123")
    client, store = _make_client()
    state = _hf_start(client)
    client.app.dependency_overrides.pop(get_authenticated_user)
    finish, post = _hf_finish(client, state)
    assert finish.status_code == 401
    post.assert_not_called()
    assert ConnectorVault(store.engine).resolve("alice", "huggingface") is None


def test_oauth_finish_refuses_a_bad_expired_or_used_state(vault_key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A forged or expired state never reaches Hugging Face; a used code is refused there."""
    monkeypatch.setattr(settings, "hf_oauth_client_id", "client-123")
    client, store = _make_client()

    forged, post = _hf_finish(client, "garbage")
    assert forged.status_code == 400
    assert forged.json()["code"] == "connectors.hf_oauth_state_invalid"
    post.assert_not_called()

    state = _hf_start(client)
    with patch("cryptography.fernet.time.time", return_value=time.time() + 3600):
        expired, post = _hf_finish(client, state)
    assert expired.status_code == 400
    assert expired.json()["code"] == "connectors.hf_oauth_state_invalid"
    post.assert_not_called()

    first, _ = _hf_finish(client, state)
    assert first.status_code == 200
    with patch(
        "core.connectors.huggingface.httpx.post",
        return_value=_response(400, {"error": "invalid_grant"}),
    ):
        reused = client.post("/connectors/huggingface/oauth/finish", json={"state": state, "code": "code-xyz"})
    assert reused.status_code == 400
    assert reused.json()["code"] == "connectors.hf_oauth_failed"
    assert ConnectorVault(store.engine).resolve("alice", "huggingface").access_token == "at-1"


def test_oauth_callback_without_a_code_redirects_with_error(vault_key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A declined sign-in bounces back with an error code and no fragment."""
    monkeypatch.setattr(settings, "hf_oauth_client_id", "client-123")
    monkeypatch.setattr(settings, "app_public_url", "https://app.example")
    client, _ = _make_client()
    callback = client.get(
        "/connectors/huggingface/oauth/callback",
        params={"error": "access_denied", "state": "s"},
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert callback.headers["location"] == (
        "https://app.example/?settings=connectors&connector_error=connectors.hf_oauth_failed"
    )


def test_expired_oauth_token_is_refreshed_before_use(vault_key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale access token is exchanged for a fresh one via the refresh grant."""
    monkeypatch.setattr(settings, "hf_oauth_client_id", "client-123")
    client, store = _make_client()
    ConnectorVault(store.engine).save(
        "alice",
        "huggingface",
        access_token="old",
        auth_method="oauth",
        account_label="alice-hf",
        refresh_token="rt-1",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    with (
        patch(
            "core.connectors.huggingface.httpx.post",
            return_value=_response(200, {"access_token": "new", "refresh_token": "rt-2", "expires_in": 3600}),
        ),
        patch("core.connectors.huggingface.httpx.get", return_value=_response(200, [])) as get,
    ):
        response = client.get("/connectors/huggingface/datasets", params={"search": "squad"})
    assert response.status_code == 200
    assert get.call_args.kwargs["headers"]["Authorization"] == "Bearer new"
    assert ConnectorVault(store.engine).resolve("alice", "huggingface").refresh_token == "rt-2"


def test_search_maps_hub_results(vault_key: str) -> None:
    """Hub search works anonymously and trims each hit to the compact shape."""
    client, _ = _make_client()
    hub = [{"id": "rajpurkar/squad", "author": "rajpurkar", "downloads": 12, "likes": 3, "gated": "auto"}]
    with patch("core.connectors.huggingface.httpx.get", return_value=_response(200, hub)) as get:
        response = client.get("/connectors/huggingface/datasets", params={"search": "squad", "limit": 5})
    assert response.status_code == 200
    assert response.json() == {
        "datasets": [
            {
                "id": "rajpurkar/squad",
                "author": "rajpurkar",
                "downloads": 12,
                "likes": 3,
                "private": False,
                "gated": True,
                "last_modified": None,
            }
        ]
    }
    assert "Authorization" not in get.call_args.kwargs["headers"]
    assert get.call_args.kwargs["params"]["search"] == "squad"


def test_splits_merge_sizes_and_404_unknown_dataset(vault_key: str) -> None:
    """Splits carry viewer sizes; an unknown repo maps to 404."""
    client, _ = _make_client()

    def fake_get(url: str, **kwargs: Any) -> httpx.Response:
        if url.endswith("/splits"):
            return _response(200, {"splits": [{"config": "default", "split": "train"}]})
        return _response(
            200,
            {
                "size": {
                    "splits": [{"config": "default", "split": "train", "num_rows": 7, "num_bytes_parquet_files": 99}]
                }
            },
        )

    with patch("core.connectors.huggingface.httpx.get", side_effect=fake_get):
        response = client.get("/connectors/huggingface/datasets/owner/name/splits")
    assert response.json() == {"splits": [{"config": "default", "split": "train", "num_rows": 7, "num_bytes": 99}]}

    with patch("core.connectors.huggingface.httpx.get", return_value=_response(404, {"error": "missing"})):
        response = client.get("/connectors/huggingface/datasets/owner/missing/splits")
    assert response.status_code == 404
    assert response.json()["code"] == "connectors.hf_dataset_not_found"
    assert response.json()["params"]["repo_id"] == "owner/missing"


def test_preview_flattens_types_and_image_cells(vault_key: str) -> None:
    """Preview rows come back JSON-friendly with viewer image assets as URLs."""
    client, _ = _make_client()
    viewer = {
        "features": [
            {"name": "question", "type": {"_type": "Value", "dtype": "string"}},
            {"name": "image", "type": {"_type": "Image"}},
        ],
        "rows": [
            {"row_idx": 0, "row": {"question": "q", "image": {"src": "https://x/img.png", "height": 1, "width": 1}}}
        ],
        "num_rows_total": 42,
    }
    with patch("core.connectors.huggingface.httpx.get", return_value=_response(200, viewer)):
        response = client.get(
            "/connectors/huggingface/datasets/owner/name/preview", params={"config": "default", "split": "train"}
        )
    assert response.json() == {
        "columns": [{"name": "question", "type": "string"}, {"name": "image", "type": "Image"}],
        "rows": [{"question": "q", "image": "https://x/img.png"}],
        "num_rows_total": 42,
    }


def _parquet_bytes(rows: list[dict[str, Any]]) -> bytes:
    """Serialise rows to an in-memory parquet file.

    Args:
        rows: Row dicts sharing one schema.

    Returns:
        The parquet bytes.
    """
    buffer = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows), buffer)
    return buffer.getvalue()


class _StreamCtx:
    """Minimal stand-in for ``httpx.stream`` yielding fixed bytes."""

    def __init__(self, payload: bytes) -> None:
        """Remember the bytes to stream.

        Args:
            payload: Body to yield.
        """
        self._payload = payload
        self.status_code = 200

    def __enter__(self) -> _StreamCtx:
        """Enter the context, returning the fake response."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """Leave the context."""

    def iter_bytes(self):
        """Yield the body in one chunk."""
        yield self._payload


def test_import_saves_split_to_library(vault_key: str) -> None:
    """A parquet-backed split lands in the library with source ``huggingface``."""
    client, store = _make_client()
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    payload = _parquet_bytes(
        [
            {"question": "What is 2+2?", "answer": "4", "image": {"bytes": png, "path": "a.png"}},
            {"question": "Capital of France?", "answer": "Paris", "image": {"bytes": None, "path": "b.png"}},
        ]
    )
    parquet_listing = {
        "parquet_files": [
            {"config": "default", "split": "train", "url": "https://huggingface.co/x/0.parquet", "size": len(payload)},
            {"config": "default", "split": "test", "url": "https://huggingface.co/x/1.parquet", "size": 5},
        ]
    }
    with (
        patch("core.connectors.huggingface.httpx.get", return_value=_response(200, parquet_listing)),
        patch("core.connectors.huggingface.httpx.stream", return_value=_StreamCtx(payload)) as stream,
    ):
        response = client.post(
            "/connectors/huggingface/import",
            json={"repo_id": "owner/name", "config": "default", "split": "train"},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deduplicated"] is False
    assert body["dataset"]["source"] == "huggingface"
    assert body["dataset"]["name"] == "owner/name · train"
    assert body["dataset"]["row_count"] == 2
    assert body["dataset"]["column_count"] == 3
    assert stream.call_count == 1

    with Session(store.engine) as session:
        assert session.execute(select(UserConnectorModel)).first() is None
    rows, schema = _library_rows(store, body["dataset"]["id"])
    assert rows[0]["question"] == "What is 2+2?"
    assert rows[0]["image"].startswith("data:image/png;base64,")
    assert rows[1]["image"] == "b.png"
    assert schema["column_order"] == ["question", "answer", "image"]
    assert schema["column_kinds"] == {"image": "image"}


def _library_rows(store: _MemStore, dataset_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read a saved dataset's rows and schema straight from the library store.

    Args:
        store: The in-memory store.
        dataset_id: Id of the saved dataset.

    Returns:
        ``(rows, column_schema)``.
    """
    library = DatasetLibraryStore(store.engine, PostgresDatasetBlobStore(store.engine))
    record = library.get_dataset(dataset_id)
    assert record is not None
    rows = library.get_rows(dataset_id)
    assert rows is not None
    return rows, record.column_schema


def test_import_refuses_oversized_split_before_download(vault_key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A split whose parquet files dwarf the file cap is refused with 413 up front."""
    monkeypatch.setattr(settings, "dataset_max_file_bytes", 1000)
    client, _ = _make_client()
    listing = {
        "parquet_files": [
            {"config": "default", "split": "train", "url": "https://huggingface.co/x/0.parquet", "size": 5000}
        ]
    }
    with (
        patch("core.connectors.huggingface.httpx.get", return_value=_response(200, listing)),
        patch("core.connectors.huggingface.httpx.stream") as stream,
    ):
        response = client.post(
            "/connectors/huggingface/import",
            json={"repo_id": "owner/name", "config": "default", "split": "train"},
        )
    stream.assert_not_called()
    assert response.status_code == 413
    assert response.json()["code"] == "connectors.hf_import_too_large"


def test_normalize_cell_handles_bytes_dates_and_nesting() -> None:
    """Cell coercion covers the parquet value types the library can't store raw."""
    stamp = datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert hf.normalize_cell({"a": [b"\x00\x01", stamp]}) == {"a": ["AAE=", stamp.isoformat()]}
    assert hf.normalize_cell(json.loads("null")) is None
