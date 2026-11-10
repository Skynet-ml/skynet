"""Check managed Mac inference authorization, wallet reservation, and relay isolation."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from core.api import agent_engine
from core.api.auth import AuthenticatedUser, get_authenticated_user
from core.api.routers import managed_inference
from core.billing import metering
from core.billing.tests.test_protected_dispatch import CATALOG, REQUEST
from core.local import managed_models
from core.models import ModelConfig
from core.service_gateway import language_models
from core.storage.models import Base, BillingCustomerModel, ExecutionBudgetModel, ExecutionOperationModel


@pytest.fixture
def inference(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Any, list[httpx.Request]]:
    """Serve real pricing and ledger code over synthetic provider responses.

    Args:
        tmp_path: Private database directory.
        monkeypatch: Replace only the provider network and catalog discovery.

    Returns:
        The router client, database, and recorded physical provider requests.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'inference.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username="alice", stripe_customer_id="fixture", balance_cents=100, grant_remaining=0)
        )
        session.commit()
    monkeypatch.setattr(managed_inference.settings, "local_mode", False)
    monkeypatch.setattr(managed_inference.settings, "openrouter_api_key", SecretStr("synthetic-platform-key"))
    monkeypatch.setattr(managed_inference.settings, "usage_markup", 1.0)
    monkeypatch.setattr(managed_inference, "require_known_model", lambda model: None)
    monkeypatch.setattr(managed_inference, "data_policy_for", lambda username: "deny")
    requests = []

    def provider(request: httpx.Request) -> httpx.Response:
        """Return live-shaped prices and authoritative cost without a model call."""
        if request.method == "GET":
            return httpx.Response(200, json={"data": CATALOG})
        requests.append(request)
        with Session(engine) as session:
            operation = session.scalar(select(ExecutionOperationModel))
            assert operation is not None
            assert operation.state == "dispatched"
        return httpx.Response(
            200,
            json={
                "id": "gen-synthetic",
                "model": "fixture/text",
                "choices": [],
                "usage": {"prompt_tokens": 8, "completion_tokens": 2, "cost": 0.001},
            },
        )

    factory = httpx.Client
    monkeypatch.setattr(
        managed_inference.httpx, "Client", lambda **kwargs: factory(transport=httpx.MockTransport(provider), **kwargs)
    )
    app = FastAPI()
    app.include_router(managed_inference.create_managed_inference_router(job_store=SimpleNamespace(engine=engine)))
    return TestClient(app), engine, requests


def test_signed_out_requests_cannot_spend(inference: tuple) -> None:
    """Reject inference before pricing or dispatch when no account authenticates.

    Args:
        inference: Isolated router and provider fixture.
    """
    client, engine, requests = inference
    assert client.post("/models/inference/v1/chat/completions", json=REQUEST).status_code == 401
    assert requests == []
    with Session(engine) as session:
        assert session.scalar(select(ExecutionBudgetModel)) is None


def test_linked_account_pays_once_after_reserved_dispatch(inference: tuple) -> None:
    """Reserve before dispatch and settle only the authenticated account's wallet.

    Args:
        inference: Isolated router and provider fixture.
    """
    client, engine, requests = inference
    client.app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser(
        "alice", "user", (), desktop=True
    )
    response = client.post("/models/inference/v1/chat/completions", json={**REQUEST, "username": "not-the-payer"})
    assert response.status_code == 200, response.text
    assert len(requests) == 1
    assert requests[0].headers["authorization"] == "Bearer synthetic-platform-key"
    body = json.loads(requests[0].content)
    assert body["provider"]["only"] == ["fixture"]
    with Session(engine) as session:
        operation = session.scalar(select(ExecutionOperationModel))
        budget = session.scalar(select(ExecutionBudgetModel))
        assert budget.username == "alice"
        assert operation.state == "settled"
        assert budget.state == "closed"
        assert operation.actual_wallet_units > 0


def test_zero_wallet_blocks_provider_dispatch(inference: tuple) -> None:
    """Refuse a managed inference call before the provider receives any request.

    Args:
        inference: Isolated router and provider fixture.
    """
    client, engine, requests = inference
    client.app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser(
        "alice", "user", (), desktop=True
    )
    with Session(engine) as session:
        wallet = session.scalar(select(BillingCustomerModel))
        wallet.balance_cents = 0
        session.commit()
    assert client.post("/models/inference/v1/chat/completions", json=REQUEST).status_code == 402
    assert requests == []


def test_hosted_inference_is_not_debited_again_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip local charging for usage already settled on the hosted account.

    Args:
        monkeypatch: Reject construction of a local billing service.
    """

    def unexpected(**kwargs: Any) -> None:
        """Fail if local billing starts for already settled model calls."""
        pytest.fail("Hosted inference must not charge the local wallet")

    monkeypatch.setattr(metering, "StripeBillingService", unexpected)
    lm = SimpleNamespace(hosted_inference=True)
    assert metering.meter_llm_run(object(), "local", [lm], description="Agent chat") == 0


def test_local_relay_binds_model_and_refreshes_through_existing_link(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep hosted credentials out of model config and refuse forged model selections.

    Args:
        monkeypatch: Replace the hosted network at the existing account-link boundary.
    """
    seen = []

    def forward(*args: Any) -> Any:
        """Record the account-bound request and return a synthetic completion."""
        seen.append(args)
        return SimpleNamespace(
            status=200,
            headers={"content-type": "application/json"},
            chunks=lambda: iter([b'{"choices": []}']),
            close=lambda: None,
        )

    monkeypatch.setattr(managed_models.hosted_proxy, "forward", forward)
    gateway = managed_models.HostedModelGateway()
    try:
        config = gateway.config(object(), "local-user", "openrouter/fixture/text")
        assert config.token_source == "managed"
        with httpx.Client(base_url=config.base_url, trust_env=False) as client:
            assert client.post("chat/completions", json=REQUEST).status_code == 403
            headers = {"Authorization": f"Bearer {config.extra['api_key']}"}
            assert (
                client.post("chat/completions", headers=headers, json={**REQUEST, "model": "other/model"}).status_code
                == 422
            )
            assert client.post("chat/completions", headers=headers, json=REQUEST).status_code == 200
        assert len(seen) == 1
        assert seen[0][1:4] == ("local-user", "POST", "models/inference/v1/chat/completions")
        assert "authorization" not in seen[0][5]
    finally:
        gateway.server.shutdown()
        gateway.server.server_close()


def test_linked_mac_without_key_keeps_openrouter_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the selected OpenRouter engine on the linked account without a saved key.

    Args:
        monkeypatch: Supply a signed-in local account and no verified key.
    """
    store = SimpleNamespace(engine=object())
    monkeypatch.setattr(agent_engine, "_has_verified_key", lambda *args: False)
    monkeypatch.setattr(agent_engine, "link_status", lambda *args: object())
    monkeypatch.setattr(agent_engine, "effective_agent_engine", lambda *args: "openrouter")
    assert agent_engine._turn_engine(store, "local") == "openrouter"


def test_managed_model_preserves_proxy_and_native_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Build a bounded managed LM through the account relay without exposing its internal marker.

    Args:
        monkeypatch: Avoid dynamic catalog requests while building the LM.
    """
    monkeypatch.setattr(language_models, "default_effort_for", lambda model: None)
    config = ModelConfig(
        name="openrouter/fixture/text",
        base_url="http://127.0.0.1:9999/v1",
        extra={
            "api_key": "scoped-local-token",
            managed_models.HOSTED_INFERENCE_KEY: True,
            "extra_body": {"provider": {"only": ["fixture/priority"]}},
        },
    )
    lm = language_models.build_language_model(config)
    assert lm.hosted_inference is True
    assert lm.subscription is False
    assert lm.model == "litellm_proxy/fixture/text"
    assert lm.kwargs["max_tokens"] > 0
    assert lm.kwargs["base_url"] == config.base_url
    assert managed_models.HOSTED_INFERENCE_KEY not in lm.kwargs
    assert lm.kwargs["extra_body"]["provider"]["only"] == ["fixture/priority"]
