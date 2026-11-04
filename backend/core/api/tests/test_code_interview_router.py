"""Tests for the code-agent SSE endpoint (``/optimizations/ai-generate-code``).

Mounts the code-agent router with ``run_code_agent`` monkeypatched, so the
wire contract (argument forwarding, model routing, the unified authoring
context and the free-turn gate) is covered without an LLM.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from .. import model_catalog, model_router
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..routers import code_agent as code_agent_router
from ..routers.code_agent import create_code_agent_router

_ALICE = AuthenticatedUser(username="alice", role="user", groups=())


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


def test_seed_endpoint_forwards_interview_brief(monkeypatch) -> None:
    """The confirmed brief rides the ai-generate-code request into the engine."""
    seen: dict[str, Any] = {}

    def fake_run(**kwargs: Any) -> Any:
        """Record kwargs and return an immediately-done stream."""
        seen.update(kwargs)

        async def gen() -> Any:
            yield {"event": "done", "data": {"signature_code": "", "metric_code": ""}}

        return gen()

    monkeypatch.setattr(code_agent_router, "run_code_agent", fake_run)
    body = {
        "dataset_columns": ["text", "label"],
        "column_roles": {"text": "input", "label": "output"},
        "sample_rows": [],
        "user_message": "",
        "interview_brief": ["Outputs must be lowercase.", "Penalize hedging."],
    }
    resp = _client().post("/optimizations/ai-generate-code", json=body)
    assert resp.status_code == 200
    assert seen["interview_brief"] == ["Outputs must be lowercase.", "Penalize hedging."]


_SEED_BODY = {
    "dataset_columns": ["text", "label"],
    "column_roles": {"text": "input", "label": "output"},
    "sample_rows": [],
    "user_message": "",
}


def _fake_run(seen: dict[str, Any]):
    """Build a ``run_code_agent`` stub that records kwargs and finishes at once."""

    def fake_run(**kwargs: Any) -> Any:
        """Record kwargs and return an immediately-done stream."""
        seen.update(kwargs)

        async def gen() -> Any:
            yield {"event": "done", "data": {"signature_code": "", "metric_code": ""}}

        return gen()

    return fake_run


def test_seed_endpoint_forwards_model_and_effort(monkeypatch) -> None:
    """An explicit catalog model + effort ride ai-generate-code into the engine."""
    seen: dict[str, Any] = {}
    monkeypatch.setattr(code_agent_router, "run_code_agent", _fake_run(seen))
    monkeypatch.setattr(
        model_catalog,
        "get_catalog_cached",
        lambda: SimpleNamespace(models=[SimpleNamespace(value="openai/gpt-test", is_default=False)]),
    )
    resp = _client().post(
        "/optimizations/ai-generate-code",
        json={**_SEED_BODY, "model": "openai/gpt-test", "reasoning_effort": "high"},
    )
    assert resp.status_code == 200
    assert seen["model"] == "openai/gpt-test"
    assert seen["reasoning_effort"] == "high"


def test_seed_endpoint_without_model_runs_catalog_default(monkeypatch) -> None:
    """An absent model and the retired 'auto:intelligent' both run the catalog default."""
    seen: list[dict[str, Any]] = []

    def fake_run(**kwargs: Any) -> Any:
        """Record kwargs and return an immediately-done stream."""
        seen.append(kwargs)

        async def gen() -> Any:
            yield {"event": "done", "data": {"signature_code": "", "metric_code": ""}}

        return gen()

    monkeypatch.setattr(code_agent_router, "run_code_agent", fake_run)
    monkeypatch.setattr(
        model_router,
        "get_catalog_cached",
        lambda: SimpleNamespace(
            models=[
                SimpleNamespace(
                    value="openrouter/anthropic/claude-sonnet-5",
                    is_default=True,
                    reasoning_efforts=None,
                    reasoning_default_enabled=None,
                )
            ]
        ),
    )
    client = _client()
    assert client.post("/optimizations/ai-generate-code", json=_SEED_BODY).status_code == 200
    assert (
        client.post(
            "/optimizations/ai-generate-code",
            json={**_SEED_BODY, "model": "auto:intelligent"},
        ).status_code
        == 200
    )
    assert [k["model"] for k in seen] == ["openrouter/anthropic/claude-sonnet-5"] * 2


def test_seed_endpoint_rejects_unknown_model(monkeypatch) -> None:
    """A non-catalog code-author model is refused before any LLM spend."""
    monkeypatch.setattr(
        model_catalog,
        "get_catalog_cached",
        lambda: SimpleNamespace(models=[SimpleNamespace(value="openai/gpt-test", is_default=False)]),
    )
    resp = _client().post("/optimizations/ai-generate-code", json={**_SEED_BODY, "model": "openai/not-a-model"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "models.unknown_model"


_UNIFIED_CONTEXT = {
    "task": {
        "objective": "Replies that resolve the ticket.",
        "background": "",
        "artifact": {"kind": "dspy_program", "starting_version": "Answer the ticket."},
    },
    "data_mode": "pairs",
    "input_fields": ["ticket"],
    "expected_field": "reply",
    "sample_cases": [{"ticket": "hi", "reply": "hello"}],
    "optimizer": "auto",
    "metric_has_model": True,
    "focus": "metric",
}


def test_seed_endpoint_forwards_unified_context_without_columns(monkeypatch) -> None:
    """A unified request may omit the dataset; its context reaches the engine as a dict."""
    seen: dict[str, Any] = {}
    monkeypatch.setattr(code_agent_router, "run_code_agent", _fake_run(seen))
    monkeypatch.setattr(code_agent_router, "consume_wizard_agent_turn", lambda *_a: None)
    resp = _client().post(
        "/optimizations/ai-generate-code",
        json={"dataset_columns": [], "column_roles": {}, "unified": _UNIFIED_CONTEXT},
    )
    assert resp.status_code == 200
    assert seen["unified"]["task"]["artifact"] == {"kind": "dspy_program", "starting_version": "Answer the ticket."}
    assert seen["unified"]["focus"] == "metric"
    assert "blackbox" not in seen


def test_dspy_seed_forwards_no_unified_context(monkeypatch) -> None:
    """Without the context the DSPy authoring path runs."""
    seen: dict[str, Any] = {}
    monkeypatch.setattr(code_agent_router, "run_code_agent", _fake_run(seen))
    monkeypatch.setattr(code_agent_router, "enforce_llm_balance", lambda *_a: None)
    assert _client().post("/optimizations/ai-generate-code", json=_SEED_BODY).status_code == 200
    assert seen["unified"] is None


def test_seed_endpoint_without_columns_or_unified_is_rejected() -> None:
    """The DSPy contract still needs the dataset columns."""
    resp = _client().post(
        "/optimizations/ai-generate-code",
        json={"dataset_columns": [], "column_roles": {}},
    )
    assert resp.status_code == 422


def test_seed_endpoint_rejects_a_malformed_unified_context() -> None:
    """The context is validated against the shared contract."""
    resp = _client().post(
        "/optimizations/ai-generate-code",
        json={**_SEED_BODY, "unified": {**_UNIFIED_CONTEXT, "focus": "nope"}},
    )
    assert resp.status_code == 422


def _record_gates(monkeypatch, calls: dict[str, Any]) -> None:
    """Record the balance gate, the wizard quota and the store each turn is metered against."""
    monkeypatch.setattr(code_agent_router, "enforce_llm_balance", lambda *_a: calls.setdefault("balance", True))
    monkeypatch.setattr(code_agent_router, "consume_wizard_agent_turn", lambda *_a: calls.setdefault("quota", True))

    async def fake_metering(source: Any, *, job_store: Any, **_kwargs: Any) -> Any:
        """Note the billed store and pass the events through."""
        calls["billed_store"] = job_store
        async for event in source:
            yield event

    monkeypatch.setattr(code_agent_router, "stream_with_llm_metering", fake_metering)


def test_unified_agent_turn_uses_the_quota_not_credits(monkeypatch) -> None:
    """The submission wizard's agent counts against the daily cap and streams unbilled."""
    calls: dict[str, Any] = {}
    _record_gates(monkeypatch, calls)
    monkeypatch.setattr(code_agent_router, "run_code_agent", _fake_run({}))
    resp = _client().post(
        "/optimizations/ai-generate-code",
        json={**_SEED_BODY, "user_message": "hi", "unified": _UNIFIED_CONTEXT},
    )
    assert resp.status_code == 200
    assert calls == {"quota": True, "billed_store": None}


def test_dspy_agent_turn_still_charges(monkeypatch) -> None:
    """Every other authoring turn keeps the balance gate and is metered."""
    calls: dict[str, Any] = {}
    _record_gates(monkeypatch, calls)
    monkeypatch.setattr(code_agent_router, "run_code_agent", _fake_run({}))
    resp = _client().post("/optimizations/ai-generate-code", json={**_SEED_BODY, "user_message": "hi"})
    assert resp.status_code == 200
    assert "quota" not in calls
    assert calls["balance"] is True


def test_unified_daily_limit_is_a_429(monkeypatch) -> None:
    """At the cap the turn is refused before any agent work starts."""

    def at_cap(*_args: Any) -> None:
        """Refuse like the quota does at the cap."""
        raise DomainError("wizard_agent.daily_limit_reached", status=429, limit=50)

    monkeypatch.setattr(code_agent_router, "consume_wizard_agent_turn", at_cap)
    resp = _client().post(
        "/optimizations/ai-generate-code",
        json={**_SEED_BODY, "user_message": "hi", "unified": _UNIFIED_CONTEXT},
    )
    assert resp.status_code == 429
    assert resp.json()["code"] == "wizard_agent.daily_limit_reached"
