"""Verify lifecycle endpoints fence paid admission before acknowledging user actions."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api.routers.optimizations import create_optimizations_router, lifecycle

from .conftest import bypass_auth
from .mocks import _BaseFakeJobStore


def test_cancel_closes_paid_admission_before_status_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent additional provider dispatch after cancellation returns to the user."""
    store = _BaseFakeJobStore()
    store.seed_job("protected-cancel", status="running", username="alice")
    store.update_job("protected-cancel", execution_budget_id="budget")
    store.engine = object()
    calls = []

    class Budgets:
        """Capture the trusted admission closure without creating any paid work."""

        def stop_admission(self, identity: str, username: str, *, reason: str) -> None:
            """Require the budget to close while the run is still active."""
            assert store.get_job("protected-cancel")["status"] == "running"
            calls.append((identity, username, reason))

    monkeypatch.setattr(lifecycle, "BudgetService", lambda **kwargs: Budgets())
    app = FastAPI()
    app.include_router(create_optimizations_router(job_store=store, get_worker_ref=lambda: None))
    bypass_auth(app)
    response = TestClient(app).post("/optimizations/protected-cancel/cancel")
    assert response.status_code == 200
    assert calls == [("budget", "alice", "user_cancelled")]
    assert store.get_job("protected-cancel")["status"] == "cancelled"


@pytest.mark.parametrize("action", ["retry", "restart", "clone"])
def test_protected_fresh_run_requires_new_budget_configuration(action: str) -> None:
    """Avoid transferring spent funding and setup evidence into a different execution."""
    store = _BaseFakeJobStore()
    store.seed_job("protected", status="failed", username="alice")
    store.update_job("protected", execution_budget_id="spent-budget")
    app = FastAPI()
    app.include_router(create_optimizations_router(job_store=store, get_worker_ref=lambda: None))
    bypass_auth(app)
    response = TestClient(app).post(f"/optimizations/protected/{action}", json={"count": 1})
    assert response.status_code == 409
    assert len(store.list_jobs()) == 1


def test_cancel_of_a_judge_session_ends_it_with_the_final_score(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hand a judge session (SKY-696) to its end, which scores the best version, not to a worker."""
    store = _BaseFakeJobStore()
    store.seed_job("judge-session", status="running", username="alice")
    store.update_job("judge-session", execution_budget_id="budget", payload={"judge": {"version": 1}})
    store.engine = object()
    ends = []

    def end_session(job_store: object, job: dict, *, ended_by: str) -> lifecycle.JobCancelResponse:
        """Record the end instead of scoring.

        Args:
            job_store: The store.
            job: The session row.
            ended_by: What ended the session.

        Returns:
            The session's new status.
        """
        ends.append((job["optimization_id"], ended_by))
        return lifecycle.JobCancelResponse(optimization_id=job["optimization_id"], status="success")

    def no_budget(**kwargs: object) -> None:
        """Fail when the cancel closes the budget itself; the end owns that step."""
        raise AssertionError("the judge end closes the budget")

    monkeypatch.setattr(lifecycle.repo_judge, "end_session", end_session)
    monkeypatch.setattr(lifecycle, "BudgetService", no_budget)
    app = FastAPI()
    app.include_router(create_optimizations_router(job_store=store, get_worker_ref=lambda: None))
    bypass_auth(app)
    response = TestClient(app).post("/optimizations/judge-session/cancel")
    assert response.status_code == 200
    assert response.json() == {"optimization_id": "judge-session", "status": "success"}
    assert ends == [("judge-session", "stop_run")]
    assert store.get_job("judge-session")["status"] == "running"


def test_bulk_cancel_skips_a_judge_session_and_cancels_a_normal_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Leave a judge session (SKY-696) to stop_run, with its budget open, and still cancel the other run."""
    store = _BaseFakeJobStore()
    store.seed_job("judge-session", status="running", username="alice")
    store.update_job("judge-session", execution_budget_id="judge-budget", payload={"judge": {"version": 1}})
    store.seed_job("normal-run", status="running", username="alice")
    store.update_job("normal-run", execution_budget_id="run-budget")
    store.engine = object()
    stops = []
    ends = []

    class Budgets:
        """Record each budget close without creating any paid work."""

        def stop_admission(self, identity: str, username: str, *, reason: str) -> None:
            """Record the close.

            Args:
                identity: The budget.
                username: The owner.
                reason: Why the budget closes.
            """
            stops.append((identity, reason))

    monkeypatch.setattr(lifecycle, "BudgetService", lambda **kwargs: Budgets())
    monkeypatch.setattr(lifecycle.repo_judge, "end_session", lambda *args, **kwargs: ends.append(args))
    app = FastAPI()
    app.include_router(create_optimizations_router(job_store=store, get_worker_ref=lambda: None))
    bypass_auth(app)
    response = TestClient(app).post(
        "/optimizations/bulk-cancel", json={"optimization_ids": ["judge-session", "normal-run"]}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cancelled"] == ["normal-run"]
    assert [entry["optimization_id"] for entry in body["skipped"]] == ["judge-session"]
    assert "stop_run" in body["skipped"][0]["reason"]
    assert stops == [("run-budget", "user_cancelled")]
    assert ends == []
    assert store.get_job("judge-session")["status"] == "running"
    assert store.get_job("normal-run")["status"] == "cancelled"


def test_bulk_cancel_skips_a_pending_judge_row_with_no_payload_and_cancels_a_normal_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skip a judge row that an open left pending with no payload, and still cancel a normal pending row."""
    store = _BaseFakeJobStore()
    store.seed_job("judge-opening", status="pending", username="alice", payload=None)
    store.seed_job("normal-pending", status="pending", username="alice", payload=None)
    store.engine = object()
    keys = []

    def is_session(job_store: object, session_id: str) -> bool:
        """Stand for the stored-key read: only ``judge-opening`` has a judge key.

        Args:
            job_store: The store.
            session_id: The row.

        Returns:
            Whether the row is a judge session.
        """
        keys.append(session_id)
        return session_id == "judge-opening"

    monkeypatch.setattr(lifecycle.repo_judge, "is_session", is_session)
    monkeypatch.setattr(lifecycle.repo_judge, "end_session", lambda *args, **kwargs: pytest.fail("no end"))
    app = FastAPI()
    app.include_router(create_optimizations_router(job_store=store, get_worker_ref=lambda: None))
    bypass_auth(app)
    response = TestClient(app).post(
        "/optimizations/bulk-cancel", json={"optimization_ids": ["judge-opening", "normal-pending"]}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cancelled"] == ["normal-pending"]
    assert body["skipped"] == [
        {"optimization_id": "judge-opening", "reason": "judge_session: call stop_run with this id to end it"}
    ]
    assert keys == ["judge-opening", "normal-pending"]
    assert store.get_job("judge-opening")["status"] == "pending"
    assert store.get_job("normal-pending")["status"] == "cancelled"
