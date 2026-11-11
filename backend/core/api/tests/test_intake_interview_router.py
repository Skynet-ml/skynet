"""Tests for the onboarding intake interview SSE endpoint.

Mounts the code-agent router with the engine's stream monkeypatched, so the
wire contract (event framing, argument forwarding, caps, error translation,
auth) and the new-account billing path are covered without an LLM.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ...storage.models import Base, UserModel
from .. import model_catalog, model_router
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..routers import code_agent as code_agent_router
from ..routers.code_agent import create_code_agent_router

_ALICE = AuthenticatedUser(username="alice@example.com", role="user", groups=())

_CATALOG = SimpleNamespace(
    providers=[SimpleNamespace(slug="openai")],
    models=[
        SimpleNamespace(
            value="openai/cheap",
            label="cheap",
            provider="openai",
            featured=True,
            available=True,
            is_default=False,
            input_cost_per_token=1e-8,
            output_cost_per_token=1e-8,
            reasoning_mandatory=False,
            reasoning_efforts=["none", "low", "medium", "high"],
            supports_thinking=True,
            reasoning_default_enabled=True,
            default_reasoning_effort="medium",
        ),
        SimpleNamespace(
            value="openai/pricey",
            label="pricey",
            provider="openai",
            featured=True,
            available=True,
            is_default=True,
            input_cost_per_token=1e-5,
            output_cost_per_token=1e-5,
            reasoning_mandatory=False,
            reasoning_efforts=["low", "medium", "high"],
            supports_thinking=True,
            reasoning_default_enabled=False,
            default_reasoning_effort=None,
        ),
    ],
)

_BODY = {
    "phase": "billing",
    "turns": [
        {"role": "assistant", "content": "Who pays for the model calls?"},
        {"role": "user", "content": "Credits"},
    ],
    "profile": {"level": "standard"},
    "locale": "en",
}


def _client(job_store: Any = None, *, authed: bool = True) -> TestClient:
    """Mount the code-agent router, authed as Alice unless ``authed`` is false."""
    app = FastAPI()
    app.include_router(create_code_agent_router(job_store=job_store))
    if authed:
        app.dependency_overrides[get_authenticated_user] = lambda: _ALICE
    else:

        def _deny() -> None:
            """Reject like the real dependency does for an anonymous caller."""
            raise HTTPException(status_code=401, detail="unauthenticated")

        app.dependency_overrides[get_authenticated_user] = _deny

    @app.exception_handler(DomainError)
    async def _domain_error_handler(_request, exc: DomainError) -> JSONResponse:
        """Mirror the app-level envelope so tests can assert on ``code``."""
        return JSONResponse(status_code=exc.status_code, content={"code": exc.code})

    return TestClient(app)


@pytest.fixture(autouse=True)
def _catalog(monkeypatch) -> None:
    """Serve the fixed test catalog to every catalog reader."""
    monkeypatch.setattr(code_agent_router, "get_catalog_cached", lambda: _CATALOG)
    monkeypatch.setattr(model_router, "get_catalog_cached", lambda: _CATALOG)
    monkeypatch.setattr(model_catalog, "get_catalog_cached", lambda: _CATALOG)


def _record_stream(monkeypatch, seen: dict[str, Any]) -> None:
    """Replace the engine stream with one that records kwargs and yields a fixed turn."""

    async def fake_stream(**kwargs: Any) -> Any:
        """Record the forwarded kwargs and yield a short turn."""
        seen.update(kwargs)
        yield {"event": "message_patch", "data": {"chunk": "Where"}}
        yield {
            "event": "interview_done",
            "data": {
                "message": "Where?",
                "options": [],
                "phase_done": False,
                "profile_patch": {},
                "skip_phases": [],
                "skip_rest": False,
                "model": kwargs["model"],
                "served_model": None,
            },
        }

    monkeypatch.setattr(code_agent_router, "intake_turn_stream", fake_stream)


def _engine_with_user(*, completed: bool) -> Any:
    """Build an in-memory database holding Alice's account row."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            UserModel(
                email=_ALICE.username,
                name="Alice",
                password_hash="x",
                intake_completed_at=datetime.now(UTC) if completed else None,
            )
        )
        session.commit()
    return engine


def test_streams_events_and_forwards_args(monkeypatch) -> None:
    """The route relays engine events and runs the cheap model at low effort by default."""
    monkeypatch.setattr(model_router, "interview_model_id", lambda: "openai/cheap")
    seen: dict[str, Any] = {}
    _record_stream(monkeypatch, seen)
    resp = _client().post("/account/intake-interview", json=_BODY)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "event: turn_started" in resp.text
    assert "event: message_patch" in resp.text
    assert "event: interview_done" in resp.text
    assert seen["phase"] == "billing"
    assert seen["turns"] == _BODY["turns"]
    assert seen["profile"] == {"level": "standard"}
    assert seen["locale"] == "en"
    assert seen["model"] == "openai/cheap"
    assert seen["reasoning_effort"] == "low"
    assert seen["provider_slugs"] == ["openai"]
    assert "catalog_models" not in seen


def test_explicit_model_and_effort_are_honoured_for_paying_accounts(monkeypatch) -> None:
    """A paying account may pick a catalog model and effort."""
    seen: dict[str, Any] = {}
    _record_stream(monkeypatch, seen)
    resp = _client().post(
        "/account/intake-interview", json={**_BODY, "model": "openai/pricey", "reasoning_effort": "high"}
    )
    assert resp.status_code == 200
    assert seen["model"] == "openai/pricey"
    assert seen["reasoning_effort"] == "high"


def test_empty_turns_opens_the_phase(monkeypatch) -> None:
    """Empty turns are the phase's opening question."""
    seen: dict[str, Any] = {}
    _record_stream(monkeypatch, seen)
    resp = _client().post("/account/intake-interview", json={"phase": "privacy"})
    assert resp.status_code == 200
    assert seen["turns"] == []
    assert seen["profile"] == {}


@pytest.mark.parametrize(
    "body",
    [
        {**_BODY, "turns": [{"role": "user", "content": "x"}] * 9},
        {**_BODY, "phase": "models"},
        {**_BODY, "phase": "language"},
        {**_BODY, "phase": "goal"},
        {**_BODY, "phase": "source"},
        {**_BODY, "turns": [{"role": "system", "content": "x"}]},
        {**_BODY, "profile": {"trust": "x" * 20_000}},
    ],
)
def test_rejects_invalid_requests(monkeypatch, body: dict[str, Any]) -> None:
    """Over 8 turns, a phase off the agenda, a bad role or an oversized profile is a 422."""
    _record_stream(monkeypatch, {})
    assert _client().post("/account/intake-interview", json=body).status_code == 422


def test_requires_authentication(monkeypatch) -> None:
    """An anonymous caller never reaches the model."""
    _record_stream(monkeypatch, {})
    assert _client(authed=False).post("/account/intake-interview", json=_BODY).status_code == 401


def test_engine_failure_becomes_error_event(monkeypatch) -> None:
    """An engine exception becomes a terminal error event, not a broken stream."""

    async def failing_stream(**_: Any) -> Any:
        """Blow up before yielding anything."""
        raise RuntimeError("provider down")
        yield  # pragma: no cover - marks this as a generator

    monkeypatch.setattr(code_agent_router, "intake_turn_stream", failing_stream)
    resp = _client().post("/account/intake-interview", json=_BODY)
    assert resp.status_code == 200
    assert "event: error" in resp.text
    assert "intake.interview.llm_failed" in resp.text


def _broke(*_: Any) -> None:
    """Refuse the turn like an account with no credits."""
    raise DomainError("billing.insufficient_funds", status=402)


def test_zero_credit_new_account_runs_on_the_platform(monkeypatch) -> None:
    """A broke account mid-intake runs unbilled on the cheap model, under the free daily cap."""
    monkeypatch.setattr(model_router, "interview_model_id", lambda: "openai/cheap")
    seen: dict[str, Any] = {}
    _record_stream(monkeypatch, seen)
    counted: list[str] = []
    metered: list[Any] = []
    monkeypatch.setattr(code_agent_router, "enforce_llm_balance", _broke)
    monkeypatch.setattr(code_agent_router, "consume_wizard_agent_turn", lambda _engine, user: counted.append(user))
    real_meter = code_agent_router.stream_with_llm_metering

    def spy_meter(source: Any, **kwargs: Any) -> Any:
        """Record which store the turn is billed to."""
        metered.append(kwargs["job_store"])
        return real_meter(source, **kwargs)

    monkeypatch.setattr(code_agent_router, "stream_with_llm_metering", spy_meter)
    store = SimpleNamespace(engine=_engine_with_user(completed=False))
    resp = _client(store).post(
        "/account/intake-interview", json={**_BODY, "model": "openai/pricey", "reasoning_effort": "high"}
    )
    assert resp.status_code == 200
    assert "event: interview_done" in resp.text
    assert counted == [_ALICE.username]
    assert metered == [None]
    assert seen["model"] == "openai/cheap"
    assert seen["reasoning_effort"] == "low"


def test_zero_credit_account_that_finished_the_intake_is_refused(monkeypatch) -> None:
    """The platform pays only for an intake still in progress."""
    _record_stream(monkeypatch, {})
    monkeypatch.setattr(code_agent_router, "enforce_llm_balance", _broke)
    store = SimpleNamespace(engine=_engine_with_user(completed=True))
    resp = _client(store).post("/account/intake-interview", json=_BODY)
    assert resp.status_code == 402
    assert resp.json()["code"] == "billing.insufficient_funds"


def test_zero_credit_account_at_the_daily_cap_is_refused(monkeypatch) -> None:
    """The free path still honours the free agent's daily cap."""
    _record_stream(monkeypatch, {})
    monkeypatch.setattr(code_agent_router, "enforce_llm_balance", _broke)

    def capped(*_: Any) -> None:
        """Refuse like an account at today's cap."""
        raise DomainError("wizard_agent.daily_limit_reached", status=429, limit=50)

    monkeypatch.setattr(code_agent_router, "consume_wizard_agent_turn", capped)
    store = SimpleNamespace(engine=_engine_with_user(completed=False))
    resp = _client(store).post("/account/intake-interview", json=_BODY)
    assert resp.status_code == 429
