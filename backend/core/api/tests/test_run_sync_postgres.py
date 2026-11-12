"""Import a desktop run bundle into a real PostgreSQL database.

SQLite stores JSON as text, so only PostgreSQL proves the imported result and
metrics land in JSONB and read back unchanged. CI provides a disposable
database through ``APP_BUDGET_TEST_DB_URL``; without it the module is skipped.
Each test works in its own schema and drops it afterwards.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from pgvector.sqlalchemy import Vector
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import core.storage.remote as remote_mod

from ...config import app_env
from ...service_gateway import run_sync
from ...storage.models import Base
from ...storage.remote import RemoteDBJobStore
from ..routers.optimizations.sync import _check_readable

TEST_DB_URL = app_env("APP_BUDGET_TEST_DB_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL or not TEST_DB_URL.startswith("postgresql"),
    reason="Set APP_BUDGET_TEST_DB_URL to an explicitly disposable PostgreSQL database.",
)


class _SchemaJobStore(RemoteDBJobStore):
    """RemoteDBJobStore bound to one throwaway schema, without pgvector tables."""

    def __init__(self, engine: Any) -> None:
        """Use ``engine`` and create every table that needs no pgvector column.

        Args:
            engine: Engine whose search path is the test schema.
        """
        self._engine = engine
        tables = [
            table
            for table in Base.metadata.sorted_tables
            if not any(isinstance(column.type, Vector) for column in table.columns)
        ]
        Base.metadata.create_all(engine, tables=tables)
        self._session_factory = sessionmaker(bind=engine)
        self._checkpoints = remote_mod.PostgresCheckpointBlobStore(engine)
        self._agent_runs = remote_mod.PostgresAgentRunStore(engine)


@pytest.fixture
def store() -> Iterator[_SchemaJobStore]:
    """Yield a job store in a fresh schema, dropped after the test."""
    schema = f"run_sync_test_{uuid4().hex}"
    admin = create_engine(TEST_DB_URL)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(TEST_DB_URL, connect_args={"options": f"-csearch_path={schema}"})
    try:
        yield _SchemaJobStore(engine)
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def _bundle() -> dict[str, Any]:
    """Build a v1 bundle like the desktop export writes.

    Returns:
        The bundle mapping.
    """
    return {
        "schema": run_sync.SCHEMA_VERSION,
        "source": {"app_version": "test", "local_mode": True},
        "optimization": {
            "source_optimization_id": "local-run-1",
            "optimization_type": "blackbox",
            "name": "Desk run",
            "description": "Support triage",
            "payload_overview": {
                "name": "Desk run",
                "optimization_type": "blackbox",
                "optimizer_name": "gepa",
                "model_name": "openai/gpt-4o-mini",
            },
        },
        "job": {
            "status": "success",
            "message": "Finished",
            "created_at": "2026-10-01T10:00:00+00:00",
            "started_at": "2026-10-01T10:00:05+00:00",
            "completed_at": "2026-10-01T10:04:00+00:00",
            "latest_metrics": {"baseline_test_metric": 0.41, "optimized_test_metric": 0.82},
            "result": {
                "optimizer_name": "gepa",
                "strategy_mode": "single",
                "engine_used": "gepa",
                "baseline_score": 0.41,
                "best_score": 0.82,
                "metric_improvement": 0.41,
                "best_candidate": "Answer briefly.",
                "runtime_seconds": 12.0,
            },
        },
        "events": [
            {"timestamp": "2026-10-01T10:01:00+00:00", "event": "baseline", "metrics": {"baseline_test_metric": 0.41}},
        ],
    }


def test_import_stores_jsonb_that_reads_back(store: _SchemaJobStore) -> None:
    """The imported result and metrics are JSONB objects and read back with the same values."""
    new_id = run_sync.import_run(store, "bob", _bundle())
    job = store.get_job(new_id)
    assert job["username"] == "bob"
    assert job["status"] == "success"
    assert job["payload"] is None
    assert job["result"]["best_score"] == 0.82
    assert job["latest_metrics"] == {"baseline_test_metric": 0.41, "optimized_test_metric": 0.82}
    with store.engine.connect() as connection:
        kinds = connection.execute(
            text("SELECT jsonb_typeof(result), jsonb_typeof(latest_metrics) FROM jobs WHERE optimization_id = :id"),
            {"id": new_id},
        ).one()
    assert tuple(kinds) == ("object", "object")
    assert len(store.get_progress_events(new_id)) == 1


def test_too_deep_bundle_writes_no_row(store: _SchemaJobStore) -> None:
    """A bundle past the depth cap is refused before PostgreSQL sees it."""
    bundle = _bundle()
    nested: Any = 1
    for _ in range(run_sync.MAX_BUNDLE_DEPTH + 1):
        nested = [nested]
    bundle["job"]["latest_metrics"] = {"deep": nested}
    with pytest.raises(run_sync.RunSyncError) as excinfo:
        run_sync.import_run(store, "bob", bundle)
    assert excinfo.value.code == "sync.invalid_bundle"
    with store.engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM jobs")).scalar() == 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b["job"]["latest_metrics"].update(optimized_test_metric=float("nan")),
        lambda b: b["job"].update(message="Fin\x00ished"),
        lambda b: b["job"]["latest_metrics"].update(note="a\x00b"),
    ],
    ids=["nan-metric", "nul-message", "nul-metric-string"],
)
def test_values_jsonb_rejects_are_refused_before_the_write(store: _SchemaJobStore, mutate: Any) -> None:
    """NaN and NUL, which PostgreSQL would reject with a server error, give a sync error and no row."""
    bundle = _bundle()
    mutate(bundle)
    with pytest.raises(run_sync.RunSyncError) as excinfo:
        run_sync.import_run(store, "bob", json.loads(json.dumps(bundle)))
    assert excinfo.value.code == "sync.invalid_bundle"
    with store.engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM jobs")).scalar() == 0


def _row_counts(store: _SchemaJobStore) -> tuple[int, int]:
    """Count the job and progress event rows in the test schema.

    Args:
        store: The schema-scoped job store.

    Returns:
        The job count and the progress event count.
    """
    with store.engine.connect() as connection:
        jobs = connection.execute(text("SELECT count(*) FROM jobs")).scalar()
        events = connection.execute(text("SELECT count(*) FROM job_progress_events")).scalar()
    return jobs, events


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b["job"].update(created_at="0001-01-01T00:00:00+14:00"),
        lambda b: b["events"][0].update(timestamp="9999-12-31T23:59:59-14:00"),
    ],
    ids=["created-before-year-1", "event-after-9999"],
)
def test_out_of_range_timestamps_write_no_row(store: _SchemaJobStore, mutate: Any) -> None:
    """A time outside the stored range gives a sync error and writes no job or event row."""
    bundle = _bundle()
    mutate(bundle)
    with pytest.raises(run_sync.RunSyncError) as excinfo:
        run_sync.import_run(store, "bob", bundle, check_row=_check_readable)
    assert excinfo.value.code == "sync.invalid_bundle"
    assert _row_counts(store) == (0, 0)


def test_unreadable_overview_rolls_back_job_and_events(store: _SchemaJobStore) -> None:
    """When the read check fails after the flush, the job and its events are rolled back."""
    bundle = _bundle()
    bundle["optimization"]["payload_overview"]["model_name"] = 7
    with pytest.raises(run_sync.RunSyncError) as excinfo:
        run_sync.import_run(store, "bob", bundle, check_row=_check_readable)
    assert excinfo.value.code == "sync.invalid_bundle"
    assert _row_counts(store) == (0, 0)


def test_overview_update_keeps_imported_storage(store: _SchemaJobStore) -> None:
    """A pin-style overview write and a result update keep counting the imported job fields."""
    bundle = _bundle()
    bundle["job"]["terminal_evidence"] = {"blob": "x" * 4096}
    new_id = run_sync.import_run(store, "bob", bundle, check_row=_check_readable)
    assert store.get_job(new_id)["stored_bytes"] >= 4096
    overview = store.get_job(new_id)["payload_overview"]
    store.set_payload_overview(new_id, {**overview, "pinned": True})
    assert store.get_job(new_id)["stored_bytes"] >= 4096
    store.update_job(new_id, result=store.get_job(new_id)["result"])
    assert store.get_job(new_id)["stored_bytes"] >= 4096
