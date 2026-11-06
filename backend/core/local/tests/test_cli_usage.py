"""Tests for the local CLI usage sums and the ``/local/cli-usage`` route."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ...api.auth import AuthenticatedUser, get_authenticated_user
from ...api.routers.local_cli_usage import create_local_cli_usage_router
from ...config import settings
from ...storage.models import (
    Base,
    ExecutionBudgetModel,
    ExecutionOperationModel,
    ExecutionUsageEvidenceModel,
)
from ..cli_probe import write_cli_limit
from ..cli_usage import local_cli_usage

_USER = "local@skynet.local"
_NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine():
    """Build an in-memory database with the ORM tables.

    Returns:
        A SQLite engine shared across threads.
    """
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def state_dir(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Point the saved CLI caps at a temporary directory.

    Args:
        tmp_path: pytest's temporary directory.
        monkeypatch: pytest's ``MonkeyPatch`` fixture.

    Returns:
        The patched global settings.
    """
    monkeypatch.setattr(settings, "local_state_dir", str(tmp_path))
    return settings


def _budget(session: Session, username: str = _USER) -> str:
    """Add one run budget.

    Args:
        session: An open ORM session.
        username: The budget owner.

    Returns:
        The budget id.
    """
    budget_id = str(uuid.uuid4())
    session.add(
        ExecutionBudgetModel(
            id=budget_id,
            username=username,
            creation_key=budget_id,
            creation_fingerprint="f",
            total_cents=100,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    return budget_id


def _call(session: Session, budget_id: str, evidence: dict[str, Any], age: timedelta) -> None:
    """Add one settled CLI call to a budget.

    Args:
        session: An open ORM session.
        budget_id: The run budget.
        evidence: The stored evidence document.
        age: How long before ``_NOW`` the call settled.
    """
    operation_id = str(uuid.uuid4())
    session.add(
        ExecutionOperationModel(
            id=operation_id,
            budget_id=budget_id,
            operation_key=operation_id,
            attempt=1,
            generation=0,
            phase="run",
            cost_kind="model",
            request_fingerprint="r",
            admission_fingerprint="a",
            price_snapshot={},
            max_units=0,
            max_wallet_units=0,
            created_at=_NOW - age,
            updated_at=_NOW - age,
        )
    )
    session.flush()
    session.add(
        ExecutionUsageEvidenceModel(
            id=str(uuid.uuid4()),
            operation_id=operation_id,
            evidence_key="final",
            fingerprint="e",
            actual_units=0,
            actual_wallet_units=0,
            billed_cents=0,
            final=True,
            evidence=evidence,
            created_at=_NOW - age,
        )
    )


def _ok(cli: str, prompt: int, completion: int) -> dict[str, Any]:
    """Build the evidence of a successful dispatcher call.

    Args:
        cli: The CLI id.
        prompt: Input tokens.
        completion: Output tokens.

    Returns:
        The evidence document that ``CliDispatcher`` stores.
    """
    return {
        "provider": "subscription",
        "model": "m",
        "cli": cli,
        "status": 200,
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
        "latency_ms": 10,
    }


def _by_cli(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index the response's CLI entries by id.

    Args:
        document: The usage document.

    Returns:
        ``{cli: entry}``.
    """
    return {entry["cli"]: entry for entry in document["clis"]}


def test_empty_database_reports_zero_for_each_cli(engine, state_dir) -> None:
    """With no runs, each driven CLI shows zero totals, no last use and no cap."""
    document = local_cli_usage(engine, state_dir, _USER, now=_NOW)
    assert document["windows"] == ["5h", "7d"]
    clis = _by_cli(document)
    assert set(clis) == {"claude", "codex"}
    for entry in clis.values():
        assert entry["last_used_at"] is None
        assert entry["cap"] is None
        assert entry["windows"]["5h"] == {
            "calls": 0,
            "failed_calls": 0,
            "runs": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }


def test_sums_calls_tokens_failures_and_runs_per_window(engine, state_dir) -> None:
    """Calls split into the 5 hour and 7 day windows; older calls and other users are left out."""
    with Session(engine) as session:
        first, second, other = _budget(session), _budget(session), _budget(session, "someone@else")
        _call(session, first, _ok("claude", 100, 20), timedelta(hours=1))
        _call(
            session,
            first,
            {"provider": "subscription", "cli": "claude", "status": 429, "failure": "x"},
            timedelta(hours=2),
        )
        _call(
            session,
            second,
            {
                "source": "host_agent",
                "cli": "claude",
                "status": "ok",
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            },
            timedelta(hours=1),
        )
        _call(session, second, _ok("claude", 1000, 200), timedelta(days=2))
        _call(session, first, _ok("codex", 50, 5), timedelta(days=3))
        _call(session, first, _ok("codex", 9999, 9999), timedelta(days=8))
        _call(session, other, _ok("claude", 5000, 5000), timedelta(hours=1))
        _call(session, first, {"provider": "openrouter", "status": 200}, timedelta(hours=1))
        session.commit()
    clis = _by_cli(local_cli_usage(engine, state_dir, _USER, now=_NOW))
    claude, codex = clis["claude"], clis["codex"]
    assert claude["windows"]["5h"] == {
        "calls": 3,
        "failed_calls": 1,
        "runs": 2,
        "input_tokens": 107,
        "output_tokens": 23,
    }
    assert claude["windows"]["7d"] == {
        "calls": 4,
        "failed_calls": 1,
        "runs": 2,
        "input_tokens": 1107,
        "output_tokens": 223,
    }
    assert claude["last_used_at"] == (_NOW - timedelta(hours=1)).isoformat()
    assert codex["windows"]["5h"]["calls"] == 0
    assert codex["windows"]["7d"] == {"calls": 1, "failed_calls": 0, "runs": 1, "input_tokens": 50, "output_tokens": 5}


def test_proposer_error_and_dispatcher_failure_count_as_failed(engine, state_dir) -> None:
    """A proposer turn with status ``error`` and a dispatcher 502 are both failed calls."""
    with Session(engine) as session:
        budget = _budget(session)
        _call(session, budget, {"source": "host_agent", "cli": "codex", "status": "error"}, timedelta(hours=1))
        _call(session, budget, {"provider": "subscription", "cli": "codex", "status": 502}, timedelta(hours=1))
        _call(session, budget, {"source": "host_agent", "cli": "codex", "status": "ok"}, timedelta(hours=1))
        session.commit()
    window = _by_cli(local_cli_usage(engine, state_dir, _USER, now=_NOW))["codex"]["windows"]["5h"]
    assert (window["calls"], window["failed_calls"]) == (3, 2)


def test_skips_a_call_that_no_cli_started(engine, state_dir) -> None:
    """A 429 that a saved cap answered before any CLI started is not a call and not a last use."""
    with Session(engine) as session:
        budget = _budget(session)
        _call(session, budget, _ok("claude", 10, 2), timedelta(hours=3))
        _call(
            session,
            budget,
            {"provider": "subscription", "cli": "claude", "status": 429, "failure": "x", "dispatched": False},
            timedelta(hours=1),
        )
        session.commit()
    claude = _by_cli(local_cli_usage(engine, state_dir, _USER, now=_NOW))["claude"]
    assert (claude["windows"]["5h"]["calls"], claude["windows"]["5h"]["failed_calls"]) == (1, 0)
    assert claude["last_used_at"] == (_NOW - timedelta(hours=3)).isoformat()


def test_counts_each_fallback_cli_that_ran(engine, state_dir) -> None:
    """A capped CLI in ``capped_runs`` counts as one failed call next to the CLI that answered."""
    with Session(engine) as session:
        budget = _budget(session)
        _call(session, budget, {**_ok("codex", 10, 2), "capped_runs": ["claude"]}, timedelta(hours=1))
        session.commit()
    clis = _by_cli(local_cli_usage(engine, state_dir, _USER, now=_NOW))
    claude, codex = clis["claude"]["windows"]["5h"], clis["codex"]["windows"]["5h"]
    assert (claude["calls"], claude["failed_calls"], claude["runs"], claude["input_tokens"]) == (1, 1, 1, 0)
    assert (codex["calls"], codex["failed_calls"], codex["input_tokens"]) == (1, 0, 10)
    assert clis["claude"]["last_used_at"] == (_NOW - timedelta(hours=1)).isoformat()


def test_reports_only_an_active_cap(engine, state_dir) -> None:
    """A saved cap shows while it lasts and disappears after its reset time."""
    write_cli_limit(state_dir, "codex", _NOW + timedelta(hours=2), "limit reached")
    write_cli_limit(state_dir, "claude", _NOW - timedelta(minutes=1), "old limit")
    clis = _by_cli(local_cli_usage(engine, state_dir, _USER, now=_NOW))
    assert clis["codex"]["cap"] == {"reset_at": (_NOW + timedelta(hours=2)).isoformat(), "message": "limit reached"}
    assert clis["claude"]["cap"] is None


def test_reading_usage_changes_no_billing_rows(engine, state_dir) -> None:
    """The report only reads: budget and evidence rows stay the same."""
    with Session(engine) as session:
        budget = _budget(session)
        _call(session, budget, _ok("claude", 1, 1), timedelta(hours=1))
        session.commit()
    local_cli_usage(engine, state_dir, _USER, now=_NOW)
    with Session(engine) as session:
        row = session.get(ExecutionBudgetModel, budget)
        assert (row.settled_units, row.billed_cents, row.wallet_settled_units) == (0, 0, 0)
        assert session.scalar(select(func.count()).select_from(ExecutionUsageEvidenceModel)) == 1


def _client(engine) -> TestClient:
    """Build a client over the usage router only.

    Args:
        engine: The test database.

    Returns:
        A test client signed in as the local user.
    """
    app = FastAPI()
    app.include_router(create_local_cli_usage_router(job_store=SimpleNamespace(engine=engine)))
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser(username=_USER, role="user", groups=())
    return TestClient(app)


def test_route_is_404_and_hidden_outside_local_mode(engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hosted deployments answer 404 and keep the route out of the schema."""
    monkeypatch.setattr(settings, "local_mode", False)
    client = _client(engine)
    assert client.get("/local/cli-usage").status_code == 404
    assert "/local/cli-usage" not in client.app.openapi()["paths"]


def test_route_answers_in_local_mode(engine, state_dir, monkeypatch: pytest.MonkeyPatch) -> None:
    """In local mode the route returns the user's usage document."""
    monkeypatch.setattr(settings, "local_mode", True)
    with Session(engine) as session:
        # The route reads the real clock, so this call settles five minutes before now.
        age = _NOW - datetime.now(UTC) + timedelta(minutes=5)
        _call(session, _budget(session), _ok("codex", 4, 2), age)
        session.commit()
    response = _client(engine).get("/local/cli-usage")
    assert response.status_code == 200
    codex = _by_cli(response.json())["codex"]
    assert codex["windows"]["5h"]["input_tokens"] == 4
