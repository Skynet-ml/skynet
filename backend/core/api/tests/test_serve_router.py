"""Tests for the ``/serve`` router (single program and grid pair flavours)."""

from __future__ import annotations

import base64
import json
import os
import pickle
from collections.abc import Generator
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ...billing.byok_vault import ProviderKeyVault
from ...config import settings
from ...i18n_keys import I18nKey
from ...models import ProgramArtifact
from ...service_gateway.agents.generalist import get_approval_registry
from ...storage.models import (
    Base,
    BillingCustomerModel,
    WalletLedgerModel,
)
from ..auth import AuthenticatedUser, get_authenticated_user

# noinspection PyProtectedMember
from ..routers import _helpers
from ..routers import serve as serve_module
from ..routers.serve import _coerce_sample_value, _collect_sample, create_serve_router
from .conftest import TEST_USER, bypass_auth
from .mocks import (
    _BaseFakeJobStore,
    make_artifact,
    make_grid_job,
    make_run_result,
)

# Use the shared base fake store; the serve router only needs get_job + seed_job.
_FakeJobStore = _BaseFakeJobStore


def _legacy_test_runtime(job_data: dict[str, Any]) -> str | None:
    """Preserve explicit coverage of retired host-only helpers in unit tests.

    Args:
        job_data: Synthetic stored job.

    Returns:
        Vercel for protected fixtures and None for legacy-only fixtures.
    """
    payload = job_data.get("payload")
    payload_budget_id = payload.get("execution_budget_id") if isinstance(payload, dict) else None
    if job_data.get("execution_budget_id") is None and payload_budget_id is None:
        return None
    return "vercel"


def _write_unpickle_marker(path: str) -> None:
    """Write a marker when an adversarial pickle is deserialized.

    Args:
        path: File path whose creation records unsafe deserialization.
    """
    Path(path).write_text("unsafe")


class _MarkerOnUnpickle:
    """Adversarial payload that records any attempted pickle load."""

    def __init__(self, path: Path) -> None:
        """Store the marker destination.

        Args:
            path: File to create if the payload is deserialized.
        """
        self.path = path

    def __reduce__(self) -> tuple[Any, tuple[str]]:
        """Return a callable that creates the marker during unpickling.

        Returns:
            The marker-writing callable and its serialized argument.
        """
        return _write_unpickle_marker, (str(self.path),)


def _adversarial_pickle_artifact(marker: Path) -> ProgramArtifact:
    """Build a serveable-looking artifact with an active pickle payload.

    Args:
        marker: File that unpickling the payload would create.

    Returns:
        An artifact with safe prompt metadata and an adversarial legacy pickle.
    """
    encoded = base64.b64encode(pickle.dumps(_MarkerOnUnpickle(marker))).decode("ascii")
    prompt = make_artifact().optimized_prompt
    return ProgramArtifact(program_pickle_base64=encoded, optimized_prompt=prompt)


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


# noinspection PyProtectedMember
@pytest.fixture(autouse=True)
def _clear_program_cache(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Reset the in-process program cache around every test in this file.

    Yields:
        ``None`` once the cache is cleared; the cache is cleared again on teardown.
    """
    monkeypatch.setattr(_helpers, "_protected_api_runtime", _legacy_test_runtime)
    monkeypatch.setattr(serve_module, "_protected_api_runtime", _legacy_test_runtime)
    registry = get_approval_registry()
    registry.bind_engine(None)
    _helpers.clear_program_cache()
    yield
    registry.bind_engine(None)
    _helpers.clear_program_cache()


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


def test_serve_info_falls_back_to_column_mapping_fields(serve_client: TestClient, serve_store: _FakeJobStore) -> None:
    """A Flex artifact carries no prompt; the column mapping still names its fields."""
    _seed_run_job(
        serve_store,
        "flex-run",
        artifact=ProgramArtifact(program_state_json={}, optimized_prompt=None),
        overview_extra={
            "module_name": "flex",
            "column_mapping": {"inputs": {"review": "text"}, "outputs": {"score": "stars"}},
        },
    )

    resp = serve_client.get("/serve/flex-run/info")

    assert resp.status_code == 200
    body = resp.json()
    assert body["input_fields"] == ["review"]
    assert body["output_fields"] == ["score"]


def test_serve_info_returns_404_for_unknown_id(serve_client: TestClient) -> None:
    """A serve-info request against an unknown id returns 404."""
    resp = serve_client.get("/serve/ghost/info")

    assert resp.status_code == 404


def test_serve_info_returns_409_for_pending_job(serve_client: TestClient, serve_store: _FakeJobStore) -> None:
    """Pending jobs cannot be served and return 409 from the info endpoint."""
    serve_store.seed_job("p", status="pending")

    resp = serve_client.get("/serve/p/info")

    assert resp.status_code == 409


def test_serve_info_returns_409_for_running_job(serve_client: TestClient, serve_store: _FakeJobStore) -> None:
    """Running jobs cannot be served and return 409 from the info endpoint."""
    serve_store.seed_job("r", status="running")

    resp = serve_client.get("/serve/r/info")

    assert resp.status_code == 409


def test_serve_info_returns_409_for_failed_job(serve_client: TestClient, serve_store: _FakeJobStore) -> None:
    """Failed jobs cannot be served and return 409 from the info endpoint."""
    serve_store.seed_job("f", status="failed")

    resp = serve_client.get("/serve/f/info")

    assert resp.status_code == 409


def test_serve_info_returns_409_for_cancelled_job(serve_client: TestClient, serve_store: _FakeJobStore) -> None:
    """Cancelled jobs cannot be served and return 409 from the info endpoint."""
    serve_store.seed_job("c", status="cancelled")

    resp = serve_client.get("/serve/c/info")

    assert resp.status_code == 409


@pytest.mark.parametrize("non_success_status", ["pending", "validating", "running", "failed", "cancelled"])
def test_serve_info_returns_409_for_all_non_success_statuses(
    serve_client: TestClient,
    serve_store: _FakeJobStore,
    non_success_status: str,
) -> None:
    """Every non-success status blocks the serve info endpoint with 409."""
    serve_store.seed_job(f"j_{non_success_status}", status=non_success_status)

    resp = serve_client.get(f"/serve/j_{non_success_status}/info")

    assert resp.status_code == 409


def test_serve_program_returns_404_for_unknown_id(serve_client: TestClient) -> None:
    """A serve POST against an unknown id returns 404."""
    resp = serve_client.post("/serve/ghost", json={"inputs": {"q": "hello"}})

    assert resp.status_code == 404


def test_serve_program_returns_409_for_pending_job(serve_client: TestClient, serve_store: _FakeJobStore) -> None:
    """Pending jobs cannot be served and return 409 from the serve endpoint."""
    serve_store.seed_job("p", status="pending")

    resp = serve_client.post("/serve/p", json={"inputs": {"q": "hello"}})

    assert resp.status_code == 409


def test_serve_program_returns_409_for_failed_job(serve_client: TestClient, serve_store: _FakeJobStore) -> None:
    """Failed jobs cannot be served and return 409 from the serve endpoint."""
    serve_store.seed_job("f", status="failed")

    resp = serve_client.post("/serve/f", json={"inputs": {"q": "hello"}})

    assert resp.status_code == 409


def test_protected_serve_metadata_never_executes_signature_side_effects_in_api_parent(
    serve_client: TestClient,
    serve_store: _FakeJobStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read protected form metadata without executing its signature in the API.

    Args:
        serve_client: Serve route client.
        serve_store: Fake completed-job store.
        tmp_path: Parent-owned directory that authored code must not write.
        monkeypatch: Pytest environment cleanup helper.
    """
    marker = tmp_path / "signature-ran-in-api.txt"
    env_name = "APP_PROTECTED_SIGNATURE_RAN_IN_API"
    monkeypatch.delenv(env_name, raising=False)
    signature_code = "\n".join(
        [
            "import os",
            "import pathlib",
            "import urllib.request",
            "import dspy",
            f"os.environ[{env_name!r}] = 'yes'",
            f"pathlib.Path({str(marker)!r}).write_text('unsafe')",
            "urllib.request.urlopen('https://parent-network.invalid')",
            "class UnsafeSignature(dspy.Signature):",
            "    question: str = dspy.InputField()",
            "    answer: str = dspy.OutputField()",
        ]
    )
    default_artifact = make_artifact()
    artifact = ProgramArtifact(
        program_state_json={},
        optimized_prompt=default_artifact.optimized_prompt,
    )
    _seed_run_job(
        serve_store,
        "protected-serve",
        artifact=artifact,
        overview_extra={
            "signature_code": signature_code,
            "module_name": "predict",
            "module_kwargs": {},
        },
    )
    serve_store.update_job(
        "protected-serve",
        execution_budget_id="budget-1",
        payload={"execution_runtime": "vercel"},
    )
    _helpers.clear_program_cache()

    with patch("urllib.request.urlopen") as urlopen:
        info = serve_client.get("/serve/protected-serve/info")
        form = serve_client.post(
            "/serve/protected-serve/request-form",
            json={"prompt": "Try the optimized prompt"},
        )
        execution = serve_client.post(
            "/serve/protected-serve",
            json={"inputs": {"question": "hello"}},
        )

    assert info.status_code == 200
    assert info.json()["input_fields"] == ["question"]
    assert info.json()["output_fields"] == ["reasoning", "answer"]
    assert form.status_code == 200
    assert form.json() == {
        "optimization_id": "protected-serve",
        "awaiting_inputs": True,
        "prompt": "Try the optimized prompt",
    }
    assert execution.status_code == 400
    assert execution.json()["code"] == "budget.idempotency_required"
    assert env_name not in os.environ
    assert not marker.exists()
    urlopen.assert_not_called()


class _FakePrediction:
    """Stub prediction object exposing both attribute access and ``toDict``."""

    answer: str = "42"

    @staticmethod
    def toDict() -> dict:  # noqa: N802 — mirrors dspy.Prediction's camelCase API
        """Return a minimal dict representation matching ``dspy.Prediction.toDict``."""
        return {"answer": "42"}


def _seed_run_job(
    store: _FakeJobStore,
    opt_id: str,
    artifact: ProgramArtifact | None = None,
    overview_extra: dict | None = None,
    status: str = "success",
) -> dict:
    """Seed a run-style job with a fake program in the cache.

    Args:
        store: Fake job store to seed into.
        opt_id: Job id to seed under.
        artifact: Program artifact override; a default is built when omitted.
        overview_extra: Extra fields merged into the payload overview.
        status: Initial job status (defaults to ``success``).

    Returns:
        The seeded job dict as returned by the underlying store.
    """
    if artifact is None:
        artifact = make_artifact()
    result = make_run_result(artifact)
    overview = {"model_name": "openai/gpt-4o-mini", **(overview_extra or {})}
    job = store.seed_job(
        opt_id,
        status=status,
        payload_overview=overview,
        result=result.model_dump(),
    )
    # noinspection PyProtectedMember
    _helpers._program_cache[opt_id] = MagicMock(return_value=_FakePrediction())
    return job


_PATCH_LM = patch(
    "core.service_gateway.language_models.build_language_model",
    return_value=MagicMock(),
)
_PATCH_DSPY_CTX = patch("dspy.context", return_value=nullcontext())


def test_serve_program_happy_path(serve_client: TestClient, serve_store: _FakeJobStore) -> None:
    """A success job serves its program output and reports the resolved model."""
    _seed_run_job(serve_store, "ok")

    with _PATCH_LM, _PATCH_DSPY_CTX:
        resp = serve_client.post("/serve/ok", json={"inputs": {"question": "hello"}})

    assert resp.status_code == 200
    body = resp.json()
    assert body["optimization_id"] == "ok"
    assert "answer" in body["outputs"]
    assert body["output_fields"] == ["reasoning", "answer"]
    assert "model_used" in body


def test_serve_program_rejects_model_override(serve_client: TestClient, serve_store: _FakeJobStore) -> None:
    """A caller-chosen model is refused; the program stays on its optimized model."""
    _seed_run_job(serve_store, "ov", overview_extra={"model_settings": {"name": "openai/gpt-4o"}})

    with _PATCH_LM, _PATCH_DSPY_CTX:
        resp = serve_client.post(
            "/serve/ov",
            json={
                "inputs": {"question": "hi"},
                "model_config_override": {"name": "openai/gpt-3.5-turbo"},
            },
        )

    assert resp.status_code == 422
    assert "model_config_override" in resp.text


def test_serve_program_runs_on_exact_stored_settings(serve_client: TestClient, serve_store: _FakeJobStore) -> None:
    """Every stored model setting, not just the name, reaches the language model."""
    settings = {"name": "openai/gpt-4o", "temperature": 0.3, "max_tokens": 512, "extra": {"top_p": 0.9}}
    _seed_run_job(serve_store, "ex", overview_extra={"model_settings": settings})
    build = MagicMock(return_value=MagicMock())

    with patch("core.api.routers.serve.build_language_model", build), _PATCH_DSPY_CTX:
        resp = serve_client.post("/serve/ex", json={"inputs": {"question": "hi"}})

    assert resp.status_code == 200
    used = build.call_args.args[0]
    assert (used.name, used.temperature, used.max_tokens, used.extra) == ("openai/gpt-4o", 0.3, 512, {"top_p": 0.9})


def test_serve_program_model_resolution_from_stored_settings(
    serve_client: TestClient, serve_store: _FakeJobStore
) -> None:
    """Stored ``model_settings`` are used when no override is supplied."""
    _seed_run_job(
        serve_store,
        "ms",
        overview_extra={"model_settings": {"name": "openai/gpt-4-turbo"}},
    )

    with _PATCH_LM, _PATCH_DSPY_CTX:
        resp = serve_client.post("/serve/ms", json={"inputs": {"question": "hi"}})

    assert resp.status_code == 200
    assert resp.json()["model_used"] == "openai/gpt-4-turbo"


def test_serve_program_model_resolution_from_model_name_only(
    serve_client: TestClient, serve_store: _FakeJobStore
) -> None:
    """A bare ``model_name`` (legacy overview) still resolves to a ``ModelConfig``."""
    # Legacy overview shape: only model_name (no model_settings) — a minimal ModelConfig must still resolve.
    _seed_run_job(serve_store, "mn")

    with _PATCH_LM, _PATCH_DSPY_CTX:
        resp = serve_client.post("/serve/mn", json={"inputs": {"question": "hi"}})

    assert resp.status_code == 200
    assert resp.json()["model_used"] == "openai/gpt-4o-mini"


def test_serve_program_returns_400_when_no_model_config(serve_client: TestClient, serve_store: _FakeJobStore) -> None:
    """Lack of any usable model config produces a localized 400."""
    _seed_run_job(serve_store, "nm", overview_extra={"model_name": "", "model_settings": {}})
    # Clear override so the fallback chain reaches the 400 branch
    # (model_name key present but empty, model_settings empty dict)
    serve_store.update_job("nm", payload_overview={})

    with _PATCH_LM, _PATCH_DSPY_CTX:
        resp = serve_client.post("/serve/nm", json={"inputs": {"question": "hi"}})

    assert resp.status_code == 400
    assert resp.json()["code"] == I18nKey.SERVE_NO_MODEL_CONFIG.value


def test_serve_program_returns_400_for_no_input_fields(serve_client: TestClient, serve_store: _FakeJobStore) -> None:
    """An artifact with no declared input fields produces a 400 error."""
    artifact = make_artifact(input_fields=[], output_fields=["answer"])
    _seed_run_job(serve_store, "nif", artifact=artifact)
    # noinspection PyProtectedMember
    # Ensure the cache reflects the artifact with empty input_fields (not a prior entry)
    _helpers._program_cache["nif"] = MagicMock(return_value=_FakePrediction())

    with _PATCH_LM, _PATCH_DSPY_CTX:
        resp = serve_client.post("/serve/nif", json={"inputs": {"question": "hi"}})

    assert resp.status_code == 400
    assert resp.json()["code"] == I18nKey.SERVE_NO_DECLARED_INPUTS.value


def test_serve_program_returns_400_for_missing_input_fields(
    serve_client: TestClient, serve_store: _FakeJobStore
) -> None:
    """Missing required input fields cause a 400 with a localized error."""
    _seed_run_job(serve_store, "mif")

    with _PATCH_LM, _PATCH_DSPY_CTX:
        resp = serve_client.post("/serve/mif", json={"inputs": {"wrong_key": "hi"}})

    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == I18nKey.SERVE_MISSING_INPUTS.value
    assert "missing" in body["params"]


def test_serve_program_output_todict_fallback(serve_client: TestClient, serve_store: _FakeJobStore) -> None:
    """When no output fields are declared, ``prediction.toDict()`` is used as the output."""
    # When the artifact declares no output_fields, the handler falls back to prediction.toDict().
    artifact = make_artifact(input_fields=["question"], output_fields=[])
    _seed_run_job(serve_store, "tdf", artifact=artifact)
    # Return a prediction whose toDict has an extra key
    pred = MagicMock()
    pred.toDict.return_value = {"answer": "via todict", "score": 0.9}
    # noinspection PyProtectedMember
    _helpers._program_cache["tdf"] = MagicMock(return_value=pred)

    with _PATCH_LM, _PATCH_DSPY_CTX:
        resp = serve_client.post("/serve/tdf", json={"inputs": {"question": "hi"}})

    assert resp.status_code == 200
    body = resp.json()
    assert body["outputs"]["answer"] == "via todict"
    assert body["output_fields"] == []


@pytest.fixture
def grid_store() -> _FakeJobStore:
    """Provide a fake job store seeded with a grid job and one cached pair program.

    Returns:
        A ``_FakeJobStore`` containing ``grid1`` with ``grid1_pair_0`` pre-cached.
    """
    store = _FakeJobStore()
    job = make_grid_job("grid1", pair_index=0)
    store.seed_raw("grid1", job=job)
    # noinspection PyProtectedMember
    _helpers._program_cache["grid1_pair_0"] = MagicMock(return_value=_FakePrediction())
    return store


@pytest.fixture
def grid_client(grid_store: _FakeJobStore) -> TestClient:
    """Build a ``TestClient`` exposing the serve router over the grid store.

    Args:
        grid_store: Fake job store seeded with a grid job.

    Returns:
        A ``TestClient`` over a minimal FastAPI app.
    """
    app = FastAPI()
    app.include_router(create_serve_router(job_store=grid_store))
    _wire_http_handler(app)
    bypass_auth(app)
    return TestClient(app, raise_server_exceptions=False)


def test_serve_pair_happy_path(grid_client: TestClient, grid_store: _FakeJobStore) -> None:
    """A grid pair serves its program output and reports the resolved model."""
    with _PATCH_LM, _PATCH_DSPY_CTX:
        resp = grid_client.post("/serve/grid1/pair/0", json={"inputs": {"question": "hi"}})

    assert resp.status_code == 200
    body = resp.json()
    assert body["optimization_id"] == "grid1"
    assert "answer" in body["outputs"]
    assert "model_used" in body


def test_serve_pair_returns_404_for_unknown_job(grid_client: TestClient) -> None:
    """A pair POST against an unknown job id returns 404."""
    resp = grid_client.post("/serve/ghost/pair/0", json={"inputs": {"question": "hi"}})

    assert resp.status_code == 404


def test_serve_pair_returns_404_for_out_of_range_index(grid_client: TestClient) -> None:
    """An out-of-range pair index returns 404."""
    with _PATCH_LM, _PATCH_DSPY_CTX:
        resp = grid_client.post("/serve/grid1/pair/99", json={"inputs": {"question": "hi"}})

    assert resp.status_code == 404


def test_serve_pair_returns_409_for_non_success_job(grid_client: TestClient, grid_store: _FakeJobStore) -> None:
    """A non-success grid job rejects pair POSTs with 409."""
    grid_store.update_job("grid1", status="failed")

    with _PATCH_LM, _PATCH_DSPY_CTX:
        resp = grid_client.post("/serve/grid1/pair/0", json={"inputs": {"question": "hi"}})

    assert resp.status_code == 409


def test_serve_pair_returns_400_for_missing_inputs(
    grid_client: TestClient,
) -> None:
    """Missing required inputs on a pair return 400 with a localized error."""
    with _PATCH_LM, _PATCH_DSPY_CTX:
        resp = grid_client.post("/serve/grid1/pair/0", json={"inputs": {"wrong": "hi"}})

    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == I18nKey.SERVE_MISSING_INPUTS.value
    assert "missing" in body["params"]


def test_serve_pair_rejects_model_override(
    grid_client: TestClient,
) -> None:
    """A pair serve request cannot swap the pair's generation model."""
    with _PATCH_LM, _PATCH_DSPY_CTX:
        resp = grid_client.post(
            "/serve/grid1/pair/0",
            json={"inputs": {"question": "hi"}, "model_config_override": {"name": "openai/gpt-3.5-turbo"}},
        )

    assert resp.status_code == 422


def test_serve_pair_info_happy_path(grid_client: TestClient) -> None:
    """The pair info endpoint returns the IO field shape and resolved model name."""
    resp = grid_client.get("/serve/grid1/pair/0/info")

    assert resp.status_code == 200
    body = resp.json()
    assert body["optimization_id"] == "grid1"
    assert body["input_fields"] == ["question"]
    assert body["output_fields"] == ["reasoning", "answer"]
    assert body["model_name"] == "openai/gpt-4o-mini"


def test_serve_pair_info_returns_404_for_unknown_id(grid_client: TestClient) -> None:
    """The pair info endpoint returns 404 for an unknown job id."""
    resp = grid_client.get("/serve/ghost/pair/0/info")

    assert resp.status_code == 404


def test_serve_pair_info_returns_404_for_out_of_range_pair(grid_client: TestClient) -> None:
    """The pair info endpoint returns 404 for an out-of-range pair index."""
    resp = grid_client.get("/serve/grid1/pair/99/info")

    assert resp.status_code == 404


def test_serve_pair_info_returns_409_for_non_success_job(grid_client: TestClient, grid_store: _FakeJobStore) -> None:
    """The pair info endpoint returns 409 when the underlying job is not in success state."""
    grid_store.update_job("grid1", status="running")

    resp = grid_client.get("/serve/grid1/pair/0/info")

    assert resp.status_code == 409


def test_protected_pair_metadata_never_deserializes_persisted_program(
    grid_client: TestClient,
    grid_store: _FakeJobStore,
    tmp_path: Path,
) -> None:
    """Read protected pair metadata while keeping its pickle inert.

    Args:
        grid_client: Grid-pair serve route client.
        grid_store: Fake store backing the client.
        tmp_path: Parent-owned directory the pickle must not modify.
    """
    marker = tmp_path / "pair-pickle-ran-in-api.txt"
    artifact = _adversarial_pickle_artifact(marker)
    job = make_grid_job("protected-grid", artifact=artifact)
    job["execution_budget_id"] = "budget-1"
    job["payload"] = {"execution_runtime": "worker"}
    grid_store.seed_raw("protected-grid", job=job)
    _helpers.clear_program_cache()

    info = grid_client.get("/serve/protected-grid/pair/0/info")
    form = grid_client.post(
        "/serve/protected-grid/pair/0/request-form",
        json={"prompt": "Compare this pair"},
    )
    execution = grid_client.post(
        "/serve/protected-grid/pair/0",
        json={"inputs": {"question": "hello"}},
    )

    assert info.status_code == 200
    assert info.json()["model_name"] == "openai/gpt-4o-mini"
    assert form.status_code == 200
    assert form.json() == {
        "optimization_id": "protected-grid",
        "pair_index": 0,
        "awaiting_inputs": True,
        "prompt": "Compare this pair",
    }
    assert execution.status_code == 400
    assert execution.json()["code"] == "budget.idempotency_required"
    assert not marker.exists()


@pytest.fixture
def stream_store(serve_store: _FakeJobStore) -> _FakeJobStore:
    """Provide a fake job store seeded with a single ``stream_job`` run.

    Args:
        serve_store: Base fake store reused for the stream tests.

    Returns:
        The same store with a ``stream_job`` entry seeded.
    """
    _seed_run_job(serve_store, "stream_job")
    return serve_store


@pytest.fixture
def stream_client(stream_store: _FakeJobStore) -> TestClient:
    """Build a ``TestClient`` exposing the serve router over ``stream_store``.

    Args:
        stream_store: Fake job store seeded with a stream-eligible job.

    Returns:
        A ``TestClient`` over a minimal FastAPI app.
    """
    app = FastAPI()
    app.include_router(create_serve_router(job_store=stream_store))
    _wire_http_handler(app)
    bypass_auth(app)
    return TestClient(app, raise_server_exceptions=False)


def test_serve_stream_returns_404_for_unknown_id(stream_client: TestClient) -> None:
    """A stream POST against an unknown id returns 404."""
    resp = stream_client.post("/serve/ghost/stream", json={"inputs": {"question": "hi"}})

    assert resp.status_code == 404


def test_serve_stream_returns_409_for_non_success_job(stream_client: TestClient, stream_store: _FakeJobStore) -> None:
    """A non-success job rejects stream POSTs with 409."""
    stream_store.seed_job("pending_s", status="pending")

    resp = stream_client.post("/serve/pending_s/stream", json={"inputs": {"question": "hi"}})

    assert resp.status_code == 409


def test_serve_stream_returns_400_for_missing_inputs(stream_client: TestClient) -> None:
    """Missing required inputs on a stream POST return 400."""
    with _PATCH_LM, _PATCH_DSPY_CTX:
        resp = stream_client.post("/serve/stream_job/stream", json={"inputs": {"wrong": "x"}})

    assert resp.status_code == 400


def test_serve_stream_fallback_when_streamify_raises(stream_client: TestClient) -> None:
    """When ``dspy.streamify`` raises, the handler falls back to a single final SSE."""
    # Contract: when dspy.streamify raises, handler MUST fall back to a blocking call and still emit one final SSE.
    with _PATCH_LM, _PATCH_DSPY_CTX, patch("dspy.streamify", side_effect=RuntimeError("not streamable")):
        resp = stream_client.post("/serve/stream_job/stream", json={"inputs": {"question": "hi"}})

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    raw = resp.text
    assert "event: final" in raw
    payload = json.loads(raw.split("data: ", 1)[1].split("\n")[0])
    assert payload["streaming_fallback"] is True
    assert "answer" in payload["outputs"]


@pytest.fixture
def pair_stream_store(grid_store: _FakeJobStore) -> _FakeJobStore:
    """Reuse the grid store for pair-stream tests.

    Args:
        grid_store: Fake job store with a grid job and cached pair program.

    Returns:
        The same ``grid_store`` instance.
    """
    return grid_store


@pytest.fixture
def pair_stream_client(pair_stream_store: _FakeJobStore) -> TestClient:
    """Build a ``TestClient`` exposing the serve router over the pair-stream store.

    Args:
        pair_stream_store: Fake job store backing the pair-stream tests.

    Returns:
        A ``TestClient`` over a minimal FastAPI app.
    """
    app = FastAPI()
    app.include_router(create_serve_router(job_store=pair_stream_store))
    bypass_auth(app)
    return TestClient(app, raise_server_exceptions=False)


def test_serve_pair_stream_returns_404_for_unknown_id(pair_stream_client: TestClient) -> None:
    """A pair-stream POST against an unknown id returns 404."""
    resp = pair_stream_client.post("/serve/ghost/pair/0/stream", json={"inputs": {"question": "hi"}})

    assert resp.status_code == 404


def test_serve_pair_stream_returns_409_for_non_success_job(
    pair_stream_client: TestClient, pair_stream_store: _FakeJobStore
) -> None:
    """A non-success grid job rejects pair-stream POSTs with 409."""
    pair_stream_store.update_job("grid1", status="failed")

    with _PATCH_LM, _PATCH_DSPY_CTX:
        resp = pair_stream_client.post("/serve/grid1/pair/0/stream", json={"inputs": {"question": "hi"}})

    assert resp.status_code == 409


def test_serve_pair_stream_returns_400_for_missing_inputs(pair_stream_client: TestClient) -> None:
    """Missing required inputs on a pair-stream POST return 400."""
    with _PATCH_LM, _PATCH_DSPY_CTX:
        resp = pair_stream_client.post("/serve/grid1/pair/0/stream", json={"inputs": {"wrong": "x"}})

    assert resp.status_code == 400


def test_serve_pair_stream_fallback_when_streamify_raises(pair_stream_client: TestClient) -> None:
    """When ``dspy.streamify`` raises on a pair stream, the handler falls back to one final SSE."""
    with _PATCH_LM, _PATCH_DSPY_CTX, patch("dspy.streamify", side_effect=RuntimeError("not streamable")):
        resp = pair_stream_client.post("/serve/grid1/pair/0/stream", json={"inputs": {"question": "hi"}})

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    raw = resp.text
    assert "event: final" in raw
    payload = json.loads(raw.split("data: ", 1)[1].split("\n")[0])
    assert payload["streaming_fallback"] is True


async def _fake_react_chat_stream(**_kwargs):
    """Yield a minimal react-chat SSE envelope for route wiring tests."""
    yield {"event": "reasoning_patch", "data": {"chunk": "thinking"}}
    yield {"event": "done", "data": {"assistant_message": "hi", "model": "openai/gpt-4o-mini"}}


def test_serve_chat_returns_404_for_unknown_id(serve_client: TestClient) -> None:
    """A chat request against an unknown id returns 404."""
    resp = serve_client.post("/serve/ghost/chat", json={"user_message": "hi"})
    assert resp.status_code == 404


def test_serve_chat_returns_409_for_non_react_run(serve_client: TestClient, serve_store: _FakeJobStore) -> None:
    """A non-react run rejects the chat with 409 ``serve.chat_not_react``."""
    _seed_run_job(serve_store, "plain")
    resp = serve_client.post("/serve/plain/chat", json={"user_message": "hi"})
    assert resp.status_code == 409
    assert resp.json()["code"] == I18nKey.SERVE_CHAT_NOT_REACT.value


def test_serve_chat_streams_react_turn(serve_client: TestClient, serve_store: _FakeJobStore) -> None:
    """A react run streams the chat turn through ``run_react_chat`` as SSE."""
    overlay = SimpleNamespace(tool_source={"kind": "live_mcp", "mcp_url": "http://mcp.local"})
    with (
        patch(
            "core.api.routers.serve.load_react_chat_inputs",
            return_value=(object, "{}", overlay, {"model_name": "openai/gpt-4o-mini"}),
        ),
        patch("core.api.routers.serve.build_language_model", return_value=MagicMock()),
        patch("core.api.routers.serve.run_react_chat", side_effect=lambda **_kw: _fake_react_chat_stream()),
    ):
        resp = serve_client.post("/serve/anything/chat", json={"user_message": "hi", "trust_mode": "ask"})

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    assert "event: done" in resp.text


def test_serve_chat_confirm_returns_404_for_unknown_id(serve_client: TestClient) -> None:
    """Confirming against an unknown run returns 404 before touching the registry."""
    resp = serve_client.post("/serve/ghost/chat/confirm", json={"call_id": "x", "approved": True})
    assert resp.status_code == 404


def test_serve_chat_confirm_returns_404_for_unknown_call_id(
    serve_client: TestClient, serve_store: _FakeJobStore
) -> None:
    """An owned run with an unregistered call id returns 404 ``unknown_call_id``."""
    _seed_run_job(serve_store, "plain")
    resp = serve_client.post("/serve/plain/chat/confirm", json={"call_id": "never-registered", "approved": True})
    assert resp.status_code == 404
    assert resp.json()["code"] == I18nKey.AGENT_APPROVAL_UNKNOWN_CALL_ID.value


class _UsageLm:
    """History-carrying LM double matching the ``usage_by_model_from_history`` contract."""

    def __init__(self) -> None:
        """Seed one history entry with a token usage block."""
        self.history = [{"usage": {"prompt_tokens": 120_000, "completion_tokens": 30_000}}]
        self.model = "openrouter/test/unpriced"


@pytest.fixture
def metered_store(serve_store: _FakeJobStore) -> _FakeJobStore:
    """Back the fake store with a real SQLite engine carrying the billing tables.

    Seeds a servable run job (``ok``) and a grid job (``grid1``) so every
    LLM-invoking serve route can be exercised against the same store.

    Args:
        serve_store: Base fake store to attach the engine to.

    Returns:
        The store with ``engine`` set and both jobs seeded.
    """
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    serve_store.engine = engine
    _seed_run_job(serve_store, "ok")
    serve_store.seed_raw("grid1", job=make_grid_job("grid1", pair_index=0))
    # noinspection PyProtectedMember
    _helpers._program_cache["grid1_pair_0"] = MagicMock(return_value=_FakePrediction())
    return serve_store


@pytest.fixture
def metered_client(metered_store: _FakeJobStore) -> TestClient:
    """Build a ``TestClient`` exposing the serve router over the metered store.

    Args:
        metered_store: Engine-backed fake job store.

    Returns:
        A ``TestClient`` over a minimal FastAPI app.
    """
    app = FastAPI()
    app.include_router(create_serve_router(job_store=metered_store))
    _wire_http_handler(app)
    bypass_auth(app)
    return TestClient(app, raise_server_exceptions=False)


def _deplete(engine: object) -> None:
    """Seed the test user's billing row with zero balance and zero grant."""
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(
                username="alice",
                stripe_customer_id="cus_alice",
                balance_cents=0,
                grant_remaining=0,
            )
        )
        session.commit()


def _fund(engine: object, cents: int = 10_000) -> None:
    """Seed the test user's billing row with a purchased balance.

    There is no free allowance, so any test that must pass the 402 balance gate
    funds the account explicitly.
    """
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(
                username="alice",
                stripe_customer_id="cus_alice",
                balance_cents=cents,
                grant_remaining=0,
            )
        )
        session.commit()


def _ledger_rows(engine: object) -> list[WalletLedgerModel]:
    """Return every wallet-ledger row, oldest first."""
    with Session(engine) as session:
        return session.query(WalletLedgerModel).order_by(WalletLedgerModel.id).all()


@pytest.mark.parametrize(
    "url",
    [
        "/serve/ok",
        "/serve/ok/stream",
        "/serve/grid1/pair/0",
        "/serve/grid1/pair/0/stream",
    ],
)
def test_serve_endpoints_return_402_when_depleted(
    metered_client: TestClient, metered_store: _FakeJobStore, url: str
) -> None:
    """Every program-serving route refuses a zero-balance account before the LLM call."""
    _deplete(metered_store.engine)

    resp = metered_client.post(url, json={"inputs": {"question": "hi"}})

    assert resp.status_code == 402
    assert resp.json()["code"] == I18nKey.BILLING_INSUFFICIENT_FUNDS.value


def test_serve_chat_returns_402_when_depleted(metered_client: TestClient, metered_store: _FakeJobStore) -> None:
    """The react-serve chat refuses a zero-balance account before streaming."""
    _deplete(metered_store.engine)
    overlay = SimpleNamespace(tool_source={"kind": "live_mcp", "mcp_url": "http://mcp.local"})

    with patch(
        "core.api.routers.serve.load_react_chat_inputs",
        return_value=(object, "{}", overlay, {"model_name": "openai/gpt-4o-mini"}),
    ):
        resp = metered_client.post("/serve/any/chat", json={"user_message": "hi"})

    assert resp.status_code == 402


def test_serve_program_debits_the_turn(metered_client: TestClient, metered_store: _FakeJobStore) -> None:
    """A blocking serve call books one ledger row with the measured tokens."""
    _fund(metered_store.engine)
    lm = _UsageLm()

    with patch("core.api.routers.serve.build_language_model", return_value=lm), _PATCH_DSPY_CTX:
        resp = metered_client.post("/serve/ok", json={"inputs": {"question": "hi"}})

    assert resp.status_code == 200
    (row,) = _ledger_rows(metered_store.engine)
    assert row.description == "Serve inference"
    assert row.delta_cents < 0
    assert row.input_tokens == 120_000
    assert row.output_tokens == 30_000


def test_serve_program_resolves_stored_custom_byok_connection(
    metered_client: TestClient,
    metered_store: _FakeJobStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interactive inference uses the caller's verified custom endpoint and key."""
    _fund(metered_store.engine)
    monkeypatch.setattr(
        settings,
        "byok_vault_key",
        SecretStr(Fernet.generate_key().decode("utf-8")),
    )
    monkeypatch.setattr("core.billing.byok_vault._pinned_address", lambda *args, **kwargs: "8.8.8.8")
    with patch("core.billing.byok_vault.ProviderKeyVault._probe", return_value="verified"):
        ProviderKeyVault(engine=metered_store.engine).save_key(
            "alice",
            "custom",
            "private-secret",
            api_base="https://inference.example/v1",
        )
    metered_store.update_job(
        "ok",
        payload_overview={
            "model_name": "openai/private-chat",
            "model_settings": {
                "name": "openai/private-chat",
                "token_source": "byok",
                "byok_provider": "custom",
            },
        },
    )
    builder = MagicMock(return_value=_UsageLm())

    with patch("core.api.routers.serve.build_language_model", builder), _PATCH_DSPY_CTX:
        resp = metered_client.post("/serve/ok", json={"inputs": {"question": "hi"}})

    assert resp.status_code == 200
    resolved = builder.call_args.args[0]
    assert resolved.name == "openai/private-chat"
    assert resolved.base_url == "https://inference.example/v1"
    assert resolved.extra["api_key"] == "private-secret"


def test_serve_stream_debits_on_completion(metered_client: TestClient, metered_store: _FakeJobStore) -> None:
    """A streamed serve call books its ledger row when the stream winds down."""
    _fund(metered_store.engine)
    lm = _UsageLm()

    with (
        patch("core.api.routers.serve.build_language_model", return_value=lm),
        _PATCH_DSPY_CTX,
        patch("dspy.streamify", side_effect=RuntimeError("not streamable")),
    ):
        resp = metered_client.post("/serve/ok/stream", json={"inputs": {"question": "hi"}})

    assert resp.status_code == 200
    assert "event: final" in resp.text
    (row,) = _ledger_rows(metered_store.engine)
    assert row.description == "Serve inference"
    assert row.delta_cents < 0


def test_serve_chat_debits_the_turn(metered_client: TestClient, metered_store: _FakeJobStore) -> None:
    """A react-serve chat turn books its ledger row under the chat label."""
    _fund(metered_store.engine)
    overlay = SimpleNamespace(tool_source={"kind": "live_mcp", "mcp_url": "http://mcp.local"})
    lm = _UsageLm()

    with (
        patch(
            "core.api.routers.serve.load_react_chat_inputs",
            return_value=(object, "{}", overlay, {"model_name": "openai/gpt-4o-mini"}),
        ),
        patch("core.api.routers.serve.build_language_model", return_value=lm),
        patch("core.api.routers.serve.run_react_chat", side_effect=lambda **_kw: _fake_react_chat_stream()),
    ):
        resp = metered_client.post("/serve/any/chat", json={"user_message": "hi"})

    assert resp.status_code == 200
    (row,) = _ledger_rows(metered_store.engine)
    assert row.description == "Serve chat"
    assert row.delta_cents < 0


def test_coerce_sample_value_keeps_clean_values_and_drops_unusable() -> None:
    """Short single-line strings and numbers are inlined; junk is dropped."""
    assert _coerce_sample_value("What is the capital of Australia?") == ("What is the capital of Australia?")
    assert _coerce_sample_value(42) == "42"
    assert _coerce_sample_value("  spaced  ") == "spaced"
    assert _coerce_sample_value("line1\nline2") is None
    assert _coerce_sample_value("x" * 201) is None
    assert _coerce_sample_value("") is None
    assert _coerce_sample_value(None) is None


def test_collect_sample_reads_fields_directly_and_via_mapping() -> None:
    """Values resolve by field name, or via the column mapping when renamed."""
    row = {"question": "Q1", "answer": "A1", "raw_col": "mapped"}
    assert _collect_sample(row, ["question"]) == {"question": "Q1"}
    assert _collect_sample(row, ["q"], {"raw_col": "q"}) == {"q": "mapped"}
    assert _collect_sample(row, ["missing"]) == {}


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


def _api_token_auth(app: FastAPI) -> None:
    """Authenticate as ``TEST_USER`` the way the PAT path does, stamp included.

    Args:
        app: App whose auth dependency is overridden.
    """

    def _pat_user(request: Request) -> AuthenticatedUser:
        """Return the test user and stamp the PAT channel.

        Args:
            request: Incoming request.

        Returns:
            The fixed test user.
        """
        request.state.auth_channel = "api_token"
        return TEST_USER

    app.dependency_overrides[get_authenticated_user] = _pat_user


def test_serve_records_one_ui_event_with_structural_properties_only(
    serve_client: TestClient, serve_store: _FakeJobStore, serve_events: list[dict[str, Any]]
) -> None:
    """A session-JWT serve call writes one ``ui`` row keyed to the job, with no inputs or outputs."""
    _seed_run_job(serve_store, "ok")

    with _PATCH_LM, _PATCH_DSPY_CTX:
        resp = serve_client.post("/serve/ok", json={"inputs": {"question": "hello"}})

    assert resp.status_code == 200
    assert serve_events == [
        {
            "username": TEST_USER.username,
            "name": "serve_inference",
            "properties": {"optimization_id": "ok", "channel": "ui", "route": "serve", "status": "ok"},
        }
    ]


def test_serve_records_api_token_channel_for_pat_caller(
    serve_store: _FakeJobStore, serve_events: list[dict[str, Any]]
) -> None:
    """A PAT-authenticated serve call records ``channel=api_token``."""
    _seed_run_job(serve_store, "ok")
    app = FastAPI()
    app.include_router(create_serve_router(job_store=serve_store))
    _api_token_auth(app)
    client = TestClient(app, raise_server_exceptions=False)

    with _PATCH_LM, _PATCH_DSPY_CTX:
        resp = client.post("/serve/ok", json={"inputs": {"question": "hello"}})

    assert resp.status_code == 200
    assert [event["properties"]["channel"] for event in serve_events] == ["api_token"]


def test_serve_records_error_when_inference_raises(
    serve_client: TestClient, serve_store: _FakeJobStore, serve_events: list[dict[str, Any]]
) -> None:
    """A failed inference still writes exactly one row, with ``status=error``."""
    _seed_run_job(serve_store, "boom")
    # noinspection PyProtectedMember
    _helpers._program_cache["boom"] = MagicMock(side_effect=RuntimeError("provider down"))

    with _PATCH_LM, _PATCH_DSPY_CTX:
        resp = serve_client.post("/serve/boom", json={"inputs": {"question": "hello"}})

    assert resp.status_code == 500
    assert len(serve_events) == 1
    assert serve_events[0]["properties"]["status"] == "error"
    assert serve_events[0]["properties"]["optimization_id"] == "boom"


def test_serve_response_survives_telemetry_failure(
    serve_client: TestClient, serve_store: _FakeJobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A telemetry write that raises leaves the serve response unchanged."""
    _seed_run_job(serve_store, "ok")
    monkeypatch.setattr(serve_module, "record_server_event", MagicMock(side_effect=RuntimeError("db down")))

    with _PATCH_LM, _PATCH_DSPY_CTX:
        resp = serve_client.post("/serve/ok", json={"inputs": {"question": "hello"}})

    assert resp.status_code == 200
    assert "answer" in resp.json()["outputs"]


def test_serve_stream_records_one_event_when_the_stream_ends(
    stream_client: TestClient, serve_events: list[dict[str, Any]]
) -> None:
    """A streamed serve call writes one ``serve_stream`` row after the stream completes."""
    with _PATCH_LM, _PATCH_DSPY_CTX, patch("dspy.streamify", side_effect=RuntimeError("not streamable")):
        resp = stream_client.post("/serve/stream_job/stream", json={"inputs": {"question": "hi"}})

    assert resp.status_code == 200
    assert "event: final" in resp.text
    assert [event["properties"] for event in serve_events] == [
        {"optimization_id": "stream_job", "channel": "ui", "route": "serve_stream", "status": "ok"}
    ]


def test_serve_stream_records_error_when_inference_fails(
    stream_client: TestClient, serve_events: list[dict[str, Any]]
) -> None:
    """A stream that ends in an ``error`` event records ``status=error`` once."""
    # noinspection PyProtectedMember
    _helpers._program_cache["stream_job"] = MagicMock(side_effect=RuntimeError("provider down"))

    with _PATCH_LM, _PATCH_DSPY_CTX, patch("dspy.streamify", side_effect=RuntimeError("not streamable")):
        resp = stream_client.post("/serve/stream_job/stream", json={"inputs": {"question": "hi"}})

    assert "event: error" in resp.text
    assert [event["properties"]["status"] for event in serve_events] == ["error"]


def test_serve_pair_routes_record_pair_index(grid_client: TestClient, serve_events: list[dict[str, Any]]) -> None:
    """Pair and pair-stream calls each write one row carrying the pair index."""
    with _PATCH_LM, _PATCH_DSPY_CTX, patch("dspy.streamify", side_effect=RuntimeError("not streamable")):
        assert grid_client.post("/serve/grid1/pair/0", json={"inputs": {"question": "hi"}}).status_code == 200
        assert grid_client.post("/serve/grid1/pair/0/stream", json={"inputs": {"question": "hi"}}).status_code == 200

    assert [event["properties"] for event in serve_events] == [
        {"optimization_id": "grid1", "channel": "ui", "route": "pair", "pair_index": 0, "status": "ok"},
        {"optimization_id": "grid1", "channel": "ui", "route": "pair_stream", "pair_index": 0, "status": "ok"},
    ]


def test_serve_chat_records_one_event(serve_client: TestClient, serve_events: list[dict[str, Any]]) -> None:
    """A streamed chat turn writes one ``chat`` row."""
    overlay = SimpleNamespace(tool_source={"kind": "live_mcp", "mcp_url": "http://mcp.local"})
    with (
        patch(
            "core.api.routers.serve.load_react_chat_inputs",
            return_value=(object, "{}", overlay, {"model_name": "openai/gpt-4o-mini"}),
        ),
        patch("core.api.routers.serve.build_language_model", return_value=MagicMock()),
        patch("core.api.routers.serve.run_react_chat", side_effect=lambda **_kw: _fake_react_chat_stream()),
    ):
        resp = serve_client.post("/serve/anything/chat", json={"user_message": "hi", "trust_mode": "ask"})

    assert resp.status_code == 200
    assert [event["properties"] for event in serve_events] == [
        {"optimization_id": "anything", "channel": "ui", "route": "chat", "status": "ok"}
    ]


def test_serve_info_and_request_form_record_nothing(
    serve_client: TestClient, serve_store: _FakeJobStore, serve_events: list[dict[str, Any]]
) -> None:
    """Schema reads and request-form echoes are not inference and write no row."""
    _seed_run_job(serve_store, "ok")

    assert serve_client.get("/serve/ok/info").status_code == 200
    assert serve_client.post("/serve/ok/request-form", json={}).status_code == 200

    assert serve_events == []
