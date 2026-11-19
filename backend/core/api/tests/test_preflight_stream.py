"""Verify real phase forwarding and error framing without dispatching paid work."""

import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from core.api.auth import AuthenticatedUser, get_authenticated_user
from core.api.preflight_execution import _Events
from core.api.preflight_progress import progress_observer, report_preflight_phase
from core.api.routers import wizard_preflight


def test_stream_preserves_phase_order_and_returns_one_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forward actual milestones and keep arbitrary guest output out of the public stream."""
    calls = []

    def run(request: Any, user: Any, store: Any) -> Any:
        """Replace paid execution while exercising the real request observer."""
        calls.append(request)
        report_preflight_phase("budget")
        report_preflight_phase("sandbox")
        _Events().put({"type": "preflight_phase", "phase": "evaluator"})
        _Events().put({"type": "preflight_phase", "phase": "untrusted-secret"})
        _Events().put({"type": "log", "message": "private-log"})
        report_preflight_phase("usage")
        result = SimpleNamespace(
            checks=[],
            pending_reason=None,
            workflow_result=None,
            model_dump=lambda **kwargs: {"status": "succeeded", "id": "saved-result"},
        )
        result.model_copy = lambda update: result
        return result

    monkeypatch.setattr(wizard_preflight, "run_preflight", run)
    monkeypatch.setattr(wizard_preflight, "enforce_submission_rate", lambda username: None)
    app = FastAPI()
    app.include_router(wizard_preflight.create_wizard_preflight_router(job_store=object()))
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser("alice", "user", ())
    with TestClient(app) as client:
        response = client.post(
            "/wizard/preflight/stream",
            json={
                "workflow": "anything",
                "scope": "evaluation",
                "payload": {
                    "scorer": {"kind": "python", "metric_code": "def score(p):\n    return 1\n"},
                    "target": {"kind": "repo"},
                },
                "execution_budget_id": "draft",
                "execution_budget_revision": 1,
            },
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = [
        json.loads(line.removeprefix("data: ")) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    assert frames == [
        {"phase": "budget"},
        {"phase": "sandbox"},
        {"phase": "evaluator"},
        {"phase": "usage"},
        {"status": "succeeded", "id": "saved-result"},
    ]
    assert len(calls) == 1
    assert "private-log" not in response.text
    assert "untrusted-secret" not in response.text
    assert progress_observer.get() is None


def test_stream_reports_admission_failure_without_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a rejected budget visible as an error instead of a successful HTTP stream."""

    def reject(username: str) -> None:
        """Reject this request before dispatch."""
        raise HTTPException(status_code=429, detail="Please wait before validating again.")

    monkeypatch.setattr(wizard_preflight, "enforce_submission_rate", reject)
    app = FastAPI()
    app.include_router(wizard_preflight.create_wizard_preflight_router(job_store=object()))
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser("alice", "user", ())
    with TestClient(app) as client:
        response = client.post(
            "/wizard/preflight/stream",
            json={
                "workflow": "anything",
                "scope": "evaluation",
                "payload": {},
                "execution_budget_id": "draft",
                "execution_budget_revision": 1,
            },
        )
    assert "event: error" in response.text
    assert "event: result" not in response.text
    assert "Please wait" in response.text


def test_stream_logs_an_unexpected_failure(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Log the cause of a failure the stream shows only as a generic line."""

    def crash(request: Any, user: Any, store: Any) -> Any:
        """Fail the check with an error the stream does not frame."""
        raise ValueError("The model credential source must be managed or byok.")

    monkeypatch.setattr(wizard_preflight, "run_preflight", crash)
    monkeypatch.setattr(wizard_preflight, "enforce_submission_rate", lambda username: None)
    app = FastAPI()
    app.include_router(wizard_preflight.create_wizard_preflight_router(job_store=object()))
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser("alice", "user", ())
    with caplog.at_level("ERROR", logger=wizard_preflight.__name__), TestClient(app) as client:
        response = client.post(
            "/wizard/preflight/stream",
            json={
                "workflow": "anything",
                "scope": "evaluation",
                "payload": {"scorer": {"metric_code": "def metric(output): return 1.0"}, "target": {"kind": "repo"}},
                "execution_budget_id": "draft",
                "execution_budget_revision": 1,
            },
        )
    assert "Setup validation could not finish" in response.text
    assert "credential source" not in response.text
    assert any("credential source" in str(record.exc_info[1]) for record in caplog.records if record.exc_info)
