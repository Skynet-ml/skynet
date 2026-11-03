"""Tests for ``POST /blackbox/run``: repository runs only, stored as legacy black-box runs."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ...constants import OPTIMIZATION_TYPE_BLACKBOX
from ...i18n import t_locale
from ...models.blackbox import BLACKBOX_MODULE_NAME
from ..routers import submissions as _sub_mod
from ..routers import wizard_preflight as _preflight
from .test_submissions_router import _FakeJobStore, make_client


def repo_payload(**overrides: Any) -> dict:
    """Build a valid repository run body.

    Args:
        **overrides: Top-level keys to replace.

    Returns:
        The request body.
    """
    body: dict[str, Any] = {
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
        "reflection_model_config": {"name": "openai/gpt-4o"},
    }
    body.update(overrides)
    return body


@pytest.fixture
def client_and_store(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, _FakeJobStore]:
    """Build a funded client whose scorer load check is a no-op.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        The test client and its fake store.
    """
    store = _FakeJobStore()
    client = make_client(store, monkeypatch=monkeypatch)
    monkeypatch.setattr(_sub_mod, "validate_blackbox_payload", lambda payload: None)
    return client, store


def test_repo_run_is_stored_with_its_target(client_and_store: tuple[Any, _FakeJobStore]) -> None:
    """A repository run keeps repo, commit, editable paths and setup command in the stored payload."""
    client, store = client_and_store

    resp = client.post("/blackbox/run", json=repo_payload())

    assert resp.status_code == 201, resp.json()
    body = resp.json()
    row = store._jobs[body["optimization_id"]]
    target = row["payload"]["target"]
    assert target["kind"] == "repo"
    assert target["repo"]["repository"] == "example/site"
    assert target["repo"]["commit"] == "0" * 40
    assert target["repo"]["editable_paths"] == ["src", "README.md"]
    assert target["setup_command"] == "npm ci"
    assert row["overview"]["optimization_type"] == OPTIMIZATION_TYPE_BLACKBOX
    assert row["overview"]["optimizer_name"] == "gepa"
    assert body["module_name"] == BLACKBOX_MODULE_NAME


def test_non_repository_target_is_rejected(client_and_store: tuple[Any, _FakeJobStore]) -> None:
    """Text targets submit to ``/optimize``; this route turns them away."""
    client, store = client_and_store

    resp = client.post("/blackbox/run", json=repo_payload(target={"kind": "text"}))

    assert resp.status_code == 400
    assert store._jobs == {}


def test_novelty_byok_preflight_and_submit_agree(
    client_and_store: tuple[Any, _FakeJobStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject the same unsupported reflection route at both HTTP boundaries."""
    client, store = client_and_store
    view = SimpleNamespace(provider="anthropic", api_base=None, status="verified")
    vault = SimpleNamespace(has_verified_connection=lambda *_: True, list_keys=lambda *_: SimpleNamespace(keys=[view]))
    monkeypatch.setattr(_sub_mod, "ProviderKeyVault", lambda **kwargs: vault)
    monkeypatch.setattr(_preflight, "enforce_submission_rate", lambda *_: None)
    client.app.include_router(_preflight.create_wizard_preflight_router(job_store=store))
    payload = repo_payload(
        reflection_model_config={"name": "anthropic/claude-sonnet-4", "token_source": "byok"},
        strategy={"mode": "single", "engine": "shinka_evolve"},
        shinka={"novelty": True},
    )
    setup = client.post(
        "/wizard/preflight",
        json={
            "workflow": "anything",
            "scope": "execution",
            "payload": payload,
            "execution_budget_id": "draft",
            "execution_budget_revision": 1,
            "locale": "he",
        },
    )
    submit = client.post("/blackbox/run", json=payload)
    assert setup.status_code == submit.status_code == 400
    assert setup.json()["code"] == submit.json()["code"] == "submission.byok_novelty_unsupported"
    assert setup.json()["detail"] == t_locale("he", "submission.byok_novelty_unsupported")
    assert not store._jobs


def test_scorer_that_does_not_load_is_a_400(
    client_and_store: tuple[Any, _FakeJobStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scorer the validator rejects never queues a job."""
    client, store = client_and_store

    def _reject(payload: Any) -> None:
        """Fail validation the way a broken scorer does.

        Args:
            payload: The request being validated.

        Raises:
            ServiceError: Always.
        """
        raise _sub_mod.ServiceError("scorer does not load")

    monkeypatch.setattr(_sub_mod, "validate_blackbox_payload", _reject)

    resp = client.post("/blackbox/run", json=repo_payload())

    assert resp.status_code == 400
    assert store._jobs == {}
