"""Tests for "Continue with OpenRouter": the PKCE start URL, state, callback, and finish.

Runs the billing router against an in-memory SQLite engine with OpenRouter's
key exchange and the vault's verify probe mocked at the :mod:`httpx` boundary,
so the tests cover the challenge/verifier pair, the encrypted state carried on
the callback URL, the callback handing the code to the app without a save,
the signed-in finish step that binds the state to its owner, and the key
landing in the vault exactly like a pasted one.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Iterator
from hashlib import sha256
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ...billing import openrouter_oauth
from ...billing.byok_vault import STATUS_VERIFIED
from ...config import settings
from ...storage.models import Base, BillingProviderKeyModel
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..routers.billing import create_billing_router

_ALICE = AuthenticatedUser(username="alice", role="user", groups=())
_BOB = AuthenticatedUser(username="bob", role="user", groups=())
_CALLBACK = "http://testserver/billing/byok/openrouter/oauth/callback"


@pytest.fixture
def engine() -> Iterator[Any]:
    """Yield an in-memory SQLite engine with the ORM tables created."""
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)


@pytest.fixture
def vault_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """Configure a throwaway Fernet vault key and a known frontend origin."""
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setattr(settings, "byok_vault_key", SecretStr(key))
    monkeypatch.setattr(settings, "app_public_url", "http://app.test")
    monkeypatch.setattr(settings, "openrouter_oauth_redirect_uri", None)
    return key


@pytest.fixture
def client(engine: Any) -> TestClient:
    """Mount the billing router authed as Alice, with the app's error envelope."""
    app = FastAPI()
    app.include_router(create_billing_router(job_store=SimpleNamespace(engine=engine)))
    app.dependency_overrides[get_authenticated_user] = lambda: _ALICE

    @app.exception_handler(DomainError)
    async def _domain_error_handler(_request, exc: DomainError) -> JSONResponse:
        """Mirror the app-level envelope so tests can assert on ``code``."""
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, "code": exc.code})

    return TestClient(app)


def _start(client: TestClient) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Run the start endpoint and split its authorize URL.

    Args:
        client: Test client with the billing router mounted.

    Returns:
        The authorize URL's query and the embedded callback URL's query.
    """
    response = client.post("/billing/byok/openrouter/oauth/start")
    assert response.status_code == 200
    url = urlparse(response.json()["authorize_url"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == openrouter_oauth.AUTHORIZE_URL
    query = parse_qs(url.query)
    callback = urlparse(query["callback_url"][0])
    assert f"{callback.scheme}://{callback.netloc}{callback.path}" == _CALLBACK
    return query, parse_qs(callback.query)


def _keys_response(status_code: int, body: Any) -> httpx.Response:
    """Build a stand-in response from OpenRouter's key-exchange endpoint.

    Args:
        status_code: HTTP status to report.
        body: JSON body to return.

    Returns:
        The response.
    """
    return httpx.Response(status_code, json=body, request=httpx.Request("POST", openrouter_oauth.KEYS_URL))


def _location(response: httpx.Response) -> dict[str, list[str]]:
    """Assert a redirect into the frontend and return its query.

    Args:
        response: The callback's response.

    Returns:
        The redirect target's parsed query.
    """
    assert response.status_code == 303
    target = urlparse(response.headers["location"])
    assert f"{target.scheme}://{target.netloc}{target.path}" == "http://app.test/"
    query = parse_qs(target.query)
    assert query["settings"] == ["providers"]
    return query


def _stored_keys(engine: Any) -> list[BillingProviderKeyModel]:
    """Return every stored BYOK row.

    Args:
        engine: The test engine.

    Returns:
        The rows.
    """
    with Session(engine) as session:
        return session.query(BillingProviderKeyModel).all()


def test_list_reports_oauth_availability(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The keys listing offers the button only when the vault key is configured."""
    monkeypatch.setattr(settings, "byok_vault_key", None)
    assert client.get("/billing/byok/keys").json()["openrouter_oauth_available"] is False
    monkeypatch.setattr(settings, "byok_vault_key", SecretStr(Fernet.generate_key().decode("ascii")))
    assert client.get("/billing/byok/keys").json()["openrouter_oauth_available"] is True


def test_start_builds_s256_challenge_and_encrypted_state(client: TestClient, vault_key: str) -> None:
    """The challenge is the S256 of the verifier sealed in the callback URL's state."""
    query, callback_query = _start(client)
    assert query["code_challenge_method"] == ["S256"]
    payload = json.loads(Fernet(vault_key.encode("ascii")).decrypt(callback_query["state"][0].encode("ascii")))
    assert payload["u"] == "alice"
    expected = base64.urlsafe_b64encode(sha256(payload["v"].encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    assert query["code_challenge"] == [expected]


def test_start_honours_configured_redirect_uri(
    client: TestClient, vault_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit redirect URI replaces the request-derived callback."""
    monkeypatch.setattr(settings, "openrouter_oauth_redirect_uri", "https://api.example.com/cb")
    response = client.post("/billing/byok/openrouter/oauth/start")
    callback = parse_qs(urlparse(response.json()["authorize_url"]).query)["callback_url"][0]
    assert callback.startswith("https://api.example.com/cb?state=")


def test_start_requires_vault(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a vault key the flow is refused with 503."""
    monkeypatch.setattr(settings, "byok_vault_key", None)
    response = client.post("/billing/byok/openrouter/oauth/start")
    assert response.status_code == 503
    assert response.json()["code"] == "billing.byok_not_configured"


def test_parse_state_rejects_forged_and_expired(vault_key: str) -> None:
    """Garbage, a foreign key, a wrong shape, or an old state are all refused."""
    with pytest.raises(DomainError) as exc:
        openrouter_oauth.parse_state("not-a-token")
    assert exc.value.code == "billing.openrouter_oauth_state_invalid"

    foreign = Fernet(Fernet.generate_key()).encrypt(b'{"u": "alice", "v": "x"}').decode("ascii")
    with pytest.raises(DomainError):
        openrouter_oauth.parse_state(foreign)

    wrong_shape = Fernet(vault_key.encode("ascii")).encrypt(b'{"u": "alice"}').decode("ascii")
    with pytest.raises(DomainError):
        openrouter_oauth.parse_state(wrong_shape)

    stale = Fernet(vault_key.encode("ascii")).encrypt_at_time(b'{"u": "alice", "v": "x"}', 0).decode("ascii")
    with pytest.raises(DomainError):
        openrouter_oauth.parse_state(stale)


def _fragment(response: httpx.Response) -> dict[str, str]:
    """Assert a clean redirect into the providers tab and return its fragment.

    Args:
        response: The callback's response.

    Returns:
        The fragment values, one string per key.
    """
    assert "byok_error" not in _location(response)
    return {key: values[0] for key, values in parse_qs(urlparse(response.headers["location"]).fragment).items()}


def _finish(client: TestClient, state: str, code: str = "c0de", **mock_kwargs: Any) -> tuple[Any, Any]:
    """Call the signed-in finish step with OpenRouter and the verify probe mocked.

    Args:
        client: Test client with the billing router mounted.
        state: The state from the callback.
        code: The code from the callback.
        **mock_kwargs: ``return_value`` or ``side_effect`` for the key exchange.

    Returns:
        The finish response and the mock of the key exchange.
    """
    mock_kwargs = mock_kwargs or {"return_value": _keys_response(200, {"key": "sk-or-minted-wxyz"})}
    with (
        patch("core.billing.openrouter_oauth.httpx.post", **mock_kwargs) as post,
        patch("core.billing.byok_vault.httpx.get", return_value=SimpleNamespace(status_code=200, is_success=True)),
    ):
        response = client.post("/billing/byok/openrouter/oauth/finish", json={"state": state, "code": code})
    return response, post


def test_callback_hands_off_and_finish_saves_key(client: TestClient, engine: Any, vault_key: str) -> None:
    """The callback stores nothing; the signed-in finish saves the key like a pasted one."""
    _, callback_query = _start(client)
    state = callback_query["state"][0]
    verifier = json.loads(Fernet(vault_key.encode("ascii")).decrypt(state.encode("ascii")))["v"]
    with patch("core.billing.openrouter_oauth.httpx.post") as never:
        response = client.get(
            "/billing/byok/openrouter/oauth/callback", params={"code": "c0de", "state": state}, follow_redirects=False
        )
    never.assert_not_called()
    assert _fragment(response) == {"oauth_flow": "openrouter", "oauth_state": state, "oauth_code": "c0de"}
    assert _stored_keys(engine) == []

    finish, post = _finish(client, state)
    assert finish.status_code == 204, finish.text
    assert post.call_args.kwargs["json"] == {"code": "c0de", "code_verifier": verifier, "code_challenge_method": "S256"}
    rows = _stored_keys(engine)
    assert [(r.username, r.provider, r.last4, r.status) for r in rows] == [
        ("alice", "openrouter", "wxyz", STATUS_VERIFIED)
    ]
    assert b"sk-or-minted-wxyz" not in rows[0].secret_ciphertext


@pytest.mark.parametrize("params", [{}, {"code": "c0de"}, {"state": "s"}])
def test_callback_without_code_or_state_redirects_with_error(
    client: TestClient, engine: Any, vault_key: str, params: dict[str, str]
) -> None:
    """A missing code or state bounces back with the failure code."""
    response = client.get("/billing/byok/openrouter/oauth/callback", params=params, follow_redirects=False)
    assert _location(response)["byok_error"] == ["billing.openrouter_oauth_failed"]
    assert _stored_keys(engine) == []


def test_finish_by_another_user_is_refused(client: TestClient, engine: Any, vault_key: str) -> None:
    """A state that Alice started cannot save a key for Bob."""
    _, callback_query = _start(client)
    client.app.dependency_overrides[get_authenticated_user] = lambda: _BOB
    finish, post = _finish(client, callback_query["state"][0])
    assert finish.status_code == 403
    assert finish.json()["code"] == "billing.openrouter_oauth_state_invalid"
    post.assert_not_called()
    assert _stored_keys(engine) == []


def test_finish_needs_a_signed_in_caller(client: TestClient, engine: Any, vault_key: str) -> None:
    """Without a bearer token the finish route answers 401 and trades nothing."""
    _, callback_query = _start(client)
    client.app.dependency_overrides.pop(get_authenticated_user)
    finish, post = _finish(client, callback_query["state"][0])
    assert finish.status_code == 401
    post.assert_not_called()
    assert _stored_keys(engine) == []


def test_finish_refuses_forged_or_expired_state(client: TestClient, engine: Any, vault_key: str) -> None:
    """A forged or expired state never reaches OpenRouter."""
    _, callback_query = _start(client)
    forged, post = _finish(client, "forged")
    assert forged.status_code == 400
    assert forged.json()["code"] == "billing.openrouter_oauth_state_invalid"
    post.assert_not_called()
    with patch("cryptography.fernet.time.time", return_value=time.time() + 3600):
        expired, post = _finish(client, callback_query["state"][0])
    assert expired.status_code == 400
    assert expired.json()["code"] == "billing.openrouter_oauth_state_invalid"
    post.assert_not_called()
    assert _stored_keys(engine) == []


@pytest.mark.parametrize(
    ("outcome", "status", "code"),
    [
        (_keys_response(403, {"error": "Invalid code or code_verifier"}), 400, "billing.openrouter_oauth_failed"),
        (_keys_response(200, {"nope": True}), 400, "billing.openrouter_oauth_failed"),
        (httpx.ConnectError("down"), 502, "billing.openrouter_unreachable"),
    ],
)
def test_finish_surfaces_exchange_failures(
    client: TestClient, engine: Any, vault_key: str, outcome: Any, status: int, code: str
) -> None:
    """A refused (for example, a used code), malformed, or unreachable exchange stores nothing."""
    _, callback_query = _start(client)
    mock_kwargs = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
    finish, _ = _finish(client, callback_query["state"][0], **mock_kwargs)
    assert finish.status_code == status
    assert finish.json()["code"] == code
    assert _stored_keys(engine) == []


def test_wallet_serves_the_current_pricing_terms(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The wallet carries the live markup and fees so the client never hardcodes them."""
    monkeypatch.setattr(settings, "usage_markup", 1.3)
    response = client.get("/billing/wallet")
    assert response.status_code == 200
    assert response.json()["pricing"] == {
        "usage_markup": 1.3,
        "byok_fee_fraction": 0.0,
        "purchase_fee_rate": 0.125,
        "purchase_fee_fixed_cents": 35,
    }
