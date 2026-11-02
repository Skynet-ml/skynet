"""Tests for the agent next-message suggestion route."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ...models import ModelConfig
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..routers import generalist_agent as agent_mod

_USER = "alice@example.com"
_TURNS = [
    {"role": "user", "content": "Help me optimize my support bot"},
    {"role": "assistant", "content": "Upload a dataset of questions and answers."},
]


class _FakeLM:
    """A DSPy-shaped LM that records prompts and returns a fixed answer."""

    def __init__(self, answer: str | Exception) -> None:
        """Store the answer, or the error to raise.

        Args:
            answer: The completion text, or an exception to raise on call.
        """
        self.answer = answer
        self.calls: list[list[dict[str, str]]] = []

    def __call__(self, *, messages: list[dict[str, str]]) -> list[str]:
        """Record the prompt and answer.

        Args:
            messages: The chat messages.

        Returns:
            One completion.

        Raises:
            Exception: The configured error.
        """
        self.calls.append(messages)
        if isinstance(self.answer, Exception):
            raise self.answer
        return [self.answer]


@pytest.fixture
def metered(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record metering calls instead of writing billing rows.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        The recorded calls.
    """
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        agent_mod,
        "meter_llm_run",
        lambda engine, username, lms, **kwargs: calls.append({"username": username, "lms": lms, **kwargs}),
    )
    return calls


def _hosted(monkeypatch: pytest.MonkeyPatch, lm: object, built: list[tuple[str, float]] | None = None) -> None:
    """Route hosted suggestions to ``lm`` on a fixed scored model.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        lm: The LM the builder returns.
        built: Records each ``(model, timeout)`` the builder gets, when given.
    """
    monkeypatch.setattr(agent_mod, "hosted_suggest_model", lambda: "openrouter/acme/small")

    def build(model: str, timeout: float) -> object:
        """Record the build and return ``lm``.

        Args:
            model: The suggestion model.
            timeout: Seconds before the call gives up.

        Returns:
            ``lm``.
        """
        if built is not None:
            built.append((model, timeout))
        return lm

    monkeypatch.setattr(agent_mod, "build_hosted_suggest_lm", build)


def _client() -> TestClient:
    """Build an app with the generalist router and a signed-in caller.

    Returns:
        A test client.
    """
    app = FastAPI()
    app.include_router(agent_mod.create_generalist_agent_router(job_store=None))
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser(username=_USER, role="user", groups=())
    return TestClient(app)


def test_returns_suggestion_and_meters_it(monkeypatch: pytest.MonkeyPatch, metered: list[dict[str, Any]]) -> None:
    """A finished turn gets a suggestion from the side-task model, metered to the caller."""
    lm = _FakeLM("Here is my dataset")
    built: list[tuple[str, float]] = []
    _hosted(monkeypatch, lm, built)
    res = _client().post(
        "/optimizations/agent-suggest",
        json={"turns": _TURNS, "actions": ["share_plan: done"], "locale": "en"},
    )
    assert res.status_code == 200
    assert res.json() == {"suggestion": "Here is my dataset"}
    body = lm.calls[0][1]["content"]
    assert "Upload a dataset of questions and answers." in body
    assert "share_plan: done" in body
    assert metered == [
        {"username": _USER, "lms": [lm], "description": "Agent suggestion", "token_source": "managed"},
    ]
    assert built == [("openrouter/acme/small", agent_mod._SUGGEST_TIMEOUT_SECONDS)]


def test_model_failure_returns_empty(monkeypatch: pytest.MonkeyPatch, metered: list[dict[str, Any]]) -> None:
    """A failed model call gives an empty suggestion, not an error, and still meters."""
    lm = _FakeLM(RuntimeError("upstream timeout"))
    _hosted(monkeypatch, lm)
    res = _client().post("/optimizations/agent-suggest", json={"turns": _TURNS})
    assert res.status_code == 200
    assert res.json() == {"suggestion": ""}
    assert len(metered) == 1


def test_blocked_balance_returns_empty_without_a_call(
    monkeypatch: pytest.MonkeyPatch, metered: list[dict[str, Any]]
) -> None:
    """A balance below the turn minimum gives an empty suggestion and no model call."""

    def blocked(job_store: object, username: str) -> None:
        """Refuse like the real balance gate.

        Args:
            job_store: Unused.
            username: Unused.

        Raises:
            DomainError: Always.
        """
        raise DomainError("billing.insufficient_funds", status=402)

    built: list[tuple[str, float]] = []
    monkeypatch.setattr(agent_mod, "enforce_llm_balance", blocked)
    _hosted(monkeypatch, None, built)
    res = _client().post("/optimizations/agent-suggest", json={"turns": _TURNS})
    assert res.json() == {"suggestion": ""}
    assert built == []
    assert metered == []


def test_no_suggestion_while_the_user_spoke_last(
    monkeypatch: pytest.MonkeyPatch, metered: list[dict[str, Any]]
) -> None:
    """A transcript that ends on a user turn (a turn still running) gets nothing."""
    built: list[tuple[str, float]] = []
    _hosted(monkeypatch, None, built)
    res = _client().post("/optimizations/agent-suggest", json={"turns": _TURNS[:1]})
    assert res.json() == {"suggestion": ""}
    assert built == []


def test_local_mode_without_engine_returns_empty(
    monkeypatch: pytest.MonkeyPatch, metered: list[dict[str, Any]]
) -> None:
    """The Mac app with no usable engine for the pick gives no suggestion."""
    monkeypatch.setattr(agent_mod.settings, "local_mode", True)
    monkeypatch.setattr(agent_mod, "engine_agent_model", lambda store, user, model: model)
    monkeypatch.setattr(agent_mod, "route_local_byok_model", lambda *a, **k: None)
    config = ModelConfig(name="claude-sonnet-4.5", token_source="subscription", cli_provider="claude")
    monkeypatch.setattr(agent_mod, "route_agent_model", lambda picked: config)
    seen: list[tuple[object, float]] = []
    monkeypatch.setattr(agent_mod, "build_local_suggest_lm", lambda config, timeout: seen.append((config, timeout)))
    res = _client().post(
        "/optimizations/agent-suggest",
        json={"turns": _TURNS, "model": "subscription/claude/claude-sonnet-4.5"},
    )
    assert res.json() == {"suggestion": ""}
    assert seen == [(config, agent_mod._SUGGEST_TIMEOUT_SECONDS)]
    assert metered == []


def test_mac_app_subscription_suggestion_logs_its_payer(
    monkeypatch: pytest.MonkeyPatch, metered: list[dict[str, Any]], caplog: pytest.LogCaptureFixture
) -> None:
    """A CLI subscription suggestion logs the subscription as its payer, not the legacy metering source."""
    monkeypatch.setattr(agent_mod.settings, "local_mode", True)
    monkeypatch.setattr(agent_mod, "engine_agent_model", lambda store, user, model: model)
    monkeypatch.setattr(agent_mod, "route_local_byok_model", lambda *a, **k: None)
    config = ModelConfig(name="claude-sonnet-4.5", token_source="subscription", cli_provider="claude")
    monkeypatch.setattr(agent_mod, "route_agent_model", lambda picked: config)
    lm = _FakeLM("ok, pasting it now")
    lm.model = "litellm_proxy/anthropic/claude-haiku-5.5"
    lm.subscription = True
    monkeypatch.setattr(agent_mod, "build_local_suggest_lm", lambda config, timeout: lm)
    with caplog.at_level("INFO", logger=agent_mod.logger.name):
        res = _client().post(
            "/optimizations/agent-suggest",
            json={"turns": _TURNS, "model": "subscription/claude/claude-sonnet-4.5"},
        )
    assert res.json() == {"suggestion": "ok, pasting it now"}
    assert "next-message suggestion on litellm_proxy/anthropic/claude-haiku-5.5 (subscription)" in caplog.text
    assert metered[0]["token_source"] == "managed"


def test_hosted_without_a_scored_model_returns_empty(
    monkeypatch: pytest.MonkeyPatch, metered: list[dict[str, Any]]
) -> None:
    """With no Artificial Analysis data there is no suggestion model, so no call and no charge."""
    built: list[tuple[str, float]] = []
    _hosted(monkeypatch, None, built)
    monkeypatch.setattr(agent_mod, "hosted_suggest_model", lambda: None)
    res = _client().post("/optimizations/agent-suggest", json={"turns": _TURNS})
    assert res.json() == {"suggestion": ""}
    assert built == []
    assert metered == []


def test_hosted_never_uses_the_interview_or_agent_model(
    monkeypatch: pytest.MonkeyPatch, metered: list[dict[str, Any]]
) -> None:
    """The hosted suggestion runs on the scored model even when the composer picked another."""
    lm = _FakeLM("Next")
    built: list[tuple[str, float]] = []
    _hosted(monkeypatch, lm, built)
    _client().post(
        "/optimizations/agent-suggest",
        json={"turns": _TURNS, "model": "openrouter/anthropic/claude-opus-5.5"},
    )
    assert [model for model, _ in built] == ["openrouter/acme/small"]


def test_suggestion_timeout_outlasts_a_cli_start() -> None:
    """A suggestion waits longer than a thinking label, as a CLI start alone takes 3-7 s."""
    assert agent_mod._SUGGEST_TIMEOUT_SECONDS > 25.0
