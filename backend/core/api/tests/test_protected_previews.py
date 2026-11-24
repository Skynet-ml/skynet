"""Verify legacy previews retain protected execution, spending, and replay contracts."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session

from core.api import protected_preview
from core.api.routers.workflows import create_workflows_router
from core.billing.operation_pricing import ChargePolicy, operation_quote
from core.billing.runtime import PaidResult, UsagePendingError
from core.storage.models import BillingCustomerModel, ExecutionBudgetModel, ExecutionOperationModel, JobModel

from .test_protected_submissions import _Gateway, _Harness
from .test_protected_submissions import harness as _harness_fixture

harness = _harness_fixture


def _workflow_payload(runtime: str = "vercel") -> dict[str, Any]:
    """Return explicit workflow inputs without a metric, gold output, or optimization payload."""
    return {
        "workflow": {
            "nodes": [
                {"id": "in", "kind": "input", "fields": [{"name": "question"}]},
                {"id": "out", "kind": "output", "fields": [{"name": "question"}]},
            ],
            "edges": [{"source": "in", "source_port": "question", "target": "out", "target_port": "question"}],
        },
        "inputs": {"question": "explicit debug value"},
        "model_config": {"name": "fixture/model"},
        "execution_runtime": runtime,
    }


@pytest.fixture
def previews(harness: _Harness, monkeypatch: pytest.MonkeyPatch) -> tuple[_Harness, dict[str, Any]]:
    """Use real API, leases, and billing around deterministic physical preview adapters.

    Args:
        harness: Real disposable file-backed job store and public submission routes.
        monkeypatch: Substitute only physical gateway and managed execution transports.

    Returns:
        Funded API and independently observed provider/runtime calls.
    """
    state: dict[str, Any] = {"calls": [], "failed": False, "pending": False, "bound": []}

    class Gateway(_Gateway):
        """Keep close-time pending usage separate from completed debug output."""

        def close(self) -> None:
            """Report the fixture's genuinely retained usage hold after results exist."""
            if state["pending"]:
                raise UsagePendingError("Final sandbox usage is pending.")

    def charge(runtime, payload: dict[str, Any], kind: str) -> None:
        """Require actual reservation before returning the deterministic paid receipt."""
        policy = ChargePolicy("managed_model")
        quote = operation_quote({"kind": kind}, Decimal("0.02"), policy, {"provider": "fixture"})

        def dispatch() -> PaidResult[None]:
            """Verify physical provider dispatch is covered by the actual setup ledger."""
            budget = runtime.service.get(runtime.budget_id, runtime.username)
            assert budget.reserved_cents >= 2
            state["calls"].append({"payload": payload, "kind": kind})
            return PaidResult(None, Decimal("0.01"), {"actual_usd": "0.01"})

        runtime.execute(quote, policy, dispatch, operation_key="physical-model", cost_kind="model")
        if state["pending"]:
            sandbox_policy = ChargePolicy("sandbox")
            pending_quote = operation_quote({"box": kind}, Decimal("0.01"), sandbox_policy, {"provider": "fixture"})
            operation = runtime.reserve(pending_quote, operation_key="physical-box", cost_kind="sandbox")
            runtime.service.mark_dispatched(operation.id, runtime.username, "fixture-box")
            runtime.service.mark_pending(operation.id, runtime.username)

    def execute(payload: dict[str, Any], artifact_id: str, events, start_method: str) -> None:
        """Replace the external guest process and return its actual-shaped output frames."""
        assert payload["_preflight"]["kind"] == "workflow_preview"
        assert "metric_code" not in payload
        assert "dataset" not in payload
        charge(payload["_fixture_runtime"], payload, "workflow")
        if payload["_preflight"]["stream"]:
            events.put({"type": "preview_token", "field": "question", "chunk": payload["inputs"]["question"]})
        events.put(
            {
                "type": "preflight_result",
                "result": {
                    "workflow_result": {
                        "outputs": None if state["failed"] else payload["inputs"],
                        "node_traces": [
                            {
                                "node_id": "in",
                                "name": "Input",
                                "kind": "input",
                                "inputs": payload["inputs"],
                                "outputs": payload["inputs"],
                                "duration_ms": 1,
                            }
                        ],
                        "model_used": "fixture/model",
                        "error": "Node failed" if state["failed"] else None,
                        "failed_node_id": "in" if state["failed"] else None,
                        "usage_by_model": [{"model": "fixture/model", "input_tokens": 10, "output_tokens": 2}],
                    }
                },
            }
        )

    state["charge"] = charge
    monkeypatch.setattr(protected_preview, "ModelGateway", Gateway)
    monkeypatch.setattr(protected_preview, "run_vercel_dspy", execute)
    monkeypatch.setattr(
        protected_preview, "bind_protected_sandbox", lambda *args, **kwargs: state["bound"].append(kwargs)
    )
    harness.client.app.include_router(create_workflows_router(job_store=harness.store))
    return harness, state


def test_preview_creates_budget_and_replays_without_spending_again(previews) -> None:
    """Return real debug output and exactly attributed charges from the shared ledger."""
    harness, state = previews
    route = "/workflows/dry-run"
    payload = _workflow_payload()
    response = harness.client.post(route, json=payload, headers={"Idempotency-Key": "preview"})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["preview_status"] == "succeeded"
    assert result["cents_charged"] == 1
    assert result["budget"]["setup_spent_cents"] == "1"
    assert result["usage_by_model"][0]["input_tokens"] == 10
    assert harness.count(JobModel) == 0
    assert result["outputs"] == payload["inputs"]
    assert result["node_traces"][0]["inputs"] == payload["inputs"]
    replay = harness.client.post(route, json=payload, headers={"Idempotency-Key": "preview"})
    assert replay.status_code == 200, replay.text
    assert replay.json()["preflight_id"] == result["preflight_id"]
    assert len(state["calls"]) == harness.count(ExecutionBudgetModel) == harness.count(ExecutionOperationModel) == 1


def test_preview_uses_shared_budget_and_rejects_changed_idempotency_input(previews) -> None:
    """Preserve one approved total and prevent a replay key from authorizing different debug code."""
    harness, state = previews
    budget = harness.budgets.create("alice", 20, idempotency_key="shared")
    payload = {**_workflow_payload(), "execution_budget_id": budget.id, "execution_budget_revision": budget.revision}
    first = harness.client.post("/workflows/dry-run", json=payload, headers={"Idempotency-Key": "same"})
    assert first.status_code == 200, first.text
    second = harness.client.post("/workflows/dry-run", json=payload, headers={"Idempotency-Key": "another"})
    assert second.status_code == 200, second.text
    assert second.json()["budget"]["id"] == budget.id
    assert second.json()["budget"]["setup_spent_cents"] == "2"
    assert second.json()["cents_charged"] == 1
    changed = {**payload, "inputs": {"question": "different"}}
    refused = harness.client.post("/workflows/dry-run", json=changed, headers={"Idempotency-Key": "same"})
    assert refused.status_code == 409, refused.text
    assert len(state["calls"]) == 2


def test_preview_retains_completed_output_while_usage_is_pending(previews) -> None:
    """Keep actual outputs, observed model usage, and uncertain runtime coverage together."""
    harness, state = previews
    state["pending"] = True
    response = harness.client.post("/workflows/dry-run", json=_workflow_payload())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["preview_status"] == "pending"
    assert body["budget"]["reserved_cents"] == "1"
    assert body["budget"]["pending_operations"] == 1
    assert body["cents_charged"] == 1
    assert body["outputs"] == {"question": "explicit debug value"}


def test_failed_preview_replay_preserves_error_and_single_charge(previews) -> None:
    """Reuse failed debug results on the same transport key without a second paid attempt."""
    harness, state = previews
    state["failed"] = True
    route = "/workflows/dry-run"
    payload = _workflow_payload()
    first = harness.client.post(route, json=payload, headers={"Idempotency-Key": "failed"})
    second = harness.client.post(route, json=payload, headers={"Idempotency-Key": "failed"})
    assert first.status_code == second.status_code == 200
    assert first.json()["preview_status"] == "failed"
    assert first.json()["error"] == second.json()["error"]
    assert second.json()["cents_charged"] == 1
    assert len(state["calls"]) == 1


def test_stream_retains_tokens_final_traces_and_budget(previews) -> None:
    """Forward actual isolated token frames and a budget-aware final SSE response."""
    harness, state = previews
    response = harness.client.post("/workflows/dry-run/stream", json=_workflow_payload("vercel"))
    assert response.status_code == 200, response.text
    assert "event: token" in response.text
    assert "event: final" in response.text
    assert "explicit debug value" in response.text
    assert '"cents_charged": 1' in response.text
    assert '"node_traces"' in response.text
    assert len(state["calls"]) == len(state["bound"]) == 1


def test_unfunded_or_foreign_preview_never_dispatches(previews) -> None:
    """Apply real account funding and shared-budget ownership before the physical adapter."""
    harness, state = previews
    foreign = harness.budgets.create("bob", 20, idempotency_key="foreign")
    response = harness.client.post(
        "/workflows/dry-run",
        json={
            **_workflow_payload(),
            "execution_budget_id": foreign.id,
            "execution_budget_revision": foreign.revision,
        },
    )
    assert response.status_code == 404
    with Session(harness.store.engine) as session:
        session.get(BillingCustomerModel, "alice").balance_cents = 0
        session.commit()
    response = harness.client.post("/workflows/dry-run", json=_workflow_payload())
    assert response.status_code == 402
    assert not state["calls"]


def test_unaffordable_preview_is_rejected_before_physical_dispatch(previews) -> None:
    """Keep the wallet untouched when the real operation bound cannot fit the balance.

    A preview is setup work, so the wallet bounds it, not the run's limit (SKY-925).
    The 1-cent balance cannot hold the 2-cent operation bound.
    """
    harness, state = previews
    budget = harness.budgets.create("alice", 50, idempotency_key="too-small")
    with Session(harness.store.engine) as session:
        session.get(BillingCustomerModel, "alice").balance_cents = 1
        session.commit()
    response = harness.client.post(
        "/workflows/dry-run",
        json={
            **_workflow_payload(),
            "execution_budget_id": budget.id,
            "execution_budget_revision": budget.revision,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["preview_status"] == "failed"
    assert response.json()["cents_charged"] == 0
    assert not state["calls"]
    assert harness.count(ExecutionOperationModel) == 0


def test_pending_preview_replay_does_not_open_another_runtime(previews) -> None:
    """Keep unknown usage covered and return its existing actual output on repeated requests."""
    harness, state = previews
    state["pending"] = True
    headers = {"Idempotency-Key": "pending-preview"}
    first = harness.client.post("/workflows/dry-run", json=_workflow_payload(), headers=headers)
    replay = harness.client.post("/workflows/dry-run", json=_workflow_payload(), headers=headers)
    assert first.status_code == replay.status_code == 200
    assert replay.json()["preflight_id"] == first.json()["preflight_id"]
    assert replay.json()["outputs"] == {"question": "explicit debug value"}
    assert replay.json()["budget"]["reserved_cents"] == "1"
    assert len(state["calls"]) == len(state["bound"]) == 1
