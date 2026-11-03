"""Tests for ``ProviderKeyVault``: encrypt-at-rest, the verify probe, and routes.

Covers that secrets are stored only as ciphertext (never plaintext), that the
verify probe classifies a provider response into verified / invalid /
unverified, that decryption round-trips for the run path, and that the
mutations refuse to run without a configured vault key. Each test stands up an
in-memory SQLite engine with the billing tables and patches ``httpx.get`` so no
network call is made.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from core.api.errors import DomainError
from core.billing.byok_upstreams import UPSTREAMS
from core.billing.byok_vault import (
    _PROVIDER_PROBES,
    STATUS_INVALID,
    STATUS_UNVERIFIED,
    STATUS_VERIFIED,
    ProviderKeyVault,
)
from core.billing.mcp_broker import _pinned_address
from core.config import settings
from core.storage.models import Base, BillingProviderKeyModel


@pytest.fixture
def engine() -> Iterator[object]:
    """Yield an in-memory SQLite engine with the billing tables created."""
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)


@pytest.fixture
def vault_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """Set a real Fernet vault key so encryption/decryption works, and return it."""
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setattr(settings, "byok_vault_key", SecretStr(key))
    monkeypatch.setattr("core.billing.byok_vault._pinned_address", lambda *args, **kwargs: "8.8.8.8")
    monkeypatch.setattr(httpx.Client, "get", lambda self, *args, **kwargs: _probe_response(200))
    return key


def _probe_response(status_code: int) -> object:
    """Build a stand-in httpx response with the given status code.

    Args:
        status_code: The HTTP status the fake probe response reports.

    Returns:
        An object exposing the ``status_code`` / ``is_success`` httpx reads.
    """
    return SimpleNamespace(status_code=status_code, is_success=200 <= status_code < 300)


def test_save_key_stores_ciphertext_not_plaintext(engine: object, vault_key: str) -> None:
    """The stored row holds Fernet ciphertext — the plaintext never appears in the DB."""
    vault = ProviderKeyVault(engine=engine)
    secret = "sk-supersecret-abcd"
    with patch("core.billing.byok_vault.httpx.get", return_value=_probe_response(200)):
        view = vault.save_key("u@x.com", "openrouter", secret)
    assert view.last4 == "abcd"
    assert view.status == STATUS_VERIFIED
    with Session(engine) as session:
        row = session.query(BillingProviderKeyModel).filter_by(username="u@x.com", provider="openrouter").one()
        assert row is not None
        assert secret.encode("utf-8") not in row.secret_ciphertext
        # The ciphertext round-trips back to the original secret under the key.
        assert Fernet(vault_key.encode("utf-8")).decrypt(row.secret_ciphertext).decode() == secret


def test_save_key_verified_on_2xx(engine: object, vault_key: str) -> None:
    """A 2xx probe response marks the key verified on entry."""
    vault = ProviderKeyVault(engine=engine)
    with patch("core.billing.byok_vault.httpx.get", return_value=_probe_response(200)):
        view = vault.save_key("u@x.com", "openrouter", "sk-ant-1234")
    assert view.status == STATUS_VERIFIED


@pytest.mark.parametrize("api_base", [None, "https://openrouter.ai", "https://openrouter.ai/api/v1"])
def test_save_openrouter_key_uses_credential_specific_probe(
    engine: object, vault_key: str, api_base: str | None
) -> None:
    """Verify an OpenRouter secret through its authenticated key endpoint.

    Args:
        engine: Local vault database.
        vault_key: Configured encryption key fixture.
        api_base: Default or canonical saved provider endpoint.
    """
    vault = ProviderKeyVault(engine=engine)
    with patch("core.billing.byok_vault.httpx.get", return_value=_probe_response(200)) as get:
        view = vault.save_key("u@x.com", "openrouter", "sk-or-1234", api_base=api_base)
    assert view.status == STATUS_VERIFIED
    assert [call.args[0] for call in get.call_args_list] == ["https://openrouter.ai/api/v1/key"]


def test_save_key_invalid_on_auth_rejection(engine: object, vault_key: str) -> None:
    """A 401 probe response marks the key invalid."""
    vault = ProviderKeyVault(engine=engine)
    with patch("core.billing.byok_vault.httpx.get", return_value=_probe_response(401)):
        view = vault.save_key("u@x.com", "openrouter", "sk-bad-9999")
    assert view.status == STATUS_INVALID


def test_save_key_unverified_on_network_error(engine: object, vault_key: str) -> None:
    """A transient/network error leaves the key unverified — not condemned as invalid."""
    vault = ProviderKeyVault(engine=engine)
    with patch("core.billing.byok_vault.httpx.get", side_effect=httpx.ConnectError("down")):
        view = vault.save_key("u@x.com", "openrouter", "sk-maybe-0000")
    assert view.status == STATUS_UNVERIFIED


def test_save_key_unverified_on_unexpected_status(engine: object, vault_key: str) -> None:
    """A 500 from the provider is inconclusive, so the key stays unverified."""
    vault = ProviderKeyVault(engine=engine)
    with patch("core.billing.byok_vault.httpx.get", return_value=_probe_response(500)):
        view = vault.save_key("u@x.com", "openrouter", "sk-shrug-1111")
    assert view.status == STATUS_UNVERIFIED


def test_save_key_rotates_in_place(engine: object, vault_key: str) -> None:
    """Saving a second key for the same provider replaces the first (rotation)."""
    vault = ProviderKeyVault(engine=engine)
    with patch("core.billing.byok_vault.httpx.get", return_value=_probe_response(200)):
        vault.save_key("u@x.com", "openrouter", "sk-first-1111")
        vault.save_key("u@x.com", "openrouter", "sk-second-2222")
    snapshot = vault.list_keys("u@x.com")
    assert len(snapshot.keys) == 1
    assert snapshot.keys[0].last4 == "2222"


def test_save_key_requires_vault_key(engine: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """Saving a key without a configured vault key raises 503, never a 500."""
    monkeypatch.setattr(settings, "byok_vault_key", None)
    vault = ProviderKeyVault(engine=engine)
    with pytest.raises(DomainError) as exc:
        vault.save_key("u@x.com", "openrouter", "sk-1234")
    assert exc.value.status_code == 503


def test_save_key_rejects_unknown_provider(engine: object, vault_key: str) -> None:
    """An unknown provider slug is rejected with a 400."""
    vault = ProviderKeyVault(engine=engine)
    with pytest.raises(DomainError) as exc:
        vault.save_key("u@x.com", "nope", "sk-1234")
    assert exc.value.status_code == 400


def test_save_key_rejects_empty_secret(engine: object, vault_key: str) -> None:
    """A blank secret is rejected with a 400 before any probe runs."""
    vault = ProviderKeyVault(engine=engine)
    with pytest.raises(DomainError) as exc:
        vault.save_key("u@x.com", "openrouter", "   ")
    assert exc.value.status_code == 400


def test_list_keys_is_secret_free_and_works_without_vault_key(
    engine: object, vault_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Listing keys returns masked views and serves even when the vault key is gone."""
    vault = ProviderKeyVault(engine=engine)
    with patch("core.billing.byok_vault.httpx.get", return_value=_probe_response(200)):
        vault.save_key("u@x.com", "openrouter", "sk-keep-7777")
    monkeypatch.setattr(settings, "byok_vault_key", None)
    snapshot = vault.list_keys("u@x.com")
    assert len(snapshot.keys) == 1
    view = snapshot.keys[0]
    assert view.provider == "openrouter"
    assert view.last4 == "7777"
    assert not hasattr(view, "secret")


def test_verify_key_reprobe_updates_status(engine: object, vault_key: str) -> None:
    """Re-verifying a key saved while the provider was down flips it to verified."""
    vault = ProviderKeyVault(engine=engine)
    with patch("core.billing.byok_vault.httpx.get", side_effect=httpx.ConnectError("down")):
        view = vault.save_key("u@x.com", "openrouter", "sk-later-8888")
    assert view.status == STATUS_UNVERIFIED
    with patch("core.billing.byok_vault.httpx.get", return_value=_probe_response(200)):
        reverified = vault.verify_key("u@x.com", "openrouter")
    assert reverified.status == STATUS_VERIFIED


def test_verify_key_keeps_verified_on_inconclusive_reprobe(engine: object, vault_key: str) -> None:
    """A re-test that can't reach the provider leaves a verified key verified."""
    vault = ProviderKeyVault(engine=engine)
    with patch("core.billing.byok_vault.httpx.get", return_value=_probe_response(200)):
        view = vault.save_key("u@x.com", "openrouter", "sk-live-9999")
    assert view.status == STATUS_VERIFIED
    with patch("core.billing.byok_vault.httpx.get", side_effect=httpx.ConnectError("down")):
        rechecked = vault.verify_key("u@x.com", "openrouter")
    assert rechecked.status == STATUS_VERIFIED


def test_verify_key_demotes_verified_on_rejection(engine: object, vault_key: str) -> None:
    """A definitive rejection still unseats a previously verified key."""
    vault = ProviderKeyVault(engine=engine)
    with patch("core.billing.byok_vault.httpx.get", return_value=_probe_response(200)):
        vault.save_key("u@x.com", "openrouter", "sk-live-1111")
    with patch("core.billing.byok_vault.httpx.get", return_value=_probe_response(401)):
        rechecked = vault.verify_key("u@x.com", "openrouter")
    assert rechecked.status == STATUS_INVALID


def test_verify_key_missing_raises_404(engine: object, vault_key: str) -> None:
    """Verifying a provider with no stored key raises 404."""
    vault = ProviderKeyVault(engine=engine)
    with pytest.raises(DomainError) as exc:
        vault.verify_key("u@x.com", "openrouter")
    assert exc.value.status_code == 404


def test_remove_key_is_idempotent(engine: object, vault_key: str) -> None:
    """Removing a key forgets it; removing again is a harmless no-op."""
    vault = ProviderKeyVault(engine=engine)
    with patch("core.billing.byok_vault.httpx.get", return_value=_probe_response(200)):
        vault.save_key("u@x.com", "openrouter", "sk-gone-3333")
    vault.remove_key("u@x.com", "openrouter")
    vault.remove_key("u@x.com", "openrouter")
    assert vault.list_keys("u@x.com").keys == []


def test_reveal_secret_round_trips(engine: object, vault_key: str) -> None:
    """The run path can decrypt the stored secret back to its plaintext."""
    vault = ProviderKeyVault(engine=engine)
    secret = "sk-reveal-4444"
    with patch("core.billing.byok_vault.httpx.get", return_value=_probe_response(200)):
        vault.save_key("u@x.com", "openrouter", secret)
    assert vault.reveal_secret("u@x.com", "openrouter") == secret


def test_reveal_secret_missing_returns_none(engine: object, vault_key: str) -> None:
    """Revealing a provider with no stored key returns None, not an error."""
    vault = ProviderKeyVault(engine=engine)
    assert vault.reveal_secret("u@x.com", "openrouter") is None


def test_save_key_custom_api_base_probes_that_endpoint(engine: object, vault_key: str) -> None:
    """A connection with a custom api_base verifies against that endpoint, not a default."""
    vault = ProviderKeyVault(engine=engine)
    captured: dict[str, str] = {}

    def fake_get(url: str, headers: dict[str, str]) -> object:
        """Record the probe URL + auth header, then report a 2xx.

        Args:
            url: The endpoint the probe hit.
            headers: The request headers (carrying the auth shape).

        Returns:
            A stand-in 200 response.
        """
        captured["url"] = url
        captured["auth"] = headers.get("Authorization", "")
        return _probe_response(200)

    with patch("core.billing.byok_vault.httpx.Client.get", side_effect=fake_get):
        view = vault.save_key("u@x.com", "custom", "sk-custom-1234", api_base="https://host.example/v1")
    assert view.status == STATUS_VERIFIED
    assert view.api_base == "https://host.example/v1"
    assert captured["url"] == "https://host.example/v1/models"
    assert captured["auth"] == "Bearer sk-custom-1234"


def test_save_key_custom_root_falls_back_across_models_paths(
    engine: object,
    vault_key: str,
) -> None:
    """A root custom URL falls back from ``/v1/models`` to ``/models``."""
    vault = ProviderKeyVault(engine=engine)

    with patch(
        "core.billing.byok_vault.httpx.Client.get",
        side_effect=[_probe_response(404), _probe_response(200)],
    ) as get:
        view = vault.save_key(
            "u@x.com",
            "custom",
            "sk-custom-1234",
            api_base="https://host.example",
        )

    assert view.status == STATUS_VERIFIED
    assert [call.args[0] for call in get.call_args_list] == [
        "https://host.example/v1/models",
        "https://host.example/models",
    ]


def test_save_key_unknown_provider_without_api_base_rejected(engine: object, vault_key: str) -> None:
    """An unknown provider with no custom api_base is rejected with a 400."""
    vault = ProviderKeyVault(engine=engine)
    with pytest.raises(DomainError) as exc:
        vault.save_key("u@x.com", "custom", "sk-1234")
    assert exc.value.status_code == 400


def test_vault_probe_rejects_private_base(engine: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse a private custom probe without sending a key."""
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(0, 0, 0, "", ("10.1.2.3", 443))])
    with pytest.raises(ValueError, match="private"):
        ProviderKeyVault(engine=engine)._probe("custom", "private", "https://private.example/v1")


def test_vault_save_rejects_private_base(engine: object, vault_key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject a private custom base before storing or probing its key."""
    monkeypatch.setattr("core.billing.byok_vault._pinned_address", _pinned_address)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(0, 0, 0, "", ("127.0.0.1", 443))])
    with pytest.raises(HTTPException) as error:
        ProviderKeyVault(engine=engine).save_key("u@x.com", "custom", "private", api_base="https://private.example/v1")
    assert error.value.status_code == 400
    assert error.value.detail == "Invalid custom provider endpoint."
    with Session(engine) as session:
        assert session.query(BillingProviderKeyModel).count() == 0


@pytest.mark.parametrize("slug", ["deepseek", "together"])
def test_probe_and_runtime_use_same_base(slug: str) -> None:
    """Keep verification and inference on the same provider endpoint."""
    assert _PROVIDER_PROBES[slug]["url"] == f"{UPSTREAMS[slug].base_url}/models"


@pytest.mark.parametrize("status", [200, 302])
def test_custom_probe_pins_host_and_tls(engine: object, monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    """Pin probe DNS and refuse redirects with the credential attached."""
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(0, 0, 0, "", ("8.8.8.8", 443))])
    requests: list[httpx.Request] = []

    def send(_transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        """Capture the pinned probe at the socket transport boundary."""
        requests.append(request)
        return httpx.Response(status, headers={"Location": "https://elsewhere.example/models"})

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", send)
    result = ProviderKeyVault(engine=engine)._probe("custom", "private", "https://public.example/v1")
    assert result == (STATUS_VERIFIED if status == 200 else STATUS_UNVERIFIED)
    assert len(requests) == 1
    assert str(requests[0].url) == "https://8.8.8.8/v1/models"
    assert requests[0].headers["host"] == "public.example"
    assert requests[0].extensions["sni_hostname"] == "public.example"
    assert requests[0].headers["authorization"] == "Bearer private"


def test_resolve_connection_round_trips_api_base_and_params(engine: object, vault_key: str) -> None:
    """The run path resolves the secret along with the custom endpoint and params."""
    vault = ProviderKeyVault(engine=engine)
    with patch("core.billing.byok_vault.httpx.get", return_value=_probe_response(200)):
        vault.save_key(
            "u@x.com",
            "custom",
            "sk-resolve-9999",
            api_base="https://host.example/v1",
            params={
                "organization": "org-1",
                "timeout": 30,
                "max_tokens": 512,
                "metadata": {"tenant": "tenant-1", "model": "routing-label"},
                "extra_headers": {"X-Request-Id": "trace-1"},
            },
        )
    resolved = vault.resolve_connection("u@x.com", "custom")
    assert resolved is not None
    assert resolved.secret == "sk-resolve-9999"
    assert resolved.api_base == "https://host.example/v1"
    assert resolved.params == {
        "organization": "org-1",
        "timeout": 30,
        "max_tokens": 512,
        "metadata": {"tenant": "tenant-1", "model": "routing-label"},
        "extra_headers": {"X-Request-Id": "trace-1"},
    }


def test_save_key_drops_connection_overrides_from_plaintext_params(
    engine: object,
    vault_key: str,
) -> None:
    """Reserved params cannot persist a second key, endpoint, or model in plaintext."""
    vault = ProviderKeyVault(engine=engine)
    with patch("core.billing.byok_vault.httpx.get", return_value=_probe_response(200)):
        vault.save_key(
            "u@x.com",
            "custom",
            "sk-resolve-9999",
            api_base="https://host.example/v1",
            params={
                "api_key": "plaintext-secret",
                "base_url": "https://untrusted.example/v1",
                "api_base": "https://also-untrusted.example/v1",
                "model": "openai/different-model",
                "organization": "org-1",
            },
        )

    resolved = vault.resolve_connection("u@x.com", "custom")
    assert resolved is not None
    assert resolved.params == {"organization": "org-1"}


def test_save_key_recursively_drops_case_varied_plaintext_credentials(
    engine: object,
    vault_key: str,
) -> None:
    """Credential-shaped params never persist, regardless of nesting or casing."""
    vault = ProviderKeyVault(engine=engine)
    with patch("core.billing.byok_vault.httpx.get", return_value=_probe_response(200)):
        vault.save_key(
            "u@x.com",
            "custom",
            "sk-resolve-9999",
            api_base="https://host.example/v1",
            params={
                "ClIeNt-SeCrEt": "top-client-secret",
                "AUTHORIZATION": "Bearer top-authorization",
                "accessToken": "top-access-token",
                "token": "top-generic-token",
                "API_KEY": "top-api-key",
                "MODEL": "openai/different-model",
                "BaseURL": "https://untrusted.example/v1",
                "API-Base": "https://also-untrusted.example/v1",
                "organization": "org-1",
                "max_tokens": 256,
                "extra_headers": {
                    "X-API-Key": "nested-api-key",
                    "Authorization": "Bearer nested-authorization",
                    "Authorization-Header": "Bearer alternate-authorization",
                    "X-Request-Id": "trace-1",
                },
                "transport": {
                    "options": [
                        {
                            "refresh_token": "nested-refresh-token",
                            "PrivateKey": "nested-private-key",
                            "region": "us-east-1",
                        }
                    ]
                },
            },
        )

    expected = {
        "organization": "org-1",
        "max_tokens": 256,
        "extra_headers": {"X-Request-Id": "trace-1"},
        "transport": {"options": [{"region": "us-east-1"}]},
    }
    with Session(engine) as session:
        row = session.query(BillingProviderKeyModel).filter_by(username="u@x.com", provider="custom").one()
        assert row.params == expected
    resolved = vault.resolve_connection("u@x.com", "custom")
    assert resolved is not None
    assert resolved.params == expected


def test_resolve_connection_redacts_credentials_from_legacy_params(
    engine: object,
    vault_key: str,
) -> None:
    """Resolution strips unsafe fields from rows saved before recursive filtering."""
    vault = ProviderKeyVault(engine=engine)
    with patch("core.billing.byok_vault.httpx.get", return_value=_probe_response(200)):
        vault.save_key(
            "u@x.com",
            "custom",
            "sk-resolve-9999",
            api_base="https://host.example/v1",
            params={"organization": "org-1"},
        )
    with Session(engine) as session:
        row = session.query(BillingProviderKeyModel).filter_by(username="u@x.com", provider="custom").one()
        row.params = {
            "organization": "org-1",
            "Default-Headers": {
                "aUtHoRiZaTiOn": "Bearer legacy-secret",
                "X-Trace-Id": "trace-legacy",
            },
            "nested": [{"CLIENT_SECRET": "legacy-client-secret", "timeout": 10}],
        }
        session.commit()

    resolved = vault.resolve_connection("u@x.com", "custom")
    assert resolved is not None
    assert resolved.params == {
        "organization": "org-1",
        "Default-Headers": {"X-Trace-Id": "trace-legacy"},
        "nested": [{"timeout": 10}],
    }


def test_resolve_connection_missing_returns_none(engine: object, vault_key: str) -> None:
    """Resolving a provider with no stored connection returns None."""
    vault = ProviderKeyVault(engine=engine)
    assert vault.resolve_connection("u@x.com", "openrouter") is None
