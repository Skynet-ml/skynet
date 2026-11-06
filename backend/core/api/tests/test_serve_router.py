"""Tests for the ``/serve`` router: repository serve, and the 410 for retired DSPy serve calls."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from ...service_gateway.optimization.blackbox import repo_serving
from ...service_gateway.optimization.blackbox.repo_tree import REPO_SNAPSHOT_KEY
from ...storage.models import ExecutionBudgetModel, ExecutionOperationModel, JobModel
from ..auth import AuthenticatedUser, get_authenticated_user
from ..routers import _helpers as helpers_module
from ..routers import serve as serve_module
from ..routers.serve import create_serve_router
from ..sharing_access import ShareRole
from .conftest import TEST_USER, bypass_auth
from .mocks import _BaseFakeJobStore, make_artifact, make_grid_job, make_run_result
from .test_protected_interactions import interactions as _interactions_fixture
from .test_protected_submissions import _Harness
from .test_protected_submissions import harness as _harness_fixture

# Use the shared base fake store; the serve router only needs get_job + seed_job.
_FakeJobStore = _BaseFakeJobStore
harness = _harness_fixture
interactions = _interactions_fixture

_RETIRED = "optimization.dspy_run_retired"


def _wire_http_handler(app: FastAPI) -> None:
    """Attach the production-shape HTTPException handler so DomainError ``code``/``params`` round-trip."""

    @app.exception_handler(HTTPException)
    async def _http_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        """Mirror the production HTTPException handler so ``code``/``params`` round-trip."""
        content: dict[str, object] = {"detail": exc.detail}
        code = getattr(exc, "code", None)
        if code:
            content["code"] = code
            content["params"] = getattr(exc, "params", None) or {}
        return JSONResponse(status_code=exc.status_code, content=content, headers=getattr(exc, "headers", None))


@pytest.fixture
def serve_store() -> _FakeJobStore:
    """Provide a fresh fake job store for the serve router.

    Returns:
        A new ``_FakeJobStore`` instance.
    """
    return _FakeJobStore()


@pytest.fixture
def serve_client(serve_store: _FakeJobStore) -> TestClient:
    """Build a ``TestClient`` exposing only the serve router.

    Args:
        serve_store: Fake job store wired into the router factory.

    Returns:
        A ``TestClient`` over a minimal FastAPI app.
    """
    app = FastAPI()
    app.include_router(create_serve_router(job_store=serve_store))
    _wire_http_handler(app)
    bypass_auth(app)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def serve_events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture every ``serve_inference`` write instead of hitting the database.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        A list that gains one ``{username, name, properties}`` dict per write.
    """
    events: list[dict[str, Any]] = []

    def _record(_engine: Any, *, username: str | None, name: str, properties: dict[str, Any] | None = None) -> None:
        """Store one telemetry write.

        Args:
            _engine: Ignored telemetry engine.
            username: Caller identity.
            name: Event name.
            properties: Event properties.
        """
        events.append({"username": username, "name": name, "properties": dict(properties or {})})

    monkeypatch.setattr(serve_module, "record_server_event", _record)
    return events


def _seed_dspy_run(store: _FakeJobStore, opt_id: str = "dspy-run") -> None:
    """Seed a finished DSPy program run, the kind Skynet no longer serves.

    Args:
        store: Fake job store to seed into.
        opt_id: Job id to seed under.
    """
    store.seed_job(
        opt_id,
        status="success",
        payload_overview={"model_name": "openai/gpt-4o-mini"},
        result=make_run_result(make_artifact()).model_dump(),
    )


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/serve/dspy-run/info", None),
        ("post", "/serve/dspy-run", {"inputs": {"question": "hello"}}),
        ("post", "/serve/dspy-run/request-form", {}),
    ],
)
def test_dspy_serve_routes_return_410(
    serve_client: TestClient, serve_store: _FakeJobStore, method: str, path: str, body: dict[str, Any] | None
) -> None:
    """Every serve route refuses a stored DSPy program run with the retired code."""
    _seed_dspy_run(serve_store)

    response = serve_client.request(method, path, json=body)

    assert response.status_code == 410
    assert response.json()["code"] == _RETIRED


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/serve/grid/pair/0/info", None),
        ("post", "/serve/grid/pair/0/request-form", {}),
    ],
)
def test_grid_pair_serve_routes_return_410(
    serve_client: TestClient, serve_store: _FakeJobStore, method: str, path: str, body: dict[str, Any] | None
) -> None:
    """Grid-search pairs are DSPy programs, so their serve routes refuse with the retired code."""
    serve_store.seed_raw("grid", job=make_grid_job("grid"))

    response = serve_client.request(method, path, json=body)

    assert response.status_code == 410
    assert response.json()["code"] == _RETIRED


@pytest.mark.parametrize("path", ["/serve/missing/info", "/serve/missing/pair/0/info", "/serve/missing/request-form"])
def test_serve_routes_return_404_for_unknown_id(serve_client: TestClient, path: str) -> None:
    """An unknown run is a 404 before the DSPy refusal, so ids do not leak."""
    response = serve_client.get(path) if path.endswith("info") else serve_client.post(path, json={})

    assert response.status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/serve/dspy-run/stream",
        "/serve/dspy-run/chat",
        "/serve/dspy-run/chat/confirm",
        "/serve/dspy-run/pair/0",
        "/serve/dspy-run/pair/0/stream",
    ],
)
def test_removed_dspy_serve_routes_are_gone(serve_client: TestClient, serve_store: _FakeJobStore, path: str) -> None:
    """The DSPy stream, chat and pair serve routes no longer exist."""
    _seed_dspy_run(serve_store)

    response = serve_client.post(path, json={"inputs": {"question": "hello"}})

    assert response.status_code in (404, 405)


def test_dspy_serve_records_one_error_event(
    serve_client: TestClient, serve_store: _FakeJobStore, serve_events: list[dict[str, Any]]
) -> None:
    """A refused DSPy serve call still writes exactly one structural row, with ``status=error``."""
    _seed_dspy_run(serve_store)

    serve_client.post("/serve/dspy-run", json={"inputs": {"question": "hello"}})

    assert serve_events == [
        {
            "username": TEST_USER.username,
            "name": "serve_inference",
            "properties": {"optimization_id": "dspy-run", "channel": "ui", "route": "serve", "status": "error"},
        }
    ]


def test_serve_info_and_request_form_record_nothing(
    serve_client: TestClient, serve_store: _FakeJobStore, serve_events: list[dict[str, Any]]
) -> None:
    """Schema reads and request-form echoes are not inference and write no row."""
    serve_store.seed_job("repo-run", **_repo_job())

    assert serve_client.get("/serve/repo-run/info").status_code == 200
    assert serve_client.post("/serve/repo-run/request-form", json={}).status_code == 200

    assert serve_events == []


_REPO_COMMIT = "c" * 40
_BEST_PATCH = "diff --git a/serve.py b/serve.py\n"


def _repo_job(
    status: str = "success", commit: str | None = _REPO_COMMIT, *, serving_enabled: bool = True
) -> dict[str, Any]:
    """Build a finished repository run whose best version beat its starting code.

    Args:
        status: Stored job status.
        commit: Pinned commit, or ``None`` for a run that never pinned one.
        serving_enabled: Whether the owner turned serving on for the run.

    Returns:
        Job fields for a store row.
    """
    return {
        "status": status,
        "payload_overview": {"username": "alice", "optimization_type": "run", "serving_enabled": serving_enabled},
        "payload": {
            "username": "alice",
            "strategy": {"engine": "gepa"},
            "target": {"kind": "repo", "repo": {"repository": "acme/app", "commit": commit, "ref": "main"}},
            "scorer": {"model": {"name": "fixture/model"}},
            "token_source": "managed",
        },
        "result": {"best_candidate": _BEST_PATCH, "baseline_score": 0.2, "best_score": 0.9},
    }


def _seed_repo_job(store: Any, optimization_id: str = "repo-run") -> None:
    """Insert a finished repository run into a real job store.

    Args:
        store: SQLite-backed store from the ledger harness.
        optimization_id: Run identifier.
    """
    job = _repo_job()
    with Session(store.engine) as session:
        session.add(
            JobModel(
                optimization_id=optimization_id,
                status=job["status"],
                created_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
                latest_metrics={},
                payload_overview=job["payload_overview"],
                payload=job["payload"],
                result=job["result"],
            )
        )
        session.commit()


@pytest.fixture
def repo_serve(
    interactions: tuple[_Harness, dict[str, Any]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[TestClient, _Harness, dict[str, Any]]:
    """Serve a repository run through the real ledger with the sandbox and GitHub replaced.

    Args:
        interactions: Real-ledger harness with the sandbox transport captured.
        monkeypatch: Pytest mutation helper.
        tmp_path: Scratch folder for the fake snapshot.

    Returns:
        ``(client, harness, captured state)``; the state also records GitHub fetches.
    """
    harness, state = interactions
    state["fetches"] = []

    def fake_fetch(repository: str, commit: str, token: str | None, workdir: Path) -> Any:
        """Record one fetch and write a placeholder archive."""
        state["fetches"].append({"repository": repository, "commit": commit, "token": token})
        archive = workdir / "tree.tgz"
        archive.write_bytes(b"tgz")
        return SimpleNamespace(archive=archive, commit=commit)

    monkeypatch.setattr(repo_serving, "github_token", lambda engine, owner: f"token-of-{owner}")
    monkeypatch.setattr(repo_serving, "fetch_snapshot", fake_fetch)
    _seed_repo_job(harness.store)
    app = FastAPI()
    app.include_router(create_serve_router(job_store=harness.store))
    _wire_http_handler(app)
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser("alice", "user", ())
    return TestClient(app, raise_server_exceptions=False), harness, state


def test_repo_serve_runs_the_best_version_once_per_key(
    repo_serve: tuple[TestClient, _Harness, dict[str, Any]],
) -> None:
    client, harness, state = repo_serve
    body = {"inputs": {"question": "hello"}, "max_cost_cents": 10}

    first = client.post("/serve/repo-run", json=body, headers={"Idempotency-Key": "repo-call"})
    replay = client.post("/serve/repo-run", json=body, headers={"Idempotency-Key": "repo-call"})

    assert first.status_code == 200, first.text
    assert first.json()["outputs"] == {"answer": "sandboxed"}
    assert first.json()["cents_charged"] == "1"
    assert first.json()["budget"]["total_cents"] == 10
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert len(state["calls"]) == 1
    assert state["fetches"] == [{"repository": "acme/app", "commit": _REPO_COMMIT, "token": "token-of-alice"}]
    sent = state["calls"][0]["payload"]
    assert sent["patch"] == _BEST_PATCH
    assert sent["inputs"] == {"question": "hello"}
    assert sent["_interaction"]["kind"] == repo_serving.INTERACTION_KIND
    assert sent[REPO_SNAPSHOT_KEY]["commit"] == _REPO_COMMIT
    assert harness.count(ExecutionBudgetModel) == 1
    assert harness.count(ExecutionOperationModel) == 1


def test_repo_serve_requires_an_idempotency_key(
    repo_serve: tuple[TestClient, _Harness, dict[str, Any]],
) -> None:
    client, _harness, state = repo_serve

    response = client.post("/serve/repo-run", json={"inputs": {"question": "hello"}, "max_cost_cents": 10})

    assert response.status_code == 400
    assert response.json()["code"] == "budget.idempotency_required"
    assert state["calls"] == []
    assert state["fetches"] == []


def test_repo_serve_cannot_replay_a_key_with_other_inputs(
    repo_serve: tuple[TestClient, _Harness, dict[str, Any]],
) -> None:
    client, _harness, state = repo_serve
    headers = {"Idempotency-Key": "repo-call"}

    client.post("/serve/repo-run", json={"inputs": {"question": "a"}, "max_cost_cents": 10}, headers=headers)
    changed = client.post("/serve/repo-run", json={"inputs": {"question": "b"}, "max_cost_cents": 10}, headers=headers)

    assert changed.status_code >= 400
    assert len(state["calls"]) == 1


def test_repo_serve_reports_a_sandbox_failure(
    repo_serve: tuple[TestClient, _Harness, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _harness, _state = repo_serve

    def broken_fetch(*_args: Any) -> Any:
        """Fail the way an unreachable repository does."""
        raise repo_serving.RepoFetchError("gone")

    monkeypatch.setattr(repo_serving, "fetch_snapshot", broken_fetch)

    response = client.post(
        "/serve/repo-run",
        json={"inputs": {"question": "hello"}, "max_cost_cents": 10},
        headers={"Idempotency-Key": "repo-fail"},
    )

    assert response.status_code == 502
    assert response.json()["code"] == "serve.protected_interaction_failed"


@pytest.mark.parametrize(
    ("status", "commit", "code"),
    [
        ("running", _REPO_COMMIT, "optimization.not_success_status_for_serve"),
        ("success", None, "optimization.no_result_pending"),
    ],
)
def test_repo_serve_refuses_a_run_it_cannot_serve(
    serve_client: TestClient, serve_store: _FakeJobStore, status: str, commit: str | None, code: str
) -> None:
    serve_store.seed_job("repo-run", **_repo_job(status=status, commit=commit))

    response = serve_client.post(
        "/serve/repo-run", json={"inputs": {"question": "hi"}, "max_cost_cents": 10}, headers={"Idempotency-Key": "k"}
    )

    assert response.status_code == 409
    assert response.json()["code"] == code


def test_repo_serve_info_reports_serve_py(serve_client: TestClient, serve_store: _FakeJobStore) -> None:
    serve_store.seed_job("repo-run", **_repo_job())

    response = serve_client.get("/serve/repo-run/info")

    assert response.status_code == 200
    assert response.json()["module_name"] == "serve.py"
    assert response.json()["optimizer_name"] == "gepa"
    assert response.json()["model_name"] == "fixture/model"


def _client_as(store: _FakeJobStore, user: AuthenticatedUser) -> TestClient:
    """Build a serve-router client that authenticates every call as ``user``.

    Args:
        store: Fake job store wired into the router factory.
        user: Caller identity for every request.

    Returns:
        A ``TestClient`` over a minimal FastAPI app.
    """
    app = FastAPI()
    app.include_router(create_serve_router(job_store=store))
    _wire_http_handler(app)
    app.dependency_overrides[get_authenticated_user] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


def test_owner_turns_serving_on_and_off(serve_store: _FakeJobStore) -> None:
    """The owner turns serving on and off, and the info route reports each state."""
    serve_store.seed_job("repo-run", **_repo_job(serving_enabled=False))
    client = _client_as(serve_store, AuthenticatedUser("alice", "user", ()))

    assert client.get("/serve/repo-run/info").json()["serving_enabled"] is False

    on = client.put("/serve/repo-run/enabled", json={"enabled": True})
    assert on.status_code == 200, on.text
    assert on.json() == {"optimization_id": "repo-run", "serving_enabled": True}
    assert client.get("/serve/repo-run/info").json()["serving_enabled"] is True
    assert serve_store.get_job("repo-run")["payload_overview"]["username"] == "alice"

    off = client.put("/serve/repo-run/enabled", json={"enabled": False})
    assert off.json() == {"optimization_id": "repo-run", "serving_enabled": False}
    assert client.get("/serve/repo-run/info").json()["serving_enabled"] is False


def test_setting_the_same_serving_state_twice_is_idempotent(serve_store: _FakeJobStore) -> None:
    """Setting the same switch state twice returns the same reply."""
    serve_store.seed_job("repo-run", **_repo_job(serving_enabled=False))
    client = _client_as(serve_store, AuthenticatedUser("alice", "user", ()))

    first = client.put("/serve/repo-run/enabled", json={"enabled": True})
    second = client.put("/serve/repo-run/enabled", json={"enabled": True})

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"optimization_id": "repo-run", "serving_enabled": True}


def test_serve_info_defaults_serving_to_off(serve_client: TestClient, serve_store: _FakeJobStore) -> None:
    """A run that never stored the switch reports serving as off."""
    job = _repo_job()
    job["payload_overview"] = {"username": "alice", "optimization_type": "run"}
    serve_store.seed_job("repo-run", **job)

    assert serve_client.get("/serve/repo-run/info").json()["serving_enabled"] is False


def test_a_shared_editor_cannot_turn_serving_on(serve_store: _FakeJobStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the owner may change the switch; a shared editor gets 403."""
    serve_store.seed_job("repo-run", **_repo_job(serving_enabled=False))
    monkeypatch.setattr(helpers_module, "_grant_role", lambda _store, _id, username: ShareRole.editor)
    client = _client_as(serve_store, AuthenticatedUser("bob", "user", ()))

    response = client.put("/serve/repo-run/enabled", json={"enabled": True})

    assert response.status_code == 403
    assert response.json()["code"] == "optimization.insufficient_role"
    assert serve_store.get_job("repo-run")["payload_overview"]["serving_enabled"] is False


def test_a_stranger_cannot_see_the_serving_switch(serve_store: _FakeJobStore) -> None:
    """A caller with no access gets 404, so the run stays hidden."""
    serve_store.seed_job("repo-run", **_repo_job(serving_enabled=False))
    client = _client_as(serve_store, AuthenticatedUser("mallory", "user", ()))

    response = client.put("/serve/repo-run/enabled", json={"enabled": True})

    assert response.status_code == 404


def test_serving_switch_refuses_a_dspy_run(serve_client: TestClient, serve_store: _FakeJobStore) -> None:
    """A DSPy program run cannot be served, so its switch refuses with 410."""
    _seed_dspy_run(serve_store)

    response = serve_client.put("/serve/dspy-run/enabled", json={"enabled": True})

    assert response.status_code == 410
    assert response.json()["code"] == _RETIRED


def test_serving_switch_refuses_an_unfinished_run(serve_client: TestClient, serve_store: _FakeJobStore) -> None:
    """A run that did not succeed keeps its switch off and gets 409."""
    serve_store.seed_job("repo-run", **_repo_job(status="running", serving_enabled=False))

    response = serve_client.put("/serve/repo-run/enabled", json={"enabled": True})

    assert response.status_code == 409
    assert response.json()["code"] == "optimization.not_success_status_for_serve"
    assert serve_store.get_job("repo-run")["payload_overview"]["serving_enabled"] is False


def test_request_form_refuses_while_serving_is_off(serve_client: TestClient, serve_store: _FakeJobStore) -> None:
    """The agent's inference card refuses while serving is off."""
    serve_store.seed_job("repo-run", **_repo_job(serving_enabled=False))

    response = serve_client.post("/serve/repo-run/request-form", json={})

    assert response.status_code == 409
    assert response.json()["code"] == "serve.not_enabled"


def test_repo_serve_waits_for_the_owner_to_turn_serving_on(
    repo_serve: tuple[TestClient, _Harness, dict[str, Any]],
) -> None:
    """A serve call does no billing or sandbox work until serving is on."""
    client, harness, state = repo_serve
    body = {"inputs": {"question": "hello"}, "max_cost_cents": 10}
    assert client.put("/serve/repo-run/enabled", json={"enabled": False}).status_code == 200

    refused = client.post("/serve/repo-run", json=body, headers={"Idempotency-Key": "off-call"})

    assert refused.status_code == 409
    assert refused.json()["code"] == "serve.not_enabled"
    assert state["calls"] == []
    assert state["fetches"] == []
    assert harness.count(ExecutionBudgetModel) == 0

    assert client.put("/serve/repo-run/enabled", json={"enabled": True}).status_code == 200
    served = client.post("/serve/repo-run", json=body, headers={"Idempotency-Key": "on-call"})

    assert served.status_code == 200, served.text
    assert len(state["calls"]) == 1
