"""Verify caller-funded completed-run interactions against the durable ledger."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest

from core.api import protected_interaction
from core.api.auth import AuthenticatedUser
from core.api.errors import DomainError
from core.api.routers import serve as serve_router
from core.api.routers.execution_budgets import budget_http_error
from core.billing.budgets import (
    BudgetBoundExceededError,
    BudgetConflictError,
    BudgetError,
    BudgetInFlightError,
    BudgetInsufficientError,
    BudgetNotFoundError,
)
from core.billing.operation_pricing import ChargePolicy, operation_quote
from core.billing.runtime import PaidResult
from core.i18n_en import t_en
from core.storage.models import ExecutionBudgetModel, ExecutionOperationModel, JobModel

from .test_protected_submissions import _Gateway, _Harness
from .test_protected_submissions import harness as _harness_fixture

harness = _harness_fixture


@pytest.fixture
def interactions(harness: _Harness, monkeypatch: pytest.MonkeyPatch) -> tuple[_Harness, dict[str, Any]]:
    """Replace physical transports while retaining real budgets and settlements.

    Args:
        harness: Funded API fixture backed by a disposable database.
        monkeypatch: Pytest mutation helper.

    Returns:
        Real ledger harness and captured sandbox dispatch state.
    """
    state: dict[str, Any] = {"calls": [], "bindings": []}

    def execute(payload: dict[str, Any], identity: str, events: Any, start_method: str) -> None:
        """Settle one measured model operation and return a guest-shaped result.

        Args:
            payload: Protected payload carrying the fixture runtime.
            identity: Physical interaction identity.
            events: Trusted parent event collector.
            start_method: Guest runner start method.
        """
        runtime = payload["_fixture_runtime"]
        policy = ChargePolicy("managed_model")
        quote = operation_quote({"interaction": identity}, Decimal("0.02"), policy, {"provider": "fixture"})

        def dispatch() -> PaidResult[None]:
            """Record the call after the wallet and request budget accepted it."""
            snapshot = runtime.service.get(runtime.budget_id, runtime.username)
            assert snapshot.reserved_cents == 2
            state["calls"].append(
                {
                    "identity": identity,
                    "payload": payload,
                    "start_method": start_method,
                    "username": runtime.username,
                }
            )
            return PaidResult(
                None,
                Decimal("0.01"),
                {"provider": "fixture", "actual_usd": "0.01"},
                provider_request_id="fixture-interaction",
            )

        runtime.execute(
            quote,
            policy,
            dispatch,
            operation_key="physical-model",
            cost_kind="model",
        )
        events.put(
            {
                "type": "interaction_result",
                "result": {
                    "optimization_id": "finished-run",
                    "outputs": {"answer": "sandboxed"},
                    "input_fields": ["question"],
                    "output_fields": ["answer"],
                    "model_used": "fixture/model",
                },
            }
        )

    monkeypatch.setattr(protected_interaction, "ModelGateway", _Gateway)
    monkeypatch.setattr(protected_interaction, "run_vercel_dspy", execute)
    monkeypatch.setattr(
        protected_interaction,
        "bind_protected_sandbox",
        lambda *_args, **kwargs: state["bindings"].append(kwargs),
    )
    return harness, state


def _payload() -> dict[str, Any]:
    """Build a minimal completed-program interaction payload."""
    return {
        "model_config": {"name": "fixture/model"},
        "token_source": "managed",
        "program_artifact": {"program_state_json": {}},
        "payload_overview": {},
        "inputs": {"question": "hello"},
        "_interaction": {
            "kind": "serve",
            "optimization_id": "finished-run",
            "input_fields": ["question"],
            "output_fields": ["answer"],
            "stream": False,
        },
    }


def test_interaction_closes_caller_budget_and_replays_without_a_second_charge(
    interactions: tuple[_Harness, dict[str, Any]],
) -> None:
    """Reserve, settle, close, and replay exactly one caller-owned request."""
    harness, state = interactions
    first = protected_interaction.run_protected_interaction(
        _payload(),
        kind="serve",
        max_cost_cents=10,
        idempotency_key="one-call",
        user=AuthenticatedUser("alice", "user", ()),
        job_store=harness.store,
    )
    replay = protected_interaction.run_protected_interaction(
        _payload(),
        kind="serve",
        max_cost_cents=10,
        idempotency_key="one-call",
        user=AuthenticatedUser("alice", "user", ()),
        job_store=harness.store,
    )

    assert first["outputs"] == {"answer": "sandboxed"}
    assert first["cents_charged"] == "1"
    assert first["budget"]["total_cents"] == 10
    assert first["budget"]["state"] == "closed"
    assert first["budget"]["reserved_cents"] == "0"
    assert replay["interaction_id"] == first["interaction_id"]
    assert len(state["calls"]) == len(state["bindings"]) == 1
    assert state["calls"][0]["username"] == "alice"
    assert state["bindings"][0]["lifetime_seconds"] == 300
    assert harness.count(ExecutionBudgetModel) == 1
    assert harness.count(ExecutionOperationModel) == 1
    assert harness.count(JobModel) == 0


def test_interaction_never_allows_a_batched_model_route(
    interactions: tuple[_Harness, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forbid batches for a completed-run call even when its reflection model asks for economy."""
    harness, _state = interactions
    options: list[dict[str, Any]] = []

    class _RecordingGateway(_Gateway):
        """Record the protection options the interaction path sends."""

        def protect_payload(self, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            """Record the options, then protect like the fixture gateway."""
            options.append(kwargs)
            return super().protect_payload(payload, **kwargs)

    monkeypatch.setattr(protected_interaction, "ModelGateway", _RecordingGateway)
    payload = _payload()
    payload["reflection_model_config"] = {"name": "fixture/model", "extra": {"economy_mode": True}}
    protected_interaction.run_protected_interaction(
        payload,
        kind="serve",
        max_cost_cents=10,
        idempotency_key="economy-call",
        user=AuthenticatedUser("alice", "user", ()),
        job_store=harness.store,
    )

    assert [option.get("allow_batch") for option in options] == [False]


def test_interaction_without_a_caller_maximum_draws_on_the_account(
    interactions: tuple[_Harness, dict[str, Any]],
) -> None:
    """Run and replay one request whose only limit is the account balance."""
    harness, state = interactions
    authority = {
        "kind": "serve",
        "max_cost_cents": None,
        "idempotency_key": "uncapped-call",
        "user": AuthenticatedUser("alice", "user", ()),
        "job_store": harness.store,
    }
    first = protected_interaction.run_protected_interaction(_payload(), **authority)
    replay = protected_interaction.run_protected_interaction(_payload(), **authority)

    assert first["outputs"] == {"answer": "sandboxed"}
    assert first["cents_charged"] == "1"
    assert first["budget"]["uncapped"] is True
    assert first["budget"]["state"] == "closed"
    assert replay["interaction_id"] == first["interaction_id"]
    assert len(state["calls"]) == 1
    assert harness.count(ExecutionBudgetModel) == 1


def test_same_transport_key_cannot_change_the_approved_ceiling(
    interactions: tuple[_Harness, dict[str, Any]],
) -> None:
    """Reject a replay that tries to increase the caller's approved maximum."""
    harness, state = interactions
    authority = {
        "kind": "serve",
        "idempotency_key": "fixed-call",
        "user": AuthenticatedUser("alice", "user", ()),
        "job_store": harness.store,
    }
    protected_interaction.run_protected_interaction(_payload(), max_cost_cents=10, **authority)

    with pytest.raises(DomainError) as caught:
        protected_interaction.run_protected_interaction(_payload(), max_cost_cents=11, **authority)

    assert caught.value.code == "budget.conflict"
    assert len(state["calls"]) == 1


def test_same_transport_key_cannot_replay_a_different_request(
    interactions: tuple[_Harness, dict[str, Any]],
) -> None:
    """Reject a completed replay whose inputs differ under the same key."""
    harness, state = interactions
    authority = {
        "kind": "serve",
        "max_cost_cents": 10,
        "idempotency_key": "fixed-payload",
        "user": AuthenticatedUser("alice", "user", ()),
        "job_store": harness.store,
    }
    protected_interaction.run_protected_interaction(_payload(), **authority)
    changed = _payload()
    changed["inputs"] = {"question": "different"}

    with pytest.raises(DomainError) as caught:
        protected_interaction.run_protected_interaction(changed, **authority)

    assert caught.value.code == "budget.conflict"
    assert len(state["calls"]) == 1


def test_shared_caller_cannot_use_legacy_owner_tool_secret(
    interactions: tuple[_Harness, dict[str, Any]],
) -> None:
    """Reject legacy inline MCP credentials before shared sandbox dispatch."""
    harness, state = interactions
    payload = _payload()
    payload["tool_source"] = {
        "kind": "live_mcp",
        "mcp_url": "https://tools.example/mcp?access_token=owner-secret",
    }

    with pytest.raises(DomainError) as caught:
        protected_interaction._resolve_parent_payload(
            payload,
            user=AuthenticatedUser("bob", "user", ()),
            engine=harness.store.engine,
            credential_owner="alice",
            credential_binding_id=None,
        )

    assert caught.value.code == "serve.caller_tool_connection_required"
    assert state["calls"] == []


def test_hosted_interaction_refuses_an_imported_subscription_run_before_any_charge(
    interactions: tuple[_Harness, dict[str, Any]],
) -> None:
    """Give a clear 409 for a local subscription model and open no budget or sandbox."""
    harness, state = interactions
    payload = _payload()
    payload["model_config"] = {"name": "claude/sonnet", "token_source": "subscription"}
    payload["token_source"] = "subscription"

    with pytest.raises(DomainError) as caught:
        protected_interaction.run_protected_interaction(
            payload,
            kind="serve",
            max_cost_cents=10,
            idempotency_key="imported-subscription",
            user=AuthenticatedUser("alice", "user", ()),
            job_store=harness.store,
        )

    assert caught.value.code == "serve.subscription_model_hosted"
    assert caught.value.status_code == 409
    assert state["calls"] == state["bindings"] == []
    assert harness.count(ExecutionBudgetModel) == 0
    assert harness.count(ExecutionOperationModel) == 0


def test_stream_error_event_carries_the_translatable_code() -> None:
    """Give streamed serve and chat errors the same code envelope as HTTP errors."""

    def run(_emit: Any) -> dict[str, Any]:
        """Refuse the interaction like a hosted subscription-model run."""
        raise DomainError("serve.subscription_model_hosted", status=409)

    async def collect() -> list[dict[str, Any]]:
        """Drain the SSE source into a list."""
        return [event async for event in serve_router._protected_sse(run, final_event="final")]

    events = asyncio.run(collect())

    assert events == [
        {
            "event": "error",
            "data": {
                "error": t_en("serve.subscription_model_hosted"),
                "code": "serve.subscription_model_hosted",
                "params": {},
            },
        }
    ]
    assert "serve.subscription_model_hosted" not in events[0]["data"]["error"]


@pytest.mark.parametrize(
    ("error", "logged"),
    [
        (DomainError("serve.protected_interaction_failed", status=502, error="guest crashed"), True),
        (DomainError("serve.subscription_model_hosted", status=409), False),
    ],
)
def test_stream_logs_server_failures_but_not_client_refusals(
    error: DomainError, logged: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """Keep a server log for a streamed 5xx, which answers 200 and leaves no other trace.

    Args:
        error: Domain error the streamed interaction raises.
        logged: Whether the stream must write an error log for it.
        caplog: Pytest log capture fixture.
    """

    def run(_emit: Any) -> dict[str, Any]:
        """Fail the interaction with the parametrized domain error."""
        raise error

    async def collect() -> list[dict[str, Any]]:
        """Drain the SSE source into a list."""
        return [event async for event in serve_router._protected_sse(run, final_event="final")]

    with caplog.at_level("WARNING", logger=serve_router.logger.name):
        events = asyncio.run(collect())

    assert events[0]["data"]["code"] == error.code
    failures = [record for record in caplog.records if record.name == serve_router.logger.name]
    assert bool(failures) is logged
    if logged:
        assert failures[0].exc_info is not None


@pytest.mark.parametrize(
    "budget_error",
    [
        BudgetInsufficientError("no funds"),
        BudgetInFlightError("settling"),
        BudgetConflictError("stale revision"),
        BudgetNotFoundError("missing"),
        BudgetBoundExceededError("over bound"),
    ],
)
def test_streamed_budget_refusal_reads_as_a_sentence(budget_error: BudgetError) -> None:
    """Send words, not a bare catalog key, when a streamed serve or chat call hits its budget.

    Args:
        budget_error: Ledger refusal the streamed interaction raises.
    """
    refusal = budget_http_error(budget_error)

    def run(_emit: Any) -> dict[str, Any]:
        """Refuse the interaction like the ledger does on admission."""
        raise refusal

    async def collect() -> list[dict[str, Any]]:
        """Drain the SSE source into a list."""
        return [event async for event in serve_router._protected_sse(run, final_event="final")]

    [event] = asyncio.run(collect())

    assert event["data"]["code"] == refusal.code
    assert event["data"]["error"] != refusal.code
    assert event["data"]["error"] == t_en(refusal.code)
