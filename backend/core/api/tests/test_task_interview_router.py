"""Tests for the submission wizard's task-interview SSE endpoint.

Mounts the code-agent router with the engine's stream monkeypatched, so the
wire contract (event framing, argument forwarding, model routing, the free
gate, the opening size gate and error translation) is covered without an LLM.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from .. import model_catalog, model_router
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..routers import code_agent as code_agent_router
from ..routers.code_agent import create_code_agent_router

_ALICE = AuthenticatedUser(username="alice", role="user", groups=())
_URL = "/optimizations/task-interview"

_BODY = {
    "turns": [
        {"role": "assistant", "content": "What do you want to optimize?"},
        {"role": "user", "content": "My support prompt"},
    ],
    "objective": "Resolve tickets in one reply.",
    "artifact_kind": "dspy_program",
    "starting_version": "Answer the ticket.",
    "sample_cases": [{"ticket": "hi"}],
    "locale": "en",
}

_DONE = {
    "message": "Confirm the card.",
    "options": [],
    "done": True,
    "objective": "Resolve tickets in one reply.",
    "background": "",
    "artifact_kind": "dspy_program",
    "starting_version": "Answer the ticket.",
    "artifact_rationale": "It is an LLM prompt.",
    "model": "m",
}


def _client() -> TestClient:
    """Mount the code-agent router authed as Alice."""
    app = FastAPI()
    app.include_router(create_code_agent_router())
    app.dependency_overrides[get_authenticated_user] = lambda: _ALICE

    @app.exception_handler(DomainError)
    async def _domain_error_handler(_request, exc: DomainError) -> JSONResponse:
        """Mirror the app-level envelope so tests can assert on ``code``."""
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "code": exc.code, "params": exc.params},
        )

    return TestClient(app)


def _catalog(monkeypatch) -> None:
    """Pin a one-model catalog so routing stays hermetic."""
    catalog = SimpleNamespace(
        models=[
            SimpleNamespace(
                value="openai/gpt-test", is_default=True, reasoning_efforts=None, reasoning_default_enabled=None
            )
        ]
    )
    monkeypatch.setattr(model_catalog, "get_catalog_cached", lambda: catalog)
    monkeypatch.setattr(model_router, "get_catalog_cached", lambda: catalog)


def _free_gate(monkeypatch, calls: dict[str, Any]) -> None:
    """Record the balance gate and the wizard quota."""
    monkeypatch.setattr(code_agent_router, "enforce_llm_balance", lambda *_a: calls.setdefault("balance", True))
    monkeypatch.setattr(code_agent_router, "consume_wizard_agent_turn", lambda *_a: calls.setdefault("quota", True))


def test_streams_events_and_forwards_the_draft(monkeypatch) -> None:
    """The route relays engine events as SSE and forwards the draft and routing."""
    _catalog(monkeypatch)
    seen: dict[str, Any] = {}

    async def fake_stream(**kwargs: Any) -> Any:
        """Record the forwarded kwargs and yield a two-event stream."""
        seen.update(kwargs)
        yield {"event": "message_patch", "data": {"chunk": "Confirm"}}
        yield {"event": "interview_done", "data": _DONE}

    monkeypatch.setattr(code_agent_router, "task_interview_turn_stream", fake_stream)
    monkeypatch.setattr(code_agent_router, "consume_wizard_agent_turn", lambda *_a: None)
    resp = _client().post(_URL, json={**_BODY, "model": "openai/gpt-test", "reasoning_effort": "high"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "event: turn_started" in resp.text
    assert "event: message_patch" in resp.text
    assert "event: interview_done" in resp.text
    assert "event: turn_end" in resp.text
    assert seen["turns"] == _BODY["turns"]
    assert seen["objective"] == _BODY["objective"]
    assert seen["artifact_kind"] == "dspy_program"
    assert seen["starting_version"] == "Answer the ticket."
    assert seen["sample_cases"] == [{"ticket": "hi"}]
    assert seen["locale"] == "en"
    assert seen["model"] == "openai/gpt-test"
    assert seen["reasoning_effort"] == "high"


def test_without_model_runs_the_interview_default(monkeypatch) -> None:
    """An absent model runs the interview pick."""
    _catalog(monkeypatch)
    monkeypatch.setattr(model_router, "interview_model_id", lambda: "openai/gpt-test")
    seen: dict[str, Any] = {}

    async def fake_stream(**kwargs: Any) -> Any:
        """Record the kwargs and finish at once."""
        seen.update(kwargs)
        yield {"event": "interview_done", "data": _DONE}

    monkeypatch.setattr(code_agent_router, "task_interview_turn_stream", fake_stream)
    monkeypatch.setattr(code_agent_router, "consume_wizard_agent_turn", lambda *_a: None)
    assert _client().post(_URL, json=_BODY).status_code == 200
    assert seen["model"] == "openai/gpt-test"


def test_rejects_an_unknown_model(monkeypatch) -> None:
    """A non-catalog interview model is refused before any LLM spend."""
    _catalog(monkeypatch)
    monkeypatch.setattr(code_agent_router, "consume_wizard_agent_turn", lambda *_a: None)
    resp = _client().post(_URL, json={**_BODY, "model": "openai/not-a-model"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "models.unknown_model"


def test_rejects_an_invalid_artifact_kind() -> None:
    """The draft is validated against the shared contract."""
    assert _client().post(_URL, json={**_BODY, "artifact_kind": "spreadsheet"}).status_code == 422


def test_turn_is_free_and_counted(monkeypatch) -> None:
    """The interview counts against the daily cap and never touches the balance."""
    _catalog(monkeypatch)
    calls: dict[str, Any] = {}
    _free_gate(monkeypatch, calls)

    async def fake_stream(**_kwargs: Any) -> Any:
        """Finish at once."""
        yield {"event": "interview_done", "data": _DONE}

    monkeypatch.setattr(code_agent_router, "task_interview_turn_stream", fake_stream)
    assert _client().post(_URL, json=_BODY).status_code == 200
    assert calls == {"quota": True}


def test_daily_limit_is_a_429(monkeypatch) -> None:
    """At the cap the turn is refused before any model work starts."""

    def at_cap(*_args: Any) -> None:
        """Refuse like the quota does at the cap."""
        raise DomainError("wizard_agent.daily_limit_reached", status=429, limit=50)

    monkeypatch.setattr(code_agent_router, "consume_wizard_agent_turn", at_cap)
    resp = _client().post(_URL, json=_BODY)
    assert resp.status_code == 429
    assert resp.json()["code"] == "wizard_agent.daily_limit_reached"


def test_engine_failure_becomes_an_error_event(monkeypatch) -> None:
    """An engine exception becomes a terminal error event, not a broken stream."""
    _catalog(monkeypatch)

    async def failing_stream(**_: Any) -> Any:
        """Blow up before yielding anything."""
        raise RuntimeError("provider down")
        yield  # pragma: no cover - marks this as a generator

    monkeypatch.setattr(code_agent_router, "task_interview_turn_stream", failing_stream)
    monkeypatch.setattr(code_agent_router, "consume_wizard_agent_turn", lambda *_a: None)
    resp = _client().post(_URL, json=_BODY)
    assert resp.status_code == 200
    assert "event: error" in resp.text
    assert "submit.v2.task.interview_failed" in resp.text


def test_oversized_opening_posts_the_fixed_opening(monkeypatch) -> None:
    """An opening turn over the size budget streams the fixed opening with no model call."""

    async def must_not_run(**_: Any) -> Any:
        """Fail if the engine is reached."""
        raise AssertionError("engine called")
        yield  # pragma: no cover - marks this as a generator

    monkeypatch.setattr(code_agent_router, "task_interview_turn_stream", must_not_run)
    monkeypatch.setattr(code_agent_router, "fits_kickoff_budget", lambda _size: False)
    resp = _client().post(_URL, json={**_BODY, "turns": []})
    assert resp.status_code == 200
    assert "event: kickoff_oversized" in resp.text


def test_later_turns_skip_the_size_gate(monkeypatch) -> None:
    """Only the opening turn is size-gated."""
    _catalog(monkeypatch)

    async def fake_stream(**_kwargs: Any) -> Any:
        """Finish at once."""
        yield {"event": "interview_done", "data": _DONE}

    monkeypatch.setattr(code_agent_router, "task_interview_turn_stream", fake_stream)
    monkeypatch.setattr(code_agent_router, "fits_kickoff_budget", lambda _size: False)
    monkeypatch.setattr(code_agent_router, "consume_wizard_agent_turn", lambda *_a: None)
    resp = _client().post(_URL, json=_BODY)
    assert "event: interview_done" in resp.text


def test_a_full_repo_context_does_not_trip_the_opening_budget(monkeypatch) -> None:
    """The repository context is capped by its schema, so the opening budget skips it."""
    _catalog(monkeypatch)

    async def fake_stream(**_kwargs: Any) -> Any:
        """Finish at once."""
        yield {"event": "interview_done", "data": _DONE}

    monkeypatch.setattr(code_agent_router, "task_interview_turn_stream", fake_stream)
    monkeypatch.setattr(code_agent_router, "consume_wizard_agent_turn", lambda *_a: None)
    context = {"repository": "me/site", "readme": "é" * 8000, "paths": ["p" * 300] * 400}
    resp = _client().post(_URL, json={**_BODY, "turns": [], "repo_context": context})
    assert "event: interview_done" in resp.text
    assert "event: kickoff_oversized" not in resp.text


def test_forwards_the_target_and_repo_context(monkeypatch) -> None:
    """The draft target and the repository context reach the engine as plain data."""
    _catalog(monkeypatch)
    seen: dict[str, Any] = {}

    async def fake_stream(**kwargs: Any) -> Any:
        """Record the kwargs and finish at once."""
        seen.update(kwargs)
        yield {"event": "interview_done", "data": _DONE}

    monkeypatch.setattr(code_agent_router, "task_interview_turn_stream", fake_stream)
    monkeypatch.setattr(code_agent_router, "consume_wizard_agent_turn", lambda *_a: None)
    context = {"repository": "me/site", "readme": "# Site", "paths": ["index.html"], "truncated": True}
    resp = _client().post(_URL, json={**_BODY, "target": "repo", "repo_context": context})
    assert resp.status_code == 200
    assert seen["target"] == "repo"
    assert seen["repo_context"] == context


def test_without_repo_context_forwards_none(monkeypatch) -> None:
    """An older client that sends neither field still runs the turn."""
    _catalog(monkeypatch)
    seen: dict[str, Any] = {}

    async def fake_stream(**kwargs: Any) -> Any:
        """Record the kwargs and finish at once."""
        seen.update(kwargs)
        yield {"event": "interview_done", "data": _DONE}

    monkeypatch.setattr(code_agent_router, "task_interview_turn_stream", fake_stream)
    monkeypatch.setattr(code_agent_router, "consume_wizard_agent_turn", lambda *_a: None)
    assert _client().post(_URL, json=_BODY).status_code == 200
    assert seen["target"] is None
    assert seen["repo_context"] is None


@pytest.mark.parametrize(
    "context",
    [
        {"repository": "me/site", "readme": "x" * 8001},
        {"repository": "me/site", "paths": ["a"] * 401},
        {"repository": "me/site", "paths": ["a" * 1025]},
        {"repository": "x" * 202},
    ],
)
def test_rejects_an_oversized_repo_context(context: dict[str, Any]) -> None:
    """The README, the path list and each path are capped before any model call."""
    assert _client().post(_URL, json={**_BODY, "repo_context": context}).status_code == 422


def test_rejects_an_unknown_target() -> None:
    """The target is one of the optimizer targets."""
    assert _client().post(_URL, json={**_BODY, "target": "spreadsheet"}).status_code == 422
