"""Tests for resume: POST /optimizations/{id}/resume and the ``resumable`` flag.

Resume is only offered for a run that stopped mid-optimization with a saved GEPA
checkpoint (terminal failed/cancelled, checkpoint present, attempts below the
cap). Everything else keeps the existing Restart path. These tests pin the
endpoint's preconditions and the in-place re-queue, plus the :func:`is_resumable`
discriminator that drives the Resume-vs-Restart affordance.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ...config import settings
from ...constants import WORKER_RESTART_CAUSE
from ...storage.tests.test_remote_jobstore import SQLiteJobStore, _fund_checkpoint, _fund_without_checkpoint
from ..auth import AuthenticatedUser
from ..routers._helpers import is_pausable, is_resumable, pausable_id_flags, resumable_id_flags
from ..routers.optimizations import create_optimizations_router
from .conftest import bypass_auth
from .mocks import _BaseFakeJobStore


@pytest.fixture
def store() -> _BaseFakeJobStore:
    """Provide a fresh fake job store per test."""
    return _BaseFakeJobStore()


@pytest.fixture
def client(store: _BaseFakeJobStore) -> TestClient:
    """Build a TestClient exposing only the optimizations router over the fake store.

    Args:
        store: Fake job store wired into the router factory.

    Returns:
        A ``TestClient`` over a minimal FastAPI app.
    """
    app = FastAPI()
    app.include_router(create_optimizations_router(job_store=store, get_worker_ref=lambda: None))
    bypass_auth(app)
    return TestClient(app, raise_server_exceptions=False)


def _seed_resumable(store: _BaseFakeJobStore, oid: str, *, status: str = "failed", attempts: int = 0) -> str:
    """Seed a terminal job that has a saved checkpoint — the resumable case."""
    store.seed_job(oid, status=status, attempts=attempts, username="alice")
    store.save_gepa_checkpoint(oid, b"GEPA-STATE", iteration=7)
    return oid


def test_resume_requeues_failed_run_in_place(client: TestClient, store: _BaseFakeJobStore) -> None:
    """A failed run with a checkpoint resumes in place: 202, same id, pending, attempts+1."""
    _seed_resumable(store, "r1", attempts=0)
    resp = client.post("/optimizations/r1/resume")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["optimization_id"] == "r1"
    assert body["status"] == "pending"
    job = store.get_job("r1")
    assert job["status"] == "pending"
    assert job["attempts"] == 1


def test_resume_allows_cancelled_run(client: TestClient, store: _BaseFakeJobStore) -> None:
    """A cancelled run with a checkpoint is resumable too."""
    _seed_resumable(store, "c1", status="cancelled")
    resp = client.post("/optimizations/c1/resume")
    assert resp.status_code == 202, resp.text


def test_resume_without_checkpoint_is_not_resumable(client: TestClient, store: _BaseFakeJobStore) -> None:
    """A failed run with no checkpoint is a regular failure → 409, keeps Restart."""
    store.seed_job("nf", status="failed", username="alice")
    resp = client.post("/optimizations/nf/resume")
    assert resp.status_code == 409
    assert "resume point" in resp.json()["detail"]


@pytest.mark.parametrize("status", ["running", "pending", "validating", "success"])
def test_resume_rejects_non_terminal_failure(client: TestClient, store: _BaseFakeJobStore, status: str) -> None:
    """Only failed/cancelled runs can be resumed, even with a checkpoint present."""
    oid = f"s_{status}"
    store.seed_job(oid, status=status, username="alice")
    store.save_gepa_checkpoint(oid, b"STATE", iteration=1)
    resp = client.post(f"/optimizations/{oid}/resume")
    assert resp.status_code == 409
    assert "partway through" in resp.json()["detail"]


def test_resume_blocked_at_attempt_cap(client: TestClient, store: _BaseFakeJobStore) -> None:
    """At ``job_max_attempts`` the run is no longer resumable (shared cap)."""
    _seed_resumable(store, "ex", attempts=settings.job_max_attempts)
    resp = client.post("/optimizations/ex/resume")
    assert resp.status_code == 409
    assert "maximum attempts" in resp.json()["detail"]


def test_resume_unknown_id_404(client: TestClient) -> None:
    """Resuming an unknown optimization is a 404."""
    resp = client.post("/optimizations/ghost/resume")
    assert resp.status_code == 404


def _seed_grid(store: _BaseFakeJobStore, oid: str, *, status: str = "success") -> str:
    """Seed a terminal grid whose pair 0 succeeded and pair 1 failed."""
    store.seed_job(
        oid,
        status=status,
        username="alice",
        payload_overview={"optimization_type": "grid_search"},
        result={
            "pair_results": [
                {"pair_index": 0, "generation_model": "g0", "reflection_model": "r0", "optimized_test_metric": 0.8},
                {"pair_index": 1, "generation_model": "g1", "reflection_model": "r1", "error": "boom"},
            ],
            "total_pairs": 2,
        },
    )
    return oid


def test_grids_are_never_whole_job_resumable(store: _BaseFakeJobStore) -> None:
    """A grid is resumed per pair (in its results), so the top-level flag stays False."""
    _seed_grid(store, "g", status="failed")
    store.save_grid_pair_result("g", 0, {"pair_index": 0})
    store.save_gepa_checkpoint("g", b"S", iteration=1, pair_index=1)
    assert is_resumable(store, store.get_job("g")) is False


@pytest.mark.parametrize("action", ["resume", "restart", "retry"])
def test_stored_grid_cannot_be_requeued(client: TestClient, store: _BaseFakeJobStore, action: str) -> None:
    """A stored grid search stays readable but is never queued again.

    Args:
        client: Router client over the fake store.
        store: Fake job store holding the stored grid.
        action: Lifecycle action that would queue new work.
    """
    _seed_grid(store, "grid1", status="failed")
    store.save_gepa_checkpoint("grid1", b"STATE", iteration=1)

    resp = client.post(f"/optimizations/grid1/{action}")

    assert resp.status_code == 409, resp.text
    assert store.get_job("grid1")["status"] == "failed"


def test_stored_grid_cannot_be_cloned(client: TestClient, store: _BaseFakeJobStore) -> None:
    """Cloning a stored grid search is refused instead of creating a run that would fail.

    Args:
        client: Router client over the fake store.
        store: Fake job store holding the stored grid.
    """
    _seed_grid(store, "grid2", status="success")
    store.seed_job(
        "grid2",
        status="success",
        username="alice",
        payload={"generation_models": [{"name": "g0"}]},
        payload_overview={"optimization_type": "grid_search"},
    )

    resp = client.post("/optimizations/grid2/clone", json={"count": 1})

    assert resp.status_code == 409, resp.text


def test_is_resumable_discriminator(store: _BaseFakeJobStore) -> None:
    """The flag is True only for terminal-failure + checkpoint + attempts under cap."""
    store.seed_job("a", status="failed", attempts=0, username="alice")
    assert is_resumable(store, store.get_job("a")) is False  # no checkpoint
    store.save_gepa_checkpoint("a", b"x", 1)
    assert is_resumable(store, store.get_job("a")) is True

    store.seed_job("b", status="success", attempts=0, username="alice")
    store.save_gepa_checkpoint("b", b"x", 1)
    assert is_resumable(store, store.get_job("b")) is False  # wrong status

    store.seed_job("c", status="failed", attempts=settings.job_max_attempts, username="alice")
    store.save_gepa_checkpoint("c", b"x", 1)
    assert is_resumable(store, store.get_job("c")) is False  # exhausted


def test_pause_running_run_in_place(client: TestClient, store: _BaseFakeJobStore) -> None:
    """Pausing a running run with a checkpoint flips it to ``paused`` in place (200)."""
    store.seed_job("p1", status="running", username="alice")
    store.save_gepa_checkpoint("p1", b"STATE", iteration=3)
    resp = client.post("/optimizations/p1/pause")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "paused"
    job = store.get_job("p1")
    assert job["status"] == "paused"
    assert job.get("completed_at") is not None


def test_pause_without_checkpoint_is_rejected(client: TestClient, store: _BaseFakeJobStore) -> None:
    """A running run with no checkpoint can't be paused yet — 409, would strand the resume."""
    store.seed_job("p2", status="running", username="alice")
    resp = client.post("/optimizations/p2/pause")
    assert resp.status_code == 409
    assert "checkpoint" in resp.json()["detail"].lower()


@pytest.mark.parametrize("status", ["pending", "validating", "failed", "cancelled", "success"])
def test_pause_rejects_non_running(client: TestClient, store: _BaseFakeJobStore, status: str) -> None:
    """Only a running optimization can be paused, even with a checkpoint present."""
    oid = f"p_{status}"
    store.seed_job(oid, status=status, username="alice")
    store.save_gepa_checkpoint(oid, b"STATE", iteration=1)
    resp = client.post(f"/optimizations/{oid}/pause")
    assert resp.status_code == 409
    assert "running" in resp.json()["detail"].lower()


def test_pause_unknown_id_404(client: TestClient) -> None:
    """Pausing an unknown optimization is a 404."""
    assert client.post("/optimizations/ghost/pause").status_code == 404


def test_resume_paused_run_does_not_consume_an_attempt(client: TestClient, store: _BaseFakeJobStore) -> None:
    """A paused run resumes (202) without bumping attempts — manual pause is cap-exempt."""
    _seed_resumable(store, "pr", status="paused", attempts=2)
    resp = client.post("/optimizations/pr/resume")
    assert resp.status_code == 202, resp.text
    job = store.get_job("pr")
    assert job["status"] == "pending"
    assert job["attempts"] == 2  # unchanged: paused resume is exempt from the cap


def test_paused_run_is_resumable_even_at_attempt_cap(store: _BaseFakeJobStore) -> None:
    """A paused run stays resumable at the attempt cap, unlike failed/cancelled."""
    _seed_resumable(store, "pcap", status="paused", attempts=settings.job_max_attempts)
    assert is_resumable(store, store.get_job("pcap")) is True


def test_is_pausable_discriminator(store: _BaseFakeJobStore) -> None:
    """Pausable only for a running single run that already has a saved checkpoint."""
    store.seed_job("rp", status="running", username="alice")
    assert is_pausable(store, store.get_job("rp")) is False  # no checkpoint yet
    store.save_gepa_checkpoint("rp", b"x", 1)
    assert is_pausable(store, store.get_job("rp")) is True

    store.seed_job("fp", status="failed", username="alice")
    store.save_gepa_checkpoint("fp", b"x", 1)
    assert is_pausable(store, store.get_job("fp")) is False  # not running

    _seed_grid(store, "gp", status="running")
    store.save_gepa_checkpoint("gp", b"x", 1, pair_index=0)
    assert is_pausable(store, store.get_job("gp")) is False  # grids pause per the whole-job rule


def test_pausable_id_flags_batches_sidebar_candidates(store: _BaseFakeJobStore) -> None:
    """Flag only running non-grid rows that already have a checkpoint."""
    store.seed_job("ready", status="running", username="alice")
    store.save_gepa_checkpoint("ready", b"x", 1)
    store.seed_job("waiting", status="running", username="alice")
    store.seed_job("done", status="success", username="alice")

    rows = [store.get_job(oid) for oid in ("ready", "waiting", "done")]

    assert pausable_id_flags(store, rows) == {"ready"}


def test_listings_mark_checkpointed_running_job_pausable(
    client: TestClient,
    store: _BaseFakeJobStore,
) -> None:
    """Expose Pause capability in dashboard and sidebar responses."""
    store.seed_job("sidebar-pause", status="running", username="alice")
    store.save_gepa_checkpoint("sidebar-pause", b"x", 1)

    for path in ("/optimizations", "/optimizations/sidebar"):
        response = client.get(path)
        assert response.status_code == 200
        item = next(row for row in response.json()["items"] if row["optimization_id"] == "sidebar-pause")
        assert item["pausable"] is True


def test_resume_budget_stopped_run_continues_without_consuming_an_attempt(
    client: TestClient, store: _BaseFakeJobStore
) -> None:
    """A run stopped at its spending limit continues (202) once raised — cap-exempt like a pause."""
    store.seed_job(
        "bs", status="stopped", stop_reason="budget_reached", attempts=settings.job_max_attempts, username="alice"
    )
    store.save_gepa_checkpoint("bs", b"GEPA-STATE", iteration=7)
    resp = client.post("/optimizations/bs/resume")
    assert resp.status_code == 202, resp.text
    job = store.get_job("bs")
    assert job["status"] == "pending"
    assert job["attempts"] == settings.job_max_attempts


def test_resume_rejects_stopped_run_without_a_budget_reason(client: TestClient, store: _BaseFakeJobStore) -> None:
    """Only a budget stop makes ``stopped`` continuable; other stops stay final."""
    store.seed_job("so", status="stopped", username="alice")
    store.save_gepa_checkpoint("so", b"STATE", iteration=1)
    resp = client.post("/optimizations/so/resume")
    assert resp.status_code == 409
    assert "partway through" in resp.json()["detail"]


def test_budget_stopped_run_is_resumable_even_at_attempt_cap(store: _BaseFakeJobStore) -> None:
    """A budget stop offers Resume at the attempt cap; a plain stop never does."""
    store.seed_job(
        "bcap", status="stopped", stop_reason="budget_reached", attempts=settings.job_max_attempts, username="alice"
    )
    store.save_gepa_checkpoint("bcap", b"x", 1)
    assert is_resumable(store, store.get_job("bcap")) is True
    store.seed_job("scap", status="stopped", username="alice")
    store.save_gepa_checkpoint("scap", b"x", 1)
    assert is_resumable(store, store.get_job("scap")) is False


def test_resume_budget_projected_pause_requires_a_limit_above_the_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refuse to continue a projection pause until the limit clears the projection, then continue cap-free.

    Args:
        monkeypatch: Fixture binding the checkpoint's immutable runtime profile.
    """
    store = SQLiteJobStore()
    store.create_job("pp")
    store.update_job("pp", status="running")
    budgets = _fund_checkpoint(store, "pp", monkeypatch)
    budget_id = store.get_job("pp")["execution_budget_id"]
    budgets.stop_admission(budget_id, "resume-owner", reason="budget_projected")
    store.update_job(
        "pp",
        status="paused",
        stop_reason="budget_projected",
        attempts=settings.job_max_attempts,
        terminal_evidence={
            "budget_projection": {
                "planned_calls": 230,
                "done_calls": 23,
                "spent_cents": "3",
                "projected_cents": 30,
                "limit_cents": 20,
            }
        },
    )
    app = FastAPI()
    app.include_router(create_optimizations_router(job_store=store, get_worker_ref=lambda: None))
    bypass_auth(app, user=AuthenticatedUser(username="resume-owner", role="admin", groups=()))
    client = TestClient(app, raise_server_exceptions=False)

    blocked = client.post("/optimizations/pp/resume")
    assert blocked.status_code == 409, blocked.text
    assert "about $0.30, above its $0.20 limit" in blocked.text
    assert store.get_job("pp")["status"] == "paused"

    budget = budgets.get(budget_id, "resume-owner")
    budgets.update_total(budget_id, "resume-owner", 40, expected_revision=budget.revision)
    resp = client.post("/optimizations/pp/resume")
    assert resp.status_code == 202, resp.text
    job = store.get_job("pp")
    assert job["status"] == "pending"
    assert job["attempts"] == settings.job_max_attempts
    assert budgets.get(budget_id, "resume-owner").state == "attached"


def _stop_as_interrupted(store: SQLiteJobStore, optimization_id: str) -> None:
    """Leave a funded run as the sweep leaves it after a worker restart whose usage never settles, at the cap.

    Args:
        store: Private fixture database.
        optimization_id: Funded run to stop.
    """
    store.update_job(
        optimization_id,
        status="failed",
        stop_reason="interrupted",
        attempts=settings.job_max_attempts,
        recovery={"state": "interrupted", "interrupted_budget_generation": 0, "cause": WORKER_RESTART_CAUSE},
    )


def _owner_client(store: SQLiteJobStore, username: str) -> TestClient:
    """Build a client over the real store, signed in as the run owner.

    Args:
        store: Private fixture database.
        username: Owner of the funded run.

    Returns:
        A ``TestClient`` over a minimal FastAPI app.
    """
    app = FastAPI()
    app.include_router(create_optimizations_router(job_store=store, get_worker_ref=lambda: None))
    bypass_auth(app, user=AuthenticatedUser(username=username, role="admin", groups=()))
    return TestClient(app, raise_server_exceptions=False)


def test_resume_restarts_an_interrupted_run_without_a_checkpoint_in_its_own_budget() -> None:
    """An interrupted run with no checkpoint offers Resume and starts again without using an attempt."""
    store = SQLiteJobStore()
    store.create_job("restart-me")
    _fund_without_checkpoint(store, "restart-me")
    _stop_as_interrupted(store, "restart-me")
    job = store.get_job("restart-me")
    assert is_resumable(store, job) is True
    assert resumable_id_flags(store, [job]) == {"restart-me"}

    resp = _owner_client(store, "restart-owner").post("/optimizations/restart-me/resume")
    assert resp.status_code == 202, resp.text
    job = store.get_job("restart-me")
    assert job["status"] == "pending"
    assert job["attempts"] == settings.job_max_attempts
    assert job["execution_budget_id"] is not None


def test_resume_of_an_interrupted_run_with_a_checkpoint_does_not_use_an_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupted run at the attempt cap still resumes from its checkpoint, and keeps its attempt count.

    Args:
        monkeypatch: Fixture binding the checkpoint's immutable runtime profile.
    """
    store = SQLiteJobStore()
    store.create_job("resume-me")
    _fund_checkpoint(store, "resume-me", monkeypatch)
    _stop_as_interrupted(store, "resume-me")
    assert is_resumable(store, store.get_job("resume-me")) is True

    resp = _owner_client(store, "resume-owner").post("/optimizations/resume-me/resume")
    assert resp.status_code == 202, resp.text
    job = store.get_job("resume-me")
    assert job["status"] == "pending"
    assert job["attempts"] == settings.job_max_attempts


def test_resume_of_a_plain_failed_run_still_counts_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed run that the sweep did not stop as interrupted keeps the attempt cap and count.

    Args:
        monkeypatch: Fixture binding the checkpoint's immutable runtime profile.
    """
    store = SQLiteJobStore()
    store.create_job("plain")
    _fund_checkpoint(store, "plain", monkeypatch)
    store.update_job("plain", status="failed", attempts=0)
    client = _owner_client(store, "resume-owner")

    resp = client.post("/optimizations/plain/resume")
    assert resp.status_code == 202, resp.text
    assert store.get_job("plain")["attempts"] == 1
    store.update_job("plain", status="failed", attempts=settings.job_max_attempts)
    assert client.post("/optimizations/plain/resume").status_code == 409


def test_resume_of_a_run_that_keeps_crashing_its_worker_counts_attempts_until_the_cap() -> None:
    """An interrupted stop with no worker-restart cause uses up an attempt on each resume, and the cap ends the loop."""
    store = SQLiteJobStore()
    store.create_job("crashes")
    _fund_without_checkpoint(store, "crashes")
    client = _owner_client(store, "restart-owner")
    statuses = []
    for _ in range(settings.job_max_attempts + 2):
        job = store.get_job("crashes")
        store.update_job(
            "crashes",
            status="failed",
            stop_reason="interrupted",
            recovery={
                "state": "interrupted",
                "interrupted_budget_generation": int(job.get("execution_budget_generation") or 0),
            },
        )
        resp = client.post("/optimizations/crashes/resume")
        statuses.append(resp.status_code)
        if statuses[-1] != 202:
            break
    # The store refuses the resume that would reach the cap, so the last attempt
    # stays for the automatic requeue.
    assert statuses == [202] * (settings.job_max_attempts - 1) + [409]
    job = store.get_job("crashes")
    assert job["attempts"] == settings.job_max_attempts - 1
    assert job["status"] == "failed"


def test_legacy_interrupted_marker_without_a_cause_is_not_exempt_from_the_cap() -> None:
    """A stop marker stored without a cause counts like a crash, so the cap still blocks its resume."""
    store = SQLiteJobStore()
    store.create_job("legacy-stop")
    _fund_without_checkpoint(store, "legacy-stop")
    store.update_job(
        "legacy-stop",
        status="failed",
        stop_reason="interrupted",
        attempts=settings.job_max_attempts,
        recovery={"state": "interrupted", "interrupted_budget_generation": 0},
    )
    assert is_resumable(store, store.get_job("legacy-stop")) is False
    resp = _owner_client(store, "restart-owner").post("/optimizations/legacy-stop/resume")
    assert resp.status_code == 409
    assert store.get_job("legacy-stop")["status"] == "failed"
