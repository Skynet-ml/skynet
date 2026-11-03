"""Tests for the dictation transcription route (OpenRouter-only + guards).

Mounts the router with the auth dependency overridden and the OpenRouter leg
monkeypatched — no network. Covers the unconfigured 503, the size-cap 413,
the happy path, the provider-failure 502, and the hourly clip cap and monthly
dictation budget against an in-memory Redis double. Free platform dictation and
the caller's own OpenRouter key run against an in-memory SQLite engine. The
OpenRouter request/response contract runs against an ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import fakeredis
import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from ...billing.byok_vault import ProviderKeyVault
from ...config import settings
from ...storage.models import Base
from .. import platform_budget
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..routers import transcription
from ..routers.transcription import create_transcription_router

_USER = AuthenticatedUser(username="alice", role="user", groups=())


def _client(job_store: Any = None) -> TestClient:
    """Mount the transcription router authed as a fixed user.

    Args:
        job_store: Store backing the eligibility check; the default has no
            engine, so there are no billing tables to gate on.

    Returns:
        A test client for the mounted router.
    """
    app = FastAPI()
    app.include_router(create_transcription_router(job_store=job_store or SimpleNamespace()))
    app.dependency_overrides[get_authenticated_user] = lambda: _USER

    @app.exception_handler(DomainError)
    async def _domain_error_handler(_request, exc: DomainError) -> JSONResponse:
        """Mirror the app-level envelope so tests can assert on ``code``."""
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "code": exc.code, "params": exc.params},
        )

    return TestClient(app)


def _post_audio(client: TestClient, payload: bytes = b"RIFFxxxx") -> httpx.Response:
    """POST a small multipart clip to /transcribe."""
    return client.post(
        "/transcribe",
        files={"audio": ("take.webm", payload, "audio/webm")},
        data={"language": "he-IL"},
    )


def test_unconfigured_returns_typed_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a transcription key, the route answers an honest 503."""
    monkeypatch.setattr(settings, "transcription_api_key", None)
    resp = _post_audio(_client())
    assert resp.status_code == 503
    assert resp.json()["code"] == "transcription.unconfigured"


def test_oversized_clip_rejected_413(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clip over the cap is rejected before the provider is contacted."""
    monkeypatch.setattr(settings, "transcription_api_key", SecretStr("sk-or-test"))
    monkeypatch.setattr(transcription, "_MAX_AUDIO_BYTES", 4)
    resp = _post_audio(_client(), payload=b"12345")
    assert resp.status_code == 413
    assert resp.json()["code"] == "transcription.too_large"


def test_openrouter_leg_returns_transcript(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a transcription key set, the leg serves the transcript."""
    monkeypatch.setattr(settings, "transcription_api_key", SecretStr("sk-or-test"))

    seen: dict[str, str] = {}

    async def _fake_openrouter(_client, _audio, _filename, key) -> tuple[str, float]:
        seen["key"] = key
        return "מהיר מאוד", 0.0001

    monkeypatch.setattr(transcription, "_openrouter_transcribe", _fake_openrouter)
    resp = _post_audio(_client())
    assert resp.status_code == 200
    assert resp.json() == {"text": "מהיר מאוד", "provider": "openrouter"}
    assert seen == {"key": "sk-or-test"}


def test_provider_failure_returns_502(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing OpenRouter call answers a typed 502."""
    monkeypatch.setattr(settings, "transcription_api_key", SecretStr("sk-or-test"))

    async def _boom(_client, _audio, _filename, _key) -> tuple[str, float]:
        raise RuntimeError("openrouter transcribe: 500")

    monkeypatch.setattr(transcription, "_openrouter_transcribe", _boom)
    resp = _post_audio(_client())
    assert resp.status_code == 502
    assert resp.json()["code"] == "transcription.failed"


@pytest.fixture
def redis_double(monkeypatch: pytest.MonkeyPatch) -> fakeredis.FakeStrictRedis:
    """Point the clip cap and the dictation budget at one in-memory Redis."""
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(transcription, "shared_redis_client", lambda: client)
    monkeypatch.setattr(platform_budget, "shared_redis_client", lambda: client)
    monkeypatch.setattr(settings, "transcription_api_key", SecretStr("sk-or-test"))
    return client


def _count_openrouter_calls(monkeypatch: pytest.MonkeyPatch, cost: float = 0.0001) -> list[int]:
    """Stub the OpenRouter leg and return a list that grows by one per call."""
    calls: list[int] = []

    async def _fake_openrouter(_client, _audio, _filename, _key) -> tuple[str, float]:
        calls.append(1)
        return "hi", cost

    monkeypatch.setattr(transcription, "_openrouter_transcribe", _fake_openrouter)
    return calls


def test_clip_cost_uses_reported_cost_then_falls_back_to_seconds() -> None:
    """OpenRouter's ``usage.cost`` wins; without it the seconds estimate keeps a 10 s floor."""
    assert transcription._clip_cost({"cost": 0.000508, "seconds": 9.2}) == pytest.approx(0.000508)
    assert transcription._clip_cost({"cost": 0}) == 0.0
    assert transcription._clip_cost({"seconds": 3.0}) == pytest.approx(10 / 3600 * 0.04)
    assert transcription._clip_cost({}) == pytest.approx(10 / 3600 * 0.04)
    assert transcription._clip_cost({"seconds": 90.0}) == pytest.approx(90 / 3600 * 0.04)


async def _call_openrouter(handler: Any) -> tuple[str, float]:
    """Run the OpenRouter leg against a mock transport.

    Args:
        handler: ``httpx.MockTransport`` request handler.

    Returns:
        What :func:`transcription._openrouter_transcribe` returned.
    """
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await transcription._openrouter_transcribe(client, b"OggS-audio", "take.webm", "sk-or-key")


def test_openrouter_request_and_response_contract() -> None:
    """The leg posts multipart audio to OpenRouter and reads ``text`` and ``usage.cost``."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["content_type"] = request.headers["content-type"]
        seen["body"] = request.read()
        return httpx.Response(
            200, json={"text": "שלום", "usage": {"seconds": 2.1, "cost": 0.000508, "total_tokens": 10}}
        )

    text, cost = asyncio.run(_call_openrouter(handler))

    assert (text, cost) == ("שלום", pytest.approx(0.000508))
    assert seen["url"] == "https://openrouter.ai/api/v1/audio/transcriptions"
    assert seen["auth"] == "Bearer sk-or-key"
    assert seen["content_type"].startswith("multipart/form-data")
    assert b'name="model"\r\n\r\nopenai/whisper-large-v3-turbo' in seen["body"]
    assert b'name="file"; filename="take.webm"' in seen["body"]
    assert b"OggS-audio" in seen["body"]


def test_openrouter_error_status_raises() -> None:
    """A non-OK OpenRouter answer raises, which the route maps to a typed 502."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"error": {"code": 402, "message": "Insufficient credits"}})

    with pytest.raises(RuntimeError, match="openrouter transcribe: 402"):
        asyncio.run(_call_openrouter(handler))


def test_spent_budget_refuses_without_calling_openrouter(
    monkeypatch: pytest.MonkeyPatch, redis_double: fakeredis.FakeStrictRedis
) -> None:
    """Once the month's dictation budget is spent, dictation answers a typed 503."""
    monkeypatch.setattr(settings, "transcription_monthly_budget_usd", 0.001)
    calls = _count_openrouter_calls(monkeypatch, cost=0.002)
    client = _client()
    assert _post_audio(client).status_code == 200
    resp = _post_audio(client)
    assert resp.status_code == 503
    assert resp.json()["code"] == "transcription.budget_exhausted"
    assert len(calls) == 1


def test_hourly_clip_cap_answers_429(monkeypatch: pytest.MonkeyPatch, redis_double: fakeredis.FakeStrictRedis) -> None:
    """A caller over the hourly clip cap is refused before OpenRouter is called."""
    monkeypatch.setattr(settings, "rate_limit_transcriptions_per_hour", 2)
    calls = _count_openrouter_calls(monkeypatch)
    client = _client()
    assert [_post_audio(client).status_code for _ in range(3)] == [200, 200, 429]
    assert len(calls) == 2


class _BillingStore:
    """Job-store stand-in exposing an engine."""

    def __init__(self, engine: Any) -> None:
        """Bind the engine.

        Args:
            engine: SQLAlchemy engine with the BYOK tables.
        """
        self.engine = engine


@pytest.fixture
def billing_engine(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Yield an in-memory SQLite engine with billing tables and a vault key."""
    monkeypatch.setattr(settings, "byok_vault_key", SecretStr(Fernet.generate_key().decode("utf-8")))
    monkeypatch.setattr(settings, "transcription_api_key", SecretStr("sk-or-platform"))
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)


def _save_key(engine: Any, provider: str, secret: str, api_base: str | None = None) -> None:
    """Store and verify a BYOK key for the test user without a network probe."""
    ok = SimpleNamespace(status_code=200, is_success=True)
    with patch("core.billing.byok_vault.httpx.get", return_value=ok):
        ProviderKeyVault(engine=engine).save_key(_USER.username, provider, secret, api_base=api_base)


def _seen_keys(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub the OpenRouter leg and return the list of API keys it was called with."""
    keys: list[str] = []

    async def _fake_openrouter(_client, _audio, _filename, key) -> tuple[str, float]:
        keys.append(key)
        return "hi", 0.0001

    monkeypatch.setattr(transcription, "_openrouter_transcribe", _fake_openrouter)
    return keys


def test_account_without_balance_dictates_on_platform_key(monkeypatch: pytest.MonkeyPatch, billing_engine: Any) -> None:
    """An account with no balance, no Pro and no BYOK key still dictates for free."""
    keys = _seen_keys(monkeypatch)
    assert _post_audio(_client(_BillingStore(billing_engine))).status_code == 200
    assert keys == ["sk-or-platform"]


def test_own_openrouter_key_is_used_and_skips_platform_budget(
    monkeypatch: pytest.MonkeyPatch, billing_engine: Any, redis_double: fakeredis.FakeStrictRedis
) -> None:
    """A verified OpenRouter BYOK key transcribes on that key, even with the platform budget spent."""
    _save_key(billing_engine, "openrouter", "sk-or-own")
    monkeypatch.setattr(settings, "transcription_api_key", None)
    monkeypatch.setattr(settings, "transcription_monthly_budget_usd", 0.000001)
    keys = _seen_keys(monkeypatch)
    client = _client(_BillingStore(billing_engine))
    assert [_post_audio(client).status_code for _ in range(2)] == [200, 200]
    assert keys == ["sk-or-own", "sk-or-own"]
    assert not list(redis_double.scan_iter("skynet:platform-budget:transcription*"))


def test_openrouter_key_with_custom_base_is_not_sent_to_openrouter(
    monkeypatch: pytest.MonkeyPatch, billing_engine: Any
) -> None:
    """An OpenRouter connection pointed at another host keeps its key off openrouter.ai."""
    monkeypatch.setattr("core.billing.byok_vault._pinned_address", lambda *args, **kwargs: "8.8.8.8")
    monkeypatch.setattr(ProviderKeyVault, "_probe", lambda *args, **kwargs: "verified")
    _save_key(billing_engine, "openrouter", "proxy-secret", api_base="https://proxy.example.com/v1")
    keys = _seen_keys(monkeypatch)
    assert _post_audio(_client(_BillingStore(billing_engine))).status_code == 200
    assert keys == ["sk-or-platform"]


def test_groq_byok_key_no_longer_serves_dictation(monkeypatch: pytest.MonkeyPatch, billing_engine: Any) -> None:
    """A verified Groq BYOK key stays a model key only; dictation uses the platform key."""
    _save_key(billing_engine, "groq", "gsk-own")
    keys = _seen_keys(monkeypatch)
    assert _post_audio(_client(_BillingStore(billing_engine))).status_code == 200
    assert keys == ["sk-or-platform"]
