"""Tests for the ``/optimizations`` router.

Covers list, get, cancel, delete, bulk delete, dashboard streams, clone
payloads and the black-box status result.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ...config import settings
from ...models.blackbox import BlackboxLaneResult, BlackboxRunResponse
from ..auth import AuthenticatedUser
from ..routers import _helpers as helpers_module
from ..routers.optimizations import create_optimizations_router
from ..routers.optimizations_meta import _sanitize_payload
from ..sharing_access import ShareRole
from .conftest import bypass_auth
from .mocks import _BaseFakeJobStore

# _ExtendedFakeJobStore is just _BaseFakeJobStore (which already has bulk-delete).
_ExtendedFakeJobStore = _BaseFakeJobStore


@pytest.fixture
def store() -> _ExtendedFakeJobStore:
    """Provide a fresh fake job store for each test.

    Returns:
        A new ``_ExtendedFakeJobStore`` instance.
    """
    return _ExtendedFakeJobStore()


@pytest.fixture
def opt_client(store: _ExtendedFakeJobStore) -> TestClient:
    """Build a ``TestClient`` exposing only the optimizations router.

    Args:
        store: Fake job store wired into the router factory.

    Returns:
        A ``TestClient`` over a minimal FastAPI app.
    """
    app = FastAPI()
    app.include_router(create_optimizations_router(job_store=store, get_worker_ref=lambda: None))
    bypass_auth(app)
    return TestClient(app, raise_server_exceptions=False)


def test_list_jobs_returns_empty_when_store_is_empty(opt_client: TestClient) -> None:
    """An empty store yields a zero-total list with no items."""
    resp = opt_client.get("/optimizations")

    assert resp.status_code == 200
    assert resp.json()["total"] == 0
    assert resp.json()["items"] == []


def test_compare_endpoint_is_not_registered(opt_client: TestClient) -> None:
    """Keep the removed cross-optimization comparison API out of OpenAPI."""
    schema = opt_client.get("/openapi.json").json()

    assert "/optimizations/compare" not in schema["paths"]


def test_list_jobs_invalid_status_returns_422(opt_client: TestClient) -> None:
    """Unknown ``status`` query values are rejected with 422."""
    resp = opt_client.get("/optimizations?status=bogus")

    assert resp.status_code == 422


def test_list_jobs_invalid_optimization_type_returns_422(opt_client: TestClient) -> None:
    """Unknown ``optimization_type`` values are rejected with 422."""
    resp = opt_client.get("/optimizations?optimization_type=bad_type")

    assert resp.status_code == 422


def test_list_jobs_valid_status_filter_returns_matching_jobs(
    opt_client: TestClient, store: _ExtendedFakeJobStore
) -> None:
    """The ``status`` filter narrows results to matching jobs only."""
    store.seed_job("j1", status="success")
    store.seed_job("j2", status="failed")

    resp = opt_client.get("/optimizations?status=success")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["optimization_id"] == "j1"


def test_get_job_returns_404_for_unknown_id(opt_client: TestClient) -> None:
    """A get against an unknown id returns 404."""
    resp = opt_client.get("/optimizations/does-not-exist")

    assert resp.status_code == 404


def test_get_job_returns_404_for_internal_tagger_job(opt_client: TestClient, store: _ExtendedFakeJobStore) -> None:
    """Internal tagger worker rows cannot render as optimization details."""
    store.seed_job("internal-tagger", payload_overview={"optimization_type": "tagging_autotag"})

    resp = opt_client.get("/optimizations/internal-tagger")

    assert resp.status_code == 404


def test_get_job_returns_404_for_unknown_internal_job_type(
    opt_client: TestClient, store: _ExtendedFakeJobStore
) -> None:
    """Unknown worker job types cannot render as optimization details."""
    store.seed_job("future-internal", payload_overview={"optimization_type": "future_internal_job"})

    resp = opt_client.get("/optimizations/future-internal")

    assert resp.status_code == 404


def test_get_job_returns_200_for_existing_job(opt_client: TestClient, store: _ExtendedFakeJobStore) -> None:
    """An existing job can be fetched by id."""
    store.seed_job("abc", status="success")

    resp = opt_client.get("/optimizations/abc")

    assert resp.status_code == 200
    assert resp.json()["optimization_id"] == "abc"


def test_get_job_returns_304_when_etag_matches(opt_client: TestClient, store: _ExtendedFakeJobStore) -> None:
    """A matching ``If-None-Match`` header yields a 304 from the get endpoint."""
    store.seed_job("e1", status="success")

    first = opt_client.get("/optimizations/e1")
    etag = first.headers.get("etag")
    assert etag is not None

    second = opt_client.get("/optimizations/e1", headers={"if-none-match": etag})

    assert second.status_code == 304
    # A 304 must carry no body: a ``JSONResponse(content=None)`` would emit
    # ``b"null"``, which real uvicorn/h11 rejects with "Too much data for
    # declared Content-Length". The TestClient transport doesn't enforce that
    # framing, so assert the empty body directly to guard the regression.
    assert second.content == b""
    assert second.headers.get("etag") == etag


def _seed_streamed_job(store: _ExtendedFakeJobStore, optimization_id: str, *, n_progress: int, n_logs: int) -> None:
    """Seed a running job with synthetic, ordered progress events and log rows.

    Args:
        store: Fake job store to populate.
        optimization_id: Identifier for the synthesised job.
        n_progress: Number of progress events to attach (tagged ``ev0``..).
        n_logs: Number of log rows to attach (messaged ``log0``..).
    """
    store.seed_job(optimization_id, status="running")
    store._progress[optimization_id] = [
        {"timestamp": f"2026-01-01T00:00:{i:02d}+00:00", "event": f"ev{i}", "metrics": {}} for i in range(n_progress)
    ]
    store._logs[optimization_id] = [
        {"timestamp": f"2026-01-01T00:00:{i:02d}+00:00", "level": "INFO", "logger": "test", "message": f"log{i}"}
        for i in range(n_logs)
    ]


def test_get_job_full_fetch_reports_zero_offsets(opt_client: TestClient, store: _ExtendedFakeJobStore) -> None:
    """A cursorless fetch returns the whole stream with zero offsets."""
    _seed_streamed_job(store, "d0", n_progress=5, n_logs=4)

    resp = opt_client.get("/optimizations/d0")

    assert resp.status_code == 200
    body = resp.json()
    assert body["progress_offset"] == 0
    assert body["logs_offset"] == 0
    assert len(body["progress_events"]) == 5
    assert len(body["logs"]) == 4


def test_get_job_delta_cursor_returns_tail_only(opt_client: TestClient, store: _ExtendedFakeJobStore) -> None:
    """A valid since_* cursor returns only the rows past the offset, echoing it back."""
    _seed_streamed_job(store, "d1", n_progress=5, n_logs=4)

    resp = opt_client.get("/optimizations/d1?since_progress=3&since_log=2")

    assert resp.status_code == 200
    body = resp.json()
    assert body["progress_offset"] == 3
    assert body["logs_offset"] == 2
    assert [e["event"] for e in body["progress_events"]] == ["ev3", "ev4"]
    assert [log["message"] for log in body["logs"]] == ["log2", "log3"]


def test_get_job_stale_cursor_falls_back_to_full(opt_client: TestClient, store: _ExtendedFakeJobStore) -> None:
    """A cursor past the current count re-sends the whole stream at offset 0."""
    _seed_streamed_job(store, "d2", n_progress=3, n_logs=3)

    resp = opt_client.get("/optimizations/d2?since_progress=99&since_log=99")

    assert resp.status_code == 200
    body = resp.json()
    assert body["progress_offset"] == 0
    assert body["logs_offset"] == 0
    assert len(body["progress_events"]) == 3
    assert len(body["logs"]) == 3


def test_get_job_delta_cursor_falls_back_at_retention_cap(
    opt_client: TestClient, store: _ExtendedFakeJobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At the eviction cap positions stop being stable, so cursors re-send everything."""
    monkeypatch.setattr(settings, "progress_events_per_job_cap", 5)
    _seed_streamed_job(store, "d3", n_progress=5, n_logs=4)

    resp = opt_client.get("/optimizations/d3?since_progress=3")

    assert resp.status_code == 200
    body = resp.json()
    assert body["progress_offset"] == 0
    assert len(body["progress_events"]) == 5


def test_get_summary_returns_404_for_unknown_id(opt_client: TestClient) -> None:
    """A summary request against an unknown id returns 404."""
    resp = opt_client.get("/optimizations/missing/summary")

    assert resp.status_code == 404


def test_get_summary_returns_200_for_existing_job(opt_client: TestClient, store: _ExtendedFakeJobStore) -> None:
    """An existing job exposes a summary keyed by ``optimization_id``."""
    store.seed_job("s1")

    resp = opt_client.get("/optimizations/s1/summary")

    assert resp.status_code == 200
    assert resp.json()["optimization_id"] == "s1"


def _agent_run(**overrides: Any) -> dict[str, Any]:
    """Build a stored agent run record.

    Args:
        **overrides: Field overrides.

    Returns:
        The record.
    """
    run: dict[str, Any] = {
        "run_id": 1,
        "phase": "version",
        "trial": 0,
        "example_id": "0",
        "case_id": "c",
        "label": "v1 · case 1",
        "status": "finished",
        "started_at": "2026-09-01T10:00:00+00:00",
        "finished_at": "2026-09-01T10:01:00+00:00",
        "exit_code": 0,
        "timed_out": False,
        "elapsed_seconds": 60.0,
        "error": None,
        "usage": {"total_tokens": 3},
        "check": {"passed": True, "exit_code": 0, "output": ""},
        "output": "answer",
        "transcript": "abcdef",
    }
    run.update(overrides)
    return run


def test_get_agent_run_returns_404_for_unknown_job(opt_client: TestClient) -> None:
    """A run lookup on an unknown optimization returns 404."""
    resp = opt_client.get("/optimizations/missing/agent-runs/1")

    assert resp.status_code == 404


def test_get_agent_run_returns_404_for_unknown_run(opt_client: TestClient, store: _ExtendedFakeJobStore) -> None:
    """A run id the job never recorded returns 404."""
    store.seed_job("r1")

    resp = opt_client.get("/optimizations/r1/agent-runs/7")

    assert resp.status_code == 404


def test_get_agent_run_serves_the_record_and_the_transcript_tail(
    opt_client: TestClient, store: _ExtendedFakeJobStore
) -> None:
    """The record comes back whole, with only the transcript past the caller's offset."""
    store.seed_job("r2")
    store.save_agent_run("r2", _agent_run())

    resp = opt_client.get("/optimizations/r2/agent-runs/1?since_transcript=4")

    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == 1
    assert body["status"] == "finished"
    assert body["output"] == "answer"
    assert body["usage"] == {"total_tokens": 3}
    assert body["check"]["passed"] is True
    assert body["transcript"] == "ef"
    assert body["transcript_offset"] == 4
    assert body["transcript_length"] == 6


def test_get_agent_run_clamps_an_offset_past_the_end(opt_client: TestClient, store: _ExtendedFakeJobStore) -> None:
    """An offset beyond the transcript yields an empty tail at the transcript's length."""
    store.seed_job("r3")
    store.save_agent_run("r3", _agent_run())

    resp = opt_client.get("/optimizations/r3/agent-runs/1?since_transcript=99")

    assert resp.status_code == 200
    assert resp.json()["transcript"] == ""
    assert resp.json()["transcript_offset"] == 6


def test_get_agent_run_rejects_a_negative_offset(opt_client: TestClient, store: _ExtendedFakeJobStore) -> None:
    """A negative offset is a validation error."""
    store.seed_job("r4")
    store.save_agent_run("r4", _agent_run())

    resp = opt_client.get("/optimizations/r4/agent-runs/1?since_transcript=-1")

    assert resp.status_code == 422


def _run_call(**overrides: Any) -> dict[str, Any]:
    """Build one relay call as the relay sends it.

    Args:
        **overrides: Field overrides.

    Returns:
        The call.
    """
    call: dict[str, Any] = {
        "candidate_id": "cand-1",
        "role": "solver",
        "kind": "model",
        "target": "openai/gpt-5",
        "started_at": "2026-11-01T10:00:00+00:00",
        "duration_ms": 50,
        "status": "ok",
        "cost_usd": 0.1,
        "request": "req",
        "response": "resp",
    }
    call.update(overrides)
    return call


def _seed_owned_run(store: _ExtendedFakeJobStore, optimization_id: str, *, status: str = "running") -> None:
    """Seed a run that ``alice`` owns.

    Args:
        store: The fake job store.
        optimization_id: The run id.
        status: The run's status.
    """
    store.seed_job(optimization_id, status=status, payload_overview={"username": "alice"})


def _client_as(store: _ExtendedFakeJobStore, user: AuthenticatedUser) -> TestClient:
    """Build a client over the optimizations router signed in as ``user``.

    Args:
        store: The fake job store.
        user: The caller.

    Returns:
        The test client.
    """
    app = FastAPI()
    app.include_router(create_optimizations_router(job_store=store, get_worker_ref=lambda: None))
    bypass_auth(app, user=user)
    return TestClient(app, raise_server_exceptions=False)


_ALICE = AuthenticatedUser(username="alice", role="user", groups=())
_BOB = AuthenticatedUser(username="bob", role="user", groups=())


def test_run_calls_serve_the_owner_a_page_with_a_cursor(store: _ExtendedFakeJobStore) -> None:
    """The owner gets calls in write order; ``next_since`` and ``has_more`` drive the next poll."""
    _seed_owned_run(store, "rc1")
    seqs = [store.record_run_call("rc1", _run_call(target=f"t{i}"), []) for i in range(3)]
    client = _client_as(store, _ALICE)

    first = client.get("/optimizations/rc1/candidates/cand-1/calls?limit=2").json()
    second = client.get(f"/optimizations/rc1/candidates/cand-1/calls?since={first['next_since']}&limit=2").json()
    empty = client.get(f"/optimizations/rc1/candidates/cand-1/calls?since={seqs[-1]}").json()

    assert [c["target"] for c in first["calls"]] == ["t0", "t1"]
    assert first["has_more"] is True
    assert first["next_since"] == seqs[1] == 2
    assert first["running"] is True
    assert [c["seq"] for c in second["calls"]] == [seqs[2]]
    assert second["has_more"] is False
    assert empty["calls"] == []
    assert empty["next_since"] == seqs[-1]


def test_run_calls_strip_secrets_before_they_are_served(store: _ExtendedFakeJobStore) -> None:
    """A secret given at write time never appears in the served record."""
    _seed_owned_run(store, "rc2")
    store.record_run_call("rc2", _run_call(request="token=tok-123", response="echo tok-123"), ["tok-123"])

    [call] = _client_as(store, _ALICE).get("/optimizations/rc2/candidates/cand-1/calls").json()["calls"]

    assert "tok-123" not in call["request"]
    assert "tok-123" not in call["response"]


def test_run_calls_report_running_from_candidate_and_run_state(store: _ExtendedFakeJobStore) -> None:
    """A done candidate stops running; an open candidate of a failed run reads as not running."""
    _seed_owned_run(store, "rc3")
    store.record_run_call("rc3", _run_call(), [])
    store.record_run_call("rc3", _run_call(candidate_id="cand-2"), [])
    store._run_call_store().finish_candidate("rc3", "cand-1", "done")
    client = _client_as(store, _ALICE)

    assert client.get("/optimizations/rc3/candidates/cand-1/calls").json()["running"] is False
    assert client.get("/optimizations/rc3/candidates/cand-2/calls").json()["running"] is True
    store.update_job("rc3", status="failed")
    assert client.get("/optimizations/rc3/candidates/cand-2/calls").json()["running"] is False


def test_run_calls_for_an_unknown_candidate_are_empty(store: _ExtendedFakeJobStore) -> None:
    """A candidate with no record yet gives an empty page that runs while the run runs."""
    _seed_owned_run(store, "rc4")
    client = _client_as(store, _ALICE)

    body = client.get("/optimizations/rc4/candidates/nobody/calls?since=7").json()

    assert body["calls"] == []
    assert body["next_since"] == 7
    assert body["running"] is True
    store.update_job("rc4", status="success")
    assert client.get("/optimizations/rc4/candidates/nobody/calls").json()["running"] is False


def test_run_candidates_list_role_state_count_and_cost(store: _ExtendedFakeJobStore) -> None:
    """The candidate list shows each candidate's totals; open candidates of a finished run read as done."""
    _seed_owned_run(store, "rc5")
    store.record_run_call("rc5", _run_call(cost_usd=0.1), [])
    store.record_run_call("rc5", _run_call(cost_usd=0.2), [])
    store.record_run_call("rc5", _run_call(candidate_id="cand-2", role="judge", kind="evaluator", cost_usd=None), [])
    store._run_call_store().finish_candidate("rc5", "cand-2", "failed")
    client = _client_as(store, _ALICE)

    live = {c["candidate_id"]: c for c in client.get("/optimizations/rc5/candidates").json()["candidates"]}
    store.update_job("rc5", status="success")
    ended = {c["candidate_id"]: c for c in client.get("/optimizations/rc5/candidates").json()["candidates"]}

    assert live["cand-1"]["state"] == "running"
    assert live["cand-1"]["call_count"] == 2
    assert live["cand-1"]["cost_usd"] == pytest.approx(0.3)
    assert live["cand-2"]["role"] == "judge"
    assert live["cand-2"]["state"] == "failed"
    assert ended["cand-1"]["state"] == "done"
    assert ended["cand-2"]["state"] == "failed"


def test_run_candidates_are_empty_before_any_call(store: _ExtendedFakeJobStore) -> None:
    """A run with no record yet lists no candidates."""
    _seed_owned_run(store, "rc6")

    body = _client_as(store, _ALICE).get("/optimizations/rc6/candidates").json()

    assert body == {"optimization_id": "rc6", "candidates": [], "next_offset": 0, "has_more": False}


def test_run_candidates_page_with_limit_and_offset(store: _ExtendedFakeJobStore) -> None:
    """The candidate list pages with ``limit`` and ``next_offset`` and refuses a page over 500."""
    _seed_owned_run(store, "rc10")
    for index in range(3):
        store.record_run_call("rc10", _run_call(candidate_id=f"cand-{index}"), [])
    client = _client_as(store, _ALICE)

    first = client.get("/optimizations/rc10/candidates?limit=2").json()
    rest = client.get(f"/optimizations/rc10/candidates?limit=2&offset={first['next_offset']}").json()

    assert [c["candidate_id"] for c in first["candidates"]] == ["cand-0", "cand-1"]
    assert (first["next_offset"], first["has_more"]) == (2, True)
    assert [c["candidate_id"] for c in rest["candidates"]] == ["cand-2"]
    assert (rest["next_offset"], rest["has_more"]) == (3, False)
    assert client.get("/optimizations/rc10/candidates?limit=501").status_code == 422
    assert client.get("/optimizations/rc10/candidates?offset=-1").status_code == 422


def test_run_calls_refuse_a_candidate_id_over_64_characters(store: _ExtendedFakeJobStore) -> None:
    """The store keeps 64 characters of a candidate id, so a longer id in the path is refused."""
    _seed_owned_run(store, "rc11")
    client = _client_as(store, _ALICE)

    assert client.get(f"/optimizations/rc11/candidates/{'c' * 64}/calls").status_code == 200
    assert client.get(f"/optimizations/rc11/candidates/{'c' * 65}/calls").status_code == 422


@pytest.mark.parametrize("path", ["/optimizations/rc7/candidates", "/optimizations/rc7/candidates/cand-1/calls"])
def test_run_record_returns_404_to_another_user(store: _ExtendedFakeJobStore, path: str) -> None:
    """A user with no access to the run cannot learn that its record exists."""
    _seed_owned_run(store, "rc7")
    store.record_run_call("rc7", _run_call(), [])

    resp = _client_as(store, _BOB).get(path)

    assert resp.status_code == 404


@pytest.mark.parametrize("path", ["/optimizations/rc8/candidates", "/optimizations/rc8/candidates/cand-1/calls"])
def test_run_record_is_owner_only_for_shared_members(
    store: _ExtendedFakeJobStore, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """A shared member who may read the run still gets 404 on its call bodies."""
    _seed_owned_run(store, "rc8")
    store.record_run_call("rc8", _run_call(), [])
    monkeypatch.setattr(helpers_module, "_grant_role", lambda job_store, optimization_id, username: ShareRole.editor)
    client = _client_as(store, _BOB)

    assert client.get("/optimizations/rc8/summary").status_code == 200
    assert client.get(path).status_code == 404


def test_run_calls_reject_a_negative_cursor_and_a_large_limit(store: _ExtendedFakeJobStore) -> None:
    """The cursor must not be negative, and a page holds at most 500 calls."""
    _seed_owned_run(store, "rc9")
    client = _client_as(store, _ALICE)

    assert client.get("/optimizations/rc9/candidates/cand-1/calls?since=-1").status_code == 422
    assert client.get("/optimizations/rc9/candidates/cand-1/calls?limit=501").status_code == 422


def test_run_record_returns_404_for_an_unknown_run(opt_client: TestClient) -> None:
    """Both routes return 404 for a run that does not exist."""
    assert opt_client.get("/optimizations/ghost/candidates").status_code == 404
    assert opt_client.get("/optimizations/ghost/candidates/cand-1/calls").status_code == 404


def test_cancel_job_returns_404_for_unknown_id(opt_client: TestClient) -> None:
    """Cancelling an unknown job returns 404."""
    resp = opt_client.post("/optimizations/ghost/cancel")

    assert resp.status_code == 404


def test_cancel_job_returns_409_when_already_terminal(opt_client: TestClient, store: _ExtendedFakeJobStore) -> None:
    """Cancelling a job already in a terminal state returns 409."""
    store.seed_job("done", status="success")

    resp = opt_client.post("/optimizations/done/cancel")

    assert resp.status_code == 409


@pytest.mark.parametrize("terminal_status", ["success", "failed", "cancelled"])
def test_cancel_job_returns_409_for_all_terminal_statuses(
    opt_client: TestClient,
    store: _ExtendedFakeJobStore,
    terminal_status: str,
) -> None:
    """Every terminal status (success/failed/cancelled) blocks cancel with 409."""
    store.seed_job(f"j_{terminal_status}", status=terminal_status)

    resp = opt_client.post(f"/optimizations/j_{terminal_status}/cancel")

    assert resp.status_code == 409


def test_cancel_job_returns_200_and_sets_cancelled_for_pending(
    opt_client: TestClient, store: _ExtendedFakeJobStore
) -> None:
    """Cancelling a pending job returns 200 with status ``cancelled``."""
    store.seed_job("active", status="pending")

    resp = opt_client.post("/optimizations/active/cancel")

    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


def test_cancel_job_updates_store_status(opt_client: TestClient, store: _ExtendedFakeJobStore) -> None:
    """Cancelling a running job persists the new ``cancelled`` status to the store."""
    store.seed_job("running_job", status="running")

    opt_client.post("/optimizations/running_job/cancel")

    assert store.get_job("running_job")["status"] == "cancelled"


def test_delete_job_returns_404_for_unknown_id(opt_client: TestClient) -> None:
    """Deleting an unknown job returns 404."""
    resp = opt_client.delete("/optimizations/ghost")

    assert resp.status_code == 404


def test_delete_job_returns_409_for_active_job(opt_client: TestClient, store: _ExtendedFakeJobStore) -> None:
    """Deleting a still-running job returns 409."""
    store.seed_job("live", status="running")

    resp = opt_client.delete("/optimizations/live")

    assert resp.status_code == 409


@pytest.mark.parametrize("active_status", ["pending", "validating", "running"])
def test_delete_job_returns_409_for_non_terminal_statuses(
    opt_client: TestClient,
    store: _ExtendedFakeJobStore,
    active_status: str,
) -> None:
    """Every non-terminal status blocks delete with 409."""
    store.seed_job(f"j_{active_status}", status=active_status)

    resp = opt_client.delete(f"/optimizations/j_{active_status}")

    assert resp.status_code == 409


def test_delete_job_returns_200_and_removes_terminal_job(opt_client: TestClient, store: _ExtendedFakeJobStore) -> None:
    """Deleting a terminal job removes it from the store and reports ``deleted=True``."""
    store.seed_job("done", status="success")

    resp = opt_client.delete("/optimizations/done")

    assert resp.status_code == 200
    body = resp.json()
    assert body["optimization_id"] == "done"
    assert body["deleted"] is True
    assert not store.job_exists("done")


def test_bulk_delete_empty_list_returns_zero_deleted(opt_client: TestClient) -> None:
    """An empty bulk-delete returns empty ``deleted`` and ``skipped`` lists."""
    resp = opt_client.post("/optimizations/bulk-delete", json={"optimization_ids": []})

    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] == []
    assert body["skipped"] == []


def test_bulk_delete_missing_ids_are_skipped(opt_client: TestClient, store: _ExtendedFakeJobStore) -> None:
    """Unknown ids are reported in ``skipped`` with a ``not_found`` reason."""
    resp = opt_client.post(
        "/optimizations/bulk-delete",
        json={"optimization_ids": ["ghost1", "ghost2"]},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] == []
    assert {s["reason"] for s in body["skipped"]} == {"not_found"}


def test_bulk_delete_active_ids_are_skipped_with_status_reason(
    opt_client: TestClient, store: _ExtendedFakeJobStore
) -> None:
    """Non-terminal jobs are skipped and report their current status as the reason."""
    store.seed_job("r1", status="running")

    resp = opt_client.post(
        "/optimizations/bulk-delete",
        json={"optimization_ids": ["r1"]},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] == []
    assert body["skipped"][0]["reason"] == "running"


def test_bulk_delete_deletes_terminal_jobs(opt_client: TestClient, store: _ExtendedFakeJobStore) -> None:
    """Terminal jobs are removed from the store and listed in ``deleted``."""
    store.seed_job("t1", status="success")
    store.seed_job("t2", status="failed")

    resp = opt_client.post(
        "/optimizations/bulk-delete",
        json={"optimization_ids": ["t1", "t2"]},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert set(body["deleted"]) == {"t1", "t2"}
    assert body["skipped"] == []
    assert not store.job_exists("t1")
    assert not store.job_exists("t2")


def test_bulk_delete_deduplicates_ids(opt_client: TestClient, store: _ExtendedFakeJobStore) -> None:
    """Duplicate ids are collapsed before deletion."""
    store.seed_job("dup", status="success")

    resp = opt_client.post(
        "/optimizations/bulk-delete",
        json={"optimization_ids": ["dup", "dup", "dup"]},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] == ["dup"]
    assert body["skipped"] == []


def test_bulk_delete_mixed_batch_reports_partial_results(opt_client: TestClient, store: _ExtendedFakeJobStore) -> None:
    """Mixed batches return partial deletions and per-id skip reasons."""
    store.seed_job("ok", status="cancelled")
    store.seed_job("busy", status="pending")

    resp = opt_client.post(
        "/optimizations/bulk-delete",
        json={"optimization_ids": ["ok", "busy", "missing"]},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] == ["ok"]
    skip_reasons = {s["optimization_id"]: s["reason"] for s in body["skipped"]}
    assert skip_reasons["busy"] == "pending"
    assert skip_reasons["missing"] == "not_found"


def test_counts_empty_store_returns_all_zeros(opt_client: TestClient) -> None:
    """An empty store returns zero counts across every status bucket."""
    resp = opt_client.get("/optimizations/counts")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    for key in ("pending", "validating", "running", "success", "failed", "cancelled"):
        assert body[key] == 0


def test_counts_reflects_mixed_statuses(opt_client: TestClient, store: _ExtendedFakeJobStore) -> None:
    """Counts reflect a mixed set of seeded statuses."""
    store.seed_job("j1", status="success")
    store.seed_job("j2", status="success")
    store.seed_job("j3", status="failed")
    store.seed_job("j4", status="pending")

    resp = opt_client.get("/optimizations/counts")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 4
    assert body["success"] == 2
    assert body["failed"] == 1
    assert body["pending"] == 1
    assert body["running"] == 0
    assert body["cancelled"] == 0


def test_counts_username_filter_restricts_to_single_user(opt_client: TestClient, store: _ExtendedFakeJobStore) -> None:
    """The ``username`` filter restricts counts to that user."""
    store.seed_job("alice_job", status="success", payload_overview={"username": "alice"})
    store.seed_job("bob_job", status="failed", payload_overview={"username": "bob"})

    resp = opt_client.get("/optimizations/counts?username=alice")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["success"] == 1
    assert body["failed"] == 0


def test_sidebar_empty_store_returns_empty_items(opt_client: TestClient) -> None:
    """An empty store returns an empty sidebar."""
    resp = opt_client.get("/optimizations/sidebar")

    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 0


def test_sidebar_returns_minimal_fields_per_job(opt_client: TestClient, store: _ExtendedFakeJobStore) -> None:
    """Sidebar items expose only the minimal fields required by the UI."""
    store.seed_job(
        "s1",
        status="running",
        payload_overview={"name": "My Run", "optimizer_name": "gepa", "model_name": "gpt-4o"},
    )

    resp = opt_client.get("/optimizations/sidebar")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["optimization_id"] == "s1"
    assert item["status"] == "running"
    assert "name" in item
    assert "optimizer_name" in item
    assert "model_name" in item


def test_sidebar_limit_caps_returned_items(opt_client: TestClient, store: _ExtendedFakeJobStore) -> None:
    """The ``limit`` query caps the number of returned items but not ``total``."""
    for i in range(5):
        store.seed_job(f"job_{i}", status="success")

    resp = opt_client.get("/optimizations/sidebar?limit=2")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["total"] == 5


def test_sidebar_pinned_flag_is_propagated(opt_client: TestClient, store: _ExtendedFakeJobStore) -> None:
    """The ``pinned`` flag is propagated to sidebar items."""
    store.seed_job("pinned_job", status="success", payload_overview={"pinned": True})
    store.seed_job("normal_job", status="success", payload_overview={"pinned": False})

    resp = opt_client.get("/optimizations/sidebar")

    assert resp.status_code == 200
    items = {item["optimization_id"]: item for item in resp.json()["items"]}
    assert items["pinned_job"]["pinned"] is True
    assert items["normal_job"]["pinned"] is False


def test_sidebar_username_filter_restricts_results(opt_client: TestClient, store: _ExtendedFakeJobStore) -> None:
    """The ``username`` filter restricts sidebar items to that user."""
    store.seed_job("alice_1", status="success", payload_overview={"username": "alice"})
    store.seed_job("alice_2", status="running", payload_overview={"username": "alice"})
    store.seed_job("bob_1", status="failed", payload_overview={"username": "bob"})

    resp = opt_client.get("/optimizations/sidebar?username=alice")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    ids = {item["optimization_id"] for item in body["items"]}
    assert ids == {"alice_1", "alice_2"}


def test_dashboard_stream_returns_text_event_stream_content_type(opt_client: TestClient) -> None:
    """The dashboard stream uses an SSE content type."""
    # With no active jobs the generator emits one data event then an idle event and closes.
    with opt_client.stream("GET", "/optimizations/stream") as resp:
        assert "text/event-stream" in resp.headers["content-type"]


def test_dashboard_stream_first_event_has_active_jobs_key(opt_client: TestClient) -> None:
    """The first SSE event includes ``active_jobs`` and ``active_count`` fields."""
    with opt_client.stream("GET", "/optimizations/stream") as resp:
        for chunk in resp.iter_lines():
            if chunk.startswith("data:"):
                payload = json.loads(chunk[len("data:") :].strip())
                assert "active_jobs" in payload
                assert "active_count" in payload
                break


def test_dashboard_stream_empty_store_sends_idle_event(opt_client: TestClient) -> None:
    """An empty store emits a single data event followed by an idle event."""
    # With no active jobs the generator emits one data event then an idle event
    # and closes — no sleep between events, so this completes quickly.
    events: list[dict] = []
    idle_seen = False
    with opt_client.stream("GET", "/optimizations/stream") as resp:
        for chunk in resp.iter_lines():
            if chunk.startswith("data:"):
                events.append(json.loads(chunk[len("data:") :].strip()))
            if chunk.startswith("event: idle"):
                idle_seen = True

    # There should be exactly one data event (active_count=0) followed by idle.
    assert len(events) >= 1
    assert events[0]["active_count"] == 0
    assert idle_seen


def test_job_stream_returns_404_for_unknown_job(opt_client: TestClient) -> None:
    """A per-job stream against an unknown id returns 404."""
    resp = opt_client.get("/optimizations/no-such-id/stream")

    assert resp.status_code == 404


def test_job_stream_returns_text_event_stream_for_known_job(
    opt_client: TestClient, store: _ExtendedFakeJobStore
) -> None:
    """A known job exposes an SSE stream with the standard content type."""
    store.seed_job("done_job", status="success")

    with opt_client.stream("GET", "/optimizations/done_job/stream") as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]


def test_job_stream_first_event_contains_expected_fields(opt_client: TestClient, store: _ExtendedFakeJobStore) -> None:
    """The first per-job SSE event carries id, status, metrics, and counters."""
    store.seed_job("done_job2", status="success")

    with opt_client.stream("GET", "/optimizations/done_job2/stream") as resp:
        for chunk in resp.iter_lines():
            if chunk.startswith("data:"):
                payload = json.loads(chunk[len("data:") :].strip())
                assert payload["optimization_id"] == "done_job2"
                assert "status" in payload
                assert "latest_metrics" in payload
                assert "log_count" in payload
                assert "progress_count" in payload
                break


def test_status_surfaces_blackbox_results_under_blackbox_result(
    opt_client: TestClient, store: _ExtendedFakeJobStore
) -> None:
    """A finished black-box job returns its lanes and best version, not a null ``result``."""
    blackbox_result = BlackboxRunResponse(
        optimizer_name="auto",
        strategy_mode="auto",
        engine_used="gepa",
        baseline_score=0.4,
        best_score=0.9,
        metric_improvement=0.5,
        seed_candidate="Be brief.",
        best_candidate="Be brief and cite sources.",
        lanes=[
            BlackboxLaneResult(engine="best_of_n", phase="explore", status="completed", best_score=0.6, scorer_runs=6),
            BlackboxLaneResult(engine="gepa", phase="continue", status="completed", best_score=0.9, scorer_runs=14),
        ],
        total_scorer_runs=20,
        runtime_seconds=12.5,
    )
    store.seed_job(
        "bb-done",
        status="success",
        payload_overview={
            "optimization_type": "blackbox",
            "module_name": "blackbox",
            "optimizer_name": "auto",
        },
        result=blackbox_result.model_dump(),
    )

    resp = opt_client.get("/optimizations/bb-done")

    assert resp.status_code == 200
    body = resp.json()
    assert "result" not in body
    assert "grid_result" not in body
    assert body["blackbox_result"]["best_candidate"] == "Be brief and cite sources."
    assert body["blackbox_result"]["engine_used"] == "gepa"
    assert [lane["engine"] for lane in body["blackbox_result"]["lanes"]] == ["best_of_n", "gepa"]
    assert body["blackbox_result"]["best_score"] == 0.9


def test_sanitize_payload_scrubs_the_scorer_model_key() -> None:
    """A black-box scorer's ``llm()`` model loses its inline key like every other model slot."""
    payload = {
        "reflection_model_settings": {"name": "m", "extra": {"api_key": "sk-2"}},
        "scorer": {
            "kind": "python",
            "metric_code": "def score(c): return 1.0",
            "model": {"name": "openai/gpt-4o", "extra": {"api_key": "sk-secret", "temperature": 0.1}},
        },
    }

    sanitised = _sanitize_payload(payload)

    assert sanitised["scorer"]["model"]["extra"] == {"temperature": 0.1}
    assert sanitised["scorer"]["metric_code"] == "def score(c): return 1.0"
    assert "api_key" not in sanitised["reflection_model_settings"]["extra"]
    assert payload["scorer"]["model"]["extra"]["api_key"] == "sk-secret"


def test_sanitize_payload_scrubs_alias_model_keys_and_scorer_secret() -> None:
    """Persisted payloads carry the wire aliases and a remote scorer's secret; both are scrubbed."""
    payload = {
        "model_config": {"name": "m", "extra": {"api_key": "sk-1"}},
        "reflection_model_config": {"name": "r", "extra": {"api_key": "sk-2", "reasoning_effort": "high"}},
        "task_model_config": {"name": "t", "extra": {"api_key": "sk-3"}},
        "scorer": {"kind": "remote", "url": "https://scorer.example", "secret": "shh", "timeout_seconds": 5},
    }

    sanitised = _sanitize_payload(payload)

    assert "api_key" not in sanitised["model_config"]["extra"]
    assert sanitised["reflection_model_config"]["extra"] == {"reasoning_effort": "high"}
    assert "api_key" not in sanitised["task_model_config"]["extra"]
    assert "secret" not in sanitised["scorer"]
    assert sanitised["scorer"]["url"] == "https://scorer.example"
    assert payload["scorer"]["secret"] == "shh"
