"""Verify fixed external evaluator transport, isolation, and real setup evidence without live calls."""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.api import preflight_execution
from core.api.auth import AuthenticatedUser
from core.api.preflight_execution import WizardPreflightRequest, run_preflight
from core.billing import mcp_broker
from core.billing.model_gateway import ModelGateway
from core.billing.model_mailbox import ModelMailbox
from core.billing.protected_credentials import (
    ProtectedCredentialVault,
    protect_execution_credentials,
    resolve_execution_credentials,
)
from core.billing.remote_evaluator import RemoteEvaluatorBroker
from core.billing.signals import BudgetReached
from core.config import settings
from core.exceptions import ServiceError
from core.models.blackbox import BlackboxScorer
from core.service_gateway.optimization.blackbox import preflight
from core.service_gateway.optimization.blackbox.preflight import verify_anything_in_sandbox
from core.service_gateway.optimization.blackbox.sandbox import LocalSubprocessRuntime, SandboxSpec
from core.service_gateway.optimization.blackbox.scorer import build_scorer
from core.storage.models import ExecutionOperationModel
from core.worker.scoped_relay import model_forwarder

from .test_model_gateway_transport import gateway as _gateway_fixture

gateway = _gateway_fixture


def test_scorer_model_pending_points_to_metric_code() -> None:
    """Tell clients where to declare a model when no reflection model exists."""
    result = verify_anything_in_sandbox(
        {"scorer": {"kind": "python", "metric_code": "llm('x')"}},
        scope="evaluation",
        identity="test",
        runtime=LocalSubprocessRuntime(),
    )
    assert result["checks"][0]["message"] == "Set the model with llm(model=...) in the metric code."


def test_preflight_resolves_declared_scorer_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve the model before constructing the sandbox scorer."""
    observed = []
    monkeypatch.setattr(preflight, "scorer_gateway", lambda model, settings: observed.append(model.name))
    monkeypatch.setattr(
        preflight, "SandboxPythonScorer", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("stop"))
    )
    with pytest.raises(RuntimeError, match="stop"):
        verify_anything_in_sandbox(
            {
                "scorer": {"kind": "python", "metric_code": "llm(model='judge')"},
                "reflection_model_config": {"name": "reflection"},
            },
            scope="evaluation",
            identity="test",
            runtime=LocalSubprocessRuntime(),
        )
    assert observed == ["judge"]


@dataclass
class _Endpoint:
    """Retain only mocked external requests and their configured actual response."""

    requests: list[httpx.Request] = field(default_factory=list)
    status: int = 200
    result: Any = field(default_factory=lambda: {"score": 0.75, "feedback": "actual endpoint feedback"})
    headers: dict[str, str] = field(default_factory=dict)


@pytest.fixture
def endpoint(monkeypatch: pytest.MonkeyPatch) -> _Endpoint:
    """Mock only external DNS and async network transport, leaving the parent HTTP server real."""
    state = _Endpoint()
    original = socket.getaddrinfo

    def addresses(host: str, *args: Any, **kwargs: Any) -> list[tuple]:
        """Pin the test evaluator while allowing the real loopback parent connection."""
        if host == "evaluator.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 443))]
        return original(host, *args, **kwargs)

    def respond(request: httpx.Request) -> httpx.Response:
        """Record the actual request after DNS pinning and preserve the selected response."""
        state.requests.append(request)
        return httpx.Response(state.status, json=state.result, headers=state.headers)

    monkeypatch.setattr(mcp_broker.socket, "getaddrinfo", addresses)
    monkeypatch.setattr(mcp_broker.httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(respond))
    return state


def _payload() -> dict[str, Any]:
    """Return an actual candidate and evaluator with credentials that must remain parent-owned."""
    return {
        "scorer": {
            "kind": "remote",
            "url": "https://evaluator.example/score?version=original&access_token=parent-query-secret",
            "secret": "parent-evaluator-secret",
            "model": {"name": "unused", "extra": {"api_key": "unused-parent-key"}},
        },
        "seed_candidate": {"system": "actual system text", "user": "actual user text"},
        "cases": [{"question": "training case"}, {"question": "held-out case"}],
        "shuffle": False,
        "split_fractions": {"train": 0.5, "val": 0, "test": 0.5},
    }


def _protect(gateway: ModelGateway) -> dict[str, Any]:
    """Issue the evaluator capability through the real parent authority."""
    return gateway.protect_payload(_payload(), managed_key="")


def test_actual_protocol_pins_destination_and_preserves_endpoint_result(
    gateway: ModelGateway,
    endpoint: _Endpoint,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep original candidate, case, auth, Host, TLS identity, and normalized feedback."""
    monkeypatch.setattr(settings, "byok_vault_key", SecretStr(Fernet.generate_key().decode()))
    vault = ProtectedCredentialVault(engine=gateway.runtime.service._engine)
    persisted = protect_execution_credentials(
        _payload(),
        username="alice",
        binding_id=gateway.runtime.budget_id,
        vault=vault,
    )
    assert "parent-evaluator-secret" not in json.dumps(persisted)
    assert "parent-query-secret" not in json.dumps(persisted)
    parent = resolve_execution_credentials(
        persisted,
        username="alice",
        binding_id=gateway.runtime.budget_id,
        vault=vault,
    )
    payload = gateway.protect_payload(parent, managed_key="")
    encoded = json.dumps(payload)
    assert all(
        secret not in encoded for secret in ("parent-evaluator-secret", "unused-parent-key", "evaluator.example")
    )
    scorer = build_scorer(
        BlackboxScorer.model_validate(payload["scorer"]), protected_route=payload["_skynet_evaluator_route"]
    )
    assert scorer(payload["seed_candidate"], {"question": "actual case"}) == (
        0.75,
        {"feedback": "actual endpoint feedback"},
    )
    [request] = endpoint.requests
    assert request.method == "POST"
    assert str(request.url) == "https://8.8.8.8/score?version=original&access_token=parent-query-secret"
    assert request.headers["host"] == "evaluator.example"
    assert request.extensions["sni_hostname"] == "evaluator.example"
    assert request.headers["authorization"] == "Bearer parent-evaluator-secret"
    assert json.loads(request.content) == {"candidate": payload["seed_candidate"], "case": {"question": "actual case"}}
    snapshot = gateway.runtime.service.get(gateway.runtime.budget_id, "alice")
    assert snapshot.setup_spent_cents == 0
    assert snapshot.pending_operations == 0
    with Session(gateway.runtime.service._engine) as session:
        assert session.scalar(select(func.count()).select_from(ExecutionOperationModel)) == 0


def test_remote_evaluator_response_cannot_echo_parent_credentials(
    gateway: ModelGateway,
    endpoint: _Endpoint,
) -> None:
    """Redact bearer and private URL values from an evaluator-controlled response body."""
    endpoint.result = {
        "score": 0.75,
        "feedback": "Bearer parent-evaluator-secret at parent-query-secret",
    }
    payload = _protect(gateway)
    scorer = build_scorer(
        BlackboxScorer.model_validate(payload["scorer"]),
        protected_route=payload["_skynet_evaluator_route"],
    )

    score, metadata = scorer(payload["seed_candidate"], {"question": "actual case"})

    assert score == 0.75
    assert metadata["feedback"] == "Bearer [REDACTED] at [REDACTED]"


@pytest.mark.parametrize("status", [400, 401, 422, 500, 302])
def test_http_failure_is_original_and_never_retried_or_redirected(
    gateway: ModelGateway, endpoint: _Endpoint, status: int
) -> None:
    """Surface the endpoint status and error text without following its redirect or retrying."""
    payload = _protect(gateway)
    endpoint.status = status
    endpoint.result = {"error": "the real endpoint rejected this candidate"}
    endpoint.headers = {"Location": "http://169.254.169.254/credentials"}
    scorer = build_scorer(
        BlackboxScorer.model_validate(payload["scorer"]), protected_route=payload["_skynet_evaluator_route"]
    )
    with pytest.raises(ServiceError, match=f"HTTP {status}.*real endpoint rejected"):
        scorer("actual candidate", None)
    assert len(endpoint.requests) == 1


def test_network_failure_is_safe_provider_error_without_hidden_retry(
    gateway: ModelGateway, endpoint: _Endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Map one failed physical connection to a safe 502 without revealing its destination."""
    payload = _protect(gateway)
    attempts = []

    def fail(request: httpx.Request) -> httpx.Response:
        """Record the single permitted attempt and raise the transport's original failure."""
        attempts.append(request)
        raise httpx.ConnectError("sensitive endpoint transport detail", request=request)

    monkeypatch.setattr(mcp_broker.httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(fail))
    scorer = build_scorer(
        BlackboxScorer.model_validate(payload["scorer"]), protected_route=payload["_skynet_evaluator_route"]
    )
    with pytest.raises(ServiceError, match="HTTP 502") as failed:
        scorer("actual candidate", None)
    assert "evaluator.example" not in str(failed.value)
    assert "sensitive endpoint transport detail" not in str(failed.value)
    assert len(attempts) == 1


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fd00:ec2::254"])
def test_private_and_metadata_destinations_fail_before_dispatch(monkeypatch: pytest.MonkeyPatch, address: str) -> None:
    """Deny every non-global DNS answer, including mixed public/private resolution."""
    monkeypatch.setattr(
        mcp_broker.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", (value, 443)) for value in ("8.8.8.8", address)
        ],
    )
    with pytest.raises(ValueError, match=r"private remote evaluator|metadata-service"):
        RemoteEvaluatorBroker(
            "https://evaluator.example/score", secret=None, timeout_seconds=10, check_admission=lambda: None
        )


@pytest.mark.parametrize("timeout", [0, -1, 601, float("nan"), float("inf")])
def test_unbounded_timeouts_are_rejected(timeout: float) -> None:
    """Validate setup's partial raw payload before DNS or transport allocation."""
    with pytest.raises(ValueError, match="timeout"):
        RemoteEvaluatorBroker(
            "https://evaluator.example/score", secret=None, timeout_seconds=timeout, check_admission=lambda: None
        )


def test_stop_before_request_propagates_without_contacting_endpoint(endpoint: _Endpoint) -> None:
    """Keep a typed budget stop outside ordinary evaluator failure handling."""
    checks = []

    def stop() -> None:
        """Trip after dispatch validation but immediately before physical HTTP work."""
        checks.append(True)
        if len(checks) == 2:
            raise BudgetReached("stopped before evaluator")

    broker = RemoteEvaluatorBroker(
        "https://evaluator.example/score", secret=None, timeout_seconds=10, check_admission=stop
    )
    with pytest.raises(BudgetReached, match="stopped before evaluator"):
        broker.dispatch({"candidate": "actual"})
    assert endpoint.requests == []


def test_capabilities_cannot_change_destination_or_invoke_other_services(
    gateway: ModelGateway, endpoint: _Endpoint
) -> None:
    """Restrict both parent HTTP and worker mailbox to this fixed evaluator's protocol."""
    payload = _protect(gateway)
    route = payload["_skynet_evaluator_route"]
    headers = {"Authorization": f"Bearer {route['token']}"}
    dispatch = model_forwarder(payload)
    for path in ("/v1/chat/completions", "/v1/responses", "/v1/_mcp", "/v1/_sandbox"):
        with pytest.raises(ValueError, match="cannot dispatch"):
            gateway.dispatch_guest(route["token"], path, {}, {})
        with pytest.raises(ValueError, match="unknown metered model capability"):
            dispatch(route["token"], path, {}, {})
        response = httpx.post(gateway.url.removesuffix("/v1") + path, headers=headers, json={}, trust_env=False)
        assert response.status_code == 401
    response = httpx.post(
        gateway.url + "/_evaluator",
        headers=headers,
        json={"candidate": "actual", "url": "https://other.example/score"},
        trust_env=False,
    )
    assert response.status_code == 422
    assert endpoint.requests == []
    response = dispatch(route["token"], "/v1/_evaluator", {"candidate": "actual", "case": None}, {})
    assert response.status == 200
    assert len(endpoint.requests) == 1


def test_seedless_readiness_never_invents_a_candidate(gateway: ModelGateway, endpoint: _Endpoint) -> None:
    """Record endpoint validation separately from a future candidate's actual score."""
    payload = _protect(gateway)
    payload.pop("seed_candidate")
    result = verify_anything_in_sandbox(
        payload,
        scope="evaluation",
        identity="seedless",
        runtime=LocalSubprocessRuntime(),
    )
    assert result["checks"][0]["status"] == "succeeded"
    assert result["evaluator_readiness"]["candidate_evaluation"] == "awaiting_first_generated_candidate"
    assert "scorer_result" not in result
    assert endpoint.requests == []


def test_guest_mailbox_preserves_evaluator_response_without_endpoint_credentials(
    gateway: ModelGateway, endpoint: _Endpoint
) -> None:
    """Exercise the real guest HTTP-to-stdout bridge using only its scoped evaluator token."""
    payload = _protect(gateway)
    route = payload["_skynet_evaluator_route"]
    session = LocalSubprocessRuntime().open(SandboxSpec(lifetime_seconds=15))
    source = (
        "import json, os, urllib.request\n"
        "request=urllib.request.Request(os.environ['SKYNET_BUDGET_RELAY_URL']+'/_evaluator', "
        "data=json.dumps({'candidate':'guest candidate','case':{'question':'guest case'}}).encode(), "
        "headers={'Content-Type':'application/json','Authorization':'Bearer '+os.environ['EVALUATOR_TOKEN']})\n"
        "with urllib.request.urlopen(request) as response: print(json.load(response)['score'])\n"
    )
    try:
        session.write_files({"evaluate.py": source})
        result = ModelMailbox(model_forwarder(payload)).run(
            session,
            "python3 evaluate.py",
            env={"EVALUATOR_TOKEN": route["token"]},
            timeout_seconds=10,
        )
    finally:
        session.close()
    assert result.ok
    assert result.stdout.strip() == "0.75"
    assert "parent-evaluator-secret" not in result.stdout
    [request] = endpoint.requests
    assert json.loads(request.content) == {"candidate": "guest candidate", "case": {"question": "guest case"}}


def test_persistent_budget_stop_crosses_relay_without_external_dispatch(
    gateway: ModelGateway, endpoint: _Endpoint
) -> None:
    """Restore the normal typed stop from authoritative parent state without a paid retry."""
    payload = _protect(gateway)
    scorer = build_scorer(
        BlackboxScorer.model_validate(payload["scorer"]), protected_route=payload["_skynet_evaluator_route"]
    )
    gateway.runtime.service.stop_admission(gateway.runtime.budget_id, "alice", reason="budget_reached")
    with pytest.raises(BudgetReached):
        scorer("actual candidate", None)
    assert endpoint.requests == []


def test_continue_uses_nonheldout_case_and_persists_actual_remote_result(
    gateway: ModelGateway, endpoint: _Endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Execute the real setup lease, gateway, and scorer through the selected outer runtime."""
    monkeypatch.setattr(preflight_execution.settings, "openrouter_api_key", None)
    monkeypatch.setattr(settings, "byok_vault_key", SecretStr(Fernet.generate_key().decode()))
    bindings = []
    monkeypatch.setattr(preflight_execution, "bind_protected_sandbox", lambda *args, **kwargs: bindings.append(kwargs))

    def execute(payload: dict[str, Any], _artifact_id: str, events: Any, _start_method: str) -> None:
        """Replace only the platform sandbox while retaining its guest preflight entry point."""
        preflight = payload["_preflight"]
        result = verify_anything_in_sandbox(
            payload,
            scope=preflight["scope"],
            identity=preflight["identity"],
            runtime=LocalSubprocessRuntime(),
        )
        events.put({"type": "preflight_result", "result": result})

    monkeypatch.setattr(preflight_execution, "run_vercel_dspy", execute)
    request = WizardPreflightRequest(
        scope="evaluation",
        workflow="anything",
        payload=_payload(),
        execution_budget_id=gateway.runtime.budget_id,
        execution_budget_revision=1,
    )
    result = run_preflight(
        request, AuthenticatedUser("alice", "user", ()), SimpleNamespace(engine=gateway.runtime.service._engine)
    ).model_dump(mode="json")
    assert result["status"] == "succeeded"
    assert result["scorer_result"]["score"] == 0.75
    [request] = endpoint.requests
    assert json.loads(request.content)["case"] == {"question": "training case"}
    assert result["budget"]["setup_spent_cents"] == "0"
    assert len(bindings) == 1
    assert bindings[0]["workflow"] == "anything"
    assert isinstance(bindings[0]["owner_id"], str)
    assert bindings[0]["owner_id"]
