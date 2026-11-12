"""Tests for the sandbox provider a user picks for each run (SKY-606). Fakes only: no network, no money."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from core.api.auth import AuthenticatedUser, get_authenticated_user
from core.api.errors import DomainError
from core.api.model_billing import enforce_local_mode_sources, enforce_raw_local_mode_sources
from core.api.routers.execution_runtimes import create_execution_runtimes_router
from core.billing.operation_pricing import UnpricedOperationError
from core.billing.protected_execution import bind_protected_sandbox
from core.billing.tests.test_protected_execution import _Gateway
from core.billing.tests.test_protected_execution import _settings as _hosted_settings
from core.config import settings as global_settings
from core.sandbox.providers import vercel as vercel_provider
from core.storage.models import ExecutionBudgetModel, ExecutionOperationModel, JobModel

from .test_protected_submissions import ROUTE, _Harness, _payload
from .test_protected_submissions import harness as _harness_fixture

harness = _harness_fixture


@pytest.fixture
def hosted_vercel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the global settings a hosted deployment with fake Vercel credentials and SDK.

    Args:
        monkeypatch: Pytest fixture.
    """
    # The provider preflight checks only that the SDK imported; a dev venv may lack it.
    monkeypatch.setattr(vercel_provider, "SandboxClient", object)
    monkeypatch.setattr(global_settings, "local_mode", False)
    monkeypatch.setattr(global_settings, "vercel_token", SecretStr("token"))
    monkeypatch.setattr(global_settings, "vercel_team_id", "team")
    monkeypatch.setattr(global_settings, "vercel_project_id", "project")


@pytest.mark.usefixtures("hosted_vercel")
def test_catalog_lists_the_offered_providers_and_the_default() -> None:
    """GET /sandbox-providers lists Vercel with its label and names it the default."""
    app = FastAPI()
    app.include_router(create_execution_runtimes_router())
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser("alice", "user", ())

    response = TestClient(app).get("/sandbox-providers")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "providers": [{"id": "vercel", "label": "Vercel Sandbox"}],
        "default_provider": "vercel",
    }


@pytest.mark.usefixtures("hosted_vercel")
@pytest.mark.parametrize("provider", ["nope", "docker", "daytona", [], {}, ["vercel"], 1])
def test_gate_refuses_a_provider_the_deployment_does_not_offer(provider: object) -> None:
    """Both setup gates refuse an unknown, local-only, unmetered or non-string provider with 422.

    Args:
        provider: The requested provider value, as a raw payload may send it.
    """
    for check in (
        lambda: enforce_local_mode_sources(SimpleNamespace(sandbox_provider=provider), "byok", []),
        lambda: enforce_raw_local_mode_sources({"sandbox_provider": provider}),
    ):
        with pytest.raises(DomainError) as refused:
            check()
        assert refused.value.status_code == 422
        assert refused.value.code == "sandbox.provider_unavailable"


@pytest.mark.usefixtures("hosted_vercel")
@pytest.mark.parametrize("provider", [None, "vercel"])
def test_gate_accepts_the_default_and_an_offered_provider(provider: str | None) -> None:
    """Both setup gates pass a run that names no provider or an offered one.

    Args:
        provider: The requested provider id.
    """
    enforce_local_mode_sources(SimpleNamespace(sandbox_provider=provider), "byok", [])
    enforce_raw_local_mode_sources({"sandbox_provider": provider})


@pytest.mark.usefixtures("hosted_vercel")
def test_run_with_an_unoffered_provider_is_refused_before_paid_work(harness: _Harness) -> None:
    """A run that names an unoffered provider gets 422, and no job, budget or paid setup exists.

    Args:
        harness: Real persistence with a measured provider stub.
    """
    response = harness.client.post(ROUTE, json={**_payload(), "sandbox_provider": "nope"})

    assert response.status_code == 422, response.text
    assert "sandbox.provider_unavailable" in response.text
    assert harness.calls == []
    assert harness.count(JobModel) == harness.count(ExecutionBudgetModel) == 0
    assert harness.count(ExecutionOperationModel) == 0


@pytest.mark.usefixtures("hosted_vercel")
def test_setup_check_with_an_unoffered_provider_is_refused_before_paid_work(harness: _Harness) -> None:
    """The setup check refuses an unoffered provider before it claims the budget.

    Args:
        harness: Real persistence with a measured provider stub.
    """
    budget = harness.budgets.create(harness.username, 20, idempotency_key="wizard")
    payload = {
        **_payload(),
        "sandbox_provider": "nope",
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

    assert response.status_code == 422, response.text
    assert "sandbox.provider_unavailable" in response.text
    assert harness.calls == []
    assert harness.count(ExecutionOperationModel) == 0
    assert harness.budgets.get(budget.id, harness.username).revision == budget.revision


@pytest.mark.usefixtures("hosted_vercel")
@pytest.mark.parametrize("requested", [{}, {"sandbox_provider": "vercel"}])
def test_stored_run_names_its_provider(harness: _Harness, requested: dict[str, str]) -> None:
    """A run stores the provider it chose, or the deployment default, so the run view can show it.

    Args:
        harness: Real persistence with a measured provider stub.
        requested: The extra request fields.
    """
    response = harness.client.post(ROUTE, json={**_payload(), **requested})

    assert response.status_code == 201, response.text
    job = harness.store.get_job(response.json()["optimization_id"], include_payload=True)
    assert job["payload"]["sandbox_provider"] == "vercel"


@pytest.mark.usefixtures("hosted_vercel")
def test_bind_refuses_a_provider_dropped_after_submission(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker bind refuses a stored provider the deployment no longer offers, before any box opens.

    Args:
        monkeypatch: Pytest fixture.
    """
    with pytest.raises(UnpricedOperationError, match="not offered"):
        bind_protected_sandbox(_Gateway(), _hosted_settings(monkeypatch), owner_id="job-1", provider="daytona")
