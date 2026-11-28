"""Deleting runs deletes the kept folder pack that no stored run uses (SKY-1005)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ...config import settings
from ...worker.local_folder import kept_snapshot
from ..routers.optimizations import create_optimizations_router
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


@pytest.fixture
def client(store: _BaseFakeJobStore) -> TestClient:
    """Build a client over only the optimizations router.

    Args:
        store: The fake job store the routes use.

    Returns:
        The client.
    """
    app = FastAPI()
    app.include_router(create_optimizations_router(job_store=store, get_worker_ref=lambda: None))
    bypass_auth(app)
    return TestClient(app, raise_server_exceptions=False)


def _pack(commit: str) -> Path:
    """Write a kept pack for a commit.

    Args:
        commit: The folder's source id.

    Returns:
        The pack.
    """
    kept = kept_snapshot(commit)
    kept.parent.mkdir(parents=True, exist_ok=True)
    kept.write_bytes(b"pack")
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


def test_a_stopped_local_run_without_a_commit_does_not_keep_a_pack(
    client: TestClient, store: _BaseFakeJobStore
) -> None:
    """A run that stopped before it pinned a commit uses no pack."""
    _local_run(store, "done", COMMIT)
    _local_run(store, "failed_early", None, status="failed")
    kept = _pack(COMMIT)

    assert client.delete("/optimizations/done").status_code == 200

    assert not kept.exists()


def test_a_failed_delete_keeps_the_pack(client: TestClient, store: _BaseFakeJobStore) -> None:
    """The pack goes only after the run row is deleted."""
    _local_run(store, "live", COMMIT, status="running")
    kept = _pack(COMMIT)

    assert client.delete("/optimizations/live").status_code == 409

    assert kept.is_file()


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
