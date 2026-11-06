"""Tests for the step-by-step repository run tools of the remote MCP server (SKY-320).

The app mounts the real remote MCP server with the repository draft tools
over in-memory SQLite. GitHub, the model prices, the wallet and the final
submission are replaced in the ``remote_mcp_repo`` namespace, so each test
checks the feedback of a step and what the submit hands to the run path.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ...config import settings
from ...storage.models import Base, McpRunDraftModel, UserModel
from .. import remote_mcp_repo
from ..remote_mcp import mount_remote_mcp
from ..remote_mcp_repo import register_repo_tools
from .test_remote_mcp import (
    READ_TOKEN,
    RESOURCE,
    USER,
    _call,
    _error_text,
    _payload,
    _seed_token,
    _Store,
)

WRITE_TOKEN = "skym_at_read-write-token"
SCORER = "def score(repo_path):\n    return 1.0\n"


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Build an app with the repository draft tools and faked outside calls.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Yields:
        A ``(client, engine, calls)`` tuple; ``calls`` records the submissions.
    """
    monkeypatch.setattr(settings, "mcp_resource_url", RESOURCE)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(UserModel(email=USER, name="Alice Example", password_hash="x"))
        session.commit()
    _seed_token(engine, READ_TOKEN, "runs:read", "mg_read")
    _seed_token(engine, WRITE_TOKEN, "runs:read runs:write", "mg_write")
    calls: dict[str, Any] = {"submits": []}
    tree = {"entries": [{"path": "src", "type": "dir"}, {"path": "src/app.py", "type": "file"}], "truncated": False}
    monkeypatch.setattr(remote_mcp_repo, "_github_token", lambda job_store, username: "gh")
    monkeypatch.setattr(remote_mcp_repo.github, "tree_entries", lambda token, name, ref: tree)
    monkeypatch.setattr(remote_mcp_repo, "box_hold_cents", lambda: Decimal("40.2"))
    monkeypatch.setattr(
        remote_mcp_repo,
        "call_hold_cents",
        lambda name, source, *, client: None if name == "unpriced/model" else Decimal("150.5"),
    )

    class _Billing:
        """Stand in for the billing service."""

        def __init__(self, engine: Any) -> None:
            """Accept the engine."""

        def spendable_cents(self, username: str) -> int:
            """Return a fixed balance."""
            return 2500

    monkeypatch.setattr(remote_mcp_repo, "StripeBillingService", _Billing)
    monkeypatch.setattr(
        remote_mcp_repo,
        "resolve_submission_replay_keys",
        lambda request, *, username, workflow, supplied_key: SimpleNamespace(budget=None, job=supplied_key),
    )

    def submit(username: str, replay_key: str | None, work: Any) -> Any:
        """Record the submission instead of starting a run."""
        calls["submits"].append({"username": username, "key": replay_key})
        return SimpleNamespace(model_dump=lambda mode: {"optimization_id": f"opt-{len(calls['submits'])}"})

    monkeypatch.setattr(remote_mcp_repo, "_submit_within_proxy_timeout", submit)
    app = FastAPI()
    mount_remote_mcp(
        app,
        service="svc",
        job_store=_Store(engine),
        get_worker=object,
        extensions=[partial(register_repo_tools, service="svc", job_store=_Store(engine))],
    )
    with TestClient(app) as client:
        yield client, engine, calls


def _step(client: TestClient, tool: str, **arguments: Any) -> dict[str, Any]:
    """Call a draft tool with the write token and return its payload."""
    return _payload(_call(client, tool, arguments))


def _complete_draft(client: TestClient) -> str:
    """Build a draft with every required step and return its id."""
    draft_id = _step(client, "repo_draft_start", objective="Make the tests faster.")["draft_id"]
    _step(client, "repo_draft_set_repository", draft_id=draft_id, repository="acme/app", editable_paths=["src"])
    _step(client, "repo_draft_set_scorer", draft_id=draft_id, metric_code=SCORER)
    _step(client, "repo_draft_set_models", draft_id=draft_id, reflection_model="openai/gpt-5")
    _step(client, "repo_draft_set_engine", draft_id=draft_id, engine="gepa")
    return draft_id


def test_start_lists_engines_and_the_first_step(harness: Any) -> None:
    """Answer the start with the engine catalog and the repository step."""
    client, _engine, _calls = harness
    body = _step(client, "repo_draft_start", objective="Make the tests faster.")
    assert body["next_tool"] == "repo_draft_set_repository"
    assert body["remaining_steps"] == ["repository", "scorer", "models", "engine"]
    assert "gepa" in {engine["id"] for engine in body["engines"]}


def test_each_step_gives_feedback_and_the_next_tool(harness: Any) -> None:
    """Chain the steps: each answer names the next required step."""
    client, _engine, _calls = harness
    draft_id = _step(client, "repo_draft_start", objective="Make the tests faster.")["draft_id"]
    body = _step(client, "repo_draft_set_repository", draft_id=draft_id, repository="acme/app", editable_paths=["src"])
    assert body["issues"] == []
    assert body["top_level_paths"] == ["src"]
    assert body["next_tool"] == "repo_draft_set_scorer"
    body = _step(client, "repo_draft_set_data", draft_id=draft_id, cases=[{"q": 1}, {"q": 2}])
    assert body["notes"] == ["The run has 2 inline cases."]
    body = _step(client, "repo_draft_set_scorer", draft_id=draft_id, metric_code=SCORER)
    assert body["issues"] == ["The scorer function score takes too few arguments. Use score(repo_path, case)."]
    assert body["next_tool"] == "repo_draft_set_scorer"
    body = _step(client, "repo_draft_set_scorer", draft_id=draft_id, metric_code="def score(path, case):\n  return 1\n")
    assert body["issues"] == []
    assert body["next_tool"] == "repo_draft_set_models"


def test_step_issues_point_back_at_the_step(harness: Any) -> None:
    """Report a missing path, a syntax error, an unpriced model and an unknown engine."""
    client, _engine, _calls = harness
    draft_id = _step(client, "repo_draft_start", objective="x")["draft_id"]
    body = _step(client, "repo_draft_set_repository", draft_id=draft_id, repository="acme/app", editable_paths=["lib"])
    assert body["issues"] == ["Editable path 'lib' does not exist in the repository."]
    body = _step(client, "repo_draft_set_scorer", draft_id=draft_id, metric_code="def score(:")
    assert "syntax error" in body["issues"][0]
    body = _step(client, "repo_draft_set_models", draft_id=draft_id, reflection_model="unpriced/model")
    assert "no verified price" in body["issues"][0]
    body = _step(client, "repo_draft_set_engine", draft_id=draft_id, engine="nope")
    assert body["issues"][0].startswith("Unknown engine 'nope'.")
    assert body["next_tool"] == "repo_draft_set_engine"


def test_another_account_cannot_load_the_draft(harness: Any) -> None:
    """Refuse a draft id that belongs to another account."""
    client, engine, _calls = harness
    with Session(engine) as session:
        row = McpRunDraftModel(username="bob@example.test", spec={}, updated_at=datetime.now(UTC))
        session.add(row)
        session.commit()
        draft_id = row.id
    assert "Unknown draft_id" in _error_text(_call(client, "repo_draft_review", {"draft_id": draft_id}))


def test_review_prices_a_complete_draft(harness: Any) -> None:
    """Price the floor as the boxes' hold plus one call of the dearest model."""
    client, _engine, _calls = harness
    draft_id = _complete_draft(client)
    body = _payload(_call(client, "repo_draft_review", {"draft_id": draft_id}, token=READ_TOKEN))
    assert body["ready"] is True
    assert body["min_max_cost_cents"] == 41 + 151
    assert body["suggested_max_cost_cents"] == 500
    assert body["spendable_cents"] == 2500
    assert body["next_tool"] == "repo_draft_submit"
    assert "scorer" not in body["draft"]


def test_review_of_an_incomplete_draft_names_the_missing_steps(harness: Any) -> None:
    """Report each missing step and give no price."""
    client, _engine, _calls = harness
    draft_id = _step(client, "repo_draft_start", objective="x")["draft_id"]
    body = _payload(_call(client, "repo_draft_review", {"draft_id": draft_id}))
    assert body["ready"] is False
    assert len(body["issues"]) == 4
    assert "min_max_cost_cents" not in body


def test_submit_needs_confirmation(harness: Any) -> None:
    """Refuse a submit until the user agreed to the limit."""
    client, _engine, calls = harness
    draft_id = _complete_draft(client)
    text = _error_text(
        _call(client, "repo_draft_submit", {"draft_id": draft_id, "max_cost_cents": 500, "confirm": False})
    )
    assert "confirm=true" in text
    assert calls["submits"] == []


def test_submit_needs_runs_write(harness: Any) -> None:
    """Refuse a submit from a read-only token."""
    client, _engine, calls = harness
    draft_id = _complete_draft(client)
    arguments = {"draft_id": draft_id, "max_cost_cents": 500, "confirm": True}
    assert "runs:write" in _error_text(_call(client, "repo_draft_submit", arguments, token=READ_TOKEN))
    assert calls["submits"] == []


def test_submit_refuses_an_incomplete_draft(harness: Any) -> None:
    """Answer not_ready with the issues instead of starting a run."""
    client, _engine, calls = harness
    draft_id = _step(client, "repo_draft_start", objective="x")["draft_id"]
    text = _error_text(
        _call(client, "repo_draft_submit", {"draft_id": draft_id, "max_cost_cents": 500, "confirm": True})
    )
    assert json.loads(text)["code"] == "draft.not_ready"
    assert calls["submits"] == []


def test_submit_refuses_a_limit_below_the_floor(harness: Any) -> None:
    """Answer below_minimum with the floor."""
    client, _engine, calls = harness
    draft_id = _complete_draft(client)
    text = _error_text(
        _call(client, "repo_draft_submit", {"draft_id": draft_id, "max_cost_cents": 100, "confirm": True})
    )
    assert json.loads(text) == {
        "code": "budget.below_minimum",
        "detail": "max_cost_cents 100 is below this run's minimum of 192 cents.",
        "min_max_cost_cents": 192,
    }
    assert calls["submits"] == []


def test_submit_default_key_follows_the_draft(harness: Any) -> None:
    """Reuse the key for the same draft, and change it when the draft changes."""
    client, _engine, calls = harness
    draft_id = _complete_draft(client)
    arguments = {"draft_id": draft_id, "max_cost_cents": 500, "confirm": True}
    body = _payload(_call(client, "repo_draft_submit", arguments))
    assert body == {"optimization_id": "opt-1", "next_tool": "get_run"}
    _payload(_call(client, "repo_draft_submit", arguments))
    _step(client, "repo_draft_set_budget", draft_id=draft_id, max_scorer_runs=50)
    _payload(_call(client, "repo_draft_submit", arguments))
    first, again, changed = (call["key"] for call in calls["submits"])
    assert first == again
    assert first != changed
    assert first.startswith(f"mcp-draft-{draft_id}-")
    assert calls["submits"][0]["username"] == USER


def test_start_purges_old_drafts_and_caps_the_count(harness: Any) -> None:
    """Delete drafts older than a week and keep at most the cap per account."""
    client, engine, _calls = harness
    old = datetime.now(UTC) - timedelta(days=8)
    recent = datetime.now(UTC) - timedelta(hours=1)
    with Session(engine) as session:
        session.add(McpRunDraftModel(id="old", username=USER, spec={}, updated_at=old))
        for index in range(remote_mcp_repo._MAX_DRAFTS_PER_ACCOUNT):
            session.add(McpRunDraftModel(id=f"d{index:02d}", username=USER, spec={}, updated_at=recent))
        session.commit()
    _step(client, "repo_draft_start", objective="x")
    with Session(engine) as session:
        ids = set(session.scalars(select(McpRunDraftModel.id).where(McpRunDraftModel.username == USER)))
        count = session.scalar(select(func.count()).select_from(McpRunDraftModel))
    assert "old" not in ids
    assert count == remote_mcp_repo._MAX_DRAFTS_PER_ACCOUNT
