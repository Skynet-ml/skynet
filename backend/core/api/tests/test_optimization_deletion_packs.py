"""Deleting runs deletes the kept folder pack that no stored run uses (SKY-1005)."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from ...config import settings
from ...models.hardening import HardeningJobModel
from ...storage.models import JobModel
from ...storage.tests.test_remote_jobstore import SQLiteJobStore
from ...storage.tree_purge import TreePurgeError
from ...worker.local_folder import FRESH_PACK_SECONDS, kept_snapshot
from ..routers.optimizations import create_optimizations_router
from ..routers.optimizations.deletion import RETRYABLE_HARDENING_MAX_AGE, UNPINNED_RUN_MAX_AGE
from .conftest import bypass_auth
from .mocks import _BaseFakeJobStore

COMMIT = "a" * 40
OTHER = "b" * 40


@pytest.fixture(autouse=True)
def local_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Run each test as the desktop local stack, with its own state folder.

    Args:
        monkeypatch: Restores the settings after the test.
        tmp_path: Scratch folder.

    Returns:
        The local state folder.
    """
    state = tmp_path / "state"
    monkeypatch.setattr(settings, "local_mode", True)
    monkeypatch.setattr(settings, "local_state_dir", str(state))
    return state


@pytest.fixture
def store() -> _BaseFakeJobStore:
    """Provide a fresh fake job store.

    Returns:
        An empty store.
    """
    return _BaseFakeJobStore()


def _client(store: Any) -> TestClient:
    """Build a client over only the optimizations router.

    Args:
        store: The job store the routes use.

    Returns:
        The client.
    """
    app = FastAPI()
    app.include_router(create_optimizations_router(job_store=store, get_worker_ref=lambda: None))
    bypass_auth(app)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def client(store: _BaseFakeJobStore) -> TestClient:
    """Build a client over the fake job store.

    Args:
        store: The fake job store the routes use.

    Returns:
        The client.
    """
    return _client(store)


@pytest.fixture
def sqlite_store() -> Iterator[SQLiteJobStore]:
    """Provide the real job store on an in-memory SQLite database.

    Yields:
        The store.
    """
    store = SQLiteJobStore()
    yield store
    store.engine.dispose()


def _pack(commit: str, *, fresh: bool = False) -> Path:
    """Write a kept pack for a commit.

    Args:
        commit: The folder's source id.
        fresh: Keep the write time; by default the pack is older than ``FRESH_PACK_SECONDS``.

    Returns:
        The pack.
    """
    kept = kept_snapshot(commit)
    kept.parent.mkdir(parents=True, exist_ok=True)
    kept.write_bytes(b"pack")
    if not fresh:
        old = time.time() - FRESH_PACK_SECONDS - 60
        os.utime(kept, (old, old))
    return kept


def _local_run(store: _BaseFakeJobStore, optimization_id: str, commit: str | None, **fields: Any) -> None:
    """Store a local folder run.

    Args:
        store: The fake job store.
        optimization_id: The run.
        commit: Its pinned commit, or ``None`` before it pins one.
        **fields: Other row fields, such as ``status``.
    """
    repo: dict[str, Any] = {"provider": "local", "repository": "app", "path": "/tmp/app"}
    if commit is not None:
        repo["commit"] = commit
    store.seed_job(optimization_id, payload={"target": {"kind": "repo", "repo": repo}}, **fields)


def test_the_pack_stays_while_a_run_uses_it_and_goes_with_the_last_run(
    client: TestClient, store: _BaseFakeJobStore
) -> None:
    """Two runs on one commit: the first delete keeps the pack, the second deletes it."""
    _local_run(store, "first", COMMIT)
    _local_run(store, "second", COMMIT)
    kept = _pack(COMMIT)

    assert client.delete("/optimizations/first").status_code == 200
    assert kept.is_file()

    assert client.delete("/optimizations/second").status_code == 200
    assert not kept.exists()


def test_bulk_delete_keeps_a_used_pack_and_deletes_an_unused_one(client: TestClient, store: _BaseFakeJobStore) -> None:
    """A bulk delete deletes only the packs whose commits no remaining run stores."""
    _local_run(store, "first", COMMIT)
    _local_run(store, "second", COMMIT)
    _local_run(store, "other", OTHER)
    used, unused = _pack(COMMIT), _pack(OTHER)

    response = client.post("/optimizations/bulk-delete", json={"optimization_ids": ["first", "other"]})

    assert sorted(response.json()["deleted"]) == ["first", "other"]
    assert used.is_file()
    assert not unused.exists()

    response = client.post("/optimizations/bulk-delete", json={"optimization_ids": ["second"]})

    assert response.json()["deleted"] == ["second"]
    assert not used.exists()


def test_a_starting_local_run_without_a_commit_keeps_every_pack(client: TestClient, store: _BaseFakeJobStore) -> None:
    """A run that has not stored its commit yet may use any pack, so none is deleted."""
    _local_run(store, "done", COMMIT)
    _local_run(store, "starting", None, status="running")
    kept = _pack(COMMIT)

    assert client.delete("/optimizations/done").status_code == 200

    assert kept.is_file()


@pytest.mark.parametrize(
    ("age", "kept"),
    [
        (UNPINNED_RUN_MAX_AGE + timedelta(hours=1), False),
        (UNPINNED_RUN_MAX_AGE - timedelta(hours=1), True),
    ],
    ids=["older-than-a-day", "younger-than-a-day"],
)
@pytest.mark.parametrize("zone", [UTC, None], ids=["aware", "naive"])
def test_a_local_run_without_a_commit_keeps_the_packs_only_for_a_day(
    client: TestClient, store: _BaseFakeJobStore, age: timedelta, kept: bool, zone: Any
) -> None:
    """A run that has not stored its commit after a day is stuck, so it no longer keeps every pack."""
    created = (datetime.now(UTC) - age).replace(tzinfo=zone).isoformat()
    _local_run(store, "done", COMMIT)
    _local_run(store, "stuck", None, status="running", created_at=created)
    pack = _pack(COMMIT)

    assert client.delete("/optimizations/done").status_code == 200

    assert pack.is_file() is kept


def test_a_long_queued_run_keeps_the_pack_it_just_wrote(client: TestClient, store: _BaseFakeJobStore) -> None:
    """A run created over a day ago that packs the folder only now keeps its fresh pack."""
    created = (datetime.now(UTC) - UNPINNED_RUN_MAX_AGE - timedelta(hours=1)).isoformat()
    _local_run(store, "done", COMMIT)
    _local_run(store, "queued", None, status="pending", created_at=created)
    kept = _pack(COMMIT, fresh=True)

    assert client.delete("/optimizations/done").status_code == 200

    assert kept.is_file()


@pytest.mark.parametrize("created", ["not a time", None, 0], ids=["unreadable", "missing", "not-a-string"])
def test_a_local_run_without_a_commit_or_a_readable_start_time_keeps_every_pack(
    client: TestClient, store: _BaseFakeJobStore, created: Any
) -> None:
    """A start time the cleanup cannot read counts as recent, so the pack stays."""
    _local_run(store, "done", COMMIT)
    _local_run(store, "starting", None, status="running", created_at=created)
    kept = _pack(COMMIT)

    assert client.delete("/optimizations/done").status_code == 200

    assert kept.is_file()


def test_a_stopped_local_run_without_a_commit_does_not_keep_a_pack(
    client: TestClient, store: _BaseFakeJobStore
) -> None:
    """A run that stopped before it pinned a commit uses no pack."""
    _local_run(store, "done", COMMIT)
    _local_run(store, "failed_early", None, status="failed")
    kept = _pack(COMMIT)

    assert client.delete("/optimizations/done").status_code == 200

    assert not kept.exists()


def test_a_refused_delete_keeps_the_pack(client: TestClient, store: _BaseFakeJobStore) -> None:
    """A run that still runs is not deleted, so its pack stays."""
    _local_run(store, "live", COMMIT, status="running")
    kept = _pack(COMMIT)

    assert client.delete("/optimizations/live").status_code == 409

    assert kept.is_file()


def test_a_failed_single_delete_keeps_the_pack(
    client: TestClient, store: _BaseFakeJobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the run row stays, its pack stays: the pack goes only after the delete succeeds."""
    _local_run(store, "done", COMMIT)
    kept = _pack(COMMIT)

    def fail(optimization_id: str) -> None:
        """Fail as a purge of the run's tree data fails.

        Args:
            optimization_id: The run.

        Raises:
            TreePurgeError: Always.
        """
        raise TreePurgeError(f"could not purge {optimization_id}")

    monkeypatch.setattr(store, "delete_job", fail)

    assert client.delete("/optimizations/done").status_code == 503

    assert kept.is_file()


@pytest.mark.parametrize("error", [TreePurgeError("purge failed"), RuntimeError("database down")])
def test_a_failed_bulk_delete_keeps_the_pack(
    client: TestClient, store: _BaseFakeJobStore, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """Both bulk failure branches skip the runs and keep their pack."""
    _local_run(store, "done", COMMIT)
    kept = _pack(COMMIT)

    def fail(optimization_ids: list[str]) -> None:
        """Fail the batch delete.

        Args:
            optimization_ids: The runs.

        Raises:
            Exception: The parametrized error, always.
        """
        raise error

    monkeypatch.setattr(store, "delete_jobs", fail)

    response = client.post("/optimizations/bulk-delete", json={"optimization_ids": ["done"]})

    assert response.json()["deleted"] == []
    assert [skip["optimization_id"] for skip in response.json()["skipped"]] == ["done"]
    assert kept.is_file()


def test_a_recent_pack_stays_after_its_last_run_is_deleted(client: TestClient, store: _BaseFakeJobStore) -> None:
    """A pack written or reused in the last minutes may belong to a run that stores its commit later."""
    _local_run(store, "done", COMMIT)
    kept = _pack(COMMIT, fresh=True)

    assert client.delete("/optimizations/done").status_code == 200

    assert kept.is_file()


def test_the_cleanup_lists_runs_without_the_per_row_counts(
    client: TestClient, store: _BaseFakeJobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real store runs count queries for each row unless ``with_counts`` is off."""
    _local_run(store, "done", COMMIT)
    _local_run(store, "other", OTHER)
    _pack(COMMIT)
    calls: list[dict[str, Any]] = []
    list_jobs = store.list_jobs

    def spy(**kwargs: Any) -> list[dict]:
        """Record the call and list the runs.

        Args:
            **kwargs: The list filters.

        Returns:
            The runs.
        """
        calls.append(kwargs)
        return list_jobs(**kwargs)

    monkeypatch.setattr(store, "list_jobs", spy)
    # The fake count lists the runs itself; a direct count keeps the spy on the cleanup's own calls.
    monkeypatch.setattr(store, "count_jobs", lambda **kwargs: len(list_jobs(limit=10**9)))

    assert client.delete("/optimizations/done").status_code == 200

    assert calls
    assert all(call.get("with_counts") is False for call in calls), calls


def test_a_hosted_run_on_the_same_commit_does_not_touch_the_pack(client: TestClient, store: _BaseFakeJobStore) -> None:
    """Only a local folder run names a kept pack."""
    store.seed_job(
        "hosted",
        payload={"target": {"kind": "repo", "repo": {"provider": "github", "repository": "o/r", "commit": COMMIT}}},
    )
    _local_run(store, "local", COMMIT)
    kept = _pack(COMMIT)

    assert client.delete("/optimizations/hosted").status_code == 200

    assert kept.is_file()


def test_a_stored_commit_that_is_not_a_sha_deletes_nothing_outside_the_pack_folder(
    client: TestClient, store: _BaseFakeJobStore, local_state: Path
) -> None:
    """A stored commit with a path in it never names a file to delete."""
    victim = local_state / "victim.tgz"
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_bytes(b"keep")
    _local_run(store, "bad", "../victim")

    assert client.delete("/optimizations/bad").status_code == 200

    assert victim.is_file()


def test_away_from_the_local_stack_no_pack_is_deleted(
    client: TestClient, store: _BaseFakeJobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cloud service keeps no folder packs, so it never deletes one."""
    _local_run(store, "done", COMMIT)
    kept = _pack(COMMIT)
    monkeypatch.setattr(settings, "local_mode", False)

    assert client.delete("/optimizations/done").status_code == 200

    assert kept.is_file()


def _sqlite_local_run(store: SQLiteJobStore, optimization_id: str, commit: str) -> None:
    """Store a stopped local folder run in the real store.

    Args:
        store: The SQLite job store.
        optimization_id: The run.
        commit: Its pinned commit.
    """
    repo = {"provider": "local", "repository": "app", "path": "/tmp/app", "commit": commit}
    store.create_job(optimization_id)
    store.update_job(
        optimization_id, status="success", username="alice", payload={"target": {"kind": "repo", "repo": repo}}
    )


def test_the_real_store_keeps_a_used_pack_and_deletes_it_with_the_last_run(sqlite_store: SQLiteJobStore) -> None:
    """``count_jobs``, ``list_jobs`` and ``payload_targets`` of the SQL store give the same result."""
    _sqlite_local_run(sqlite_store, "first", COMMIT)
    _sqlite_local_run(sqlite_store, "second", COMMIT)
    kept = _pack(COMMIT)
    client = _client(sqlite_store)

    assert client.delete("/optimizations/first").status_code == 200
    assert kept.is_file()

    assert client.delete("/optimizations/second").status_code == 200
    assert not kept.exists()


@pytest.mark.parametrize(("fresh", "kept"), [(False, False), (True, True)], ids=["old-pack", "fresh-pack"])
def test_the_real_store_drops_a_stuck_run_after_a_day(sqlite_store: SQLiteJobStore, fresh: bool, kept: bool) -> None:
    """The SQL store's ``created_at`` ends the hold of a running run that never stored its commit."""
    _sqlite_local_run(sqlite_store, "done", COMMIT)
    sqlite_store.create_job("stuck")
    sqlite_store.update_job(
        "stuck",
        status="running",
        username="alice",
        payload={"target": {"kind": "repo", "repo": {"provider": "local", "repository": "app", "path": "/tmp/app"}}},
    )
    with Session(sqlite_store.engine) as session:
        session.query(JobModel).filter_by(optimization_id="stuck").update(
            {"created_at": datetime.now(UTC) - UNPINNED_RUN_MAX_AGE - timedelta(hours=1)}
        )
        session.commit()
    pack = _pack(COMMIT, fresh=fresh)

    assert _client(sqlite_store).delete("/optimizations/done").status_code == 200

    assert pack.is_file() is kept


def _hardening_job(store: SQLiteJobStore, status: str, commit: str | None, age: timedelta = timedelta()) -> None:
    """Store a metric-hardening job on a local folder.

    Args:
        store: The SQLite job store.
        status: The job status.
        commit: The commit its first pass pinned, or ``None`` before it.
        age: How long ago the job last changed.
    """
    repo: dict[str, Any] = {"provider": "local", "repository": "app", "path": "/tmp/app"}
    if commit is not None:
        repo["commit"] = commit
    with Session(store.engine) as session:
        session.add(
            HardeningJobModel(
                username="alice",
                draft_id=f"draft-{status}",
                status=status,
                job={"target": {"kind": "repo", "repo": repo}},
                updated_at=datetime.now(UTC) - age,
            )
        )
        session.commit()


@pytest.mark.parametrize(("status", "commit"), [("waiting_review", COMMIT), ("preflight", None)])
def test_an_unfinished_hardening_job_keeps_the_pack(
    sqlite_store: SQLiteJobStore, status: str, commit: str | None
) -> None:
    """A hardening job's later passes reuse the pack of its pinned commit, so the pack stays."""
    _sqlite_local_run(sqlite_store, "done", COMMIT)
    _hardening_job(sqlite_store, status, commit)
    kept = _pack(COMMIT)

    assert _client(sqlite_store).delete("/optimizations/done").status_code == 200

    assert kept.is_file()


@pytest.mark.parametrize(
    ("status", "kept"),
    [("failed", True), ("stale", True), ("rejected", True), ("done", False), ("budget_stop", False)],
)
def test_a_retryable_hardening_job_keeps_its_pack(sqlite_store: SQLiteJobStore, status: str, kept: bool) -> None:
    """A retry reuses the pinned commit's pack; a job that ended for good needs it no more."""
    _sqlite_local_run(sqlite_store, "done", COMMIT)
    _hardening_job(sqlite_store, status, COMMIT)
    pack = _pack(COMMIT)

    assert _client(sqlite_store).delete("/optimizations/done").status_code == 200

    assert pack.is_file() is kept


def test_a_retryable_hardening_job_without_a_commit_keeps_no_pack(sqlite_store: SQLiteJobStore) -> None:
    """A job that failed before it pinned a commit packs the folder again on retry."""
    _sqlite_local_run(sqlite_store, "done", COMMIT)
    _hardening_job(sqlite_store, "preflight_failed", None)
    pack = _pack(COMMIT)

    assert _client(sqlite_store).delete("/optimizations/done").status_code == 200

    assert not pack.exists()


@pytest.mark.parametrize("status", ["failed", "preflight_failed", "stale", "rejected"])
@pytest.mark.parametrize(
    ("age", "kept"),
    [
        (RETRYABLE_HARDENING_MAX_AGE + timedelta(days=1), False),
        (RETRYABLE_HARDENING_MAX_AGE - timedelta(days=1), True),
    ],
    ids=["older-than-30-days", "younger-than-30-days"],
)
def test_a_retryable_hardening_job_keeps_its_pack_for_30_days(
    sqlite_store: SQLiteJobStore, status: str, age: timedelta, kept: bool
) -> None:
    """A failed, stale or rejected job untouched for 30 days is not retried, so its pack can go."""
    _sqlite_local_run(sqlite_store, "done", COMMIT)
    _hardening_job(sqlite_store, status, COMMIT, age)
    pack = _pack(COMMIT)

    assert _client(sqlite_store).delete("/optimizations/done").status_code == 200

    assert pack.is_file() is kept


@pytest.mark.parametrize(
    ("status", "commit"),
    [("waiting_review", COMMIT), ("waiting_fix", COMMIT), ("running", COMMIT), ("preflight", None)],
)
def test_an_active_hardening_job_keeps_the_pack_at_any_age(
    sqlite_store: SQLiteJobStore, status: str, commit: str | None
) -> None:
    """The 30-day bound is only for retryable jobs; a working job keeps the pack however old it is."""
    _sqlite_local_run(sqlite_store, "done", COMMIT)
    _hardening_job(sqlite_store, status, commit, RETRYABLE_HARDENING_MAX_AGE + timedelta(days=1))
    pack = _pack(COMMIT)

    assert _client(sqlite_store).delete("/optimizations/done").status_code == 200

    assert pack.is_file()
