"""Tests for the agent engine: the automatic pick, account data and routing."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ... import config
from ...billing import cli_dispatch
from ...billing.tests.test_cli_runners import CODEX_TURN, emitting, fake_app_server, local_settings, requests
from ...local.cli_catalog import CLI_MODELS
from ...local.cli_probe import CliStatus
from ...models import ModelConfig
from ...service_gateway import language_models
from ...service_gateway.agents import code as code_agent_engine
from ...service_gateway.language_models import build_language_model
from ...storage.models import AgentEnginePreferenceModel, Base, UserModel
from .. import agent_engine, model_fast_mode, model_router
from .. import auth as auth_mod
from ..account_data_service import delete_account, export_account
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..model_catalog import CatalogModel
from ..routers import code_agent as code_agent_mod
from ..routers import generalist_agent as agent_mod
from ..routers import tagger_assist as tagger_mod
from ..routers.code_agent import create_code_agent_router
from ..routers.generalist_agent import create_generalist_agent_router
from ..routers.tagger_assist import create_tagger_assist_router
from .test_generalist_persistence import _SECRET, _fund, _session_token

_USER = "alice@example.com"
_OTHER = "bob@example.com"
_CODEX_SLUG = next(iter(CLI_MODELS["codex"]))
_CODEX_ID = f"subscription/codex/{_CODEX_SLUG}"


def _status(cli: str, **overrides: Any) -> CliStatus:
    """Build a ready CLI status, with fields overridden.

    Args:
        cli: The CLI id.
        **overrides: Fields that make it not ready.

    Returns:
        The probe result.
    """
    fields: dict[str, Any] = {
        "id": cli,
        "path": f"/usr/local/bin/{cli}",
        "version": "9.9.9",
        "min_version": "0.0.0",
        "version_ok": True,
        "logged_in": True,
        "login_hint": "",
        "rate_limited_until": None,
        "models": [],
        **overrides,
    }
    return CliStatus(**fields)


def _engine() -> Engine:
    """Return an in-memory database with every table."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


class _NoDatabase:
    """A job store whose database must not be touched."""

    @property
    def engine(self) -> Engine:
        """Fail the test on any database access."""
        raise AssertionError("hosted mode read the database")


@pytest.fixture
def hosted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep local mode off, and fail if anything probes a CLI."""
    current = config.settings.model_copy(update={"local_mode": False})
    for module in (config, agent_engine, model_router):
        monkeypatch.setattr(module, "settings", current)

    def no_probe(_settings: object) -> list[CliStatus]:
        """Fail the test if hosted mode probes a CLI."""
        raise AssertionError("hosted mode probed a CLI")

    monkeypatch.setattr(agent_engine, "probe_clis", no_probe)


@pytest.fixture
def local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[CliStatus]:
    """Turn local mode on everywhere, with a fake codex and a fresh loopback gateway.

    Returns:
        The probe results the engine default reads; tests replace its items.
    """
    fake_app_server(tmp_path, emitting(*CODEX_TURN))
    current = local_settings(tmp_path)
    for module in (config, agent_engine, model_router, language_models):
        monkeypatch.setattr(module, "settings", current)
    monkeypatch.setattr(cli_dispatch, "_gateway", None)
    monkeypatch.setattr(cli_dispatch, "_list_prices", lambda model: None)
    statuses = [_status("codex"), _status("claude")]
    monkeypatch.setattr(agent_engine, "probe_clis", lambda _settings: list(statuses))
    return statuses


@pytest.mark.usefixtures("hosted")
def test_hosted_default_is_openrouter_without_a_database_read() -> None:
    """Answer ``openrouter`` in hosted mode, and leave an agent pick untouched, with no DB read."""
    assert agent_engine.effective_agent_engine(_NoDatabase(), _USER) == "openrouter"
    assert agent_engine.engine_agent_model(_NoDatabase(), _USER, None) is None
    assert agent_engine.engine_agent_model(_NoDatabase(), _USER, "openai/gpt-5.5") == "openai/gpt-5.5"


def test_local_default_is_the_first_ready_cli(local: list[CliStatus]) -> None:
    """Skip a missing, old, signed-out or capped CLI; with none ready, answer null and refuse the turn.

    Args:
        local: The probe results, edited in place.
    """
    store = SimpleNamespace(engine=_engine())
    assert agent_engine.effective_agent_engine(store, _USER) == "codex"
    local[0] = _status("codex", logged_in=False)
    local[1] = _status("claude", rate_limited_until="2099-01-01T00:00:00Z")
    assert agent_engine.effective_agent_engine(store, _USER) is None
    local[1] = _status("claude")
    assert agent_engine.effective_agent_engine(store, _USER) == "claude"
    local[1] = _status("claude", version_ok=False)
    assert agent_engine.effective_agent_engine(store, _USER) is None
    assert agent_engine.engine_agent_model(None, _USER, None) is None
    with pytest.raises(DomainError) as caught:
        model_router.route_agent_model(agent_engine.engine_agent_model(None, _USER, None))
    assert caught.value.code == "models.managed_unavailable_local"


def test_droid_is_never_the_engine(local: list[CliStatus]) -> None:
    """Skip a ready droid CLI: it cannot skip the user's config, hooks and MCP servers.

    Args:
        local: The probe results, edited in place.
    """
    local[:] = [_status("droid"), _status("claude")]
    assert agent_engine.effective_agent_engine(None, _USER) == "claude"
    local[:] = [_status("droid")]
    assert agent_engine.effective_agent_engine(None, _USER) is None
    assert agent_engine.engine_agent_model(None, _USER, None) is None


@pytest.mark.parametrize("stored", ["claude", "openrouter", "droid"])
def test_stored_engine_rows_are_ignored(local: list[CliStatus], stored: str) -> None:
    """Run on the first ready CLI for every caller, whatever engine an old row names.

    Args:
        local: The probe results, edited in place.
        stored: The engine an old ``agent_engine_preferences`` row names.
    """
    engine = _engine()
    with Session(engine) as session:
        session.add(AgentEnginePreferenceModel(username=_USER, engine=stored))
        session.commit()
    store = SimpleNamespace(engine=engine)
    for username in (_USER, _OTHER):
        assert agent_engine.effective_agent_engine(store, username) == "codex"
        assert agent_engine.engine_agent_model(store, username, None) == _CODEX_ID
    local[0] = _status("codex", path=None)
    local[1] = _status("claude", logged_in=False)
    assert agent_engine.effective_agent_engine(store, _USER) is None
    assert agent_engine.engine_agent_model(store, _USER, None) is None


@pytest.mark.usefixtures("local")
@pytest.mark.parametrize(
    ("model", "expected"),
    [
        (None, _CODEX_ID),
        ("", _CODEX_ID),
        ("auto:intelligent", _CODEX_ID),
        ("subscription/claude/anthropic/claude-haiku-4.5", "subscription/claude/anthropic/claude-haiku-4.5"),
        ("openai/gpt-5.5", "openai/gpt-5.5"),
        ("subscription/codex/openai/gpt-5.6-terra", "subscription/codex/openai/gpt-5.6-terra"),
        ("openrouter/x-ai/grok-build", _CODEX_ID),
    ],
    ids=[
        "none",
        "empty",
        "retired_sentinel",
        "explicit_subscription",
        "explicit_managed",
        "subscription_matching_removed_pattern",
        "removed_managed",
    ],
)
def test_engine_fills_only_an_empty_pick(model: str | None, expected: str) -> None:
    """Fill an empty, retired or removed pick from the engine, and keep any explicit pick.

    Args:
        model: The request's model value.
        expected: The model the router receives.
    """
    assert agent_engine.engine_agent_model(SimpleNamespace(engine=_engine()), _USER, model) == expected


def test_account_data_exports_and_deletes_the_engine() -> None:
    """Export the caller's engine and delete only the caller's row."""
    engine = _engine()
    with Session(engine) as session:
        session.add_all(
            [
                AgentEnginePreferenceModel(username=_USER, engine="codex"),
                AgentEnginePreferenceModel(username=_OTHER, engine="claude"),
            ]
        )
        session.commit()
    with Session(engine) as session:
        assert export_account(session, _USER)["agent_engine"] == {"engine": "codex"}
        delete_account(session, _USER)
        session.commit()
    with Session(engine) as session:
        assert session.get(AgentEnginePreferenceModel, _USER) is None
        assert session.get(AgentEnginePreferenceModel, _OTHER).engine == "claude"
        assert export_account(session, _USER)["agent_engine"] == {"engine": None}


@pytest.mark.usefixtures("local")
@pytest.mark.parametrize(
    ("effort", "fast", "sent"),
    [(None, False, None), ("max", False, "max"), ("xhigh", True, "xhigh"), ("ultra", True, "max")],
)
def test_local_chat_turn_with_no_model_runs_on_codex_with_no_remote_call(
    effort: str | None, fast: bool, sent: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run a chat turn that picks no model on the codex engine, over loopback only.

    The agent loop itself is replaced by one model call on the turn's model
    config, built the way the agent builds it. Every non-loopback connection
    is blocked and recorded, so an OpenRouter call fails the test. The catalog
    gives the slug a shorter effort ladder and a default, which the CLI turn
    must not take: it runs at the picked effort, or at the CLI's own default.
    A retired level from an old pick runs at the model's top level.

    Args:
        effort: The turn's picked effort, or ``None`` for no pick.
        fast: Whether the turn asks for fast mode.
        sent: The effort the codex turn gets.
        tmp_path: Holds the fake codex and its call log.
        monkeypatch: Swaps the agent loop and guards sockets.
    """
    engine = _engine()
    _fund(engine)
    with Session(engine) as session:
        session.add(UserModel(email=_USER, name=_USER, password_hash="x"))
        session.commit()
    monkeypatch.setattr(agent_mod, "queue_conversation_embed", lambda *a, **k: None)
    ladder = CatalogModel(
        value=_CODEX_SLUG,
        label="Codex",
        provider="openai",
        supports_thinking=True,
        reasoning_efforts=["low", "medium", "high"],
        default_reasoning_effort="medium",
        reasoning_default_enabled=True,
    )
    monkeypatch.setattr(model_router, "get_catalog_cached", lambda: SimpleNamespace(models=[ladder]))
    seen: dict[str, Any] = {}

    async def one_model_call(**kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        """Answer the turn with one call on its model config."""
        config_ = kwargs["model_config"]
        lm = build_language_model(config_, disable_cache=True)
        seen.update(config=config_, reply=await asyncio.to_thread(lm, "ping"), cost=lm.usage_totals.reported_cost_usd)
        yield {"event": "done", "data": {"assistant_message": seen["reply"][0], "model": config_.name}}

    monkeypatch.setattr(agent_mod, "run_generalist_agent", one_model_call)
    remote: list[object] = []
    real_connect = socket.socket.connect

    def loopback_only(sock: socket.socket, address: Any) -> None:
        """Allow loopback connections and block every other one."""
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in {"127.0.0.1", "::1", "localhost"}:
            remote.append(address)
            raise OSError("remote connection blocked by test")
        real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", loopback_only)
    monkeypatch.setattr(auth_mod.settings, "backend_auth_secret", SecretStr(_SECRET))
    store = SimpleNamespace(engine=engine)
    app = FastAPI()
    app.state.job_store = store
    app.include_router(create_generalist_agent_router(job_store=store))
    response = TestClient(app).post(
        "/optimizations/generalist-agent",
        json={
            "user_message": "hi",
            "chat_history": [],
            "wizard_state": {},
            "trust_mode": "ask",
            "reasoning_effort": effort,
            "fast_mode": fast,
        },
        headers={"Authorization": f"Bearer {_session_token(_USER)}"},
    )
    assert response.status_code == 200, response.text
    assert "pong" in response.text
    model_config = seen["config"]
    assert (model_config.token_source, model_config.cli_provider, model_config.name) == (
        "subscription",
        "codex",
        _CODEX_SLUG,
    )
    assert seen["reply"] == ["pong"]
    assert seen["cost"] == 0.0
    thread = next(message for message in requests(tmp_path) if message.get("method") == "thread/start")
    assert thread["params"]["model"] == CLI_MODELS["codex"][_CODEX_SLUG]
    turn = next(message for message in requests(tmp_path) if message.get("method") == "turn/start")
    assert turn["params"].get("effort") == sent
    assert (turn["params"].get("serviceTier") == "priority") is fast
    assert remote == []


@pytest.mark.parametrize("effort", ["ultra", "ultracode"])
@pytest.mark.parametrize("priority", [False, True])
def test_hosted_chat_turn_maps_retired_effort_and_applies_supported_fast_mode(
    effort: str, priority: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run a hosted turn at its top level with fast mode only on supported endpoints.

    Args:
        effort: A retired level from an old pick.
        priority: Whether the selected model supports OpenRouter priority routing.
        monkeypatch: Swaps the agent loop and the catalog.
    """
    engine = _engine()
    _fund(engine)
    with Session(engine) as session:
        session.add(UserModel(email=_USER, name=_USER, password_hash="x"))
        session.commit()
    monkeypatch.setattr(agent_mod, "queue_conversation_embed", lambda *a, **k: None)
    slug = "openrouter/anthropic/claude-opus-5.5" if priority else "anthropic/claude-opus-5.5"
    monkeypatch.setattr(model_fast_mode, "fast_providers", lambda model: ("anthropic/fast",) if priority else ())
    ladder = CatalogModel(
        value=slug,
        label="Opus",
        provider="anthropic",
        available=True,
        supports_thinking=True,
        reasoning_efforts=["low", "medium", "high"],
        default_reasoning_effort="medium",
        reasoning_default_enabled=True,
    )
    monkeypatch.setattr(model_router, "get_catalog_cached", lambda: SimpleNamespace(models=[ladder]))
    monkeypatch.setattr(model_router, "require_known_model", lambda model: None)
    seen: dict[str, Any] = {}

    async def record_config(**kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        """Record the turn's model config and finish the turn."""
        seen["config"] = kwargs["model_config"]
        yield {"event": "done", "data": {"assistant_message": "pong", "model": slug}}

    monkeypatch.setattr(agent_mod, "run_generalist_agent", record_config)
    monkeypatch.setattr(auth_mod.settings, "backend_auth_secret", SecretStr(_SECRET))
    store = SimpleNamespace(engine=engine)
    app = FastAPI()
    app.state.job_store = store
    app.include_router(create_generalist_agent_router(job_store=store))
    response = TestClient(app).post(
        "/optimizations/generalist-agent",
        json={
            "user_message": "hi",
            "chat_history": [],
            "wizard_state": {},
            "trust_mode": "ask",
            "model": slug,
            "reasoning_effort": effort,
            "fast_mode": True,
        },
        headers={"Authorization": f"Bearer {_session_token(_USER)}"},
    )
    assert response.status_code == 200, response.text
    model_config = seen["config"]
    assert model_config.token_source != "subscription"
    assert model_config.extra.get("reasoning_effort") == "high"
    assert cli_dispatch.CLI_FAST_KEY not in (model_config.extra.get("extra_body") or {})
    if priority:
        assert model_config.extra["extra_body"]["provider"]["only"] == ["anthropic/fast"]


# Each route, its routing function and its module, with a minimal valid body.
_AGENT_ROUTES = [
    ("/account/intake-interview", code_agent_mod, "route_interview_model", {"phase": "billing"}),
    ("/tagging-sessions/s1/assist/interview", tagger_mod, "route_interview_model", {}),
    ("/tagging-sessions/s1/assist/interview/stream", tagger_mod, "route_interview_model", {}),
]


@pytest.mark.usefixtures("local")
@pytest.mark.parametrize(
    ("path", "module", "router_fn", "body"),
    _AGENT_ROUTES,
    ids=[
        "intake_interview",
        "tagger_interview",
        "tagger_interview_stream",
    ],
)
def test_agent_route_with_no_model_routes_the_engine_model(
    path: str, module: Any, router_fn: str, body: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Send the engine's model, not the empty pick, to the model router on each agent route.

    The balance gates and the session load are stubbed, and the model router
    records its input and stops the turn, so no agent runs and nothing is
    charged. The test fails if a route stops filling the pick from the engine.

    Args:
        path: The agent route.
        module: The router module whose routing call is recorded.
        router_fn: The routing function the route calls.
        body: A valid request body with no model.
        monkeypatch: Stubs the gates and records the routed model.
    """
    engine = _engine()
    routed: list[str | None] = []

    def record(model: str | None) -> str:
        """Record the routed model and stop the turn before any agent runs."""
        routed.append(model)
        raise DomainError("test.routed", status=418)

    monkeypatch.setattr(module, router_fn, record)
    monkeypatch.setattr(tagger_mod, "fits_kickoff_budget", lambda _size: True)
    monkeypatch.setattr(code_agent_mod, "gate_intake_turn", lambda *_args: (None, False))
    monkeypatch.setattr(code_agent_mod, "_intake_catalog", lambda: ([], []))
    monkeypatch.setattr(tagger_mod, "enforce_llm_balance", lambda *_args: None)
    monkeypatch.setattr(tagger_mod, "_load_for_role", lambda *_args: SimpleNamespace(columns=[], data=[]))
    monkeypatch.setattr(tagger_mod, "_interview_config", lambda _row: {})
    store = SimpleNamespace(engine=engine)
    app = FastAPI()

    @app.exception_handler(DomainError)
    async def _domain_error(request: Request, exc: DomainError) -> JSONResponse:
        """Render a domain error with its i18n code."""
        return JSONResponse(status_code=exc.status_code, content={"code": exc.code})

    app.include_router(create_code_agent_router(job_store=store))
    app.include_router(create_tagger_assist_router(job_store=store, get_worker_ref=lambda: None))
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser(username=_USER, role="user", groups=())
    response = TestClient(app).post(path, json=body)
    assert (response.status_code, response.json()) == (418, {"code": "test.routed"})
    assert routed == [_CODEX_ID]


# Each composer route that runs a turn on the picked model, the engine call it
# makes, and a minimal valid body.
_COMPOSER_ROUTES = [
    ("/tagging-sessions/s1/assist/interview", tagger_mod.tagging, "interview_turn", {}),
    ("/tagging-sessions/s1/assist/interview/stream", tagger_mod.tagging, "interview_turn_stream", {}),
]


@pytest.mark.usefixtures("local")
@pytest.mark.parametrize(
    ("path", "module", "engine_fn", "body"),
    _COMPOSER_ROUTES,
    ids=["tagger_interview", "tagger_interview_stream"],
)
@pytest.mark.parametrize("fast", [True, False])
@pytest.mark.parametrize("openrouter", [False, True])
def test_composer_route_passes_effort_and_fast_mode_to_the_engine(
    path: str,
    module: Any,
    engine_fn: str,
    body: dict[str, Any],
    fast: bool,
    openrouter: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pass effort and fast mode through each composer route for CLI and OpenRouter BYOK.

    The gates and the session load are stubbed, and the engine call records
    its arguments and stops the turn, so no model runs and nothing is charged.

    Args:
        path: The composer route.
        module: The module whose engine call is recorded.
        engine_fn: The engine call the route makes.
        body: A valid request body with no model.
        fast: Whether the turn asks for fast mode.
        openrouter: Whether the local engine uses its OpenRouter key instead of a CLI.
        monkeypatch: Stubs the gates and records the engine call.
    """
    engine = _engine()
    seen: dict[str, Any] = {}

    def record(*_args: Any, **kwargs: Any) -> Any:
        """Record the engine call and stop the turn before any model runs."""
        seen.update(kwargs)
        raise DomainError("test.routed", status=418)

    monkeypatch.setattr(module, engine_fn, record)
    monkeypatch.setattr(tagger_mod, "fits_kickoff_budget", lambda _size: True)
    monkeypatch.setattr(tagger_mod, "enforce_llm_balance", lambda *_args: None)
    monkeypatch.setattr(tagger_mod, "_load_for_role", lambda *_args: SimpleNamespace(columns=[], data=[]))
    monkeypatch.setattr(tagger_mod, "_interview_config", lambda _row: {})
    if openrouter:
        byok = ModelConfig(
            name="openrouter/anthropic/claude-opus-5.5",
            token_source="byok",
            byok_provider="openrouter",
            extra={"api_key": "synthetic"},
        )
        monkeypatch.setattr(tagger_mod, "route_local_byok_model", lambda *args, **kwargs: byok)
        monkeypatch.setattr(tagger_mod, "effective_reasoning_effort", lambda model, effort: effort)
        monkeypatch.setattr(model_fast_mode, "fast_providers", lambda model: ("anthropic/fast",))
    store = SimpleNamespace(engine=engine)
    app = FastAPI()

    @app.exception_handler(DomainError)
    async def _domain_error(request: Request, exc: DomainError) -> JSONResponse:
        """Render a domain error with its i18n code."""
        return JSONResponse(status_code=exc.status_code, content={"code": exc.code})

    app.include_router(create_code_agent_router(job_store=store))
    app.include_router(create_tagger_assist_router(job_store=store, get_worker_ref=lambda: None))
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser(username=_USER, role="user", groups=())
    TestClient(app).post(path, json={**body, "model": _CODEX_ID, "reasoning_effort": "max", "fast_mode": fast})
    assert seen["model"] == (byok.name if openrouter else _CODEX_ID)
    assert seen["reasoning_effort"] == "max"
    config_ = seen["model_config"]
    if openrouter:
        assert config_.token_source == "byok"
        assert config_.extra["api_key"] == "synthetic"
        if fast:
            assert config_.extra["extra_body"]["provider"]["only"] == ["anthropic/fast"]
        else:
            assert "extra_body" not in config_.extra
        return
    if not fast:
        assert config_ is None
        return
    assert (config_.token_source, config_.cli_provider, config_.name) == ("subscription", "codex", _CODEX_SLUG)
    assert config_.extra["extra_body"] == {cli_dispatch.CLI_FAST_KEY: True}


@pytest.mark.usefixtures("local")
@pytest.mark.parametrize("fast", [True, False])
def test_code_agent_lm_runs_the_codex_turn_at_max_in_fast_mode(
    fast: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run one code-agent model call on codex at the max level, in fast mode only when asked.

    Args:
        fast: Whether the turn asks for fast mode.
        tmp_path: Holds the fake codex and its call log.
        monkeypatch: Not used beyond the ``local`` fixture.
    """
    lm = code_agent_engine._build_agent_lm(_CODEX_ID, "max", model_config=model_router.cli_fast_config(_CODEX_ID, fast))
    assert lm("ping") == ["pong"]
    turn = next(message for message in requests(tmp_path) if message.get("method") == "turn/start")
    assert turn["params"].get("effort") == "max"
    assert (turn["params"].get("serviceTier") == "priority") is fast
