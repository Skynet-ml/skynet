"""Tests for restart: POST /optimizations/{id}/restart.

Restart re-runs a terminal failed/cancelled optimization from scratch *in place*:
the same id is flipped back to ``pending`` after its prior attempt's result,
checkpoint and child artefacts are wiped — unlike retry (which mints a new id)
and unlike resume (which continues from a checkpoint). These tests pin the
endpoint's preconditions and the in-place reset.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ...constants import OPTIMIZATION_TYPE_BLACKBOX, OPTIMIZATION_TYPE_TAGGING
from ..routers._helpers import has_retired_target
from ..routers.optimizations import create_optimizations_router
from .conftest import bypass_auth
from .mocks import _BaseFakeJobStore

# Restart and resume refuse a stored text or agent run (SKY-834), so the default row is a repository run.
_REPO_PAYLOAD = {"target": {"kind": "repo", "repo": {"repository": "octo/hello", "editable_paths": ["src"]}}}


class _RepoJobStore(_BaseFakeJobStore):
    """Seed repository runs by default: a stored DSPy run can no longer be re-queued (SKY-316)."""

    def seed_job(self, optimization_id: str, **fields: Any) -> dict:
        """Insert a job row whose overview marks a repository run unless the test sets one.

        Args:
            optimization_id: Identifier for the synthesised job.
            **fields: Overrides applied on top of the default row.

        Returns:
            The stored job row.
        """
        fields.setdefault("payload_overview", {"optimization_type": OPTIMIZATION_TYPE_BLACKBOX})
        fields.setdefault("payload", _REPO_PAYLOAD)
        return super().seed_job(optimization_id, **fields)


@pytest.fixture
def store() -> _BaseFakeJobStore:
    """Provide a fresh fake job store per test."""
    return _RepoJobStore()


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


def test_restart_requeues_failed_run_in_place(client: TestClient, store: _BaseFakeJobStore) -> None:
    """A failed run restarts in place: 202, same id, pending, attempts reset to 0."""
    store.seed_job("r1", status="failed", attempts=2, username="alice")
    resp = client.post("/optimizations/r1/restart")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["optimization_id"] == "r1"
    assert body["status"] == "pending"
    job = store.get_job("r1")
    assert job["status"] == "pending"
    assert job["attempts"] == 0


def test_restart_clears_prior_attempt(client: TestClient, store: _BaseFakeJobStore) -> None:
    """The prior attempt's result and checkpoint are discarded by a restart."""
    store.seed_job("r2", status="failed", result={"score": 0.9}, username="alice")
    store.save_gepa_checkpoint("r2", b"GEPA-STATE", iteration=7)
    resp = client.post("/optimizations/r2/restart")
    assert resp.status_code == 202, resp.text
    job = store.get_job("r2")
    assert job["result"] is None
    assert store.has_gepa_checkpoint("r2") is False


def test_restart_allows_cancelled_run(client: TestClient, store: _BaseFakeJobStore) -> None:
    """A cancelled run can also be restarted from scratch."""
    store.seed_job("c1", status="cancelled", username="alice")
    resp = client.post("/optimizations/c1/restart")
    assert resp.status_code == 202, resp.text


@pytest.mark.parametrize("status", ["running", "pending", "validating", "success"])
def test_restart_rejects_non_terminal_failure(client: TestClient, store: _BaseFakeJobStore, status: str) -> None:
    """Only failed/cancelled runs may be restarted — others 409 and stay untouched."""
    oid = f"s_{status}"
    store.seed_job(oid, status=status, username="alice")
    resp = client.post(f"/optimizations/{oid}/restart")
    assert resp.status_code == 409
    assert store.get_job(oid)["status"] == status


def test_restart_unknown_id_is_404(client: TestClient) -> None:
    """Restarting a missing optimization is a 404."""
    resp = client.post("/optimizations/nope/restart")
    assert resp.status_code == 404


@pytest.mark.parametrize("optimization_type", ["run", "grid_search"])
def test_restart_of_a_stored_dspy_row_is_404(
    client: TestClient, store: _BaseFakeJobStore, optimization_type: str
) -> None:
    """A leftover DSPy run or grid row is not a user-facing run, so it reads as not found.

    Args:
        client: Test client over the optimizations router.
        store: Fake job store.
        optimization_type: A retired DSPy job kind.
    """
    store.seed_job("d1", status="failed", username="alice", payload_overview={"optimization_type": optimization_type})
    resp = client.post("/optimizations/d1/restart")
    assert resp.status_code == 404
    assert store.get_job("d1")["status"] == "failed"


# A stored text run from before SKY-834 may hold no target: the target kind defaults to text.
_TARGETLESS_TEXT_PAYLOAD = {
    "candidate": "Answer briefly.",
    "scorer": {"metric_code": "def score(candidate, case):\n    return 1.0\n"},
    "reflection_model_config": {"name": "openai/gpt-4o-mini"},
}


@pytest.mark.parametrize(
    "payload",
    [
        {"target": {"kind": "text"}},
        {"target": {"kind": "agent"}},
        _TARGETLESS_TEXT_PAYLOAD,
        # A row with no target that no longer validates is still not a repository run.
        {"candidate": "x", "scorer": {"metric_code": "x"}},
    ],
    ids=["text", "agent", "no_target", "no_target_invalid"],
)
def test_restart_of_a_stored_text_or_agent_run_is_refused_and_keeps_its_history(
    client: TestClient, store: _BaseFakeJobStore, payload: dict[str, Any]
) -> None:
    """Restart refuses a text or agent run, so its result, logs and checkpoint stay (SKY-834).

    Args:
        client: Test client over the optimizations router.
        store: Fake job store.
        payload: A stored text, agent or target-less payload.
    """
    store.seed_job("t1", status="failed", result={"score": 0.9}, username="alice", payload=payload)
    store._logs["t1"] = [{"level": "INFO", "message": "iteration 1"}]
    store.save_gepa_checkpoint("t1", b"GEPA-STATE", iteration=7)
    resp = client.post("/optimizations/t1/restart")
    assert resp.status_code == 409
    assert "it cannot restart" in resp.json()["detail"]
    job = store.get_job("t1")
    assert job["status"] == "failed"
    assert job["result"] == {"score": 0.9}
    assert store.get_log_count("t1") == 1
    assert store.has_gepa_checkpoint("t1") is True


def test_restart_still_requeues_a_judge_session_payload(client: TestClient, store: _BaseFakeJobStore) -> None:
    """A judge payload is not a black-box run, so the retired-target rule skips it."""
    store.seed_job("j1", status="failed", username="alice", payload={"judge": {"opening": False}})
    resp = client.post("/optimizations/j1/restart")
    assert resp.status_code == 202, resp.text


def test_restart_skips_the_retired_target_rule_for_a_tagging_job(client: TestClient, store: _BaseFakeJobStore) -> None:
    """A tagging job with no repository target is not a retired run, and restart answers 404, never 409 (SKY-834).

    The route hides every job that is not a black-box run, so the retired-target rule must skip such a job.

    Args:
        client: Test client over the optimizations router.
        store: Fake job store.
    """
    store.seed_job(
        "tag1",
        status="failed",
        username="alice",
        payload={"rows": [{"text": "hello"}]},
        payload_overview={"optimization_type": OPTIMIZATION_TYPE_TAGGING},
    )
    assert not has_retired_target(store.get_job("tag1"))
    resp = client.post("/optimizations/tag1/restart")
    assert resp.status_code == 404, resp.text
