"""Tests for subscription routes and bound capabilities on the run model gateway."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

from core import config
from core.api import preflight_execution
from core.billing import cli_dispatch
from core.billing.budgets import BudgetService
from core.billing.model_dispatch import ModelHTTPResult
from core.billing.model_gateway import ROUTE_KEY, ModelGateway
from core.billing.operation_pricing import UnpricedOperationError
from core.billing.runtime import BudgetRuntime
from core.billing.tests.test_cli_runners import CLAUDE_SUCCESS, fake_cli, local_settings, printing
from core.storage.models import Base, BillingCustomerModel, ExecutionOperationModel, WalletLedgerModel

MODEL = "anthropic/claude-sonnet-4.6"
REQUEST = {"model": MODEL, "messages": [{"role": "user", "content": "ping"}]}


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Engine]:
    """Keep wallet and operation evidence in a private test database."""
    engine = create_engine(f"sqlite:///{tmp_path / 'gateway.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username="alice", stripe_customer_id="fixture", balance_cents=100, grant_remaining=0)
        )
        session.commit()
    yield engine
    engine.dispose()


@pytest.fixture
def gateway(database: Engine) -> Iterator[ModelGateway]:
    """Start a gateway for a 50-cent setup budget."""
    service = BudgetService(engine=database)
    budget = service.create("alice", 50, idempotency_key="setup")
    runtime = BudgetRuntime(service, username="alice", budget_id=budget.id, generation=0, phase="setup", wait_timeout=0)
    instance = ModelGateway(runtime)
    yield instance
    instance.close()


@pytest.fixture
def local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn local mode on with a fake claude and fixed list prices."""
    fake_cli(tmp_path, "claude", printing(CLAUDE_SUCCESS))
    monkeypatch.setattr(config, "settings", local_settings(tmp_path))
    monkeypatch.setattr(cli_dispatch, "_list_prices", lambda model: None)


def _post(url: str, token: str, path: str, body: dict) -> httpx.Response:
    """POST one JSON body to the gateway with a route token."""
    with httpx.Client(trust_env=False) as client:
        return client.post(f"{url}{path}", json=body, headers={"Authorization": f"Bearer {token}"})


@pytest.mark.usefixtures("local")
def test_subscription_route_answers_and_bills_nothing(gateway: ModelGateway, database: Engine) -> None:
    """Answer a guest call from the CLI and record the operation at zero cents."""
    route = gateway.register_subscription(MODEL, "claude", "judge")
    response = _post(route["url"], route["token"], "/chat/completions", REQUEST)
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "pong"
    snapshot = gateway.runtime.service.get(gateway.runtime.budget_id, "alice")
    assert snapshot.billed_cents == 0
    assert snapshot.reserved_cents == 0
    with Session(database) as session:
        [operation] = session.scalars(select(ExecutionOperationModel)).all()
        deltas = session.scalars(select(WalletLedgerModel.delta_cents)).all()
        balance = session.get(BillingCustomerModel, "alice").balance_cents
    assert operation.actual_units == 0
    assert operation.role == "judge"
    assert all(delta == 0 for delta in deltas)
    assert balance == 100


@pytest.mark.usefixtures("local")
def test_protect_payload_routes_subscription_roles(gateway: ModelGateway) -> None:
    """Route a ``subscription/<cli>/<slug>`` model and a ``cli_provider`` model through the CLI."""
    payload = {
        "model_config": {
            "name": f"subscription/claude/{MODEL}",
            "token_source": "subscription",
            "cli_provider": "claude",
        },
        "reflection_model_config": {"name": MODEL, "token_source": "subscription", "cli_provider": "claude"},
    }
    protected = gateway.protect_payload(payload, managed_key="")
    for key in ("model_config", "reflection_model_config"):
        route = protected[key]["extra"][ROUTE_KEY]
        assert route["model"] == MODEL
        assert "base_url" not in protected[key]
        assert _post(route["url"], route["token"], "/chat/completions", REQUEST).status_code == 200


def test_protect_payload_refuses_subscription_outside_local_mode(
    gateway: ModelGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuse a subscription model role when local mode is off."""
    monkeypatch.setattr(config.settings, "local_mode", False)
    payload = {"model_config": {"name": MODEL, "token_source": "subscription", "cli_provider": "claude"}}
    with pytest.raises(UnpricedOperationError):
        gateway.protect_payload(payload, managed_key="managed")
    with pytest.raises(UnpricedOperationError):
        gateway.register_subscription(MODEL, "claude", "task")


@pytest.mark.usefixtures("local")
@pytest.mark.parametrize(
    "model_config",
    [
        {"name": MODEL, "token_source": "subscription", "cli_provider": "droid"},
        {"name": f"subscription/droid/{MODEL}", "token_source": "subscription", "cli_provider": "droid"},
    ],
)
def test_protect_payload_refuses_a_stored_run_that_names_droid(gateway: ModelGateway, model_config: dict) -> None:
    """Refuse a stored payload that still names the removed Droid CLI, before any CLI starts.

    Args:
        gateway: The model gateway under test.
        model_config: A stored model config that names Droid.
    """
    with pytest.raises(UnpricedOperationError, match="needs a CLI"):
        gateway.protect_payload({"model_config": model_config}, managed_key="")


@pytest.mark.usefixtures("local")
def test_subscription_agent_target_needs_its_model_config(gateway: ModelGateway) -> None:
    """Refuse a subscription agent target that has no model config to route."""
    payload = {"target": {"model": MODEL}, "token_source": "subscription"}
    with pytest.raises(UnpricedOperationError):
        gateway.protect_payload(payload, managed_key="managed")


@pytest.mark.usefixtures("local")
def test_bound_capability_needs_a_route_token(gateway: ModelGateway) -> None:
    """Serve a bound path to route holders only, over HTTP and the sandbox mailbox."""
    received: list[tuple[bytes, dict[str, str]]] = []

    def handler(body: bytes, headers: dict[str, str]) -> ModelHTTPResult:
        """Echo the request back."""
        received.append((body, headers))
        return ModelHTTPResult(200, "application/json", body)

    gateway.bind_capability("/v1/_host_agent", handler)
    route = gateway.register_subscription(MODEL, "claude", "task")
    assert _post(route["url"], route["token"], "/_host_agent", {"goal": "x"}).json() == {"goal": "x"}
    assert _post(route["url"], "wrong", "/_host_agent", {"goal": "x"}).status_code == 401
    guest = gateway.dispatch_guest(route["token"], "/v1/_host_agent", {"goal": "y"}, {})
    assert json.loads(guest.body) == {"goal": "y"}
    assert len(received) == 2
    with pytest.raises(ValueError):
        gateway.bind_capability("/v1/_host_agent", handler)
    with pytest.raises(ValueError):
        gateway.bind_capability("/v1/_mcp", handler)


@pytest.mark.usefixtures("local")
def test_capability_with_its_own_token_refuses_model_route_tokens(gateway: ModelGateway) -> None:
    """Serve a capability bound to a dedicated token to that token only, over HTTP and the mailbox."""
    received: list[bytes] = []

    def handler(body: bytes, headers: dict[str, str]) -> ModelHTTPResult:
        """Echo the request back."""
        received.append(body)
        return ModelHTTPResult(200, "application/json", body)

    dedicated = "host-agent-capability-token"
    gateway.bind_capability("/v1/_host_agent", handler, token=dedicated)
    route = gateway.register_subscription(MODEL, "claude", "task")
    assert _post(route["url"], dedicated, "/_host_agent", {"goal": "x"}).json() == {"goal": "x"}
    with httpx.Client(trust_env=False) as client:
        by_api_key = client.post(f"{route['url']}/_host_agent", json={"goal": "k"}, headers={"x-api-key": dedicated})
    assert by_api_key.json() == {"goal": "k"}
    assert json.loads(gateway.dispatch_guest(dedicated, "/v1/_host_agent", {"goal": "y"}, {}).body) == {"goal": "y"}
    for token in (route["token"], "wrong", "t\u00e9"):
        refused = _post(route["url"], token, "/_host_agent", {"goal": "x"}) if token.isascii() else None
        if refused is not None:
            assert refused.status_code == 403
            assert refused.json()["error"]["code"] == "forbidden"
        guest = gateway.dispatch_guest(token, "/v1/_host_agent", {"goal": "x"}, {})
        assert guest.status == 403
        assert json.loads(guest.body)["error"]["message"] == "This capability needs its own token."
    assert len(received) == 3
    assert _post(route["url"], dedicated, "/chat/completions", REQUEST).status_code == 401
    with pytest.raises(ValueError):
        gateway.dispatch_guest(dedicated, "/v1/chat/completions", REQUEST, {})


@pytest.mark.usefixtures("local")
def test_unbound_capability_is_not_found(gateway: ModelGateway) -> None:
    """Answer 404 for an unbound ``/v1/_`` path instead of sending it to a model route."""
    route = gateway.register_subscription(MODEL, "claude", "task")
    response = _post(route["url"], route["token"], "/_host_agent", {"goal": "x"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert gateway.dispatch_guest(route["token"], "/v1/_host_agent", {"goal": "x"}, {}).status == 404
    assert _post(route["url"], "wrong", "/_host_agent", {"goal": "x"}).status_code == 401


@pytest.mark.usefixtures("local")
def test_native_preflight_reaches_a_subscription_optimization_model(gateway: ModelGateway) -> None:
    """Pass the setup check of a native engine, which sends the optimization role ``/v1/messages``."""
    gateway.register_subscription(MODEL, "claude", "optimization")
    gateway.register_subscription(MODEL, "claude", "task")
    checks = preflight_execution._verify_model_routes(gateway, native=True)
    assert {check["key"]: check["status"] for check in checks} == {
        "model.optimization": "succeeded",
        "model.task": "succeeded",
    }
