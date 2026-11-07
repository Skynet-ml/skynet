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

from ...billing.budgets import BudgetConflictError
from ...config import settings
from ...storage.models import Base, McpRunDraftModel, UserModel
from .. import remote_mcp_repo
from ..errors import DomainError
from ..remote_mcp import mount_remote_mcp
from ..remote_mcp_repo import register_repo_tools
from .test_remote_mcp import (
    READ_TOKEN,
    RESOURCE,
    USER,
    _call,
    _error_text,
    _payload,
    _rpc,
    _seed_token,
    _Store,
)

WRITE_TOKEN = "skym_at_read-write-token"
SCORER = 'def score(repo_path):\n    return 1.0, "ok"\n'


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

    def submit(request: Any, *, service: Any, job_store: Any, current_user: Any, idempotency_key: str) -> Any:
        """Record the submission instead of starting a run."""
        calls["submits"].append({"username": current_user.username, "key": idempotency_key})
        return SimpleNamespace(model_dump=lambda mode: {"optimization_id": f"opt-{len(calls['submits'])}"})

    monkeypatch.setattr(remote_mcp_repo, "submit_repo_payload", submit)
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
    body = _step(
        client,
        "repo_draft_set_data",
        draft_id=draft_id,
        cases=[{"input": {"q": 1}, "expected": 2}, {"input": {"q": 2}}],
    )
    assert body["issues"] == []
    assert body["notes"] == ["The run has 2 inline cases."]
    body = _step(client, "repo_draft_set_scorer", draft_id=draft_id, metric_code=SCORER)
    assert body["issues"] == []
    assert body["notes"] == ["The scorer function score does not take the case, so every case gets the same score."]
    body = _step(client, "repo_draft_set_scorer", draft_id=draft_id, metric_code="def score(path, case):\n  return 1\n")
    assert "returns a bare number" in body["issues"][0]
    assert body["next_tool"] == "repo_draft_set_scorer"
    body = _step(
        client,
        "repo_draft_set_scorer",
        draft_id=draft_id,
        metric_code="def score(path, case):\n  def inner():\n    return 1\n  return {'score': 1.0, 'feedback': 'ok'}\n",
    )
    assert body["issues"] == []
    assert body["notes"] == []
    assert body["next_tool"] == "repo_draft_set_models"


@pytest.mark.parametrize(
    ("cases", "first"),
    [
        ([{"q": 1}], "Case 1 has q"),
        ([{"input": 1}, {"expected": 2}], "Case 2 has expected"),
        ([{"input": 1, "expected": 2, "note": "x"}], "Case 1 has expected, input, note"),
        ([{"input": 1}, {}], "Case 2 has no keys"),
    ],
)
def test_inline_case_with_another_shape_is_an_issue(harness: Any, cases: list[dict[str, Any]], first: str) -> None:
    """Refuse inline cases that are not {"input", "expected"}, like staged cases (E42)."""
    client, _engine, _calls = harness
    draft_id = _step(client, "repo_draft_start", objective="x")["draft_id"]
    body = _step(client, "repo_draft_set_data", draft_id=draft_id, cases=cases)
    assert len(body["issues"]) == 1
    assert body["issues"][0].startswith('Each inline case must be {"input": ..., "expected": ...}')
    assert first in body["issues"][0]
    assert body["next_tool"] == "repo_draft_set_data"


def test_inline_case_issue_counts_the_bad_cases() -> None:
    """Name the first bad case and count all of them, not one issue for each case."""
    cases = [{"input": 1}, {"q": 1}, {"q": 2}]
    assert remote_mcp_repo.case_shape_issues(cases) == [
        'Each inline case must be {"input": ..., "expected": ...}; "expected" is optional. '
        "Case 2 has q, and 2 of 3 cases have another shape."
    ]


def test_data_tool_says_the_candidate_gets_only_input(harness: Any) -> None:
    """Tell the agent in the tool description that the candidate gets only the case input (E42)."""
    client, _engine, _calls = harness
    resp = _rpc(client, "tools/list", {}, WRITE_TOKEN, None)
    assert resp.status_code == 200, resp.text
    tools = resp.json()["result"]["tools"]
    schema = next(tool for tool in tools if tool["name"] == "repo_draft_set_data")["inputSchema"]
    assert "The candidate gets only input" in json.dumps(schema)


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


def test_submit_waits_while_other_usage_settles(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer in_progress, without an error, while the wallet has pending usage."""
    client, _engine, _calls = harness

    def pending(request: Any, **kwargs: Any) -> Any:
        """Refuse the submission like a wallet with unsettled usage."""
        raise DomainError("budget.pending", status=409)

    monkeypatch.setattr(remote_mcp_repo, "submit_repo_payload", pending)
    draft_id = _complete_draft(client)
    body = _payload(_call(client, "repo_draft_submit", {"draft_id": draft_id, "max_cost_cents": 500, "confirm": True}))
    assert body["status"] == "in_progress"
    assert body["retry_after_seconds"] == remote_mcp_repo._PENDING_RETRY_SECONDS


def test_submit_refuses_a_lower_limit_for_a_used_key(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse a retry whose key already made a higher budget, and name its run."""
    client, _engine, calls = harness

    class _Budgets:
        """Stand in for a budget service that holds a 900-cent budget for the key."""

        def __init__(self, *, engine: Any) -> None:
            """Accept the engine."""

        def find_by_creation_key(self, username: str, idempotency_key: str) -> Any:
            """Return the budget the first call made."""
            return SimpleNamespace(total_cents=900, job_id="opt-old")

        def create(self, username: str, total_cents: int, **kwargs: Any) -> Any:
            """Refuse another total for the same key."""
            raise BudgetConflictError("total differs")

    monkeypatch.setattr(remote_mcp_repo, "BudgetService", _Budgets)
    monkeypatch.setattr(
        remote_mcp_repo,
        "resolve_submission_replay_keys",
        lambda request, *, username, workflow, supplied_key: SimpleNamespace(budget="budget-key", job=supplied_key),
    )
    draft_id = _complete_draft(client)
    arguments = {"draft_id": draft_id, "max_cost_cents": 500, "confirm": True}
    error = json.loads(_error_text(_call(client, "repo_draft_submit", arguments)))
    assert (error["code"], error["optimization_id"]) == ("budget.conflict", "opt-old")
    assert calls["submits"] == []
    assert _payload(_call(client, "repo_draft_submit", {**arguments, "max_cost_cents": 900})) == {
        "optimization_id": "opt-1",
        "next_tool": "get_run",
    }


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


def test_models_step_refuses_a_source_this_server_cannot_run(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Report managed models in the Mac app and subscription models on a hosted server, like the submit does."""
    client, _engine, _calls = harness
    draft_id = _complete_draft(client)
    body = _step(
        client, "repo_draft_set_models", draft_id=draft_id, reflection_model="openai/gpt-5", token_source="subscription"
    )
    assert "only in the Mac app" in body["issues"][0]
    monkeypatch.setattr(settings, "local_mode", True)
    body = _step(client, "repo_draft_set_models", draft_id=draft_id, reflection_model="openai/gpt-5")
    assert "cannot use managed models" in body["issues"][0]
    review = _payload(_call(client, "repo_draft_review", {"draft_id": draft_id}))
    assert review["ready"] is False
    body = _step(
        client, "repo_draft_set_models", draft_id=draft_id, reflection_model="openai/gpt-5", token_source="subscription"
    )
    assert "subscription/<codex|claude>/<model>" in body["issues"][0]


def test_subscription_draft_is_ready_in_the_mac_app(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Read a subscription/<cli>/<slug> name as a subscription model on that CLI, and price only the boxes."""
    client, _engine, _calls = harness
    monkeypatch.setattr(settings, "local_mode", True)
    draft_id = _complete_draft(client)
    body = _step(client, "repo_draft_set_models", draft_id=draft_id, reflection_model="subscription/claude/claude-x")
    assert body["issues"] == []
    review = _payload(_call(client, "repo_draft_review", {"draft_id": draft_id}))
    assert review["issues"] == []
    assert review["ready"] is True
    assert review["min_max_cost_cents"] == 41
    with Session(_engine) as session:
        spec = session.get(McpRunDraftModel, draft_id).spec
    config = remote_mcp_repo.build_request(spec).reflection_model_settings
    assert (config.token_source, config.cli_provider) == ("subscription", "claude")


def test_expired_draft_is_refused(harness: Any) -> None:
    """Refuse a draft that nobody changed for more than a week, before any purge runs."""
    client, engine, _calls = harness
    with Session(engine) as session:
        session.add(
            McpRunDraftModel(id="stale", username=USER, spec={}, updated_at=datetime.now(UTC) - timedelta(days=8))
        )
        session.commit()
    assert "expired" in _error_text(_call(client, "repo_draft_review", {"draft_id": "stale"}))
    assert "expired" in _error_text(
        _call(client, "repo_draft_set_scorer", {"draft_id": "stale", "metric_code": SCORER})
    )


def test_save_step_locks_the_draft_row(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Load the draft with a row lock, so parallel steps do not overwrite each other's parts."""
    client, _engine, _calls = harness
    draft_id = _step(client, "repo_draft_start", objective="x")["draft_id"]
    locks: list[bool] = []
    load = remote_mcp_repo._load_draft

    def spy(session: Session, draft: str, username: str, *, lock: bool = False) -> McpRunDraftModel:
        """Record the lock flag of each load."""
        locks.append(lock)
        return load(session, draft, username, lock=lock)

    monkeypatch.setattr(remote_mcp_repo, "_load_draft", spy)
    _step(client, "repo_draft_set_scorer", draft_id=draft_id, metric_code=SCORER)
    assert locks == [True]


@pytest.mark.parametrize(
    "code",
    [
        "from mylib import score\n",
        "class S:\n  def __call__(self, path):\n    return 1.0, 'ok'\n\nscore = S()\n",
        "def _mk():\n  return lambda path: (1.0, 'ok')\n\nscore = _mk()\n",
        "@with_feedback\ndef score(path):\n  return 1.0\n",
        "def score(path):\n  return 1.0\n\nscore = add_feedback(score)\n",
        "from lib import score\n\ndef metric(path):\n  return 1.0\n",
    ],
)
def test_imported_or_assigned_scorer_is_a_note(code: str) -> None:
    """Leave an imported or assigned scorer to the run's setup check, like the runner accepts it."""
    issues, notes = remote_mcp_repo.scorer_issues(code, has_cases=True)
    assert issues == []
    assert "imported, assigned or decorated" in notes[0]


@pytest.mark.parametrize("value", ["1", "-1", "0.5", "True", "-0.5"])
def test_bare_number_return_is_an_issue(value: str) -> None:
    """Flag a number, negative number or bool literal, which the runner refuses."""
    issues, _notes = remote_mcp_repo.scorer_issues(f"def score(path):\n  return {value}\n", has_cases=False)
    assert "returns a bare number" in issues[0]


def test_bare_number_on_an_error_path_is_an_issue() -> None:
    """Flag a bare number on one path too, because the first one stops the whole run."""
    code = "def score(path):\n  if not path:\n    return 0.0\n  return 1.0, 'ok'\n"
    issues, _notes = remote_mcp_repo.scorer_issues(code, has_cases=False)
    assert "returns a bare number" in issues[0]


@pytest.mark.parametrize(
    ("code", "issue"),
    [
        ("async def score(p, case):\n  return 1.0, 'ok'\n", "is async"),
        ("def score(p, case):\n  yield 1.0, 'ok'\n", "uses yield"),
        ("def score(p, case, output, extra):\n  return 1.0, 'ok'\n", "needs more arguments"),
        ("def score(p, *, mode):\n  return 1.0, 'ok'\n", "needs more arguments"),
        ("def score(p):\n  return " + "-" * 200_000 + "1\n", "cannot parse"),
    ],
)
def test_scorer_the_runner_cannot_call_is_an_issue(code: str, issue: str) -> None:
    """Flag a scorer that the runner cannot call or parse, before the paid run."""
    issues, _notes = remote_mcp_repo.scorer_issues(code, has_cases=True)
    assert issue in issues[0]


def test_optional_extra_arguments_are_fine() -> None:
    """Accept extra arguments with defaults and a nested generator."""
    code = "def score(p, case=None, extra=1, *, mode='a'):\n  def gen():\n    yield 1\n  return 1.0, 'ok'\n"
    assert remote_mcp_repo.scorer_issues(code, has_cases=True) == ([], [])


@pytest.mark.parametrize("signature", ["score(repo_path)", "score(repo_path, case)", "score(repo_path, case, output)"])
def test_each_scorer_signature_is_accepted(signature: str) -> None:
    """Accept the scorer with the checkout, the case, and the [run] entry's output for the case (E39)."""
    code = f"def {signature}:\n  return 1.0, 'ok'\n"
    issues, _notes = remote_mcp_repo.scorer_issues(code, has_cases=True)
    assert issues == []


def test_missing_scorer_issue_names_all_three_signatures() -> None:
    """Name every signature the runner accepts when the code defines no scorer."""
    issues, _notes = remote_mcp_repo.scorer_issues("x = 1\n", has_cases=True)
    assert all(
        name in issues[0] for name in ("score(repo_path)", "score(repo_path, case)", "score(repo_path, case, output)")
    )


def test_scorer_with_four_required_arguments_names_the_output() -> None:
    """Refuse a fourth required argument and name the three the runner passes."""
    issues, _notes = remote_mcp_repo.scorer_issues("def score(p, c, o, x):\n  return 1.0, 'ok'\n", True)
    assert "(repo_path, case, output)" in issues[0]
