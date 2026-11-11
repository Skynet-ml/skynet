"""Tests for the Mac app's own OpenRouter key: save, clear, and route model calls onto it.

Every key here is fake. The verify probe and the OpenRouter chat call are
answered at the HTTP layer, and every non-loopback socket is blocked, so no
test reaches OpenRouter.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
import socket
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
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
from ...billing import byok_vault
from ...billing.tests.test_cli_runners import local_settings
from ...constants import TOKEN_SOURCE_BYOK, TOKEN_SOURCE_MANAGED
from ...exceptions import ServiceError
from ...local.cli_catalog import CLI_MODELS
from ...local.cli_probe import CliStatus
from ...local.vault_key import VAULT_KEY_FILE
from ...models import ModelConfig
from ...service_gateway import language_models, tagging
from ...service_gateway.agents import code as code_agent
from ...service_gateway.agents import generalist
from ...service_gateway.language_models import build_language_model
from ...storage.models import AgentEnginePreferenceModel, Base, BillingProviderKeyModel, UserModel
from .. import agent_engine, interview_model, model_catalog, model_router
from .. import auth as auth_mod
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..routers import generalist_agent as agent_mod
from ..routers.billing import create_billing_router
from ..routers.generalist_agent import create_generalist_agent_router
from .test_generalist_persistence import _SECRET, _fund, _session_token

_USER = "alice@example.com"
_FAKE_KEY = "sk-or-v1-FAKE0000000000000000000000000000000000000000000000000000001"
_MODEL = "openrouter/anthropic/claude-haiku-4.5"
_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
# The fixture replaces this with a fake; the empty-pick test needs the real one.
_REAL_DEFAULT_MODEL_ID = model_router.default_model_id


def _engine() -> Engine:
    """Return an in-memory database with every table."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _status(cli: str) -> CliStatus:
    """Build a CLI status that is not installed.

    Args:
        cli: The CLI id.

    Returns:
        The probe result.
    """
    return CliStatus(
        id=cli,
        path=None,
        version=None,
        min_version="0.0.0",
        version_ok=False,
        logged_in=None,
        login_hint="",
        rate_limited_until=None,
        models=[],
    )


def _probe(status_code: int) -> httpx.Response:
    """Build the verify probe's answer.

    Args:
        status_code: The HTTP status OpenRouter's ``/key`` endpoint returns.

    Returns:
        The response.
    """
    return httpx.Response(
        status_code, json={"data": {}}, request=httpx.Request("GET", "https://openrouter.ai/api/v1/key")
    )


def _client(store: object) -> TestClient:
    """Mount the billing router for one caller, rendering domain errors with their code.

    Args:
        store: The job store the router uses.

    Returns:
        A test client.
    """
    app = FastAPI()

    @app.exception_handler(DomainError)
    async def _domain_error(request: Request, exc: DomainError) -> JSONResponse:
        """Render a domain error with its i18n code."""
        return JSONResponse(status_code=exc.status_code, content={"code": exc.code})

    app.include_router(create_billing_router(job_store=store))
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser(username=_USER, role="user", groups=())
    return TestClient(app)


def _save(client: TestClient, status_code: int = 200) -> dict[str, Any]:
    """Save the fake OpenRouter key through the settings API, with the verify probe answered locally.

    Args:
        client: The test client.
        status_code: What the verify probe answers.

    Returns:
        The response body.
    """
    with patch("core.billing.byok_vault.httpx.get", return_value=_probe(status_code)) as probe:
        response = client.put("/billing/byok/keys", json={"provider": "openrouter", "secret": _FAKE_KEY})
    assert response.status_code == 200, response.text
    [call] = probe.call_args_list
    assert call.args[0] == "https://openrouter.ai/api/v1/key"
    return response.json()


@pytest.fixture
def local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[object]]:
    """Run in local mode with the vault key from the state directory, no ready CLI and no remote sockets.

    Yields:
        The non-loopback addresses something tried to reach; each test checks it stays empty.
    """
    current = local_settings(tmp_path, byok_vault_key=None, generalist_agent_base_url="https://gateway.invalid/v1")
    # Run the real settings validator, as the API and worker do at start.
    current = current._local_vault_key()
    assert (tmp_path / "state" / VAULT_KEY_FILE).exists()
    for module in (
        config,
        agent_engine,
        model_router,
        language_models,
        byok_vault,
        tagging,
        code_agent,
    ):
        monkeypatch.setattr(module, "settings", current)
    monkeypatch.setattr(agent_engine, "probe_clis", lambda _settings: [_status(cli) for cli in CLI_MODELS])
    monkeypatch.setattr(model_router, "get_catalog_cached", lambda: SimpleNamespace(models=[]))
    monkeypatch.setattr(model_router, "require_known_model", _require_catalog_model)
    monkeypatch.setattr(
        model_router,
        "get_byok_catalog_cached",
        lambda: SimpleNamespace(
            models=[
                model_catalog.CatalogModel(value=name, label=name, provider="openrouter", supports_thinking=True)
                for name in {_MODEL, config.DEFAULT_AGENT_MODEL_ID, "openrouter/anthropic/claude-sonnet-4.5"}
            ]
        ),
    )
    monkeypatch.setattr(model_router, "default_model_id", lambda: _MODEL)
    monkeypatch.setattr(model_router, "interview_model_id", lambda: _MODEL)
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
    yield remote
    assert remote == []


def _require_catalog_model(model: str | None) -> None:
    """Accept only the test catalog's one model.

    Args:
        model: The picked id.

    Raises:
        DomainError: 422 for any other id.
    """
    if model and model != _MODEL:
        raise DomainError("models.unknown_model", status=422)


def test_hosted_mode_never_reads_the_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    """Leave hosted routing unchanged: no BYOK route, and no database read.

    Args:
        monkeypatch: Turns local mode off.
    """
    hosted = config.settings.model_copy(update={"local_mode": False})
    monkeypatch.setattr(model_router, "settings", hosted)

    class NoDatabase:
        """A job store whose database must not be touched."""

        @property
        def engine(self) -> Engine:
            """Fail the test on any database access."""
            raise AssertionError("hosted mode read the vault")

    assert model_router.route_local_byok_model(_MODEL, job_store=NoDatabase(), username=_USER) is None
    assert model_router.route_local_byok_model(None, job_store=NoDatabase(), username=_USER, interview=True) is None
    assert model_router.metering_token_source(None) == "managed"


def test_without_a_verified_key_local_turns_stay_refused(local: list[object]) -> None:
    """Refuse managed turns with no key, and route nothing on a key OpenRouter rejected.

    Args:
        local: The blocked remote addresses.
    """
    store = SimpleNamespace(engine=_engine())
    client = _client(store)
    assert model_router.route_local_byok_model(None, job_store=store, username=_USER) is None
    with pytest.raises(DomainError) as caught:
        model_router.route_agent_model(None)
    assert caught.value.code == "models.managed_unavailable_local"

    assert _save(client, status_code=401)["status"] == "invalid"
    assert model_router.route_local_byok_model(_MODEL, job_store=store, username=_USER) is None


def test_save_route_and_clear_the_key(local: list[object]) -> None:
    """Save a key encrypted at rest, route catalog picks onto it, and stop when it is cleared.

    Args:
        local: The blocked remote addresses.
    """
    engine = _engine()
    store = SimpleNamespace(engine=engine)
    client = _client(store)
    saved = _save(client)
    assert (saved["provider"], saved["status"], saved["last4"]) == ("openrouter", "verified", _FAKE_KEY[-4:])
    assert _FAKE_KEY not in str(saved)
    with Session(engine) as session:
        row = session.query(BillingProviderKeyModel).one()
        stored = [getattr(row, column.key) for column in BillingProviderKeyModel.__table__.columns]
    assert all(_FAKE_KEY.encode() not in (v if isinstance(v, bytes) else str(v).encode()) for v in stored)
    listed = client.get("/billing/byok/keys").json()["keys"]
    assert [(k["provider"], k["status"]) for k in listed] == [("openrouter", "verified")]

    for picked, interview in ((None, False), ("", True), ("auto:intelligent", False), (_MODEL, True)):
        routed = model_router.route_local_byok_model(picked, job_store=store, username=_USER, interview=interview)
        assert routed is not None
        assert (routed.name, routed.token_source, routed.byok_provider) == (_MODEL, TOKEN_SOURCE_BYOK, "openrouter")
        assert routed.extra["api_key"] == _FAKE_KEY
        assert not routed.base_url
        assert model_router.metering_token_source(routed) == TOKEN_SOURCE_BYOK
    subscription = f"subscription/codex/{next(iter(CLI_MODELS['codex']))}"
    assert model_router.route_local_byok_model(subscription, job_store=store, username=_USER) is None
    with pytest.raises(DomainError) as caught:
        model_router.route_local_byok_model("anthropic/not-an-openrouter-id", job_store=store, username=_USER)
    assert caught.value.status_code == 422

    assert agent_engine.engine_agent_model(store, _USER, None) is None

    assert client.delete("/billing/byok/keys/openrouter").json()["keys"] == []
    assert model_router.route_local_byok_model(None, job_store=store, username=_USER) is None
    with pytest.raises(DomainError) as caught:
        model_router.route_agent_model(agent_engine.engine_agent_model(store, _USER, None))
    assert caught.value.code == "models.managed_unavailable_local"


@pytest.mark.parametrize("stored", [None, "openrouter", "droid"])
def test_empty_pick_with_no_ready_cli_runs_on_the_saved_key(local: list[object], stored: str | None) -> None:
    """Keep an empty pick empty with no ready CLI, so the router runs it on the saved key.

    Args:
        local: The blocked remote addresses.
        stored: An old engine row, which the engine ignores, or ``None`` for no row.
    """
    engine = _engine()
    store = SimpleNamespace(engine=engine)
    client = _client(store)
    if stored is not None:
        with Session(engine) as session:
            session.add(AgentEnginePreferenceModel(username=_USER, engine=stored))
            session.commit()
    assert agent_engine.effective_agent_engine(store, _USER) is None
    assert agent_engine.engine_agent_model(store, _USER, None) is None
    assert model_router.route_local_byok_model(None, job_store=store, username=_USER) is None

    _save(client)
    assert agent_engine.effective_agent_engine(store, _USER) is None
    picked = agent_engine.engine_agent_model(store, _USER, None)
    assert picked is None
    routed = model_router.route_local_byok_model(picked, job_store=store, username=_USER)
    assert routed is not None
    assert (routed.token_source, routed.byok_provider) == (TOKEN_SOURCE_BYOK, "openrouter")

    client.delete("/billing/byok/keys/openrouter")
    assert model_router.route_local_byok_model(None, job_store=store, username=_USER) is None


def test_engine_prefers_a_ready_cli_over_the_saved_key(local: list[object], monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the first ready CLI as the default when a verified key is also saved.

    Args:
        local: The blocked remote addresses.
        monkeypatch: Makes the Codex CLI ready.
    """
    ready = dataclasses.replace(_status("codex"), path="/usr/local/bin/codex", version_ok=True)
    monkeypatch.setattr(agent_engine, "probe_clis", lambda _settings: [ready])
    store = SimpleNamespace(engine=_engine())
    client = _client(store)
    _save(client)
    assert agent_engine.effective_agent_engine(store, _USER) == "codex"
    assert (
        agent_engine.engine_agent_model(store, _USER, None) == f"subscription/codex/{next(iter(CLI_MODELS['codex']))}"
    )


def test_explicit_pick_routes_on_the_saved_key_with_the_real_empty_catalog(
    local: list[object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Route an explicit OpenRouter pick onto the saved key when the curated catalog is empty.

    The Mac app has no platform OpenRouter key, so the real curated catalog
    lists no OpenRouter model. The pick runs on the user's own key, so the
    curated catalog must not refuse it. A malformed id still gets 422.

    Args:
        local: The blocked remote addresses.
        monkeypatch: Restores the real catalog functions and clears the cache.
    """
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(model_catalog, "_cached_response", None)
    monkeypatch.setattr(model_router, "get_catalog_cached", model_catalog.get_catalog_cached)
    monkeypatch.setattr(model_router, "require_known_model", model_catalog.require_known_model)
    picked = "openrouter/anthropic/claude-sonnet-4.5"
    assert all(entry.value != picked for entry in model_catalog.get_catalog_cached().models)
    store = SimpleNamespace(engine=_engine())
    _save(_client(store))

    routed = model_router.route_local_byok_model(picked, job_store=store, username=_USER)
    assert routed is not None
    assert (routed.name, routed.token_source, routed.byok_provider) == (picked, TOKEN_SOURCE_BYOK, "openrouter")
    assert routed.extra["api_key"] == _FAKE_KEY
    for malformed in ("anthropic/claude-sonnet-4.5", "openrouter/claude-sonnet-4.5", "openrouter//x", "gpt-5"):
        with pytest.raises(DomainError) as caught:
            model_router.route_local_byok_model(malformed, job_store=store, username=_USER)
        assert (caught.value.code, caught.value.status_code) == ("models.unknown_model", 422)


def test_empty_picks_route_the_real_defaults_on_the_saved_key(
    local: list[object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Route an empty agent pick and an empty interview pick onto the saved key with the real defaults.

    The Mac app sends no pick when the user keeps the default, so the real
    ``default_model_id`` and ``interview_model_id`` pick the model, over the
    real empty curated catalog. The result must be an OpenRouter id, because
    the user's OpenRouter key pays for it.

    Args:
        local: The blocked remote addresses.
        monkeypatch: Restores the real catalog and default functions and clears the cache.
    """
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(model_catalog, "_cached_response", None)
    monkeypatch.setattr(model_router, "get_catalog_cached", model_catalog.get_catalog_cached)
    monkeypatch.setattr(model_router, "default_model_id", _REAL_DEFAULT_MODEL_ID)
    monkeypatch.setattr(model_router, "interview_model_id", interview_model.interview_model_id)
    store = SimpleNamespace(engine=_engine())
    _save(_client(store))

    for interview in (False, True):
        routed = model_router.route_local_byok_model("", job_store=store, username=_USER, interview=interview)
        assert routed is not None
        assert re.fullmatch(r"openrouter/[^/\s]+/[^/\s]+", routed.name), routed.name
        assert (routed.token_source, routed.byok_provider) == (TOKEN_SOURCE_BYOK, "openrouter")
        assert routed.extra["api_key"] == _FAKE_KEY


def test_key_deleted_between_reads_gives_one_clean_message(
    local: list[object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Name the provider once when the saved key disappears after the verified-key check.

    Args:
        local: The blocked remote addresses.
        monkeypatch: Makes the vault lose the key between the check and the read.
    """
    store = SimpleNamespace(engine=_engine())
    _save(_client(store))
    monkeypatch.setattr(byok_vault.ProviderKeyVault, "resolve_connection", lambda self, username, provider, **_: None)

    with pytest.raises(DomainError) as caught:
        model_router.route_local_byok_model("", job_store=store, username=_USER)
    assert (caught.value.code, caught.value.status_code) == ("billing.byok_missing_connection", 400)
    assert caught.value.detail == (
        "No saved key for openrouter. Add a connection in Settings → Providers to run with your own key."
    )


def test_byok_configs_skip_the_managed_gateway_base_url(local: list[object]) -> None:
    """Send a BYOK call to OpenRouter, never to the configured managed gateway that would receive the key.

    Args:
        local: The blocked remote addresses.
    """
    config_ = ModelConfig(
        name=_MODEL, token_source=TOKEN_SOURCE_BYOK, byok_provider="openrouter", extra={"api_key": _FAKE_KEY}
    )
    agent_lm = code_agent._build_agent_lm(None, "low", 512, model_config=config_)
    assist_lm = tagging._build_assist_lm(None, model_config=config_)
    for lm in (agent_lm, assist_lm):
        assert lm.model == _MODEL
        assert lm.kwargs.get("api_key") == _FAKE_KEY
        assert "base_url" not in lm.kwargs
        assert "api_base" not in lm.kwargs


def test_local_chat_turn_runs_on_the_saved_key(
    local: list[object], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Run a chat turn with no CLI and no pick on the saved key, meter it as BYOK, and log no key.

    The agent loop is replaced by one model call on the turn's model config,
    built the way the agent builds it. OpenRouter's chat endpoint is answered
    at the HTTP layer.

    Args:
        local: The blocked remote addresses.
        monkeypatch: Swaps the agent loop and the HTTP transport.
        caplog: Captures every log record of the turn.
    """
    caplog.set_level(logging.DEBUG)
    engine = _engine()
    _fund(engine)
    with Session(engine) as session:
        session.add(UserModel(email=_USER, name=_USER, password_hash="x"))
        session.commit()
    store = SimpleNamespace(engine=engine)
    _save(_client(store))
    monkeypatch.setattr(agent_mod, "queue_conversation_embed", lambda *a, **k: None)
    monkeypatch.setattr(auth_mod.settings, "backend_auth_secret", SecretStr(_SECRET))
    requests: list[httpx.Request] = []
    real_send = httpx.Client.send

    def openrouter(self: httpx.Client, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        """Answer OpenRouter's chat endpoint locally, and pass the test client's own requests through."""
        if request.url.host == "testserver":
            return real_send(self, request, **kwargs)
        requests.append(request)
        body = {
            "id": "gen-test",
            "object": "chat.completion",
            "created": 1,
            "model": "anthropic/claude-haiku-4.5",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        }
        return httpx.Response(200, json=body, request=request)

    monkeypatch.setattr(httpx.Client, "send", openrouter)
    seen: dict[str, Any] = {}

    async def one_model_call(**kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        """Answer the turn with one call on its model config."""
        config_ = kwargs["model_config"]
        lm = build_language_model(config_, disable_cache=True)
        kwargs["usage_sink"].append(lm)
        seen.update(config=config_, reply=await asyncio.to_thread(lm, "ping"))
        yield {"event": "done", "data": {"assistant_message": seen["reply"][0], "model": config_.name}}

    monkeypatch.setattr(agent_mod, "run_generalist_agent", one_model_call)
    real_metering = agent_mod.stream_with_llm_metering
    metered: list[str] = []

    def record_metering(source: Any, **kwargs: Any) -> Any:
        """Record the billing source the turn is metered under."""
        metered.append(kwargs["token_source"])
        return real_metering(source, **kwargs)

    monkeypatch.setattr(agent_mod, "stream_with_llm_metering", record_metering)
    app = FastAPI()
    app.state.job_store = store
    app.include_router(create_generalist_agent_router(job_store=store))
    response = TestClient(app).post(
        "/optimizations/generalist-agent",
        json={"user_message": "hi", "chat_history": [], "wizard_state": {}, "trust_mode": "ask"},
        headers={"Authorization": f"Bearer {_session_token(_USER)}"},
    )
    assert response.status_code == 200, response.text
    assert "pong" in response.text
    assert "config" in seen, response.text
    assert seen["config"].token_source == TOKEN_SOURCE_BYOK
    [request] = requests
    assert str(request.url) == _CHAT_URL
    assert request.headers["authorization"] == f"Bearer {_FAKE_KEY}"
    assert metered == [TOKEN_SOURCE_BYOK]
    assert _FAKE_KEY not in response.text
    assert _FAKE_KEY not in caplog.text
    assert _FAKE_KEY[-12:] not in caplog.text


@pytest.mark.parametrize(
    ("token_source", "expected_base"),
    [(TOKEN_SOURCE_BYOK, None), (TOKEN_SOURCE_MANAGED, "https://gateway.invalid/v1")],
)
def test_generalist_byok_turn_skips_the_managed_gateway_base_url(
    monkeypatch: pytest.MonkeyPatch, token_source: str, expected_base: str | None
) -> None:
    """Build a BYOK agent chat model without the managed gateway URL, which would receive the user's key.

    The managed case shows the gateway URL is set, so the BYOK case proves the guard drops it.

    Args:
        monkeypatch: Sets the gateway URL and records the built model config.
        token_source: The turn's billing source.
        expected_base: The base URL the agent's model must use.
    """
    monkeypatch.setattr(
        generalist,
        "settings",
        generalist.settings.model_copy(update={"generalist_agent_base_url": "https://gateway.invalid/v1"}),
    )
    built: list[ModelConfig] = []

    def record(config_: ModelConfig, **kwargs: Any) -> Any:
        """Record the model config and stop the turn before any model call."""
        built.append(config_)
        raise ServiceError("stop before the model call")

    monkeypatch.setattr(generalist, "build_language_model", record)
    config_ = ModelConfig(
        name=_MODEL,
        token_source=token_source,
        byok_provider="openrouter" if token_source == TOKEN_SOURCE_BYOK else None,
        extra={"api_key": _FAKE_KEY} if token_source == TOKEN_SOURCE_BYOK else {},
    )

    async def first_event() -> dict[str, Any]:
        """Return the turn's first event."""
        stream = generalist.run_generalist_agent(
            wizard_state=generalist.WizardState(), chat_history=[], user_message="hi", model_config=config_
        )
        return await stream.__anext__()

    event = asyncio.run(first_event())
    assert event["event"] == "error"
    [override] = built
    assert override.base_url == expected_base
    assert override.token_source == token_source
