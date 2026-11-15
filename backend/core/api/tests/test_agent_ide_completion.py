"""Check that editor completions need a session, pay through protected dispatch, and stay bounded."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from core.api.auth import AuthenticatedUser, get_authenticated_user
from core.api.errors import DomainError
from core.api.routers import agent_ide_completion
from core.billing.byok_vault import STATUS_UNVERIFIED, ProviderKeyVault
from core.billing.tests.test_protected_dispatch import CATALOG
from core.storage.models import Base, BillingCustomerModel, ExecutionBudgetModel, ExecutionOperationModel

REQUEST = {"path": "src/app.py", "prefix": "def add(a, b):\n    ret", "suffix": "\n"}
# The fixture replaces the lookup in every test; these tests call the real one.
REAL_OWN_KEY = agent_ide_completion._own_openrouter_key
ANSWER = {"completion": "urn a + b", "next_edit": {"after": "def main():", "lines": ["    print(add(1, 2))"]}}


def _reply(content: str) -> dict[str, Any]:
    """Build a live-shaped chat completion with an authoritative cost.

    Args:
        content: The model's message text.

    Returns:
        The provider's JSON body.
    """
    return {
        "id": "gen-synthetic",
        "model": "fixture/text",
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 80, "completion_tokens": 20, "cost": 0.001},
    }


@pytest.fixture
def ide(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Serve the real pricing and ledger code over a synthetic provider.

    Args:
        tmp_path: Private database directory.
        monkeypatch: Replace only the provider network, the model pick and the key lookup.

    Returns:
        The client, the database, the recorded provider calls and a settable reply.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'ide.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username="alice", stripe_customer_id="fixture", balance_cents=100, grant_remaining=0)
        )
        session.commit()
    settings = agent_ide_completion.settings
    monkeypatch.setattr(settings, "local_mode", False)
    monkeypatch.setattr(settings, "openrouter_api_key", SecretStr("synthetic-platform-key"))
    monkeypatch.setattr(settings, "usage_markup", 1.0)
    monkeypatch.setattr(agent_ide_completion, "hosted_suggest_model", lambda: "openrouter/fixture/text")
    monkeypatch.setattr(agent_ide_completion, "byok_suggest_model", lambda: "openrouter/fixture/text")
    monkeypatch.setattr(agent_ide_completion, "data_policy_for", lambda username: "deny")
    monkeypatch.setattr(agent_ide_completion, "_own_openrouter_key", lambda engine, username: None)
    state = SimpleNamespace(requests=[], status=200, reply=_reply(json.dumps(ANSWER)))

    def provider(request: httpx.Request) -> httpx.Response:
        """Return catalog prices, or the reply after checking the call is reserved."""
        if request.method == "GET":
            return httpx.Response(200, json={"data": CATALOG})
        state.requests.append(request)
        with Session(engine) as session:
            operation = session.scalar(select(ExecutionOperationModel))
            assert operation is not None
            assert operation.state == "dispatched"
        return httpx.Response(state.status, json=state.reply)

    factory = httpx.Client
    monkeypatch.setattr(
        agent_ide_completion.httpx,
        "Client",
        lambda **kwargs: factory(transport=httpx.MockTransport(provider), **kwargs),
    )
    app = FastAPI()
    app.include_router(
        agent_ide_completion.create_agent_ide_completion_router(job_store=SimpleNamespace(engine=engine))
    )
    state.client = TestClient(app)
    state.engine = engine
    return state


def _sign_in(ide: SimpleNamespace) -> None:
    """Authenticate the client as alice.

    Args:
        ide: The router fixture.
    """
    ide.client.app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser("alice", "user", ())


def _operation(ide: SimpleNamespace) -> tuple[Any, Any]:
    """Read the one budget and the one operation the call wrote.

    Args:
        ide: The router fixture.

    Returns:
        ``(budget, operation)``.
    """
    with Session(ide.engine) as session:
        return session.scalar(select(ExecutionBudgetModel)), session.scalar(select(ExecutionOperationModel))


def test_signed_out_request_cannot_spend(ide: SimpleNamespace) -> None:
    """Reject a call with no session before any budget or provider call.

    Args:
        ide: The router fixture.
    """
    assert ide.client.post("/agent-ide/completions", json=REQUEST).status_code == 401
    assert ide.requests == []
    assert _operation(ide) == (None, None)


def test_managed_call_reserves_then_settles_on_the_wallet(ide: SimpleNamespace) -> None:
    """Reserve before the provider call, settle the cost, and return the parsed answer.

    Args:
        ide: The router fixture.
    """
    _sign_in(ide)
    response = ide.client.post("/agent-ide/completions", json=REQUEST)
    assert response.status_code == 200, response.text
    assert response.json() == ANSWER
    assert len(ide.requests) == 1
    assert ide.requests[0].headers["authorization"] == "Bearer synthetic-platform-key"
    sent = json.loads(ide.requests[0].content)
    assert sent["model"] == "fixture/text"
    assert sent["max_tokens"] == agent_ide_completion.MAX_OUTPUT_TOKENS
    assert "def add(a, b):\n    ret<CARET>\n" in sent["messages"][-1]["content"]
    budget, operation = _operation(ide)
    assert budget.username == "alice"
    assert budget.state == "closed"
    assert operation.state == "settled"
    assert operation.actual_wallet_units > 0


def test_empty_wallet_blocks_the_provider_call(ide: SimpleNamespace) -> None:
    """Refuse a platform-key call before the provider sees it when the wallet is empty.

    Args:
        ide: The router fixture.
    """
    _sign_in(ide)
    with Session(ide.engine) as session:
        session.scalar(select(BillingCustomerModel)).balance_cents = 0
        session.commit()
    assert ide.client.post("/agent-ide/completions", json=REQUEST).status_code == 402
    assert ide.requests == []


def test_byok_key_pays_the_provider_with_no_wallet_charge(
    ide: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Send a call with a verified BYOK key on that key, with no Skynet model charge.

    The budget still opens against the account, as for a BYOK run, so an
    account with no funds gets no call.

    Args:
        ide: The router fixture.
        monkeypatch: Return a verified OpenRouter key for alice.
    """
    _sign_in(ide)
    monkeypatch.setattr(agent_ide_completion, "_own_openrouter_key", lambda engine, username: "alice-own-key")
    monkeypatch.setattr(agent_ide_completion.settings, "openrouter_api_key", None)
    response = ide.client.post("/agent-ide/completions", json=REQUEST)
    assert response.status_code == 200, response.text
    assert response.json() == ANSWER
    assert ide.requests[0].headers["authorization"] == "Bearer alice-own-key"
    assert "provider" not in json.loads(ide.requests[0].content)
    budget, operation = _operation(ide)
    assert budget.state == "closed"
    assert operation.state == "settled"
    assert operation.actual_wallet_units == 0
    with Session(ide.engine) as session:
        wallet = session.scalar(select(BillingCustomerModel))
        assert wallet.balance_cents == 100
        wallet.balance_cents = 0
        session.commit()
    assert ide.client.post("/agent-ide/completions", json=REQUEST).status_code == 402
    assert len(ide.requests) == 1


def test_window_over_the_cap_is_refused_before_spending(ide: SimpleNamespace) -> None:
    """Refuse a prefix or suffix over the cap with 422 and no provider call.

    Args:
        ide: The router fixture.
    """
    _sign_in(ide)
    long_prefix = {**REQUEST, "prefix": "x" * (agent_ide_completion.MAX_PREFIX_CHARS + 1)}
    long_suffix = {**REQUEST, "suffix": "x" * (agent_ide_completion.MAX_SUFFIX_CHARS + 1)}
    assert ide.client.post("/agent-ide/completions", json=long_prefix).status_code == 422
    assert ide.client.post("/agent-ide/completions", json=long_suffix).status_code == 422
    assert ide.requests == []
    at_cap = {**REQUEST, "prefix": "x" * agent_ide_completion.MAX_PREFIX_CHARS}
    assert ide.client.post("/agent-ide/completions", json=at_cap).status_code == 200


def test_provider_error_answers_502_and_still_settles(ide: SimpleNamespace) -> None:
    """Map a provider failure to 502, and close the budget the call opened.

    Args:
        ide: The router fixture.
    """
    _sign_in(ide)
    ide.status = 500
    ide.reply = {"error": {"message": "boom"}}
    assert ide.client.post("/agent-ide/completions", json=REQUEST).status_code == 502
    budget, _ = _operation(ide)
    assert budget.state == "closed"


def test_no_platform_key_and_no_byok_answers_503(ide: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer 503 with no key to pay, before any budget is opened.

    Args:
        ide: The router fixture.
        monkeypatch: Remove the platform key.
    """
    _sign_in(ide)
    monkeypatch.setattr(agent_ide_completion.settings, "openrouter_api_key", None)
    assert ide.client.post("/agent-ide/completions", json=REQUEST).status_code == 503
    assert _operation(ide) == (None, None)


def test_rate_limit_stops_the_call_before_spending(ide: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer 429 when the caller is over the limit, with no provider call.

    Args:
        ide: The router fixture.
        monkeypatch: Make the limiter refuse.
    """
    _sign_in(ide)
    keys = []

    class Refuse:
        """A limiter that is always over the limit."""

        def __init__(self, client: Any) -> None:
            """Accept the Redis client and ignore it."""

        def enforce(self, key: str, *, limit: int, window_seconds: int) -> None:
            """Record the key and refuse."""
            keys.append(key)
            raise DomainError("rate_limit.exceeded", status=429)

    monkeypatch.setattr(agent_ide_completion, "RateLimiter", Refuse)
    assert ide.client.post("/agent-ide/completions", json=REQUEST).status_code == 429
    assert keys == ["agent-ide-complete:alice"]
    assert ide.requests == []


def _vault(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure a fresh vault key, so the vault can store and read keys.

    Args:
        monkeypatch: Set the vault key on the settings.
    """
    monkeypatch.setattr(agent_ide_completion.settings, "byok_vault_key", SecretStr(Fernet.generate_key().decode()))


def _save_key(engine: Any, secret: str, api_base: str | None = None) -> None:
    """Store an OpenRouter key for alice, with the entry probe answering 200.

    Args:
        engine: The test database.
        secret: The key to store.
        api_base: Optional custom endpoint of the connection.
    """
    ok = SimpleNamespace(status_code=200, is_success=True)
    with patch("core.billing.byok_vault.httpx.get", return_value=ok):
        ProviderKeyVault(engine=engine).save_key("alice", "openrouter", secret, api_base=api_base)


def test_real_lookup_returns_a_verified_openrouter_key(ide: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """Return the secret of a verified OpenRouter connection on openrouter.ai.

    Args:
        ide: The router fixture.
        monkeypatch: Configure the vault key.
    """
    _vault(monkeypatch)
    _save_key(ide.engine, "alice-own-key")
    assert REAL_OWN_KEY(ide.engine, "alice") == "alice-own-key"
    assert REAL_OWN_KEY(None, "alice") is None
    assert REAL_OWN_KEY(ide.engine, "bob") is None


def test_real_lookup_without_a_vault_key_returns_none(ide: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """Return None when the server has no vault key, even with a stored connection.

    Args:
        ide: The router fixture.
        monkeypatch: Configure, then remove, the vault key.
    """
    _vault(monkeypatch)
    _save_key(ide.engine, "alice-own-key")
    monkeypatch.setattr(agent_ide_completion.settings, "byok_vault_key", None)
    assert REAL_OWN_KEY(ide.engine, "alice") is None


def test_real_lookup_skips_an_unverified_key(ide: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """Return None for a stored key that the provider did not verify.

    Args:
        ide: The router fixture.
        monkeypatch: Configure the vault key and make the entry probe unverified.
    """
    _vault(monkeypatch)
    monkeypatch.setattr(ProviderKeyVault, "_probe", lambda *args, **kwargs: STATUS_UNVERIFIED)
    _save_key(ide.engine, "alice-own-key")
    assert REAL_OWN_KEY(ide.engine, "alice") is None


def test_real_lookup_falls_back_when_the_vault_fails(ide: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """Return None, so the call uses the platform key, when the vault raises a DomainError.

    Args:
        ide: The router fixture.
        monkeypatch: Configure the vault key and make the vault read fail.
    """
    _vault(monkeypatch)
    _save_key(ide.engine, "alice-own-key")

    def broken(*args: Any, **kwargs: Any) -> None:
        """Fail as a vault with a rotated key does."""
        raise DomainError("billing.byok_vault_unavailable", status=503)

    monkeypatch.setattr(ProviderKeyVault, "resolve_connection", broken)
    assert REAL_OWN_KEY(ide.engine, "alice") is None


def test_key_for_another_host_never_goes_to_openrouter(ide: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a key for a custom api_base off openrouter.ai, and pay the call on the platform key.

    Args:
        ide: The router fixture.
        monkeypatch: Configure the vault, accept the custom host, and restore the real lookup.
    """
    _vault(monkeypatch)
    monkeypatch.setattr("core.billing.byok_vault._pinned_address", lambda *args, **kwargs: "8.8.8.8")
    monkeypatch.setattr(ProviderKeyVault, "_probe", lambda *args, **kwargs: "verified")
    _save_key(ide.engine, "proxy-secret", api_base="https://proxy.example.com/v1")
    assert REAL_OWN_KEY(ide.engine, "alice") is None
    monkeypatch.setattr(agent_ide_completion, "_own_openrouter_key", REAL_OWN_KEY)
    _sign_in(ide)
    assert ide.client.post("/agent-ide/completions", json=REQUEST).status_code == 200
    assert [request.headers["authorization"] for request in ide.requests] == ["Bearer synthetic-platform-key"]


@pytest.mark.parametrize(
    ("content", "suffix", "expected"),
    [
        ('```json\n{"completion": "x = 1", "next_edit": null}\n```', "", {"completion": "x = 1", "next_edit": None}),
        ("no json here", "", {"completion": "", "next_edit": None}),
        ('{"completion": 5, "next_edit": {"after": "", "lines": ["a"]}}', "", {"completion": "", "next_edit": None}),
        ('{"completion": ")", "next_edit": null}', ")\n", {"completion": "", "next_edit": None}),
        ('{"completion": "a", "next_edit": {"after": "b", "lines": []}}', "", {"completion": "a", "next_edit": None}),
    ],
)
def test_parse_drops_what_cannot_be_shown(content: str, suffix: str, expected: dict[str, Any]) -> None:
    """Read fenced JSON, and drop bad fields, a repeat of the suffix and an empty next edit.

    Args:
        content: The model's message text.
        suffix: Text after the caret.
        expected: The response the route returns.
    """
    assert agent_ide_completion._parse(content, suffix).model_dump() == expected
