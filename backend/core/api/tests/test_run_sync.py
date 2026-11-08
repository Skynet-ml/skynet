"""Tests for desktop run sync: export a local run, import it into the hosted account.

Both sides run on a real ``RemoteDBJobStore`` over in-memory SQLite, so the
import writes real job and progress rows and the hosted listing, detail and
artifact routes read them back through their normal code paths.
"""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from types import SimpleNamespace
from typing import Any

import fakeredis
import httpx
import pytest
from fakeredis import _basefakesocket
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import core.storage.remote as remote_mod

from ...config import settings
from ...i18n_en import t_en
from ...service_gateway import run_sync
from ...storage.models import Base, ExecutionBudgetModel, JobModel, ProgressEventModel, WalletLedgerModel
from ...storage.remote import RemoteDBJobStore
from .. import rate_limit
from ..auth import AuthenticatedUser
from ..routers.optimizations import create_optimizations_router
from ..routers.optimizations import sync as sync_mod
from ..routers.optimizations_meta import create_optimizations_meta_router
from .conftest import bypass_auth

DESK_USER = AuthenticatedUser(username="desk-user", role="user", groups=())
HOSTED_USER = AuthenticatedUser(username="bob", role="user", groups=())
_SIGNATURE_CODE = "class QA(dspy.Signature):\n    question = dspy.InputField()\n    answer = dspy.OutputField()"
_PROGRAM_STATE = {"predict": {"signature": {"instructions": "Answer briefly."}, "demos": []}}


class _SQLiteJobStore(RemoteDBJobStore):
    """RemoteDBJobStore on an in-memory SQLite engine, skipping PostgreSQL-only setup."""

    def __init__(self) -> None:
        """Create the engine and every ORM table."""
        self._engine = create_engine(
            "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(self._engine)
        self._session_factory = sessionmaker(bind=self._engine)
        self._checkpoints = remote_mod.PostgresCheckpointBlobStore(self._engine)
        self._grid_pair_results = remote_mod.PostgresGridPairResultStore(self._engine)
        self._agent_runs = remote_mod.PostgresAgentRunStore(self._engine)


def _client(store: _SQLiteJobStore, user: AuthenticatedUser) -> TestClient:
    """Build a client over the optimizations router for ``user``.

    Args:
        store: Job store the router reads and writes.
        user: Identity every request authenticates as.

    Returns:
        A ``TestClient`` that returns error responses instead of raising.
    """
    app = FastAPI()
    app.include_router(create_optimizations_router(job_store=store, get_worker_ref=lambda: None))
    bypass_auth(app, user=user)
    return TestClient(app, raise_server_exceptions=False)


def _seed_finished_run(store: _SQLiteJobStore, optimization_id: str = "local-run-1", status: str = "success") -> None:
    """Write a finished local run with a result, artifact and progress events.

    Args:
        store: The local job store.
        optimization_id: Id of the run to create.
        status: Terminal or active status to leave the run in.
    """
    store.create_job(optimization_id, username=DESK_USER.username)
    store.set_payload_overview(
        optimization_id,
        {
            "name": "Desk run",
            "description": "Support triage",
            "username": DESK_USER.username,
            "optimization_type": "run",
            "module_name": "predict",
            "optimizer_name": "gepa",
            "model_name": "openai/gpt-4o-mini",
            "signature_code": _SIGNATURE_CODE,
            "max_cost_cents": 500,
            "estimated_cents_low": 10,
            "estimated_cents_high": 40,
            "source_dataset_id": "local-dataset-9",
        },
    )
    store.record_progress(optimization_id, "baseline", {"baseline_test_metric": 0.41})
    store.record_progress(optimization_id, "done", {"optimized_test_metric": 0.82})
    store.update_job(
        optimization_id,
        status=status,
        message="Finished",
        latest_metrics={"baseline_test_metric": 0.41, "optimized_test_metric": 0.82},
        result={
            "module_name": "predict",
            "optimizer_name": "gepa",
            "metric_name": "accuracy",
            "split_counts": {"train": 6, "val": 2, "test": 2},
            "baseline_test_metric": 0.41,
            "optimized_test_metric": 0.82,
            "metric_improvement": 0.41,
            "program_artifact_path": "/Users/desk/runs/local-run-1",
            "program_artifact": {
                "path": "/Users/desk/runs/local-run-1/program.json",
                "program_state_json": _PROGRAM_STATE,
                "program_pickle_base64": base64.b64encode(b"not-a-real-pickle").decode(),
            },
        },
    )


@pytest.fixture
def local_store() -> _SQLiteJobStore:
    """Provide the local (Mac app) job store with one finished run."""
    store = _SQLiteJobStore()
    _seed_finished_run(store)
    return store


@pytest.fixture
def hosted_store() -> _SQLiteJobStore:
    """Provide an empty hosted job store."""
    return _SQLiteJobStore()


@pytest.fixture
def bundle(local_store: _SQLiteJobStore, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Export the seeded run through the local-mode route.

    Args:
        local_store: The local job store with the seeded run.
        monkeypatch: Pytest fixture used to switch local mode on for the export.

    Returns:
        The exported bundle.
    """
    monkeypatch.setattr(settings, "local_mode", True)
    resp = _client(local_store, DESK_USER).get("/optimizations/local-run-1/export")
    assert resp.status_code == 200, resp.text
    monkeypatch.setattr(settings, "local_mode", False)
    return resp.json()


def _import(store: _SQLiteJobStore, payload: Any, user: AuthenticatedUser = HOSTED_USER) -> Any:
    """Post ``payload`` to the hosted import route.

    Args:
        store: The hosted job store.
        payload: Bundle to send as JSON.
        user: The hosted caller.

    Returns:
        The HTTP response.
    """
    return _client(store, user).post("/optimizations/import", content=json.dumps(payload))


def _count(store: _SQLiteJobStore, model: type) -> int:
    """Count the rows of ``model`` in ``store``.

    Args:
        store: The job store to query.
        model: ORM model whose table is counted.

    Returns:
        The row count.
    """
    with Session(store.engine) as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


def test_export_bundle_shape(bundle: dict[str, Any]) -> None:
    """The bundle follows the v1 contract and leaves owner and cost data behind."""
    assert bundle["schema"] == run_sync.SCHEMA_VERSION
    assert bundle["source"]["local_mode"] is True
    assert bundle["job"]["status"] == "success"
    assert "username" not in bundle["optimization"]["payload_overview"]
    assert "program_artifact" not in bundle["job"]["result"]
    assert [event["event"] for event in bundle["events"]] == ["baseline", "done"]
    artifact = json.loads(base64.b64decode(bundle["artifacts"][run_sync.PROGRAM_ARTIFACT_NAME]))
    assert artifact["program_state_json"] == _PROGRAM_STATE


def test_round_trip_shows_same_scores_and_artifact(hosted_store: _SQLiteJobStore, bundle: dict[str, Any]) -> None:
    """An imported run appears in the hosted listing and detail with the same scores and program."""
    resp = _import(hosted_store, bundle)
    assert resp.status_code == 201, resp.text
    new_id = resp.json()["optimization_id"]
    assert new_id != "local-run-1"

    client = _client(hosted_store, HOSTED_USER)
    listing = client.get("/optimizations")
    assert listing.status_code == 200, listing.text
    assert [item["optimization_id"] for item in listing.json()["items"]] == [new_id]

    detail = client.get(f"/optimizations/{new_id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["status"] == "success"
    assert body["result"]["baseline_test_metric"] == 0.41
    assert body["result"]["optimized_test_metric"] == 0.82
    assert body["description"] == f"Support triage ({run_sync.IMPORT_LABEL})"
    assert len(body["progress_events"]) == 2

    artifact = client.get(f"/optimizations/{new_id}/artifact")
    assert artifact.status_code == 200, artifact.text
    program = artifact.json()["program_artifact"]
    assert program["program_state_json"] == _PROGRAM_STATE
    assert program["program_pickle_base64"] is None
    assert program["path"] is None


def test_import_lands_under_caller_and_ignores_bundle_identity(
    hosted_store: _SQLiteJobStore, bundle: dict[str, Any]
) -> None:
    """Owner, ids, dataset links and cost ceilings in the bundle never reach the hosted row."""
    bundle["optimization"]["payload_overview"].update(username="mallory", optimization_id="victim-run")
    bundle["optimization"]["source_optimization_id"] = "victim-run"
    bundle["job"]["username"] = "mallory"
    bundle["job"]["execution_budget_id"] = "budget-123"
    resp = _import(hosted_store, bundle)
    assert resp.status_code == 201, resp.text
    job = hosted_store.get_job(resp.json()["optimization_id"])
    overview = job["payload_overview"]
    assert job["username"] == HOSTED_USER.username
    assert overview["username"] == HOSTED_USER.username
    assert overview[run_sync.IMPORTED_FROM_KEY] == run_sync.IMPORTED_FROM_DESKTOP
    for dropped in ("max_cost_cents", "estimated_cents_low", "estimated_cents_high", "source_dataset_id"):
        assert dropped not in overview
    assert job["payload"] is None
    assert job["execution_budget_id"] is None
    assert job["result"].get("program_artifact_path") is None
    with pytest.raises(KeyError):
        hosted_store.get_job("victim-run")


def test_import_makes_no_wallet_or_budget_rows(hosted_store: _SQLiteJobStore, bundle: dict[str, Any]) -> None:
    """Importing writes job and progress rows only, never money rows."""
    assert _import(hosted_store, bundle).status_code == 201
    assert _count(hosted_store, WalletLedgerModel) == 0
    assert _count(hosted_store, ExecutionBudgetModel) == 0
    assert _count(hosted_store, JobModel) == 1
    assert _count(hosted_store, ProgressEventModel) == 2


def test_imported_run_cannot_be_requeued(hosted_store: _SQLiteJobStore, bundle: dict[str, Any]) -> None:
    """Restart of an imported failed DSPy run is refused, and the failed copy keeps no result."""
    bundle["job"]["status"] = "failed"
    resp = _import(hosted_store, bundle)
    assert resp.status_code == 201, resp.text
    new_id = resp.json()["optimization_id"]
    restart = _client(hosted_store, HOSTED_USER).post(f"/optimizations/{new_id}/restart")
    # The bundle is a DSPy run, and Skynet no longer starts DSPy runs (SKY-316).
    assert restart.status_code == 410
    assert restart.json()["detail"] == t_en("optimization.dspy_run_retired")
    job = hosted_store.get_job(new_id)
    assert job["status"] == "failed"
    assert job["result"] is None


def test_import_in_local_mode_is_422(
    hosted_store: _SQLiteJobStore, bundle: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Mac app refuses imports; they run only on hosted."""
    monkeypatch.setattr(settings, "local_mode", True)
    resp = _import(hosted_store, bundle)
    assert resp.status_code == 422
    assert resp.json()["detail"] == t_en("sync.import_local_only_disabled")
    assert _count(hosted_store, JobModel) == 0


def test_export_outside_local_mode_is_404(local_store: _SQLiteJobStore) -> None:
    """Hosted deployments hide the export route."""
    resp = _client(local_store, DESK_USER).get("/optimizations/local-run-1/export")
    assert resp.status_code == 404
    assert resp.json()["detail"] == t_en("local.not_available")


def test_export_refuses_other_users_and_active_runs(
    local_store: _SQLiteJobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Export answers 404 for another user's run and 409 for a run still in progress."""
    monkeypatch.setattr(settings, "local_mode", True)
    assert _client(local_store, HOSTED_USER).get("/optimizations/local-run-1/export").status_code == 404
    _seed_finished_run(local_store, "local-run-2", status="running")
    resp = _client(local_store, DESK_USER).get("/optimizations/local-run-2/export")
    assert resp.status_code == 409
    assert resp.json()["detail"] == t_en("sync.export_not_finished", status="running")


def test_unknown_schema_is_422(hosted_store: _SQLiteJobStore, bundle: dict[str, Any]) -> None:
    """A bundle from another schema version is refused before anything is written."""
    bundle["schema"] = "skynet.run-export/v2"
    resp = _import(hosted_store, bundle)
    assert resp.status_code == 422
    assert resp.json()["detail"] == t_en("sync.unsupported_schema", schema="skynet.run-export/v2")
    assert _count(hosted_store, JobModel) == 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b["job"].update(status="running"),
        lambda b: b["optimization"].update(optimization_type="grid_search"),
        lambda b: b["artifacts"].update({run_sync.PROGRAM_ARTIFACT_NAME: "%%%not-base64"}),
        lambda b: b["job"]["result"].pop("split_counts"),
        lambda b: b["events"].append({"timestamp": "yesterday", "event": "x", "metrics": {}}),
    ],
    ids=["active-status", "grid-type", "bad-artifact", "bad-result", "bad-timestamp"],
)
def test_malformed_bundle_is_422(hosted_store: _SQLiteJobStore, bundle: dict[str, Any], mutate: Any) -> None:
    """Bundles that do not match the contract are refused with no rows written."""
    mutate(bundle)
    resp = _import(hosted_store, bundle)
    assert resp.status_code == 422
    assert resp.json()["detail"] == t_en("sync.invalid_bundle")
    assert _count(hosted_store, JobModel) == 0


def test_non_json_body_is_422(hosted_store: _SQLiteJobStore) -> None:
    """A body that is not JSON is refused as an invalid bundle."""
    resp = _client(hosted_store, HOSTED_USER).post("/optimizations/import", content=b"\xff not json")
    assert resp.status_code == 422
    assert resp.json()["detail"] == t_en("sync.invalid_bundle")


def test_oversized_bundle_is_413_before_parsing(hosted_store: _SQLiteJobStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """A body over the cap is refused on its size; parsing it would have given 422 instead."""
    monkeypatch.setattr("core.api.routers.optimizations.sync.MAX_BUNDLE_BYTES", 64)
    resp = _client(hosted_store, HOSTED_USER).post("/optimizations/import", content=b"not json " * 20)
    assert resp.status_code == 413
    assert resp.json()["detail"] == t_en("sync.bundle_too_large", max_mb=50)
    assert _count(hosted_store, JobModel) == 0


def test_default_cap_is_50_mb() -> None:
    """The bundle cap is the 50 MB the contract names."""
    assert run_sync.MAX_BUNDLE_BYTES == 50 * 1024 * 1024


def test_inline_result_artifact_is_cleaned(hosted_store: _SQLiteJobStore, bundle: dict[str, Any]) -> None:
    """A program artifact placed inside the result loses its pickle and path, like the artifacts entry."""
    encoded = bundle["artifacts"].pop(run_sync.PROGRAM_ARTIFACT_NAME)
    inline = json.loads(base64.b64decode(encoded))
    inline.update(path="/Users/desk/secret.json", program_pickle_base64=base64.b64encode(b"evil").decode())
    bundle["job"]["result"]["program_artifact"] = inline
    resp = _import(hosted_store, bundle)
    assert resp.status_code == 201, resp.text
    artifact = _client(hosted_store, HOSTED_USER).get(f"/optimizations/{resp.json()['optimization_id']}/artifact")
    assert artifact.status_code == 200, artifact.text
    program = artifact.json()["program_artifact"]
    assert program["program_state_json"] == _PROGRAM_STATE
    assert program["program_pickle_base64"] is None
    assert program["path"] is None


def test_artifact_without_signature_code_is_422(hosted_store: _SQLiteJobStore, bundle: dict[str, Any]) -> None:
    """Without a signature the hosted copy could rebuild the program only from a pickle, so it is refused."""
    bundle["optimization"]["payload_overview"].pop("signature_code")
    resp = _import(hosted_store, bundle)
    assert resp.status_code == 422
    assert resp.json()["detail"] == t_en("sync.invalid_bundle")
    assert _count(hosted_store, JobModel) == 0


def test_deeply_nested_body_is_422(hosted_store: _SQLiteJobStore) -> None:
    """A body nested past the JSON parser's recursion limit is refused, not a server error."""
    resp = _client(hosted_store, HOSTED_USER).post("/optimizations/import", content=b"[" * 5000)
    assert resp.status_code == 422
    assert resp.json()["detail"] == t_en("sync.invalid_bundle")


def test_deeply_nested_metrics_are_422(hosted_store: _SQLiteJobStore, bundle: dict[str, Any]) -> None:
    """Nesting that parses but passes the depth cap is refused before it can break the run list."""
    nested: Any = 1
    for _ in range(run_sync.MAX_BUNDLE_DEPTH + 1):
        nested = {"x": nested}
    bundle["job"]["latest_metrics"] = {"deep": nested}
    resp = _import(hosted_store, bundle)
    assert resp.status_code == 422
    assert resp.json()["detail"] == t_en("sync.invalid_bundle")
    assert _count(hosted_store, JobModel) == 0


def test_too_many_values_are_422(
    hosted_store: _SQLiteJobStore, bundle: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A body with more values than the cap is refused, so a small-number flood cannot exhaust API memory."""
    count = len(json.dumps(bundle))
    monkeypatch.setattr(run_sync, "MAX_BUNDLE_NODES", count)
    assert run_sync._is_storable([0] * count, run_sync.MAX_BUNDLE_DEPTH) is False
    assert run_sync._is_storable([0] * (count - 1), run_sync.MAX_BUNDLE_DEPTH) is True
    assert _import(hosted_store, bundle).status_code == 201

    bundle["job"]["result"] = {"flood": [0] * count}
    resp = _import(hosted_store, bundle)
    assert resp.status_code == 422
    assert resp.json()["detail"] == t_en("sync.invalid_bundle")
    assert _count(hosted_store, JobModel) == 1


@pytest.mark.parametrize(
    "model_settings",
    [{"name": "openai/gpt-4o-mini", "temperature": 9}, ["openai/gpt-4o-mini"]],
    ids=["temperature-out-of-range", "not-a-mapping"],
)
def test_invalid_model_settings_are_422(
    hosted_store: _SQLiteJobStore, bundle: dict[str, Any], model_settings: Any
) -> None:
    """Model settings serve cannot rebuild are refused at import, so the copy can never make serve answer 500."""
    bundle["optimization"]["payload_overview"]["model_settings"] = model_settings
    resp = _import(hosted_store, bundle)
    assert resp.status_code == 422
    assert resp.json()["detail"] == t_en("sync.invalid_bundle")
    assert _count(hosted_store, JobModel) == 0


def test_valid_model_settings_are_kept(hosted_store: _SQLiteJobStore, bundle: dict[str, Any]) -> None:
    """Model settings that validate are stored unchanged on the copy."""
    settings_value = {"name": "openai/gpt-4o-mini", "temperature": 0.5}
    bundle["optimization"]["payload_overview"]["model_settings"] = settings_value
    resp = _import(hosted_store, bundle)
    assert resp.status_code == 201, resp.text
    with Session(hosted_store.engine) as session:
        row = session.execute(select(JobModel)).scalar_one()
    assert row.payload_overview["model_settings"] == settings_value


@pytest.mark.parametrize("field", ["latest_metrics", "terminal_evidence"])
def test_oversized_job_field_is_422(hosted_store: _SQLiteJobStore, bundle: dict[str, Any], field: str) -> None:
    """Metrics or terminal evidence over the field cap are refused, so no import can store them uncounted."""
    bundle["job"][field] = {"blob": "x" * (run_sync.MAX_JOB_FIELD_BYTES + 1)}
    resp = _import(hosted_store, bundle)
    assert resp.status_code == 422
    assert resp.json()["detail"] == t_en("sync.invalid_bundle")
    assert _count(hosted_store, JobModel) == 0


@pytest.mark.parametrize("budget", ["x", 5, [1, 2]])
def test_non_object_execution_budget_is_422(hosted_store: _SQLiteJobStore, bundle: dict[str, Any], budget: Any) -> None:
    """A terminal-evidence budget the detail view cannot read is refused, so the run's detail page cannot 500."""
    bundle["job"]["terminal_evidence"] = {"execution_budget": budget}
    resp = _import(hosted_store, bundle)
    assert resp.status_code == 422
    assert resp.json()["detail"] == t_en("sync.invalid_bundle")
    assert _count(hosted_store, JobModel) == 0


def test_job_fields_count_toward_storage(
    hosted_store: _SQLiteJobStore, bundle: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Metrics and terminal evidence within the cap count in the stored size, so the quota sees them."""
    blob = "x" * (run_sync.MAX_JOB_FIELD_BYTES - 1024)
    bundle["job"]["latest_metrics"] = {"blob": blob}
    bundle["job"]["terminal_evidence"] = {"blob": blob}
    resp = _import(hosted_store, bundle)
    assert resp.status_code == 201, resp.text
    used = hosted_store.compute_user_storage(HOSTED_USER.username).total
    assert used >= 2 * len(blob)

    body_bytes = len(json.dumps(bundle).encode())
    monkeypatch.setattr(hosted_store, "get_effective_user_storage_quota", lambda username: used + body_bytes - 1)
    resp = _import(hosted_store, bundle)
    assert resp.status_code == 409
    assert _count(hosted_store, JobModel) == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b["job"]["latest_metrics"].update(optimized_test_metric=float("nan")),
        lambda b: b["events"][0]["metrics"].update(baseline_test_metric=float("inf")),
        lambda b: b["job"].update(message="Fin\x00ished"),
        lambda b: b["job"]["latest_metrics"].update({"bad\x00key": 1}),
        lambda b: b["job"].update(message="Fin\ud800ished"),
        lambda b: b["job"]["latest_metrics"].update({"bad\udfffkey": 1}),
        lambda b: b["optimization"]["payload_overview"].update(is_private=5),
        lambda b: b["optimization"]["payload_overview"].update(is_private="1.0"),
        lambda b: b["job"]["latest_metrics"].update(optimized_test_metric=10**400),
        lambda b: b["events"][0]["metrics"].update(baseline_test_metric=-(10**309)),
    ],
    ids=[
        "nan-metric",
        "infinite-event-metric",
        "nul-message",
        "nul-key",
        "surrogate-message",
        "surrogate-key",
        "number-is-private",
        "string-is-private",
        "huge-int-metric",
        "huge-negative-int-event-metric",
    ],
)
def test_values_postgres_cannot_store_are_422(
    hosted_store: _SQLiteJobStore, bundle: dict[str, Any], mutate: Any
) -> None:
    """Values Postgres cannot store or cast are refused before the write, though SQLite would accept them.

    A lone surrogate fails the JSONB write. A non-boolean ``is_private`` or an
    integer beyond the float range fails a public dashboard cast for every user.
    """
    mutate(bundle)
    resp = _client(hosted_store, HOSTED_USER).post("/optimizations/import", content=json.dumps(bundle))
    assert resp.status_code == 422
    assert resp.json()["detail"] == t_en("sync.invalid_bundle")
    assert _count(hosted_store, JobModel) == 0


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("name", "n" * 256),
        ("model_name", "m" * 256),
        ("module_name", "d" * 129),
        ("optimizer_name", "o" * 65),
        ("model_name", {"id": "gpt"}),
    ],
    ids=["long-name", "long-model", "long-module", "long-optimizer", "dict-model"],
)
def test_display_fields_the_embedding_sweep_cannot_write_are_422(
    hosted_store: _SQLiteJobStore, bundle: dict[str, Any], key: str, value: Any
) -> None:
    """A display field that cannot fit its job_embeddings column is refused.

    Postgres would fail the embedding write on every sweep, and the oldest-first
    sweep would then never repair another user's embedding.
    """
    bundle["optimization"]["payload_overview"][key] = value
    resp = _client(hosted_store, HOSTED_USER).post("/optimizations/import", content=json.dumps(bundle))
    assert resp.status_code == 422
    assert resp.json()["detail"] == t_en("sync.invalid_bundle")
    assert _count(hosted_store, JobModel) == 0


def test_embedding_column_widths_bound_the_engine_and_allow_the_limit() -> None:
    """``engine_used`` must fit ``optimizer_name``, and values at a column's width pass."""
    at_limit = {"name": "n" * 255, "model_name": "m" * 255, "module_name": "d" * 128, "optimizer_name": "o" * 64}
    assert run_sync._fits_embedding_columns(at_limit, {"engine_used": "e" * 64})
    assert not run_sync._fits_embedding_columns({}, {"engine_used": "e" * 65})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("created_at", "0001-01-01T00:00:00+14:00"),
        ("completed_at", "9999-12-31T23:59:59-14:00"),
        ("created_at", "1969-12-31T23:59:59+00:00"),
        ("event", "9999-12-31T23:59:59-14:00"),
    ],
    ids=["created-before-year-1", "completed-after-9999", "before-1970", "event-after-9999"],
)
def test_out_of_range_timestamps_are_422(
    hosted_store: _SQLiteJobStore, bundle: dict[str, Any], field: str, value: str
) -> None:
    """A time that leaves the datetime range in UTC, or is outside 1970..2100, is refused.

    Before the fix, such a run imported with 201 and then broke the list, detail and delete routes.
    """
    if field == "event":
        bundle["events"][0]["timestamp"] = value
    else:
        bundle["job"][field] = value
    resp = _import(hosted_store, bundle)
    assert resp.status_code == 422
    assert resp.json()["detail"] == t_en("sync.invalid_bundle")
    assert _count(hosted_store, JobModel) == 0
    assert _client(hosted_store, HOSTED_USER).get("/optimizations").status_code == 200


def test_offset_timestamp_is_stored_in_utc(hosted_store: _SQLiteJobStore, bundle: dict[str, Any]) -> None:
    """A time with an offset is stored as the same instant in UTC."""
    bundle["job"]["created_at"] = "2026-05-01T12:00:00+02:00"
    resp = _import(hosted_store, bundle)
    assert resp.status_code == 201, resp.text
    job = hosted_store.get_job(resp.json()["optimization_id"])
    assert job["created_at"].startswith("2026-05-01T10:00:00")


@pytest.mark.parametrize("value", ["Infinity", "inf", "1e400", "NaN"])
def test_non_finite_runtime_text_is_422(hosted_store: _SQLiteJobStore, bundle: dict[str, Any], value: str) -> None:
    """Runtime text that converts to a non-finite float is refused."""
    bundle["job"]["accumulated_runtime_seconds"] = value
    resp = _import(hosted_store, bundle)
    assert resp.status_code == 422
    assert resp.json()["detail"] == t_en("sync.invalid_bundle")
    assert _count(hosted_store, JobModel) == 0


@pytest.mark.parametrize("value", ["12.5", 3])
def test_finite_runtime_is_accepted(hosted_store: _SQLiteJobStore, bundle: dict[str, Any], value: Any) -> None:
    """Runtime as numeric text or an integer still imports."""
    bundle["job"]["accumulated_runtime_seconds"] = value
    resp = _import(hosted_store, bundle)
    assert resp.status_code == 201, resp.text
    assert hosted_store.get_job(resp.json()["optimization_id"])["accumulated_runtime_seconds"] == float(value)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b["job"]["result"].update(baseline_test_metric="Infinity"),
        lambda b: b["optimization"]["payload_overview"].update(model_name=7),
        lambda b: b["optimization"]["payload_overview"].update(dataset_rows="abc"),
        lambda b: b["optimization"]["payload_overview"].update(dataset_rows=5.5),
        lambda b: b["optimization"]["payload_overview"].update(seed="abc"),
    ],
    ids=["infinite-result-metric", "numeric-model-name", "text-dataset-rows", "fractional-dataset-rows", "text-seed"],
)
def test_values_the_read_routes_cannot_serve_are_422(
    hosted_store: _SQLiteJobStore, bundle: dict[str, Any], mutate: Any
) -> None:
    """A bundle whose row would break the caller's run list or detail is refused, and the list stays readable."""
    mutate(bundle)
    resp = _import(hosted_store, bundle)
    assert resp.status_code == 422
    assert resp.json()["detail"] == t_en("sync.invalid_bundle")
    assert _count(hosted_store, JobModel) == 0
    assert _count(hosted_store, ProgressEventModel) == 0
    assert _client(hosted_store, HOSTED_USER).get("/optimizations").status_code == 200


def test_pin_and_rename_keep_imported_storage(hosted_store: _SQLiteJobStore, bundle: dict[str, Any]) -> None:
    """Pin, rename and a later update keep counting the imported metrics and terminal evidence."""
    blob = "x" * 4096
    bundle["job"]["latest_metrics"] = {"blob": blob}
    bundle["job"]["terminal_evidence"] = {"blob": blob}
    resp = _import(hosted_store, bundle)
    assert resp.status_code == 201, resp.text
    optimization_id = resp.json()["optimization_id"]
    imported_bytes = hosted_store.get_job(optimization_id)["stored_bytes"]
    assert imported_bytes >= 2 * len(blob)

    app = FastAPI()
    app.include_router(create_optimizations_meta_router(job_store=hosted_store))
    bypass_auth(app, user=HOSTED_USER)
    meta = TestClient(app, raise_server_exceptions=False)
    assert meta.patch(f"/optimizations/{optimization_id}/pin").status_code == 200
    pinned_bytes = hosted_store.get_job(optimization_id)["stored_bytes"]
    assert pinned_bytes >= imported_bytes
    assert meta.patch(f"/optimizations/{optimization_id}/name", json={"name": "Renamed"}).status_code == 200
    assert hosted_store.get_job(optimization_id)["stored_bytes"] >= 2 * len(blob)

    hosted_store.update_job(optimization_id, message="Note")
    hosted_store.update_job(optimization_id, result=hosted_store.get_job(optimization_id)["result"])
    assert hosted_store.get_job(optimization_id)["stored_bytes"] >= 2 * len(blob)
    assert hosted_store.update_job_if_status(
        optimization_id, ("success",), result=hosted_store.get_job(optimization_id)["result"]
    )
    assert hosted_store.get_job(optimization_id)["stored_bytes"] >= 2 * len(blob)


def test_imported_run_test_results_is_404(hosted_store: _SQLiteJobStore, bundle: dict[str, Any]) -> None:
    """An imported run has no stored payload, so its test results give 404, not 500."""
    resp = _import(hosted_store, bundle)
    assert resp.status_code == 201, resp.text
    r = _client(hosted_store, HOSTED_USER).get(f"/optimizations/{resp.json()['optimization_id']}/test-results")
    assert r.status_code == 404
    assert r.json()["detail"] == t_en("optimization.payload_unavailable")


async def test_import_does_not_block_other_requests(
    hosted_store: _SQLiteJobStore, bundle: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """While an import is stored, the event loop still serves another request."""
    store_started = threading.Event()
    release = threading.Event()
    real_import = run_sync.import_run

    def _slow_import(*args: Any, **kwargs: Any) -> str:
        """Hold the import until the concurrent request is served.

        Args:
            *args: Positional arguments for ``import_run``.
            **kwargs: Keyword arguments for ``import_run``.

        Returns:
            The new optimization id.
        """
        store_started.set()
        assert release.wait(timeout=10), "the concurrent request was not served while the import ran"
        return real_import(*args, **kwargs)

    monkeypatch.setattr(sync_mod, "import_run", _slow_import)
    app = FastAPI()
    app.include_router(create_optimizations_router(job_store=hosted_store, get_worker_ref=lambda: None))

    @app.get("/ping")
    async def _ping() -> dict[str, bool]:
        """Answer at once and let the held import finish.

        Returns:
            A fixed body.
        """
        release.set()
        return {"ok": True}

    bypass_auth(app, user=HOSTED_USER)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        import_task = asyncio.create_task(client.post("/optimizations/import", content=json.dumps(bundle)))
        assert await asyncio.to_thread(store_started.wait, 10)
        ping = await asyncio.wait_for(client.get("/ping"), timeout=5)
        resp = await asyncio.wait_for(import_task, timeout=10)
    assert ping.status_code == 200
    assert resp.status_code == 201, resp.text


def test_import_is_rate_limited(
    hosted_store: _SQLiteJobStore, bundle: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Imports share the submission rate cap, so a refused import still counts toward it."""
    # fakeredis reads the wall clock on every command, so a slow CI shard saw the
    # window TTL count down mid-test (SKY-391). A fixed clock makes Retry-After exact.
    clock = [1_000_000.0]
    monkeypatch.setattr(_basefakesocket, "time", SimpleNamespace(time=lambda: clock[0]))
    redis = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(rate_limit, "shared_redis_client", lambda: redis)
    monkeypatch.setattr(settings, "rate_limit_submissions_per_minute", 2)
    assert _import(hosted_store, {"schema": "nope"}).status_code == 422
    assert _import(hosted_store, bundle).status_code == 201
    resp = _import(hosted_store, bundle)
    assert resp.status_code == 429
    assert resp.json()["detail"] == t_en("rate_limit.exceeded", retry_after=60)
    clock[0] += 2
    resp = _import(hosted_store, bundle)
    assert resp.status_code == 429
    assert resp.json()["detail"] == t_en("rate_limit.exceeded", retry_after=58)
    assert _count(hosted_store, JobModel) == 1
