"""Tests for the submission wizard's inputs-inference endpoint.

Mounts the code-agent router with the engine monkeypatched, so the wire
contract (request validation, model routing, the free gate) is covered
without an LLM.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from ...models import InputsInferenceRequest, InputsInferenceResponse
from ...models.common import SplitFractions
from .. import model_catalog, model_router
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..routers import code_agent as code_agent_router
from ..routers.code_agent import create_code_agent_router

_ALICE = AuthenticatedUser(username="alice", role="user", groups=())
_URL = "/optimizations/infer-inputs"
_BODY = {
    "objective": "Classify support tickets.",
    "artifact_kind": "dspy_program",
    "columns": ["ticket", "label"],
    "sample_rows": [{"ticket": "refund please", "label": "billing"}],
    "row_count": 40,
    "locale": "en",
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
    monkeypatch.setattr(model_router, "interview_model_id", lambda: "openai/gpt-test")


def _fake_infer(seen: dict[str, Any]):
    """Build an ``infer_inputs`` stub that records its request."""

    def fake(req: InputsInferenceRequest, **_kwargs: Any) -> InputsInferenceResponse:
        """Record the request and answer a fixed pairs proposal."""
        seen["req"] = req
        return InputsInferenceResponse(
            mode="pairs",
            input_fields=["ticket"],
            expected_field="label",
            rationale="Rows are labeled tickets.",
            suggested_split=SplitFractions(),
            held_out_count=6,
            model=req.model,
        )

    return fake


def test_returns_the_proposal_and_routes_the_model(monkeypatch) -> None:
    """The proposal comes back as JSON and the interview model runs it."""
    _catalog(monkeypatch)
    seen: dict[str, Any] = {}
    monkeypatch.setattr(code_agent_router, "infer_inputs", _fake_infer(seen))
    monkeypatch.setattr(code_agent_router, "consume_wizard_agent_turn", lambda *_a: None)
    resp = _client().post(_URL, json=_BODY)
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "pairs"
    assert body["input_fields"] == ["ticket"]
    assert body["held_out_count"] == 6
    assert body["suggested_split"] == {"train": 0.7, "val": 0.15, "test": 0.15}
    assert seen["req"].model == "openai/gpt-test"
    assert seen["req"].columns == ["ticket", "label"]


def test_rejects_an_unknown_model(monkeypatch) -> None:
    """A non-catalog model is refused before any LLM spend."""
    _catalog(monkeypatch)
    monkeypatch.setattr(code_agent_router, "consume_wizard_agent_turn", lambda *_a: None)
    resp = _client().post(_URL, json={**_BODY, "model": "openai/not-a-model"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "models.unknown_model"


def test_rejects_too_many_sample_rows() -> None:
    """The request is validated against the shared contract."""
    resp = _client().post(_URL, json={**_BODY, "sample_rows": [{"ticket": str(i)} for i in range(6)]})
    assert resp.status_code == 422


def test_call_is_free_and_counted(monkeypatch) -> None:
    """The inference counts against the daily cap and never touches the balance."""
    _catalog(monkeypatch)
    calls: dict[str, Any] = {}
    monkeypatch.setattr(code_agent_router, "enforce_llm_balance", lambda *_a: calls.setdefault("balance", True))
    monkeypatch.setattr(code_agent_router, "consume_wizard_agent_turn", lambda *_a: calls.setdefault("quota", True))
    monkeypatch.setattr(code_agent_router, "infer_inputs", _fake_infer({}))
    assert _client().post(_URL, json=_BODY).status_code == 200
    assert calls == {"quota": True}


def test_daily_limit_is_a_429(monkeypatch) -> None:
    """At the cap the call is refused before any model work."""

    def at_cap(*_args: Any) -> None:
        """Refuse like the quota does at the cap."""
        raise DomainError("wizard_agent.daily_limit_reached", status=429, limit=50)

    monkeypatch.setattr(code_agent_router, "consume_wizard_agent_turn", at_cap)
    resp = _client().post(_URL, json=_BODY)
    assert resp.status_code == 429
    assert resp.json()["code"] == "wizard_agent.daily_limit_reached"
