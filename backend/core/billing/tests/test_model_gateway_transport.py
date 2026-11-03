"""Exercise real scoped HTTP and offline guest mailbox boundaries without provider calls."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from core.api.preflight_execution import _verify_model_routes
from core.billing import model_gateway as gateway_module
from core.billing.budgets import BudgetInsufficientError, BudgetService
from core.billing.model_dispatch import ModelHTTPResult
from core.billing.model_gateway import (
    EMBEDDING_ROUTE_KEY,
    PARENT_HOSTS_KEY,
    ROUTE_KEY,
    SHINKA_EMBEDDING_MODEL,
    ModelGateway,
)
from core.billing.model_mailbox import ModelMailbox
from core.billing.operation_pricing import ChargePolicy
from core.billing.protected_credentials import (
    ProtectedCredentialVault,
    protect_execution_credentials,
    resolve_execution_credentials,
)
from core.billing.runtime import BudgetRuntime
from core.billing.usage_tags import CALLER_PROPOSER, KIND_EMBEDDING, USAGE_TAGS_HEADER, encode_tags
from core.billing.vercel_usage import PACKAGE_REGISTRY_HOSTS
from core.config import VERCEL_SANDBOX_LIFETIME_CEILING_SECONDS, settings
from core.exceptions import InfrastructureInterruptionError, ServiceError
from core.service_gateway.optimization.blackbox.remote_sandbox import RemoteSandboxRuntime
from core.service_gateway.optimization.blackbox.sandbox import CommandResult, LocalSubprocessRuntime, SandboxSpec
from core.service_gateway.optimization.blackbox.sandbox_broker import SandboxBroker
from core.storage.models import Base, BillingCustomerModel, ExecutionOperationModel, ExecutionUsageEvidenceModel
from core.worker.scoped_relay import model_forwarder

IMAGE = "fixture@sha256:" + "a" * 64
CATALOG = {
    "id": "fixture/text",
    "endpoints": [
        {
            "tag": "fixture",
            "provider_name": "Fixture",
            "context_length": 1000,
            "max_completion_tokens": 1000,
            "pricing": {"prompt": "0.00001", "completion": "0.00002"},
        }
    ],
}


@pytest.fixture(autouse=True)
def _at_cost_markup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the usage markup to 1.0 so these tests check settlement arithmetic, not pricing policy."""
    monkeypatch.setattr(settings, "usage_markup", 1.0)


@pytest.fixture
def gateway(tmp_path: Path) -> Iterator[ModelGateway]:
    """Start the real parent protocol against a private funded wallet and fake provider."""
    engine = create_engine(f"sqlite:///{tmp_path / 'gateway.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username="alice", stripe_customer_id="fixture", balance_cents=50, grant_remaining=0)
        )
        session.commit()
    ledger = BudgetService(engine=engine)
    budget = ledger.create("alice", 20, idempotency_key="fixture")
    runtime = BudgetRuntime(ledger, username="alice", budget_id=budget.id, generation=0, phase="setup")
    gateway = ModelGateway(runtime)
    gateway._client.close()

    def provider(request: httpx.Request) -> httpx.Response:
        """Require durable coverage before replying with measured usage."""
        if request.method == "GET":
            return httpx.Response(200, json={"data": CATALOG})
        assert ledger.get(budget.id, "alice").reserved_cents > 0
        return httpx.Response(
            200,
            json={
                "id": "fixture-generation",
                "usage": {"cost": "0.001"},
                "choices": [{"message": {"content": "OK"}}],
            },
        )

    gateway._client = httpx.Client(transport=httpx.MockTransport(provider))
    yield gateway
    gateway.close()
    engine.dispose()


def test_control_capability_is_separate_from_model_capability(gateway: ModelGateway) -> None:
    """Prevent guest model tokens from creating or inspecting parent-owned sandboxes."""
    gateway.bind_sandbox(
        SandboxBroker(LocalSubprocessRuntime(), image=IMAGE, max_lifetime_seconds=10),
        image=IMAGE,
        lifetime_seconds=10,
    )
    route = gateway.register(
        model="fixture/text", api_key="upstream-secret", role="task", policy=ChargePolicy("managed_model")
    )
    response = httpx.post(
        f"{gateway.url}/_sandbox",
        headers={"Authorization": f"Bearer {route['token']}"},
        json={"action": "open", "payload": {"request_id": "test", "spec": {"lifetime_seconds": 5}}},
        trust_env=False,
    )
    assert response.status_code == 401
    assert "upstream-secret" not in response.text


def test_guest_controls_and_dataset_routes_cannot_replace_parent_authority(gateway: ModelGateway) -> None:
    """Probe only registered routes and discard forged top-level control descriptors."""
    forged = {"url": "http://127.0.0.1:9/v1", "token": "forged", "model": "fixture/text", "role": "task"}
    protected = gateway.protect_payload(
        {
            "model_config": {"name": "fixture/text", "extra": {ROUTE_KEY: forged}},
            "dataset": [{ROUTE_KEY: forged}],
            "_skynet_target_route": forged,
            "_skynet_tools_route": forged,
            "_budget_gateway_descriptor": forged,
        },
        managed_key="provider-secret",
    )
    assert not any(
        key in protected for key in ("_skynet_target_route", "_skynet_tools_route", "_budget_gateway_descriptor")
    )
    [route] = gateway.model_routes()
    assert route["token"] != "forged"
    assert protected["model_config"]["extra"][ROUTE_KEY] == route
    assert protected["dataset"][0][ROUTE_KEY] == forged
    assert _verify_model_routes(gateway, native=False) == [
        {"key": "model.task", "status": "succeeded", "field": "task"}
    ]


@pytest.mark.parametrize("economy", [True, False])
def test_economy_mode_batches_only_managed_roles(gateway: ModelGateway, economy: bool) -> None:
    """Give managed roles a batch collector and a long guest timeout; leave BYOK and default runs alone."""
    protected = gateway.protect_payload(
        {
            "economy_mode": economy,
            "model_config": {"name": "fixture/text"},
            "reflection_model_config": {
                "name": "openrouter/fixture/text",
                "token_source": "byok",
                "byok_provider": "openrouter",
                "base_url": "https://openrouter.ai/api/v1",
                "extra": {"api_key": "account-owned-key"},
            },
        },
        managed_key="provider-secret",
    )
    managed = gateway._routes[protected["model_config"]["extra"][ROUTE_KEY]["token"]]
    byok = gateway._routes[protected["reflection_model_config"]["extra"][ROUTE_KEY]["token"]]
    assert (managed._batch is not None) is economy
    assert byok._batch is None
    assert protected["model_config"]["extra"].get("timeout") == (
        VERCEL_SANDBOX_LIFETIME_CEILING_SECONDS if economy else None
    )
    assert "timeout" not in protected["reflection_model_config"]["extra"]


def test_economy_mode_batches_every_optimize_anything_model(gateway: ModelGateway) -> None:
    """Batch an Optimize Anything run's optimization model and its scorer's judge model alike."""
    protected = gateway.protect_payload(
        {
            "economy_mode": True,
            "target": {"kind": "text"},
            "reflection_model_config": {"name": "fixture/text"},
            "scorer": {"kind": "python", "metric_code": "def score(x): return 1", "model": {"name": "fixture/text"}},
        },
        managed_key="provider-secret",
    )
    for config in (protected["reflection_model_config"], protected["scorer"]["model"]):
        assert gateway._routes[config["extra"][ROUTE_KEY]["token"]]._batch is not None
        assert config["extra"]["timeout"] == VERCEL_SANDBOX_LIFETIME_CEILING_SECONDS


def test_economy_mode_batches_only_the_models_that_ask_for_it(gateway: ModelGateway) -> None:
    """Batch each managed model whose own config asks for economy, and leave its siblings at full price."""
    protected = gateway.protect_payload(
        {
            "target": {"kind": "text"},
            "reflection_model_config": {"name": "fixture/text", "extra": {"economy_mode": True}},
            "extra_reflection_model_configs": [{"name": "fixture/text"}],
            "scorer": {"kind": "python", "metric_code": "def score(x): return 1", "model": {"name": "fixture/text"}},
        },
        managed_key="provider-secret",
    )
    reflection = protected["reflection_model_config"]
    assert gateway.batches(reflection["extra"][ROUTE_KEY]["token"])
    assert reflection["extra"]["timeout"] == VERCEL_SANDBOX_LIFETIME_CEILING_SECONDS
    assert "economy_mode" not in reflection["extra"]
    for config in (protected["extra_reflection_model_configs"][0], protected["scorer"]["model"]):
        assert not gateway.batches(config["extra"][ROUTE_KEY]["token"])
        assert "timeout" not in config["extra"]


def test_model_economy_never_batches_an_own_key_model(gateway: ModelGateway) -> None:
    """Keep an own-key model at its provider's price even when its config asks for economy."""
    protected = gateway.protect_payload(
        {
            "model_config": {
                "name": "openrouter/fixture/text",
                "token_source": "byok",
                "byok_provider": "openrouter",
                "base_url": "https://openrouter.ai/api/v1",
                "extra": {"api_key": "account-owned-key", "economy_mode": True},
            },
        },
        managed_key="provider-secret",
    )
    config = protected["model_config"]
    assert not gateway.batches(config["extra"][ROUTE_KEY]["token"])
    assert "economy_mode" not in config["extra"]
    assert "timeout" not in config["extra"]


def test_byok_agent_task_uses_its_scoped_fee_metered_route(gateway: ModelGateway) -> None:
    """Route the evaluated agent through its own key while billing only the BYOK fee policy."""
    protected = gateway.protect_payload(
        {
            "token_source": "byok",
            "target": {"kind": "agent", "model": "openrouter/fixture/text"},
            "task_model_config": {
                "name": "openrouter/fixture/text",
                "token_source": "byok",
                "byok_provider": "openrouter",
                "base_url": "https://openrouter.ai/api/v1",
                "extra": {"api_key": "account-owned-key"},
            },
        },
        managed_key="",
    )

    route = protected["_skynet_target_route"]
    assert protected["task_model_config"]["extra"][ROUTE_KEY] == route
    assert "account-owned-key" not in json.dumps(protected)
    dispatcher = gateway._routes[route["token"]]
    assert dispatcher.policy.kind == "byok_model"


def test_selected_tools_keep_credentials_outside_guest_and_cannot_call_models(
    gateway: ModelGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise distinct real HTTP capabilities without contacting an external MCP service."""
    selected = {}

    class SelectedTools:
        """Record the parent's fixed endpoint and simulate its selected roster."""

        def __init__(self, url, **kwargs):
            """Retain the exact connection chosen before any guest request."""
            selected.update(url=url, **kwargs)
            self.check = kwargs["check_admission"]

        def dispatch(self, body):
            """Return a roster only while the original execution owner remains admitted."""
            self.check()
            return ModelHTTPResult(200, "application/json", b'{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}')

    monkeypatch.setattr(gateway_module, "McpToolsBroker", SelectedTools)
    monkeypatch.setattr(settings, "byok_vault_key", SecretStr(Fernet.generate_key().decode()))
    payload = {
        "model_config": {"name": "fixture/text"},
        "tool_source": {
            "kind": "live_mcp",
            "mcp_url": "https://selected.example/mcp?key=endpoint-secret",
            "mcp_auth_header": "Bearer tool-secret",
            "tool_filter": ["lookup"],
        },
    }
    vault = ProtectedCredentialVault(engine=gateway.runtime.service._engine)
    persisted = protect_execution_credentials(
        payload,
        username="alice",
        binding_id=gateway.runtime.budget_id,
        vault=vault,
    )
    assert "tool-secret" not in json.dumps(persisted)
    assert "endpoint-secret" not in json.dumps(persisted)
    parent = resolve_execution_credentials(
        persisted,
        username="alice",
        binding_id=gateway.runtime.budget_id,
        vault=vault,
    )
    protected = gateway.protect_payload(
        parent,
        managed_key="upstream-secret",
    )
    assert selected["url"] == "https://selected.example/mcp?key=endpoint-secret"
    assert selected["auth_header"] == "Bearer tool-secret"
    assert selected["tool_filter"] == ["lookup"]
    assert all(secret not in json.dumps(protected) for secret in ("tool-secret", "endpoint-secret", "upstream-secret"))
    tools = protected["_skynet_tools_route"]
    headers = {"Authorization": f"Bearer {tools['token']}"}
    assert (
        httpx.post(
            f"{gateway.url}/_mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
            trust_env=False,
        ).status_code
        == 200
    )
    assert httpx.post(f"{gateway.url}/chat/completions", headers=headers, json={}, trust_env=False).status_code == 401
    state = httpx.get(f"{gateway.url}/_budget/state", headers=headers, trust_env=False)
    assert state.status_code == 200
    assert "account_available_cents" not in state.json()
    gateway.runtime.service.fence_generation(gateway.runtime.budget_id, "alice", expected_generation=0)
    state = httpx.get(f"{gateway.url}/_budget/state", headers=headers, trust_env=False)
    assert state.json()["blocked_reason"] == "generation_fenced"


def test_remote_runtime_streams_and_metered_mailbox_scrubs_protocol(gateway: ModelGateway) -> None:
    """Round-trip a real guest model request without exposing protocol data in output."""
    gateway.bind_sandbox(
        SandboxBroker(
            LocalSubprocessRuntime(),
            image=IMAGE,
            max_lifetime_seconds=20,
            command_runner=ModelMailbox(gateway.dispatch_guest).run,
        ),
        image=IMAGE,
        lifetime_seconds=20,
    )
    protected = gateway.protect_payload(
        {"model_config": {"name": "fixture/text"}},
        managed_key="upstream-secret",
    )
    descriptor = protected["_budget_gateway_descriptor"]
    route = protected["model_config"]["extra"]["_skynet_budget_route"]
    runtime = RemoteSandboxRuntime(descriptor["url"], descriptor["control_token"])
    session = runtime.open(SandboxSpec(lifetime_seconds=20, image=IMAGE, network_disabled=True))
    source = (
        "import json, os, urllib.request\n"
        "body=json.dumps({'model':'fixture/text','max_tokens':16,'messages':[{'role':'user','content':'private prompt'}]}).encode()\n"
        "req=urllib.request.Request(os.environ['OPENAI_BASE_URL']+'/chat/completions', data=body, "
        "headers={'Content-Type':'application/json','Authorization':'Bearer '+os.environ['ROLE_TOKEN']})\n"
        "with urllib.request.urlopen(req) as response: print(json.load(response)['choices'][0]['message']['content'])\n"
    )
    session.write_files({"check.py": source})
    output = []
    try:
        result = session.run(
            "python3 check.py",
            env={"ROLE_TOKEN": route["token"]},
            timeout_seconds=15,
            on_output=lambda stream, text: output.append(text),
        )
    finally:
        session.close()
    assert result.ok
    assert result.stdout.strip() == "OK"
    assert "private prompt" not in result.stdout
    assert "APP_MODEL_" not in "".join(output)
    assert gateway.runtime.service.get(gateway.runtime.budget_id, "alice").setup_spent_cents == Decimal("0.1")
    assert "upstream-secret" not in json.dumps(protected)


def test_mailbox_retries_have_independent_coverage_and_duplicate_frames_do_not_redispatch(
    gateway: ModelGateway,
) -> None:
    """Cover two physical attempts separately while deduplicating one delivery replay."""
    gateway._client.close()
    posts: list[httpx.Request] = []
    holds: list[Decimal] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """Record the atomic hold visible before each physical provider request."""
        if request.method == "GET":
            return httpx.Response(200, json={"data": CATALOG})
        posts.append(request)
        holds.append(gateway.runtime.service.get(gateway.runtime.budget_id, gateway.runtime.username).reserved_cents)
        attempt = len(posts)
        return httpx.Response(
            503 if attempt == 1 else 200,
            json={
                "id": f"retry-{attempt}",
                "usage": {"cost": "0.001"},
                "choices": [{"message": {"content": "retry" if attempt == 1 else "OK"}}],
            },
        )

    gateway._client = httpx.Client(transport=httpx.MockTransport(provider))
    protected = gateway.protect_payload(
        {"model_config": {"name": "fixture/text"}},
        managed_key="upstream-secret",
    )
    route = protected["model_config"]["extra"][ROUTE_KEY]
    body = base64.b64encode(
        json.dumps(
            {
                "model": "fixture/text",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "retry safely"}],
            }
        ).encode()
    ).decode()
    documents = [
        {
            "id": "a" * 32,
            "path": "/v1/chat/completions",
            "body": body,
            "headers": {"authorization": f"Bearer {route['token']}"},
        },
        {
            "id": "b" * 32,
            "path": "/v1/chat/completions",
            "body": body,
            "headers": {"authorization": f"Bearer {route['token']}"},
        },
    ]

    class ReplaySession:
        """Emit one mailbox frame twice before a distinct retry frame."""

        def __init__(self) -> None:
            """Create an inspectable in-memory guest filesystem."""
            self.files: dict[str, str] = {}

        def write_files(self, files: dict[str, str]) -> None:
            """Persist supervisor inputs and responses by guest path."""
            self.files.update(files)

        def run(self, _command: str, **kwargs: Any) -> CommandResult:
            """Replay the first delivery frame, then emit the real retry."""
            config_path = next(path for path in self.files if path.endswith("/config.json"))
            prefix = json.loads(self.files[config_path])["prefix"]
            output = kwargs["on_output"]
            output("stdout", prefix + json.dumps(documents[0]) + "\n")
            output("stdout", prefix + json.dumps(documents[0]) + "\n")
            output("stdout", prefix + json.dumps(documents[1]) + "\n")
            return CommandResult(exit_code=0)

    ModelMailbox(gateway.dispatch_guest, concurrency=1).run(ReplaySession(), "ignored")

    assert len(posts) == 2
    assert len(holds) == 2
    assert all(hold > 0 for hold in holds)
    snapshot = gateway.runtime.service.get(gateway.runtime.budget_id, gateway.runtime.username)
    assert snapshot.setup_spent_cents == Decimal("0.2")
    assert snapshot.reserved_cents == 0
    with Session(gateway.runtime.service._engine) as session:
        operations = session.scalars(
            select(ExecutionOperationModel).order_by(ExecutionOperationModel.operation_key)
        ).all()
    assert [(operation.operation_key, operation.state) for operation in operations] == [
        ("model-attempt:" + "a" * 32, "settled"),
        ("model-attempt:" + "b" * 32, "settled"),
    ]


def test_cost_ceiling_is_the_total_or_what_the_account_can_fund(gateway: ModelGateway) -> None:
    """Hand a guest a capped budget's total and an uncapped budget's account allowance.

    Args:
        gateway: Parent protocol over the capped fixture budget.
    """
    assert gateway.cost_ceiling_cents() == 20
    ledger = gateway.runtime.service
    uncapped = ledger.create("alice", 1, idempotency_key="uncapped", uncapped=True)
    other = ModelGateway(BudgetRuntime(ledger, username="alice", budget_id=uncapped.id, generation=0, phase="run"))
    try:
        ceiling = other.cost_ceiling_cents()
    finally:
        other.close()
    assert ceiling == int(uncapped.available_cents) > uncapped.total_cents


def test_bound_evaluator_answers_only_its_capability(gateway: ModelGateway) -> None:
    """A parent-owned evaluator serves the guest's scoring requests behind its own token."""
    received: list[Any] = []

    class _Evaluator:
        """Echo the candidate back as its score."""

        def dispatch(self, body: Any) -> ModelHTTPResult:
            """Record the request and score it.

            Args:
                body: The guest's request.

            Returns:
                A fixed score.
            """
            received.append(body)
            return ModelHTTPResult(200, "application/json", b'{"score": 0.5}')

    route = gateway.bind_evaluator(_Evaluator())
    with pytest.raises(ValueError, match="already owns an evaluator"):
        gateway.bind_evaluator(_Evaluator())
    body = {"candidate": "diff", "case": None}
    reply = httpx.post(
        f"{route['url']}/_evaluator", headers={"Authorization": f"Bearer {route['token']}"}, json=body, trust_env=False
    )
    assert reply.json() == {"score": 0.5}
    assert received == [body]
    refused = httpx.post(
        f"{route['url']}/_evaluator", headers={"Authorization": "Bearer wrong"}, json=body, trust_env=False
    )
    assert refused.status_code == 401
    assert len(received) == 1


def test_parent_registry_box_is_funded_but_unreachable_from_the_guest_route(gateway: ModelGateway) -> None:
    """Fund the parent's box, keep its hosts out of the guest's descriptor, and refuse them remotely."""
    gateway.bind_sandbox(
        SandboxBroker(LocalSubprocessRuntime(), image=IMAGE, max_lifetime_seconds=20),
        image=IMAGE,
        lifetime_seconds=20,
    )
    with pytest.raises(ValueError, match="Fund"):
        gateway.parent_sandbox_runtime()
    gateway.fund_parent_sandbox(PACKAGE_REGISTRY_HOSTS)
    protected = gateway.protect_payload({"model_config": {"name": "fixture/text"}}, managed_key="upstream-secret")
    descriptor = protected["_budget_gateway_descriptor"]

    assert PARENT_HOSTS_KEY not in descriptor
    assert "pypi.org" not in json.dumps(descriptor)
    remote = RemoteSandboxRuntime(descriptor["url"], descriptor["control_token"])
    with pytest.raises(ServiceError, match="network access"):
        remote.open(SandboxSpec(lifetime_seconds=20, image=IMAGE, allowed_hosts=("pypi.org",)))
    gateway.parent_sandbox_runtime()


EMBEDDING_CATALOG = {
    "id": SHINKA_EMBEDDING_MODEL,
    "architecture": {"output_modalities": ["embeddings"]},
    "endpoints": [
        {
            "tag": "openai",
            "provider_name": "OpenAI",
            "context_length": 8192,
            "pricing": {"prompt": "0.00000002", "completion": "0"},
        }
    ],
}


@pytest.mark.parametrize("novelty", [True, False])
def test_shinka_novelty_adds_a_billed_embeddings_route(gateway: ModelGateway, novelty: bool) -> None:
    """Give only a duplicate-rejecting ShinkaEvolve run an embeddings route, billed as optimization.

    Args:
        gateway: Parent protocol over the funded fixture budget.
        novelty: Whether the run turns duplicate rejection on.
    """
    sent: list[tuple[str, dict[str, Any]]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """Serve the embeddings catalog and a measured embeddings answer.

        Args:
            request: Outbound provider request.

        Returns:
            The fake provider's answer.
        """
        if request.method == "GET":
            catalog = EMBEDDING_CATALOG if "embedding" in str(request.url) else CATALOG
            return httpx.Response(200, json={"data": catalog})
        sent.append((request.url.path, json.loads(request.content)))
        return httpx.Response(
            200,
            json={
                "id": "embedding-generation",
                "object": "list",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
                "usage": {"prompt_tokens": 3, "total_tokens": 3, "cost": "0.0001"},
            },
        )

    gateway._client.close()
    gateway._client = httpx.Client(transport=httpx.MockTransport(provider))
    protected = gateway.protect_payload(
        {"shinka": {"novelty": novelty}, "reflection_model_config": {"name": "fixture/text"}},
        managed_key="provider-secret",
    )
    route = protected["reflection_model_config"]["extra"][ROUTE_KEY]
    if not novelty:
        assert EMBEDDING_ROUTE_KEY not in route
        return
    embedding = route[EMBEDDING_ROUTE_KEY]
    assert embedding["model"] == SHINKA_EMBEDDING_MODEL
    assert embedding["role"] == "optimization"
    assert "provider-secret" not in json.dumps(protected)
    forward = model_forwarder(protected)
    body = {"model": SHINKA_EMBEDDING_MODEL, "input": ["def f(): pass"]}
    tags = {USAGE_TAGS_HEADER: encode_tags({"caller": CALLER_PROPOSER, "kind": KIND_EMBEDDING})}
    with pytest.raises(ValueError, match="unknown metered model capability"):
        forward(route["token"], "/v1/embeddings", body, {})
    with pytest.raises(ValueError, match="unknown metered model capability"):
        forward(embedding["token"], "/v1/chat/completions", body, {})
    chat = httpx.post(
        f"{gateway.url}/chat/completions",
        headers={"Authorization": f"Bearer {embedding['token']}"},
        json={**body, "messages": [{"role": "user", "content": "hi"}]},
        trust_env=False,
    )
    assert chat.status_code == 422
    answer = httpx.post(
        f"{gateway.url}/embeddings",
        headers={"Authorization": f"Bearer {embedding['token']}", **tags},
        json=body,
        trust_env=False,
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["data"][0]["embedding"] == [0.1, 0.2]
    [(path, upstream)] = sent
    assert path == "/api/v1/embeddings"
    assert upstream["provider"]["only"] == ["openai"]
    snapshot = gateway.runtime.service.get(gateway.runtime.budget_id, gateway.runtime.username)
    assert snapshot.setup_spent_cents == Decimal("0.01")
    with Session(gateway.runtime.service._engine) as session:
        [operation] = session.scalars(select(ExecutionOperationModel)).all()
        [evidence] = session.scalars(select(ExecutionUsageEvidenceModel)).all()
    assert operation.state == "settled"
    assert operation.role == "optimization"
    assert evidence.evidence["tags"] == {"caller": CALLER_PROPOSER, "kind": KIND_EMBEDDING}


def test_readiness_probes_skip_the_novelty_embeddings_route(gateway: ModelGateway) -> None:
    """Probe only chat routes, so a novelty run's embeddings route cannot fail setup with a chat call.

    Args:
        gateway: Parent protocol over the funded fixture budget.
    """
    protected = gateway.protect_payload(
        {"shinka": {"novelty": True}, "reflection_model_config": {"name": "fixture/text"}},
        managed_key="provider-secret",
    )
    route = protected["reflection_model_config"]["extra"][ROUTE_KEY]
    assert [probe["token"] for probe in gateway.model_routes()] == [route["token"]]
    assert _verify_model_routes(gateway, native=False) == [
        {"key": "model.optimization", "status": "succeeded", "field": "optimization"}
    ]


def test_readiness_probes_skip_parent_only_routes(gateway: ModelGateway) -> None:
    """A route only the parent calls, such as a rule judge, is not probed as one of the run's model roles.

    Args:
        gateway: Parent protocol over the funded fixture budget.
    """
    guest = gateway.register(
        model="fixture/text", api_key="provider-secret", role="optimization", policy=ChargePolicy("managed_model")
    )
    gateway.register(
        model="fixture/text",
        api_key="provider-secret",
        role="judge",
        policy=ChargePolicy("managed_model"),
        parent_only=True,
    )

    assert [probe["token"] for probe in gateway.model_routes()] == [guest["token"]]


class _FlakyGet:
    """Fail a set number of budget-state reads before answering."""

    def __init__(self, failures: int) -> None:
        """Remember how many reads time out before one succeeds.

        Args:
            failures: Reads that raise a transport timeout first.
        """
        self.failures = failures
        self.calls = 0

    def __call__(self, url: str, **_: Any) -> httpx.Response:
        """Time out until the failures are used up, then report an open budget.

        Args:
            url: Requested budget-state endpoint.

        Returns:
            An open budget state.

        Raises:
            httpx.ReadTimeout: While failures remain.
        """
        self.calls += 1
        if self.calls <= self.failures:
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(200, json={"blocked_reason": None}, request=httpx.Request("GET", url))


def test_budget_check_survives_a_slow_relay_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """One slow relay reply after a finished run is retried, not treated as an interruption."""
    flaky = _FlakyGet(failures=2)
    monkeypatch.setattr(gateway_module.httpx, "get", flaky)
    monkeypatch.setattr(gateway_module.time, "sleep", lambda _: None)
    gateway_module.raise_gateway_stop({"url": "http://relay", "token": "t"})
    assert flaky.calls == 3


def test_budget_check_reports_an_interruption_once_retries_run_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """A relay that never answers still surfaces as an infrastructure interruption."""
    flaky = _FlakyGet(failures=10)
    monkeypatch.setattr(gateway_module.httpx, "get", flaky)
    monkeypatch.setattr(gateway_module.time, "sleep", lambda _: None)
    with pytest.raises(InfrastructureInterruptionError):
        gateway_module.raise_gateway_stop({"url": "http://relay", "token": "t"})
    assert flaky.calls == gateway_module._BUDGET_STATE_ATTEMPTS


def test_guest_call_the_limit_refuses_is_remembered(gateway: ModelGateway) -> None:
    """Keep the reason a guest model call was refused for budget, so setup can report the limit.

    Args:
        gateway: Funded real HTTP gateway fixture.
    """

    class _Route:
        """Refuse every call the way the ledger does when one hold exceeds the limit."""

        def dispatch(self, path: str, body: Any, **kwargs: Any) -> ModelHTTPResult:
            """Raise the ledger's refusal."""
            raise BudgetInsufficientError("The next operation needs up to 135 cents.")

    gateway._routes["fixture-token"] = _Route()  # type: ignore[assignment]
    assert gateway.budget_refusal is None

    try:
        with pytest.raises(BudgetInsufficientError):
            gateway.dispatch_guest("fixture-token", "/v1/chat/completions", {"model": "fixture/text"}, {})
    finally:
        del gateway._routes["fixture-token"]

    assert str(gateway.budget_refusal) == "The next operation needs up to 135 cents."


def test_a_role_observer_sees_each_answered_model_call_of_that_role_only(gateway: ModelGateway) -> None:
    """The rule watch counts the coding agent's model calls, over HTTP and the mailbox, and no other role's.

    Args:
        gateway: Funded real HTTP gateway fixture.
    """
    agent = gateway.register(
        model="fixture/text", api_key="upstream-secret", role="optimization", policy=ChargePolicy("managed_model")
    )
    task = gateway.register(
        model="fixture/text", api_key="upstream-secret", role="task", policy=ChargePolicy("managed_model")
    )
    seen: list[dict[str, Any]] = []

    def observe(body: dict[str, Any]) -> None:
        """Record the call, then fail, which must not reach the agent.

        Args:
            body: The model request.

        Raises:
            RuntimeError: Always.
        """
        seen.append(body)
        raise RuntimeError("observer failed")

    gateway.observe_role("optimization", observe)
    body = {"model": "fixture/text", "max_tokens": 16, "messages": [{"role": "user", "content": "step one"}]}
    response = httpx.post(
        f"{gateway.url}/chat/completions",
        headers={"Authorization": f"Bearer {agent['token']}"},
        json=body,
        trust_env=False,
    )
    mailbox = gateway.dispatch_guest(
        agent["token"], "/v1/chat/completions", {**body, "messages": [{"role": "user", "content": "step two"}]}, {}
    )
    httpx.post(
        f"{gateway.url}/chat/completions",
        headers={"Authorization": f"Bearer {task['token']}"},
        json=body,
        trust_env=False,
    )

    assert response.status_code == 200
    assert mailbox.status == 200
    assert [call["messages"][0]["content"] for call in seen] == ["step one", "step two"]
