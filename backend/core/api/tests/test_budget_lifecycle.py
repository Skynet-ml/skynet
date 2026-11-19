"""Verify lifecycle endpoints fence paid admission before acknowledging user actions."""

from __future__ import annotations

import logging

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
    releases = []

    class Budgets:
        """Capture the trusted admission closure without creating any paid work."""

        def stop_admission(self, identity: str, username: str, *, reason: str) -> None:
            """Record each close with the run's status, so the test can require the first one before the write."""
            calls.append((identity, username, reason, store.get_job("protected-cancel")["status"]))

        def close_for_cancelled_job(
            self, identity: str, username: str, *, job_id: str, expected_generation: int
        ) -> bool:
            """Record the close that stops admission again and frees the cancelled run's unstarted holds.

            Args:
                identity: The budget.
                username: The owner.
                job_id: The cancelled run.
                expected_generation: The budget generation the cancel read after its write.

            Returns:
                Always ``True``.
            """
            releases.append((identity, username, job_id, expected_generation, store.get_job(job_id)["status"]))
            return True

    monkeypatch.setattr(lifecycle, "BudgetService", lambda **kwargs: Budgets())
    app = FastAPI()
    app.include_router(create_optimizations_router(job_store=store, get_worker_ref=lambda: None))
    bypass_auth(app)
    response = TestClient(app).post("/optimizations/protected-cancel/cancel")
    assert response.status_code == 200
    assert calls == [("budget", "alice", "user_cancelled", "running")]
    # The close after the write stops admission that a resume reopened before the write.
    assert releases == [("budget", "alice", "protected-cancel", 0, "cancelled")]
    assert store.get_job("protected-cancel")["status"] == "cancelled"


def test_cancel_leaves_the_budget_to_a_resume_after_its_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """When a resume moves the run on after the cancel's write, the cancel does not close the resumed budget."""
    store = _BaseFakeJobStore()
    store.seed_job("resumed-run", status="running", username="alice")
    store.update_job("resumed-run", execution_budget_id="budget")
    store.engine = object()
    write = store.update_job
    closes = []

    def resume_after_cancel(optimization_id: str, **fields: object) -> None:
        """Apply the write, then let a resume move a cancelled run back to pending.

        Args:
            optimization_id: The run.
            **fields: The columns to write.
        """
        write(optimization_id, **fields)
        if fields.get("status") == "cancelled":
            write(optimization_id, status="pending", completed_at=None)

    class Budgets:
        """Accept the first stop and record any close after the write."""

        def stop_admission(self, identity: str, username: str, *, reason: str) -> None:
            """Accept the stop before the write.

            Args:
                identity: The budget.
                username: The owner.
                reason: Why the budget closes.
            """

        def close_for_cancelled_job(
            self, identity: str, username: str, *, job_id: str, expected_generation: int
        ) -> bool:
            """Record the close, which must not happen.

            Args:
                identity: The budget.
                username: The owner.
                job_id: The run.
                expected_generation: The budget generation the cancel read.

            Returns:
                Always ``True``.
            """
            closes.append(job_id)
            return True

    monkeypatch.setattr(store, "update_job", resume_after_cancel)
    monkeypatch.setattr(lifecycle, "BudgetService", lambda **kwargs: Budgets())
    app = FastAPI()
    app.include_router(create_optimizations_router(job_store=store, get_worker_ref=lambda: None))
    bypass_auth(app)

    response = TestClient(app).post("/optimizations/resumed-run/cancel")

    assert response.status_code == 200
    assert closes == []
    assert store.get_job("resumed-run")["status"] == "pending"


def test_cancel_succeeds_when_the_budget_release_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed release of the unstarted holds does not turn a cancelled run into a route error, and logs one error."""
    store = _BaseFakeJobStore()
    budgets = {"single-run": "budget-a", "bulk-run": "budget-b"}
    for run_id, budget_id in budgets.items():
        store.seed_job(run_id, status="running", username="alice")
        store.update_job(run_id, execution_budget_id=budget_id)
    store.engine = object()

    class Budgets:
        """Close admission, then fail the release of the holds."""

        def stop_admission(self, identity: str, username: str, *, reason: str) -> None:
            """Accept the admission close.

            Args:
                identity: The budget.
                username: The owner.
                reason: Why the budget closes.
            """

        def close_for_cancelled_job(
            self, identity: str, username: str, *, job_id: str, expected_generation: int
        ) -> bool:
            """Fail as a lost database connection would.

            Args:
                identity: The budget.
                username: The owner.
                job_id: The cancelled run.
                expected_generation: The budget generation the cancel read.

            Raises:
                RuntimeError: Always.
            """
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(lifecycle, "BudgetService", lambda **kwargs: Budgets())
    app = FastAPI()
    app.include_router(create_optimizations_router(job_store=store, get_worker_ref=lambda: None))
    bypass_auth(app)
    client = TestClient(app)
    caplog.set_level(logging.ERROR, logger=lifecycle.__name__)

    single = client.post("/optimizations/single-run/cancel")
    bulk = client.post("/optimizations/bulk-cancel", json={"optimization_ids": ["bulk-run"]})

    assert single.status_code == 200
    assert single.json()["status"] == "cancelled"
    assert bulk.status_code == 200
    assert bulk.json()["cancelled"] == ["bulk-run"]
    errors = [
        record.getMessage()
        for record in caplog.records
        if record.name == "core.api.routers.optimizations.lifecycle" and record.levelno == logging.ERROR
    ]
    assert len(errors) == 2
    for message, (run_id, budget_id) in zip(errors, budgets.items(), strict=True):
        assert run_id in message
        assert budget_id in message
    assert bulk.json()["skipped"] == []
    assert store.get_job("single-run")["status"] == "cancelled"
    assert store.get_job("bulk-run")["status"] == "cancelled"


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
    releases = []
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

        def close_for_cancelled_job(
            self, identity: str, username: str, *, job_id: str, expected_generation: int
        ) -> bool:
            """Record the close of the cancelled run's budget.

            Args:
                identity: The budget.
                username: The owner.
                job_id: The cancelled run.
                expected_generation: The budget generation the cancel read.

            Returns:
                Always ``True``.
            """
            releases.append(identity)
            return True

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
    assert releases == ["run-budget"]
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
