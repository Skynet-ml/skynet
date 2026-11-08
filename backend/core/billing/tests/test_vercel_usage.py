"""Verify protected Vercel admission and stopped-session metering without paid calls."""

from __future__ import annotations

import ast
import json
import time
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session
from vercel.sandbox import SandboxApiError

from core.billing import vercel_usage
from core.billing.budget_amounts import cent_units
from core.billing.budgets import BudgetInsufficientError, BudgetService
from core.billing.operation_pricing import UnpricedOperationError
from core.billing.runtime import BudgetRuntime, UsagePendingError
from core.billing.vercel_usage import (
    PROPOSER_REGISTRY_HOSTS,
    SANDBOX_NETWORK_BYTES_CAP,
    proposer_hosts,
    quote_vercel_sandbox,
    vercel_actual_usd,
    vercel_sandbox_cost_range,
)
from core.config import settings
from core.service_gateway.optimization.blackbox import sandbox as sandbox_module
from core.service_gateway.optimization.blackbox.sandbox import SandboxSpec, VercelCredentials, VercelSandboxRuntime
from core.storage.models import Base, BillingCustomerModel, ExecutionOperationModel, ExecutionUsageEvidenceModel

IMAGE = "vcr.example/skynet/optimizer@sha256:" + "a" * 64
CREATE = {
    "image": IMAGE,
    "lifetime_ms": 120_000,
    "vcpus": 2,
    "network_disabled": True,
    "ports": [],
    "persistent": False,
}
NETWORKED = {
    **CREATE,
    "network_disabled": False,
    "allowed_hosts": ["api.anthropic.com"],
    "network_bytes_cap": SANDBOX_NETWORK_BYTES_CAP,
}
RECEIPT = {
    "id": "session-one",
    "sourceSandboxName": "sandbox-one",
    "status": "stopped",
    "region": "iad1",
    "vcpus": 2,
    "memory": 4096,
    "timeout": 120_000,
    "cwd": "/vercel/sandbox",
    "requestedAt": 1_000,
    "startedAt": 2_000,
    "stoppedAt": 63_000,
    "activeCpuDurationMs": 5_000,
    "networkTransfer": {"ingress": 0, "egress": 0},
}


@pytest.fixture(autouse=True)
def _at_cost_markup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the usage markup to 1.0 so these tests check settlement arithmetic, not pricing policy."""
    monkeypatch.setattr(settings, "usage_markup", 1.0)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Engine]:
    """Create a private funded wallet and ledger for runtime integration tests."""
    engine = create_engine(f"sqlite:///{tmp_path / 'vercel.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username="alice", stripe_customer_id="fixture", balance_cents=100, grant_remaining=0)
        )
        session.commit()
    yield engine
    engine.dispose()


def _runtime(database: Engine, total: int = 50) -> BudgetRuntime:
    """Bind real admission to a local database, leaving provider I/O mocked."""
    service = BudgetService(engine=database)
    budget = service.create("alice", total, idempotency_key="draft")
    return BudgetRuntime(service, username="alice", budget_id=budget.id, generation=0, phase="setup", wait_timeout=0)


def test_quote_covers_all_regions_and_settles_cpu_separately_from_wall_time() -> None:
    """Cover maximum active CPU, charge actual CPU, and round only memory duration."""
    quote = quote_vercel_sandbox(CREATE)
    minimum_cents, maximum_cents = vercel_sandbox_cost_range(CREATE)
    maximum_usd = (
        Decimal("0.0000006")
        + Decimal(240_000) * Decimal("0.177") / 3_600_000
        + Decimal(480_000) * Decimal("0.0294") / 3_600_000
    )
    assert quote.maximum.wallet == quote.maximum.total == maximum_usd * 100
    assert minimum_cents == Decimal("0.141393334")
    assert maximum_cents == quote.maximum.total
    actual = vercel_actual_usd(RECEIPT, session_id="session-one", vcpus=2)
    expected = (
        Decimal("0.0000006")
        + Decimal(5_000) * Decimal("0.128") / 3_600_000
        + Decimal(480_000) * Decimal("0.0212") / 3_600_000
    )
    assert actual == expected
    assert actual < maximum_usd
    assert quote.price_snapshot["policy"]["kind"] == "sandbox"


@pytest.mark.parametrize(
    "change",
    [
        {"image": "mutable:latest"},
        {"network_disabled": False},
        {"ports": [8080]},
        {"persistent": True},
        {"lifetime_ms": 0},
        {"lifetime_ms": 86_400_001},
        {"vcpus": 3},
    ],
)
def test_unbounded_creation_is_not_quoted(change: dict[str, Any]) -> None:
    """Reject dependencies, network paths, or resource lifetimes without enforceable bounds."""
    with pytest.raises(UnpricedOperationError):
        quote_vercel_sandbox({**CREATE, **change})


@pytest.mark.parametrize(
    "change",
    [
        {"status": "stopping"},
        {"activeCpuDurationMs": None},
        {"activeCpuDurationMs": True},
        {"networkTransfer": {"ingress": 30, "egress": 0}},
        {"networkTransfer": {"ingress": 0, "egress": 5}},
        {"region": "future-region"},
        {"id": "resumed-session"},
        {"memory": 8192},
    ],
)
def test_unconfirmed_usage_is_not_fabricated_or_misclassified(change: dict[str, Any]) -> None:
    """Leave absent metrics and ambiguous transfer pending instead of charging estimates."""
    with pytest.raises(UsagePendingError) as pending:
        vercel_actual_usd({**RECEIPT, **change}, session_id="session-one", vcpus=2)
    assert "Vercel" not in str(pending.value)


def test_pending_usage_messages_never_name_the_sandbox_vendor() -> None:
    """Keep the vendor name out of every pending-usage message, because failed runs show it to users."""
    tree = ast.parse(Path(vercel_usage.__file__).read_text())
    messages = [
        "".join(part.value for part in ast.walk(node.args[0]) if isinstance(part, ast.Constant))
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "UsagePendingError"
        and node.args
    ]

    assert len(messages) >= 15
    assert [message for message in messages if "vercel" in message.lower()] == []


def test_control_plane_transfer_settles_only_under_an_offline_admission() -> None:
    """Exclude a deny-all sandbox's control-plane bytes from the charge, never traffic an admission allowed."""
    snapshot = quote_vercel_sandbox(CREATE).price_snapshot
    receipt = {**RECEIPT, "networkTransfer": {"ingress": 21_124, "egress": 7_548}}
    assert vercel_actual_usd(receipt, session_id="session-one", vcpus=2, price_snapshot=snapshot) == vercel_actual_usd(
        RECEIPT, session_id="session-one", vcpus=2, price_snapshot=snapshot
    )
    connected = {**snapshot, "request": {**CREATE, "network_disabled": False}}
    with pytest.raises(UsagePendingError, match="classification"):
        vercel_actual_usd(receipt, session_id="session-one", vcpus=2, price_snapshot=connected)


def test_allowlisted_box_is_funded_for_its_transfer_cap_and_charged_per_byte() -> None:
    """Fund a networked box for its whole byte cap, then charge every byte it moved."""
    offline = quote_vercel_sandbox(CREATE)
    quote = quote_vercel_sandbox(NETWORKED)
    assert quote.maximum.total - offline.maximum.total == Decimal(SANDBOX_NETWORK_BYTES_CAP) * Decimal("0.15") / 10**7
    assert quote.price_snapshot["maximum_billable_network_bytes"] == SANDBOX_NETWORK_BYTES_CAP
    assert offline.price_snapshot["maximum_billable_network_bytes"] == 0
    assert offline.price_snapshot["network_basis"].startswith("deny-all")
    receipt = {**RECEIPT, "networkTransfer": {"ingress": 600_000_000, "egress": 400_000_000}}
    moved = vercel_actual_usd(receipt, session_id="session-one", vcpus=2, price_snapshot=quote.price_snapshot)
    idle = vercel_actual_usd(RECEIPT, session_id="session-one", vcpus=2, price_snapshot=quote.price_snapshot)
    assert moved - idle == Decimal("0.15")


def test_transfer_beyond_the_admitted_cap_stays_pending() -> None:
    """Never settle a networked box that moved more than its admission funded."""
    snapshot = quote_vercel_sandbox(NETWORKED).price_snapshot
    receipt = {**RECEIPT, "networkTransfer": {"ingress": SANDBOX_NETWORK_BYTES_CAP, "egress": 1}}
    with pytest.raises(UsagePendingError, match="allowance"):
        vercel_actual_usd(receipt, session_id="session-one", vcpus=2, price_snapshot=snapshot)


@pytest.mark.parametrize(
    "change",
    [
        {"allowed_hosts": []},
        {"allowed_hosts": ["*"]},
        {"allowed_hosts": ["b.example", "a.example"]},
        {"network_bytes_cap": None},
        {"network_bytes_cap": SANDBOX_NETWORK_BYTES_CAP + 1},
        {"network_disabled": True},
    ],
)
def test_unbounded_allowlist_is_not_quoted(change: dict[str, Any]) -> None:
    """Refuse an open, unsorted, or uncapped allowlist, and an allowlist on a deny-all box."""
    with pytest.raises(UnpricedOperationError):
        quote_vercel_sandbox({**NETWORKED, **change})


def _mock_provider(
    monkeypatch: pytest.MonkeyPatch,
    runtime: BudgetRuntime,
    *,
    receipt: dict[str, Any] | None = None,
    fail_create: bool = False,
    reject_create: bool = False,
    network_policy: dict[str, Any] | None = None,
    overshoot_ms: int = 0,
) -> list[httpx.Request]:
    """Install a real Python SDK transport with deterministic Vercel API responses.

    Args:
        monkeypatch: Local patch lifetime.
        runtime: Ledger whose pre-dispatch hold is asserted by the provider.
        receipt: Optional stopped-session metadata override.
        fail_create: Simulate a network failure after creation may have been accepted.
        reject_create: Answer the first create call with the 400 Vercel returns for a lifetime above its ceiling.
        network_policy: The create body's expected network policy, deny-all when omitted.
        overshoot_ms: Extra lifetime Vercel reports beyond each requested extension.

    Returns:
        Captured provider requests for replay and lifecycle assertions.
    """
    requests: list[httpx.Request] = []
    sandbox = {"name": "sandbox-one", "currentSessionId": "session-one", "persistent": False}
    final = {**RECEIPT, **(receipt or {})}
    limit = {"timeout": 0}

    def provider(request: httpx.Request) -> httpx.Response:
        """Require durable coverage before returning realistic provider metadata."""
        requests.append(request)
        if request.method == "POST" and request.url.path.endswith("/v3/sandboxes"):
            assert runtime.service.get(runtime.budget_id, "alice").reserved_cents > 0
            body = json.loads(request.content)
            limit["timeout"] = body["timeout"]
            assert body["persistent"] is False
            assert body["ports"] == []
            assert body["networkPolicy"] == (network_policy or {"mode": "deny-all"})
            assert body["resources"] == {"vcpus": 2, "memory": 4096}
            first_creation = not any(earlier.url.path.endswith("/v3/sandboxes") for earlier in requests[:-1])
            if reject_create and first_creation:
                return httpx.Response(
                    400,
                    json={
                        "error": {"code": "bad_request", "message": "Invalid request: `timeout` should be <= 86400000."}
                    },
                )
            if fail_create:
                raise httpx.ReadError("creation response lost", request=request)
            active = {
                key: value
                for key, value in RECEIPT.items()
                if key not in {"stoppedAt", "activeCpuDurationMs", "networkTransfer"}
            }
            active["status"] = "running"
            active["timeout"] = limit["timeout"]
            return httpx.Response(
                200, json={"sandbox": {**sandbox, "status": "running"}, "session": active, "routes": []}
            )
        if request.method == "POST" and request.url.path.endswith("/session-one/extend-timeout"):
            limit["timeout"] += json.loads(request.content)["duration"] + overshoot_ms
            funded = quote_vercel_sandbox({**CREATE, "lifetime_ms": limit["timeout"] - overshoot_ms})
            with Session(runtime.service._engine) as session:
                operation = session.scalar(select(ExecutionOperationModel))
                assert operation.max_units >= cent_units(funded.maximum.total)
            active = {key: value for key, value in RECEIPT.items() if key not in {"stoppedAt", "activeCpuDurationMs"}}
            return httpx.Response(
                200,
                json={
                    "sandbox": {**sandbox, "status": "running"},
                    "session": {**active, "status": "running", "timeout": limit["timeout"]},
                },
            )
        if request.method == "POST" and request.url.path.endswith("/session-one/stop"):
            return httpx.Response(200, json={"sandbox": {**sandbox, "status": "stopped"}, "session": final})
        if request.method == "DELETE":
            return httpx.Response(
                200, json={"sandbox": {**sandbox, "status": "stopped"}, "session": final, "routes": []}
            )
        raise AssertionError(f"Unexpected provider operation: {request.method} {request.url.path}")

    class FixtureClient(httpx.Client):
        """Preserve the SDK's concrete client type validation with a local transport."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            """Keep response hooks and SDK serialization while replacing network I/O."""
            super().__init__(*args, transport=httpx.MockTransport(provider), **kwargs)

    monkeypatch.setattr(sandbox_module.httpx, "Client", FixtureClient)
    return requests


def _sandbox_runtime(runtime: BudgetRuntime) -> VercelSandboxRuntime:
    """Create a protected runtime with fixed non-secret test credentials."""
    return VercelSandboxRuntime(
        VercelCredentials(token="fixture", team_id="team", project_id="project"), image=IMAGE, budget=runtime
    )


def _spec(lifetime_seconds: int = 120, key: str = "evaluation-one") -> SandboxSpec:
    """Use an offline sandbox identity stable across delivery retries."""
    return SandboxSpec(lifetime_seconds=lifetime_seconds, name="sandbox-one", network_disabled=True, operation_key=key)


def test_real_sdk_stop_metrics_are_preserved_and_settled_once(
    database: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Meter raw fields discarded by the installed SDK and debit without model markup."""
    runtime = _runtime(database)
    requests = _mock_provider(monkeypatch, runtime)
    sandbox = _sandbox_runtime(runtime).open(_spec())
    assert list(runtime._pinned.values()) == [cent_units(quote_vercel_sandbox(CREATE).maximum.total)]
    sandbox.close()
    sandbox.close()
    assert runtime._pinned == {}
    assert [request.method for request in requests] == ["POST", "POST", "DELETE"]
    snapshot = runtime.service.get(runtime.budget_id, "alice")
    assert snapshot.reserved_cents == 0
    assert snapshot.billed_cents == 1
    assert snapshot.setup_spent_cents == Decimal("0.300504445")
    with Session(database) as session:
        evidence = session.scalar(
            select(ExecutionUsageEvidenceModel).where(ExecutionUsageEvidenceModel.final.is_(True))
        )
        assert evidence.evidence["session"]["activeCpuDurationMs"] == 5_000
        assert evidence.evidence["session"]["networkTransfer"] == {"ingress": 0, "egress": 0}


def test_control_plane_transfer_is_recorded_and_settled(database: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Settle a deny-all sandbox at CPU and memory cost while keeping its reported transfer as evidence."""
    runtime = _runtime(database)
    requests = _mock_provider(monkeypatch, runtime, receipt={"networkTransfer": {"ingress": 42, "egress": 1}})
    sandbox = _sandbox_runtime(runtime).open(_spec())
    sandbox.close()
    assert requests[-1].method == "DELETE"
    snapshot = runtime.service.get(runtime.budget_id, "alice")
    assert snapshot.reserved_cents == 0
    assert snapshot.billed_cents == 1
    assert snapshot.pending_operations == 0
    assert snapshot.setup_spent_cents == Decimal("0.300504445")
    with Session(database) as session:
        evidence = session.scalar(
            select(ExecutionUsageEvidenceModel).where(ExecutionUsageEvidenceModel.final.is_(True))
        )
        assert evidence.evidence["session"]["networkTransfer"] == {"ingress": 42, "egress": 1}


def test_allowlisted_box_reaches_only_its_host_with_the_key_added_at_the_edge(
    database: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Send Vercel an allowlist-only policy and keep the injected key out of the funded request."""
    runtime = _runtime(database)
    requests = _mock_provider(
        monkeypatch,
        runtime,
        network_policy={
            "allow": {
                "api.anthropic.com": [{"transform": [{"headers": {"x-api-key": "sk-ant-secret"}}]}],
            }
        },
    )
    spec = SandboxSpec(
        lifetime_seconds=120,
        name="sandbox-one",
        operation_key="evaluation-one",
        allowed_hosts=("api.anthropic.com",),
        inject_headers={"api.anthropic.com": {"x-api-key": "sk-ant-secret"}},
    )
    _sandbox_runtime(runtime).open(spec).close()
    assert requests[-1].method == "DELETE"
    with Session(database) as session:
        operation = session.scalar(select(ExecutionOperationModel))
        assert "sk-ant-secret" not in json.dumps(operation.price_snapshot, default=str)
        assert operation.price_snapshot["request"]["allowed_hosts"] == ["api.anthropic.com"]


def test_lost_creation_response_is_pending_and_never_repeated(
    database: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retain an uncertain creation hold and block another physical dispatch on replay."""
    runtime = _runtime(database)
    requests = _mock_provider(monkeypatch, runtime, fail_create=True)
    sandbox_runtime = _sandbox_runtime(runtime)
    with pytest.raises(httpx.ReadError):
        sandbox_runtime.open(_spec())
    with pytest.raises(UsagePendingError, match="already dispatched"):
        sandbox_runtime.open(_spec())
    assert len(runtime._pinned) == 1
    assert len(requests) == 1
    assert runtime.service.get(runtime.budget_id, "alice").pending_operations == 1


def test_refused_creation_releases_the_hold_and_admits_the_next_attempt(
    database: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Give back coverage the provider refused outright and record the refusal as final evidence."""
    runtime = _runtime(database)
    requests = _mock_provider(monkeypatch, runtime, reject_create=True)
    sandbox_runtime = _sandbox_runtime(runtime)
    with pytest.raises(SandboxApiError, match="86400000"):
        sandbox_runtime.open(_spec())
    assert runtime._pinned == {}
    assert [request.method for request in requests] == ["POST"]
    snapshot = runtime.service.get(runtime.budget_id, "alice")
    assert snapshot.pending_operations == 0
    assert snapshot.reserved_cents == 0
    assert snapshot.billed_cents == 0
    with Session(database) as session:
        assert session.scalar(select(ExecutionOperationModel)).state == "released"
        evidence = session.scalar(select(ExecutionUsageEvidenceModel))
        assert evidence.issue == "rejected"
        assert evidence.final is True
        assert evidence.billed_cents == 0
        assert evidence.evidence["status_code"] == 400
    sandbox_runtime.open(_spec(key="evaluation-two")).close()
    assert sum(request.url.path.endswith("/v3/sandboxes") for request in requests) == 2
    snapshot = runtime.service.get(runtime.budget_id, "alice")
    assert snapshot.pending_operations == 0
    assert snapshot.reserved_cents == 0
    assert snapshot.billed_cents == 1


def test_insufficient_sandbox_coverage_never_calls_provider(database: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require the full hard compute bound before entering the provider SDK."""
    runtime = _runtime(database, total=1)
    requests = _mock_provider(monkeypatch, runtime)
    with pytest.raises(BudgetInsufficientError):
        _sandbox_runtime(runtime).open(_spec(lifetime_seconds=3600))
    assert requests == []
    with Session(database) as session:
        assert session.scalar(select(ExecutionOperationModel)) is None


_SHORT = {"stoppedAt": 2_900, "activeCpuDurationMs": 500, "timeout": 1_000}


def _slices(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink coverage slices to one second so renewals run within a test."""
    monkeypatch.setattr(sandbox_module, "COVERAGE_SLICE_SECONDS", 1)
    monkeypatch.setattr(sandbox_module, "_RENEWAL_LEAD_SECONDS", 0.9)
    monkeypatch.setattr(sandbox_module, "_RENEWAL_MARGIN_SECONDS", 0.0)


def _extensions(requests: list[httpx.Request]) -> list[int]:
    """Return the durations of the extensions Vercel was asked for."""
    return [json.loads(r.content)["duration"] for r in requests if r.url.path.endswith("/extend-timeout")]


def _wait_for(condition: Any) -> None:
    """Poll until ``condition()`` holds or two seconds pass."""
    for _ in range(200):
        if condition():
            return
        time.sleep(0.01)


def test_protected_box_is_funded_one_slice_at_a_time_up_to_its_lifetime(
    database: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Open for the first slice, fund each next slice before Vercel extends, and stop at the ceiling."""
    _slices(monkeypatch)
    runtime = _runtime(database)
    requests = _mock_provider(monkeypatch, runtime, receipt={**_SHORT, "stoppedAt": 4_500, "timeout": 3_000})
    sandbox = _sandbox_runtime(runtime).open(_spec(lifetime_seconds=3))
    assert json.loads(requests[0].content)["timeout"] == 1_000
    first = runtime.service.get(runtime.budget_id, "alice").reserved_cents
    _wait_for(lambda: len(_extensions(requests)) == 2)
    time.sleep(0.2)
    assert _extensions(requests) == [1_000, 1_000]
    assert runtime.service.get(runtime.budget_id, "alice").reserved_cents > first
    assert list(runtime._pinned.values()) == [
        cent_units(runtime.service.get(runtime.budget_id, "alice").reserved_cents)
    ]
    sandbox.close()
    snapshot = runtime.service.get(runtime.budget_id, "alice")
    assert snapshot.reserved_cents == 0
    assert snapshot.pending_operations == 0


def test_unfundable_slice_is_never_requested_from_vercel(database: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Leave the box to end on its funded time when the budget cannot pay for the next slice."""
    _slices(monkeypatch)
    runtime = _runtime(database)
    requests = _mock_provider(monkeypatch, runtime, receipt=_SHORT)
    attempts: list[str] = []

    def refuse(*args: Any, **kwargs: Any) -> Any:
        """Refuse every extension as the ledger does when funds run out."""
        attempts.append(kwargs["evidence_key"])
        raise BudgetInsufficientError("no room")

    monkeypatch.setattr(runtime.service, "extend_coverage", refuse)
    sandbox = _sandbox_runtime(runtime).open(_spec(lifetime_seconds=3))
    _wait_for(lambda: attempts)
    time.sleep(0.2)
    assert attempts == ["vercel-extend:2000"]
    assert _extensions(requests) == []
    sandbox.close()


def test_extension_past_funded_time_ends_the_box(database: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop the box at once when Vercel reports a limit beyond the coverage the ledger holds."""
    _slices(monkeypatch)
    runtime = _runtime(database)
    requests = _mock_provider(monkeypatch, runtime, receipt=_SHORT, overshoot_ms=60_000)
    sandbox = _sandbox_runtime(runtime).open(_spec(lifetime_seconds=3))
    _wait_for(lambda: any(r.url.path.endswith("/session-one/stop") for r in requests))
    assert _extensions(requests) == [1_000]
    assert any(r.url.path.endswith("/session-one/stop") for r in requests)
    sandbox.close()


def test_a_proposer_with_no_network_key_reaches_only_the_registries() -> None:
    """Give a proposer the package registries by default, and nothing else (E57)."""
    assert proposer_hosts(None) == PROPOSER_REGISTRY_HOSTS
    assert proposer_hosts(None, "https://pypi.org/simple") == PROPOSER_REGISTRY_HOSTS
    assert "raw.githubusercontent.com" not in proposer_hosts(None)


def test_a_proposer_network_list_adds_hosts_and_an_empty_list_stays_offline() -> None:
    """Add listed hosts to the registries, and treat ``network = []`` as relay only."""
    assert proposer_hosts(()) == ()
    assert proposer_hosts(("docs.python.org",)) == tuple(sorted((*PROPOSER_REGISTRY_HOSTS, "docs.python.org")))


def test_a_custom_index_replaces_the_pypi_hosts() -> None:
    """Reach a saved custom index instead of pypi.org and its file host."""
    assert proposer_hosts(None, "https://pkgs.example.com/simple") == ("pkgs.example.com", "registry.npmjs.org")
