"""Verify dependency resolution uses PyPI and the authenticated account budget."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api import protected_preview
from core.api.auth import AuthenticatedUser, get_authenticated_user
from core.api.routers import scorer_dependencies
from core.api.tests.test_protected_previews import previews as _previews_fixture
from core.api.tests.test_protected_submissions import harness as _harness_fixture


def test_lock_resolves_from_pypi_with_the_callers_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forward PyPI as the index for every account, with the caller's exact authorized budget.

    Args:
        monkeypatch: Fixture replacing paid execution while observing the route contract.
    """
    seen: list[dict[str, Any]] = []

    def execute(payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """Record the protected execution request.

        Args:
            payload: Exact dependency request.
            **kwargs: Authenticated execution context.

        Returns:
            Deterministic adapter result.
        """
        seen.append({**payload, "owner": kwargs["user"].username, "key": kwargs["idempotency_key"]})
        return {"ok": True}

    monkeypatch.setattr(scorer_dependencies, "run_protected_preview", execute)
    monkeypatch.setattr(scorer_dependencies, "enforce_submission_rate", lambda username: None)
    app = FastAPI()
    # The route must not read account storage: an engine-less store proves no index lookup remains.
    app.include_router(scorer_dependencies.create_scorer_dependencies_router(job_store=SimpleNamespace()))
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser("alice", "user", ())
    with TestClient(app) as client:
        body = {"code": "import example", "execution_budget_id": "owned-budget", "execution_budget_revision": 2}
        assert client.post("/wizard/scorer-dependencies", json=body).status_code == 200
        assert client.post("/wizard/scorer-dependencies", json=body).status_code == 200
        assert seen[0]["registry_url"] == "https://pypi.org/simple"
        assert seen[0]["dependency_registry"] == "https://pypi.org/simple"
        assert seen[0]["owner"] == "alice"
        assert seen[0]["execution_budget_id"] == "owned-budget"
        assert seen[0]["seed_candidate"] is None
        assert seen[0]["key"] == seen[1]["key"]
        candidate_body = {**body, "seed_candidate": "import pyrender"}
        assert client.post("/wizard/scorer-dependencies", json=candidate_body).status_code == 200
        assert seen[-1]["seed_candidate"] == "import pyrender"
        assert seen[-1]["key"] != seen[0]["key"]
        app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser("bob", "user", ())
        assert client.post("/wizard/scorer-dependencies", json=body).status_code == 200
        assert seen[-1]["owner"] == "bob"
        assert seen[-1]["registry_url"] == seen[-1]["dependency_registry"] == "https://pypi.org/simple"
        app.dependency_overrides.clear()
        assert client.post("/wizard/scorer-dependencies", json=body).status_code == 401


harness = _harness_fixture
previews = _previews_fixture


@pytest.mark.parametrize("pending", [False, True])
def test_dependency_lock_survives_saved_preview_and_replay(
    previews, monkeypatch: pytest.MonkeyPatch, pending: bool
) -> None:
    """Keep resolved packages visible without repeating paid work while usage settles.

    Args:
        previews: Real storage and billing harness with deterministic execution.
        monkeypatch: Fixture replacing only the package execution adapter.
        pending: Whether the provider retains an unsettled usage reservation.
    """
    harness, state = previews
    state["pending"] = pending
    lock = {"artifacts": [{"name": "example", "version": "1.0"}]}

    def resolve(gateway, payload: dict[str, Any], identity: str, on_token) -> dict[str, Any]:
        """Exercise real protected charging before returning deterministic package evidence.

        Args:
            gateway: Admitted budget gateway.
            payload: Authenticated package request.
            identity: Persisted preview identity.
            on_token: Optional streaming callback.

        Returns:
            Successful package lock, independently of usage settlement.
        """
        state["charge"](gateway.runtime, payload, "dependencies")
        return {"ok": True, "dependency_lock": lock}

    monkeypatch.setattr(protected_preview, "_run_dependencies", resolve)
    harness.client.app.include_router(scorer_dependencies.create_scorer_dependencies_router(job_store=harness.store))
    budget = harness.budgets.create("alice", 20, idempotency_key="packages")
    body = {"code": "import example", "execution_budget_id": budget.id, "execution_budget_revision": budget.revision}
    first = harness.client.post("/wizard/scorer-dependencies", json=body)
    replay = harness.client.post("/wizard/scorer-dependencies", json=body)
    assert first.status_code == replay.status_code == 200
    for response in (first, replay):
        result = response.json()
        assert result["ok"] is True
        assert result["dependency_lock"] == lock
        assert result["preview_status"] == ("pending" if pending else "succeeded")
        assert result["budget"]["pending_operations"] == int(pending)
    assert first.json()["preflight_id"] == replay.json()["preflight_id"]
    assert len(state["calls"]) == 1
