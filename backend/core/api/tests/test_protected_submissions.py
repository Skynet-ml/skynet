"""Verify public submissions against real job, setup, and wallet transactions."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from core.api import preflight_execution
from core.api.auth import AuthenticatedUser, get_authenticated_user
from core.api.model_billing import normalize_model_token_sources
from core.api.routers import submissions
from core.api.routers.execution_budgets import create_execution_budgets_router
from core.api.routers.optimizations_meta import create_optimizations_meta_router
from core.api.routers.wizard_preflight import create_wizard_preflight_router
from core.billing.budgets import (
    AccountFundsError,
    BudgetConflictError,
    BudgetFencedError,
    BudgetInsufficientError,
    BudgetService,
)
from core.billing.operation_pricing import ChargePolicy, operation_quote
from core.billing.protected_credentials import scrub_execution_credentials
from core.billing.runtime import BudgetRuntime, PaidResult
from core.billing.service import StripeBillingService
from core.billing.signals import BudgetReached
from core.models import BlackboxRunRequest
from core.storage.models import (
    Base,
    BillingCustomerModel,
    ExecutionBudgetModel,
    ExecutionOperationModel,
    ExecutionUsageEvidenceModel,
    JobModel,
    WalletLedgerModel,
)
from core.storage.preflights import PreflightStore, WizardPreflightModel
from core.storage.remote import RemoteDBJobStore

ROUTE = "/blackbox/run"


class _SQLiteJobStore(RemoteDBJobStore):
    """Exercise production store methods with a disposable file database."""

    def __init__(self, path: Path) -> None:
        """Replace only PostgreSQL bootstrap with SQLite schema creation.

        Args:
            path: Private database file belonging to this test.
        """
        self._engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 10})
        Base.metadata.create_all(self._engine)
        self._session_factory = sessionmaker(bind=self._engine)


class _Gateway:
    """Replace external transports while retaining production spending admission."""

    def __init__(self, runtime: BudgetRuntime) -> None:
        """Retain the real setup authority created by the API."""
        self.runtime = runtime

    def protect_payload(self, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """Give the fixture runtime adapter access to the actual setup authority."""
        return {**scrub_execution_credentials(payload), "_fixture_runtime": self.runtime}

    def close(self) -> None:
        """Complete the deterministic transport without external resources."""

    def model_routes(self) -> list[dict[str, str]]:
        """Return no external model capabilities from the deterministic fixture adapter."""
        return []


@dataclass
class _Harness:
    """Retain real persistence and the single replaceable provider adapter."""

    store: _SQLiteJobStore
    client: TestClient | None = None
    username: str = "alice"
    role: str = "user"
    calls: list[dict[str, Any]] = field(default_factory=list)
    result_status: str = "succeeded"
    usage_final: bool = True

    @property
    def budgets(self) -> BudgetService:
        """Read and mutate the same authoritative ledger as the API."""
        return BudgetService(engine=self.store.engine)

    def verify(self, runtime: BudgetRuntime, payload: dict[str, Any], identity: str) -> dict[str, Any]:
        """Charge one measured setup operation through real reserve, dispatch, and settlement.

        Args:
            runtime: Setup authority created by the production preflight flow.
            payload: Materialized user inputs sent to the isolated adapter.
            identity: Durable preflight identity used for the physical operation key.

        Returns:
            Actual deterministic readiness outcome from this fixture adapter.
        """
        policy = ChargePolicy("managed_model")
        quote = operation_quote({"setup": identity}, Decimal("0.02"), policy, {"provider": "fixture"})

        def dispatch() -> PaidResult[None]:
            """Assert real coverage exists before reporting one synthetic provider receipt."""
            snapshot = runtime.service.get(runtime.budget_id, runtime.username)
            assert snapshot.reserved_cents == 2
            assert snapshot.pending_operations == 1
            self.calls.append({"budget_id": runtime.budget_id, "identity": identity, "payload": payload})
            return PaidResult(
                None,
                Decimal("0.01") if self.usage_final else None,
                {"provider": "fixture", "cost": "0.01"},
                provider_request_id=f"fixture-{identity}",
                final=self.usage_final,
            )

        runtime.execute(quote, policy, dispatch, operation_key=f"setup:{identity}", cost_kind="model")
        return {"checks": [{"key": "execution", "status": self.result_status}]}

    def count(self, model: Any) -> int:
        """Count durable rows independently of HTTP responses."""
        with Session(self.store.engine) as session:
            return session.scalar(select(func.count()).select_from(model))


def _stage_without_sandbox(payload: dict[str, Any], **kwargs: Any) -> tuple[dict[str, Any], SimpleNamespace]:
    """Return the repository payload as staged, with a fixture environment.

    Args:
        payload: Protected repository payload.
        **kwargs: Owner and budget binding, unused by the fixture.

    Returns:
        The payload with a repository environment, and a stand-in checkout with no files or secrets.
    """
    payload["target"]["repo"]["environment"] = {"setup_command": "npm ci"}
    return payload, SimpleNamespace(
        snapshot=SimpleNamespace(archive=None), secrets={}, close_scorers=lambda: None, cleanup=lambda: None
    )


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Harness]:
    """Serve public routes with real persistence and no worker or paid provider traffic.

    Args:
        tmp_path: Disposable database directory.
        monkeypatch: Isolate provider adapters, authentication, and deployment settings.

    Yields:
        API client backed by the production job, preflight, and billing services.
    """
    store = _SQLiteJobStore(tmp_path / "submissions.db")
    fixture = _Harness(store)
    with Session(store.engine) as session:
        session.add_all(
            BillingCustomerModel(
                username=name, stripe_customer_id=f"fixture-{name}", balance_cents=100, grant_remaining=0
            )
            for name in ("alice", "bob")
        )
        session.commit()

    def verify_anything(gateway: _Gateway, payload: dict[str, Any], *, scope: str, identity: str) -> dict:
        """Substitute the physical scorer/runtime adapter with measured fixture execution."""
        return fixture.verify(gateway.runtime, payload, identity)

    monkeypatch.setattr(preflight_execution, "ModelGateway", _Gateway)
    monkeypatch.setattr(preflight_execution, "bind_protected_sandbox", lambda *args, **kwargs: None)
    monkeypatch.setattr(preflight_execution, "_verify_anything", verify_anything)
    # Repository staging clones from GitHub inside a sandbox; the fixture
    # keeps the payload and only records the environment the check reports.
    monkeypatch.setattr(preflight_execution, "stage_repository", _stage_without_sandbox)
    monkeypatch.setattr(preflight_execution, "check_repository", lambda *args, **kwargs: [])
    monkeypatch.setattr(preflight_execution, "infer_repo_setup", lambda *args, **kwargs: None)
    monkeypatch.setattr(preflight_execution, "bind_repo_scorer", lambda *args, **kwargs: None)
    # The unified sandbox verifier is replaced the same way so no sandbox or
    # provider is reached.
    monkeypatch.setattr(preflight_execution, "_verify_unified", verify_anything)
    # Holds here are sized against tight budgets in raw provider cost; the markup is pricing policy, tested in core/billing.
    monkeypatch.setattr(submissions.settings, "usage_markup", 1.0)
    monkeypatch.setattr(submissions.settings, "worker_enabled", False)
    monkeypatch.setattr(submissions.settings, "submissions_paused", False)
    monkeypatch.setattr(submissions.settings, "max_concurrent_jobs_per_user", 0)
    monkeypatch.setattr(submissions.settings, "global_daily_spend_ceiling_cents", 0)
    monkeypatch.setattr(submissions.settings, "rate_limit_submissions_per_minute", 0)
    monkeypatch.setattr(submissions.settings, "openrouter_api_key", SecretStr("fixture-only"))
    monkeypatch.setattr(submissions.settings, "byok_vault_key", SecretStr(Fernet.generate_key().decode()))
    app = FastAPI()
    app.include_router(submissions.create_submissions_router(service=object(), job_store=store))
    app.include_router(create_execution_budgets_router(job_store=store))
    app.include_router(create_wizard_preflight_router(job_store=store))
    app.include_router(create_optimizations_meta_router(job_store=store))
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser(fixture.username, fixture.role, ())
    with TestClient(app) as client:
        fixture.client = client
        yield fixture
    store.engine.dispose()


def _payload(*, total: int = 20) -> dict[str, Any]:
    """Build a valid repository request body with no budget or verification metadata.

    Args:
        total: Approved total setup and run allowance.

    Returns:
        Request exercising the production schema and canonicalization.
    """
    return {
        "username": "spoofed-owner",
        "name": "repo-run",
        "objective": "Make the tests pass.",
        "recipe": "repo",
        "scorer": {"kind": "python", "metric_code": "def score(repo_path, case=None):\n    return 1.0"},
        "strategy": {"mode": "single", "engine": "gepa"},
        "target": {
            "kind": "repo",
            "repo": {
                "provider": "github",
                "repository": "example/site",
                "branch": "main",
                "commit": "0" * 40,
                "editable_paths": ["src", "README.md"],
            },
            "setup_command": "npm ci",
        },
        "reflection_model_config": {"name": "fixture/text"},
        "max_cost_cents": total,
    }


def _unified_payload(*, total: int = 20) -> dict[str, Any]:
    """Build a valid unified request body with no budget or verification metadata.

    Args:
        total: Approved total setup and run allowance.

    Returns:
        Request exercising the production schema and canonicalization.
    """
    return {
        "username": "spoofed-owner",
        "task": {
            "objective": "Answer the question in one word.",
            "artifact": {"kind": "dspy_program", "starting_version": "Answer briefly: "},
        },
        "data": {
            "mode": "pairs",
            "cases": [{"input": {"question": f"Q{index}?"}, "expected": "A"} for index in range(10)],
            "input_fields": ["question"],
        },
        "optimizer": {"choice": "dspy_gepa"},
        "models": {"reflection_model": {"name": "fixture/text"}, "task_model": {"name": "fixture/task"}},
        "workspace": {
            "run_code": "def run(artifact, input):\n    return artifact + input['question']\n",
            "metric_code": (
                "def score(input, output, expected=None):\n    return float(output == expected), {'feedback': ''}\n"
            ),
        },
        "max_cost_cents": total,
    }


def _prepare(harness: _Harness) -> tuple[dict, dict]:
    """Run actual paid Continue and return its submission evidence.

    Args:
        harness: Funded real API fixture.

    Returns:
        Protected submission body and completed preflight response.
    """
    budget = harness.budgets.create(harness.username, 20, idempotency_key="wizard")
    payload = {
        **_payload(),
        "execution_budget_id": budget.id,
        "execution_budget_revision": budget.revision,
    }
    response = harness.client.post(
        "/wizard/preflight",
        json={
            "scope": "execution",
            "workflow": "anything",
            "payload": payload,
            "execution_budget_id": budget.id,
            "execution_budget_revision": budget.revision,
        },
    )
    assert response.status_code == 200, response.text
    checked = response.json()
    assert checked["status"] == "succeeded", checked
    return {
        **payload,
        "execution_budget_revision": checked["budget"]["revision"],
        "preflight_id": checked["id"],
        "preflight_fingerprint": checked["fingerprint"],
    }, checked


def test_anything_evaluation_protects_the_declared_scorer_model(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolve the scorer before protecting an anything evaluation."""
    seen: list[dict[str, Any]] = []
    protect = _Gateway.protect_payload

    def capture(gateway: _Gateway, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """Capture the payload at the protected boundary."""
        seen.append(payload)
        return protect(gateway, payload, **kwargs)

    monkeypatch.setattr(_Gateway, "protect_payload", capture)
    budget = harness.budgets.create(harness.username, 20, idempotency_key="anything-evaluation")
    payload = {
        "reflection_model_config": {"name": "openai/reflection"},
        "scorer": {"kind": "python", "metric_code": "llm(model='openai/judge')"},
    }
    response = harness.client.post(
        "/wizard/preflight",
        json={
            "scope": "evaluation",
            "workflow": "anything",
            "payload": payload,
            "execution_budget_id": budget.id,
            "execution_budget_revision": budget.revision,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "succeeded"
    assert seen[0]["scorer"]["model"]["name"] == "openai/judge"
    assert "model" not in payload["scorer"]


def test_anything_evaluation_invalid_model_fails_setup(harness: _Harness) -> None:
    """Return a setup failure for an unsafe metric declaration."""
    budget = harness.budgets.create(harness.username, 20, idempotency_key="invalid-anything")
    response = harness.client.post(
        "/wizard/preflight",
        json={
            "scope": "evaluation",
            "workflow": "anything",
            "payload": {
                "reflection_model_config": {"name": "openai/reflection"},
                "scorer": {"kind": "python", "metric_code": "llm(model=choice)"},
            },
            "execution_budget_id": budget.id,
            "execution_budget_revision": budget.revision,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "failed"
    assert response.json()["checks"][0]["key"] == "setup"
    assert not harness.calls


def test_legacy_submission_attaches_paid_setup_once(harness: _Harness) -> None:
    """Persist a single account-owned budget, setup receipt, and atomically attached root job.

    Args:
        harness: Real persistence with a measured provider stub.
    """
    first = harness.client.post(ROUTE, json=_payload(), headers={"Idempotency-Key": "legacy-submit"})
    assert first.status_code == 201, first.text
    identity = first.json()["optimization_id"]
    job = harness.store.get_job(identity, include_payload=True)
    budget = harness.budgets.get(job["execution_budget_id"], "alice")
    assert budget.state == "attached"
    assert budget.job_id == identity
    assert budget.generation == job["execution_budget_generation"] == job["payload"]["execution_budget_generation"]
    assert budget.id == job["payload"]["execution_budget_id"] == harness.calls[0]["budget_id"]
    assert budget.setup_spent_cents == 1
    assert budget.run_spent_cents == budget.reserved_cents == 0
    assert budget.available_cents == 19
    assert budget.billed_cents == 1
    assert job["username"] == job["payload"]["username"] == "alice"
    assert job["status"] == "pending"
    assert harness.count(JobModel) == harness.count(ExecutionBudgetModel) == harness.count(WizardPreflightModel) == 1
    assert harness.count(ExecutionOperationModel) == harness.count(WalletLedgerModel) == 1
    assert harness.count(ExecutionUsageEvidenceModel) >= 1
    replay = harness.client.post(ROUTE, json=_payload(), headers={"Idempotency-Key": "legacy-submit"})
    assert replay.status_code == 201, replay.text
    assert replay.json()["optimization_id"] == identity
    assert len(harness.calls) == harness.count(JobModel) == harness.count(ExecutionOperationModel) == 1
    with pytest.raises(BudgetConflictError):
        harness.budgets.reserve(
            budget.id,
            "alice",
            generation=budget.generation,
            phase="setup",
            cost_kind="model",
            operation_key="late-setup",
            request_fingerprint="late",
            price_snapshot={"version": "fixture-v1"},
            max_cents=1,
        )


def test_keyless_legacy_submission_replays_one_paid_job(harness: _Harness) -> None:
    """Reuse one synthesized key across lookup, setup authority, and root persistence.

    Args:
        harness: Real persistence with measured setup execution.
    """
    first = harness.client.post(ROUTE, json=_payload())
    repeated = harness.client.post(ROUTE, json=_payload())

    assert first.status_code == 201, first.text
    assert repeated.status_code == 201, repeated.text
    identity = first.json()["optimization_id"]
    assert repeated.json()["optimization_id"] == identity
    with Session(harness.store.engine) as session:
        job = session.get(JobModel, identity)
        assert job is not None
        budget = session.get(ExecutionBudgetModel, job.execution_budget_id)
        assert budget is not None
        assert job.idempotency_key == budget.creation_key
        assert job.idempotency_key.startswith("legacy-paid-v1:")
    assert len(harness.calls) == 1
    assert harness.count(JobModel) == 1
    assert harness.count(ExecutionBudgetModel) == 1
    assert harness.count(WizardPreflightModel) == 1
    assert harness.count(ExecutionOperationModel) == 1


def test_keyless_uncertain_retry_does_not_repeat_paid_preflight(harness: _Harness) -> None:
    """Recover the same budget and pending evidence after a lost final usage receipt.

    Args:
        harness: Real ledger configured to retain uncertain setup coverage.
    """
    harness.usage_final = False
    payload = _payload()
    payload.pop("max_cost_cents")

    first = harness.client.post(ROUTE, json=payload)
    repeated = harness.client.post(ROUTE, json=payload)

    assert first.status_code == 409, first.text
    assert repeated.status_code == 409, repeated.text
    assert len(harness.calls) == 1
    assert harness.count(JobModel) == 0
    assert harness.count(ExecutionBudgetModel) == 1
    assert harness.count(WizardPreflightModel) == 1
    assert harness.count(ExecutionOperationModel) == 1
    budget = harness.budgets.get(harness.calls[0]["budget_id"], "alice")
    assert budget.total_cents == 100
    assert budget.pending_operations == 1
    assert budget.reserved_cents == 2


def test_wizard_submission_reuses_actual_continue_evidence(harness: _Harness) -> None:
    """Attach paid wizard evidence without rerunning its physical checks.

    Args:
        harness: Real setup, wallet, and job services.
    """
    payload, checked = _prepare(harness)
    response = harness.client.post(ROUTE, json=payload)
    assert response.status_code == 201, response.text
    job = harness.store.get_job(response.json()["optimization_id"], include_payload=True)
    assert job["payload"]["preflight_id"] == checked["id"]
    assert job["execution_budget_id"] == payload["execution_budget_id"]
    assert len(harness.calls) == 1


@pytest.mark.parametrize("attack", ["missing", "forged", "fingerprint", "changed", "foreign", "revision"])
def test_invalid_evidence_cannot_queue(harness: _Harness, attack: str) -> None:
    """Reject untrusted or obsolete approval metadata before persisting any runnable job.

    Args:
        harness: Real setup and submission routes.
        attack: Missing, forged, modified, foreign, or stale request evidence.
    """
    payload, _checked = _prepare(harness)
    if attack == "missing":
        payload.pop("preflight_id")
        payload.pop("preflight_fingerprint")
    elif attack == "forged":
        payload["preflight_id"] = str(uuid4())
    elif attack == "fingerprint":
        payload["preflight_fingerprint"] = "0" * 64
    elif attack == "changed":
        payload["objective"] = "A different objective"
    elif attack == "foreign":
        harness.username = "bob"
    else:
        harness.budgets.update_total(payload["execution_budget_id"], "alice", 21, expected_revision=1)
    response = harness.client.post(ROUTE, json=payload)
    assert response.status_code in {404, 409}, response.text
    assert harness.count(JobModel) == 0
    assert harness.budgets.get(payload["execution_budget_id"], "alice").job_id is None
    assert len(harness.calls) == 1


@pytest.mark.parametrize("pending_usage", [False, True])
def test_failed_or_uncertain_paid_setup_never_queues(harness: _Harness, pending_usage: bool) -> None:
    """Retain actual setup charges or uncertain holds without fabricating successful readiness.

    Args:
        harness: Real legacy submission routes and spending authority.
        pending_usage: Whether the provider has not confirmed its final charge.
    """
    harness.result_status = "failed"
    harness.usage_final = not pending_usage
    response = harness.client.post(ROUTE, json=_payload())
    assert response.status_code == 409, response.text
    assert harness.count(JobModel) == 0
    budget = harness.budgets.get(harness.calls[0]["budget_id"], "alice")
    assert budget.job_id is None
    assert budget.reserved_cents == (2 if pending_usage else 0)
    assert budget.setup_spent_cents == (0 if pending_usage else 1)


@pytest.mark.parametrize(
    ("stop", "detail"),
    [
        (BudgetInsufficientError, "The spending limit is below the minimum this run needs."),
        (AccountFundsError, "Payload validation failed."),
    ],
)
def test_a_check_stopped_by_the_limit_says_the_limit_is_too_low(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, stop: type[Exception], detail: str
) -> None:
    """Name a too-low limit, not the payload, when the limit stopped the setup check.

    Args:
        harness: Real legacy submission routes and spending authority.
        monkeypatch: Make the setup check stop on the budget.
        stop: The budget stop the check raises.
        detail: Start of the expected refusal text.
    """

    def stopped(gateway: Any, payload: dict[str, Any], *, scope: str, identity: str) -> dict:
        """Stop like a limit or a wallet that cannot cover one model call."""
        raise stop("The next operation needs more than the limit allows.")

    monkeypatch.setattr(preflight_execution, "_verify_anything", stopped)
    response = harness.client.post(ROUTE, json=_payload(total=1))
    assert response.status_code == 409, response.text
    assert response.json()["detail"].startswith(detail)
    assert harness.count(JobModel) == 0


def test_attachment_rolls_back_job_when_budget_already_attached(harness: _Harness) -> None:
    """Reject a second root and roll its inserted job back in the attachment transaction.

    Args:
        harness: Real job store and budget authority.
    """
    payload, _checked = _prepare(harness)
    first = harness.client.post(ROUTE, json=payload)
    assert first.status_code == 201, first.text
    existing = harness.store.get_job(first.json()["optimization_id"], include_payload=True)
    second_id = str(uuid4())
    with pytest.raises(BudgetConflictError):
        harness.store.create_job(
            second_id,
            username="alice",
            budget_service=harness.budgets,
            preflight_store=PreflightStore(harness.store.engine),
            execution_budget_id=payload["execution_budget_id"],
            execution_budget_revision=payload["execution_budget_revision"],
            preflight_id=payload["preflight_id"],
            preflight_fingerprint=payload["preflight_fingerprint"],
            preflight_payload=existing["payload"],
            preflight_workflow="unified",
        )
    assert harness.count(JobModel) == 1
    with pytest.raises(KeyError):
        harness.store.get_job(second_id)
    assert harness.budgets.get(payload["execution_budget_id"], "alice").job_id == existing["optimization_id"]


@pytest.mark.parametrize("limit", ["total", "wallet"])
def test_run_admission_cannot_dispatch_beyond_remaining_coverage(harness: _Harness, limit: str) -> None:
    """Apply setup spend, cumulative rounding, and shared-wallet holds to the attached run.

    Args:
        harness: Real submission and billing transactions.
        limit: Total-budget exhaustion or another operation's wallet commitment.
    """
    response = harness.client.post(ROUTE, json=_payload())
    assert response.status_code == 201, response.text
    job = harness.store.get_job(response.json()["optimization_id"])
    runtime = BudgetRuntime(
        harness.budgets,
        username="alice",
        budget_id=job["execution_budget_id"],
        generation=job["execution_budget_generation"],
        phase="run",
        wait_timeout=0,
    )
    policy = ChargePolicy("sandbox")
    quote = operation_quote({"run": "bounded"}, Decimal("0.18"), policy, {"provider": "fixture"})
    dispatched = []

    def dispatch() -> PaidResult[str]:
        """Record the physical request only after the real ledger admits it."""
        dispatched.append(True)
        return PaidResult("actual result", Decimal("0.18"), {"runtime_cost": "0.18"})

    if limit == "wallet":
        other = harness.budgets.create("alice", 90, idempotency_key="other-draft")
        operation = harness.budgets.reserve(
            other.id,
            "alice",
            generation=0,
            phase="setup",
            cost_kind="sandbox",
            operation_key="other-usage",
            request_fingerprint="other",
            price_snapshot={"version": "fixture-v1"},
            max_cents=90,
        )
        harness.budgets.mark_dispatched(operation.id, "alice", "other-provider-request")
        harness.budgets.settle(
            operation.id, "alice", evidence_key="other-final", actual_cents=90, evidence={"measured_cents": 90}
        )
        with pytest.raises(BudgetReached):
            runtime.execute(quote, policy, dispatch, operation_key="run", cost_kind="sandbox")
        assert not dispatched
    else:
        assert runtime.execute(quote, policy, dispatch, operation_key="run", cost_kind="sandbox") == "actual result"
        budget = harness.budgets.get(runtime.budget_id, "alice")
        assert budget.setup_spent_cents == 1
        assert budget.run_spent_cents == 18
        assert budget.available_cents == 1
        assert budget.billed_cents == 19
        assert StripeBillingService(engine=harness.store.engine).spendable_cents("alice") == 81
        with pytest.raises(BudgetReached):
            runtime.execute(quote, policy, dispatch, operation_key="next", cost_kind="sandbox")
        assert len(dispatched) == 1
    assert harness.budgets.get(runtime.budget_id, "alice").reserved_cents == 0


def test_attachment_uses_current_generation_and_fences_old_setup(harness: _Harness) -> None:
    """Stamp the durable generation and reject an obsolete setup owner's next operation.

    Args:
        harness: Real preflight and attachment services.
    """
    payload, _checked = _prepare(harness)
    fenced = harness.budgets.fence_generation(payload["execution_budget_id"], "alice", expected_generation=0)
    payload["execution_budget_revision"] = fenced.revision
    response = harness.client.post(ROUTE, json=payload)
    assert response.status_code == 201, response.text
    job = harness.store.get_job(response.json()["optimization_id"])
    assert job["execution_budget_generation"] == fenced.generation == 1
    with pytest.raises(BudgetFencedError):
        harness.budgets.reserve(
            fenced.id,
            "alice",
            generation=0,
            phase="run",
            cost_kind="model",
            operation_key="stale-worker",
            request_fingerprint="old",
            price_snapshot={"version": "fixture-v1"},
            max_cents=1,
        )


def test_concurrent_attachment_keeps_one_complete_root(harness: _Harness) -> None:
    """Serialize competing root attachments so the losing insert and budget mutation roll back.

    Args:
        harness: Real file-backed store and completed setup evidence.
    """
    payload, _checked = _prepare(harness)
    typed = BlackboxRunRequest.model_validate(payload)
    typed.username = "alice"
    normalize_model_token_sources(typed)
    canonical = submissions._protected_submission(typed, harness.store, "anything")
    assert canonical is not None

    def attach(identity: str) -> str:
        """Run the actual job and budget transaction in a separate database connection."""
        try:
            harness.store.create_job(
                identity,
                username="alice",
                budget_service=harness.budgets,
                preflight_store=PreflightStore(harness.store.engine),
                execution_budget_id=payload["execution_budget_id"],
                execution_budget_revision=payload["execution_budget_revision"],
                preflight_id=payload["preflight_id"],
                preflight_fingerprint=payload["preflight_fingerprint"],
                preflight_payload=canonical,
                preflight_workflow="anything",
            )
            return identity
        except BudgetConflictError:
            return "rejected"

    identities = [str(uuid4()), str(uuid4())]
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attach, identities))
    assert outcomes.count("rejected") == 1
    assert harness.count(JobModel) == 1
    budget = harness.budgets.get(payload["execution_budget_id"], "alice")
    assert budget.job_id in outcomes
    job = harness.store.get_job(budget.job_id)
    assert job["execution_budget_id"] == budget.id
    assert job["execution_budget_generation"] == budget.generation


def test_paid_setup_does_not_rewrite_the_approved_total(harness: _Harness) -> None:
    """Preserve the shared total after setup has already debited part of that allowance.

    Args:
        harness: Real API, setup ledger, and wallet with exactly the approved total.
    """
    with Session(harness.store.engine) as session:
        session.get(BillingCustomerModel, "alice").balance_cents = 20
        session.commit()
    response = harness.client.post(ROUTE, json=_payload())
    assert response.status_code == 201, response.text
    job = harness.store.get_job(response.json()["optimization_id"], include_payload=True)
    budget = harness.budgets.get(job["execution_budget_id"], "alice")
    assert budget.total_cents == 20
    assert budget.setup_spent_cents == 1
    assert job["payload"]["max_cost_cents"] == budget.total_cents


def test_legacy_commitment_is_counted_once_during_protected_submission(harness: _Harness) -> None:
    """Keep legacy work covered while permitting paid setup within the genuinely free wallet.

    Args:
        harness: Real wallet, one active legacy job, and the protected API.
    """
    harness.store.create_job("legacy", username="alice")
    harness.store.set_payload_overview("legacy", {"max_cost_cents": 60, "token_source": "managed"})
    assert StripeBillingService(engine=harness.store.engine).spendable_cents("alice") == 40
    response = harness.client.post(ROUTE, json=_payload())
    assert response.status_code == 201, response.text
    job = harness.store.get_job(response.json()["optimization_id"])
    assert job["execution_budget_id"] is not None
    assert StripeBillingService(engine=harness.store.engine).spendable_cents("alice") == 39


def test_parallel_submissions_with_their_own_keys_both_start(harness: _Harness) -> None:
    """Start two different simultaneous submissions from one account without a server error.

    Args:
        harness: Real persistence with measured setup execution.
    """
    other = {**_payload(), "name": "other"}
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(
                lambda body_key: harness.client.post(ROUTE, json=body_key[0], headers={"Idempotency-Key": body_key[1]}),
                [(_payload(), "one"), (other, "two")],
            )
        )

    assert [response.status_code for response in responses] == [201, 201], [r.text for r in responses]
    assert len({response.json()["optimization_id"] for response in responses}) == 2
    assert harness.count(JobModel) == harness.count(ExecutionBudgetModel) == 2


def _check_models(harness: _Harness, models: dict[str, Any]) -> tuple[Any, str]:
    """Run the wizard's Ready-to-run check with the given model roles.

    Args:
        harness: Funded real API fixture.
        models: The unified ``models`` block to check.

    Returns:
        The HTTP response and the status of the check's durable row.
    """
    budget = harness.budgets.create(harness.username, 20, idempotency_key="models")
    payload = {
        **_unified_payload(),
        "models": models,
        "execution_budget_id": budget.id,
        "execution_budget_revision": budget.revision,
    }
    response = harness.client.post(
        "/wizard/preflight",
        json={
            "scope": "execution",
            "workflow": "unified",
            "payload": payload,
            "execution_budget_id": budget.id,
            "execution_budget_revision": budget.revision,
        },
    )
    with Session(harness.store.engine) as session:
        status = session.scalar(select(WizardPreflightModel.status))
    return response, status


def test_local_check_accepts_subscription_task_and_reflection_models(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finish a local Ready-to-run check whose roles both run on a subscription CLI.

    Args:
        harness: Real setup and wallet services.
        monkeypatch: Turns local mode on.
    """
    monkeypatch.setattr(submissions.settings, "local_mode", True)
    subscription = {"token_source": "subscription", "cli_provider": "claude"}
    models = {
        "reflection_model": {"name": "anthropic/claude-sonnet-4.6", **subscription},
        "task_model": {"name": "anthropic/claude-haiku-4.5", **subscription},
    }

    response, status = _check_models(harness, models)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "succeeded", response.json()
    assert status == "succeeded"
    sent = harness.calls[0]["payload"]["models"]
    assert {role: sent[role]["token_source"] for role in ("reflection_model", "task_model")} == {
        "reflection_model": "subscription",
        "task_model": "subscription",
    }


def test_unusable_model_credentials_fail_the_check_instead_of_leaving_it_running(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Record a failed check, not a row stuck at running, when a role has no usable credentials.

    Args:
        harness: Real setup and wallet services.
        monkeypatch: Simulate a connection disappearing after admission.
    """
    view = SimpleNamespace(provider="openai", api_base=None, status="verified")
    vault = SimpleNamespace(has_verified_connection=lambda *_: True, list_keys=lambda *_: SimpleNamespace(keys=[view]))
    monkeypatch.setattr(submissions, "ProviderKeyVault", lambda **kwargs: vault)
    models = {
        "reflection_model": {"name": "fixture/text"},
        "task_model": {"name": "gpt-4o", "token_source": "byok", "byok_provider": "openai"},
    }

    response, status = _check_models(harness, models)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "failed"
    assert status == "failed"
    assert any("No verified openai connection" in (check.get("message") or "") for check in body["checks"]), body
    assert harness.calls == []


def test_guest_funds_refusal_keeps_the_account_funds_type() -> None:
    """Report the balance, not the limit, when the guest relays a funds refusal as a budget stop."""
    events = preflight_execution._Events()
    events.put({"type": "error", "error_type": "BudgetInsufficientError", "error": "budget_insufficient"})
    refusal = AccountFundsError("The balance cannot fund this call.")
    gateway = SimpleNamespace(budget_refusal=refusal)

    with pytest.raises(AccountFundsError) as raised:
        events.evidence(gateway)

    assert raised.value is refusal
