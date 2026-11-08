"""Verify paid Continue deduplication and current evidence ownership at submission."""

from __future__ import annotations

import copy
import io
import tarfile
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from core.api import preflight_execution as wizard_preflight
from core.api.auth import AuthenticatedUser, get_authenticated_user
from core.api.errors import DomainError
from core.api.model_billing import normalize_model_token_sources
from core.api.routers import submissions as submission_router
from core.api.routers import wizard_preflight as preflight_router
from core.api.routers.execution_budgets import create_execution_budgets_router
from core.billing.budgets import BudgetConflictError
from core.billing.byok_vault import ProviderKeyVault
from core.billing.model_gateway import ROUTE_KEY
from core.billing.operation_pricing import ChargePolicy, operation_quote
from core.billing.protected_credentials import (
    ProtectedCredentialVault,
    protect_execution_credentials,
)
from core.billing.runtime import PaidResult, UsagePendingError
from core.config import settings
from core.i18n import t_locale
from core.models import BlackboxRunRequest
from core.storage.models import Base, BillingCustomerModel, BillingProviderKeyModel
from core.storage.preflights import PreflightStore, setup_seed

PAYLOAD = {
    "seed_candidate": "A real starting candidate",
    "scorer": {"kind": "python", "metric_code": "def score(candidate): return 1.0"},
    "reflection_model_config": {"name": "fixture/text"},
    "strategy": {"mode": "single", "engine": "gepa"},
    "max_cost_cents": 20,
}


class _Gateway:
    """Replace only external transport while keeping the real spending authority."""

    def __init__(self, runtime) -> None:
        """Retain the setup budget passed by the real API."""
        self.runtime = runtime

    def protect_payload(self, payload: dict, **kwargs) -> dict:
        """Return test inputs without provisioning model credentials."""
        return payload

    def close(self) -> None:
        """Complete the deterministic test transport."""


@pytest.fixture(autouse=True)
def _at_cost_markup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the usage markup to 1.0 so these tests check settlement arithmetic, not pricing policy."""
    monkeypatch.setattr(settings, "usage_markup", 1.0)


@pytest.fixture
def setup_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, object, list]]:
    """Serve real evidence and ledger tables around one synthetic paid setup adapter."""
    engine = create_engine(f"sqlite:///{tmp_path / 'setup.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username="alice", stripe_customer_id="local", balance_cents=100, grant_remaining=0)
        )
        session.commit()
    calls = []

    def verify(gateway, payload, **kwargs) -> dict:
        """Settle a measured setup charge using the production admission path."""
        calls.append(payload)
        policy = ChargePolicy("managed_model")
        quote = operation_quote({"case": len(calls)}, Decimal("0.02"), policy, {"provider": "fixture"})
        gateway.runtime.execute(
            quote,
            policy,
            lambda: PaidResult(None, Decimal("0.01"), {"actual": "0.01"}),
            operation_key=f"check-{len(calls)}",
            cost_kind="model",
        )
        return {"checks": [{"key": "scorer", "status": "succeeded"}]}

    monkeypatch.setattr(wizard_preflight, "ModelGateway", _Gateway)
    monkeypatch.setattr(wizard_preflight, "bind_protected_sandbox", lambda *args, **kwargs: None)
    monkeypatch.setattr(wizard_preflight, "_verify_anything", verify)
    monkeypatch.setattr(preflight_router, "enforce_submission_rate", lambda username: None)
    monkeypatch.setattr(wizard_preflight.settings, "openrouter_api_key", SecretStr("test-only"))
    app = FastAPI()

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, error: HTTPException) -> JSONResponse:
        """Expose the same domain error fields as the application."""
        return JSONResponse(
            status_code=error.status_code, content={"detail": error.detail, "code": getattr(error, "code", None)}
        )

    store = SimpleNamespace(engine=engine)
    app.include_router(create_execution_budgets_router(job_store=store))
    app.include_router(preflight_router.create_wizard_preflight_router(job_store=store))
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser("alice", "user", ())
    with TestClient(app) as client:
        yield client, engine, calls
    engine.dispose()


def _budget(client: TestClient) -> dict:
    """Create one funded draft authority for the API scenarios."""
    return client.post("/execution-budgets", json={"total_cents": 20}, headers={"Idempotency-Key": "draft"}).json()


def _request(budget: dict, payload: dict | None = None) -> dict:
    """Build the same Continue request shape used by both wizard workflows."""
    return {
        "scope": "execution",
        "workflow": "anything",
        "payload": payload or PAYLOAD,
        "execution_budget_id": budget["id"],
        "execution_budget_revision": budget["revision"],
    }


def test_continue_reuses_matching_success_and_preserves_setup_spend(setup_client) -> None:
    """Reuse current evidence across retries and cosmetic edits without another paid operation."""
    client, _engine, calls = setup_client
    budget = _budget(client)
    first = client.post("/wizard/preflight", json=_request(budget))
    assert first.status_code == 200
    assert first.json()["status"] == "succeeded"
    assert first.json()["may_advance"] is True
    assert first.json()["budget"]["setup_spent_cents"] == "1"
    second = client.post("/wizard/preflight", json=_request(budget, {**PAYLOAD, "name": "Renamed", "is_private": True}))
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["budget"]["setup_spent_cents"] == "1"
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("model", "base", "novelty", "expected"),
    [
        ("anthropic/claude-sonnet-4", None, True, "submission.byok_novelty_unsupported"),
    ],
)
def test_preflight_rejects_invalid_byok_before_execution(
    setup_client, monkeypatch: pytest.MonkeyPatch, model: str, base: str | None, novelty: bool, expected: str
) -> None:
    """Return localized BYOK admission errors before setup work starts."""
    client, _engine, calls = setup_client
    view = SimpleNamespace(provider="anthropic", api_base=base, status="verified")
    vault = SimpleNamespace(has_verified_connection=lambda *_: True, list_keys=lambda *_: SimpleNamespace(keys=[view]))
    monkeypatch.setattr(submission_router, "ProviderKeyVault", lambda **kwargs: vault)
    payload = {
        **PAYLOAD,
        "reflection_model_config": {"name": model, "token_source": "byok", "byok_provider": "anthropic"},
        "strategy": {"mode": "single", "engine": "shinka_evolve"},
        "shinka": {"novelty": novelty},
    }
    response = client.post("/wizard/preflight", json={**_request(_budget(client), payload), "locale": "he"})
    assert response.status_code == 400
    assert response.json()["code"] == expected
    assert response.json()["detail"] == t_locale("he", expected, model=model, provider="anthropic")
    assert not calls


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("groq", "openai/gpt-oss-120b"),
        ("together", "google/gemma-3-27b-it"),
        ("novita", "deepseek/deepseek-r1"),
        ("openrouter", "openai/gpt-4o"),
        ("openrouter", "anthropic/claude-sonnet-4"),
    ],
)
def test_preflight_accepts_native_provider_namespace(
    setup_client, monkeypatch: pytest.MonkeyPatch, provider: str, model: str
) -> None:
    """Pass native provider IDs with foreign namespaces through admission."""
    client, _engine, calls = setup_client
    view = SimpleNamespace(provider=provider, api_base=None, status="verified")
    vault = SimpleNamespace(has_verified_connection=lambda *_: True, list_keys=lambda *_: SimpleNamespace(keys=[view]))
    monkeypatch.setattr(submission_router, "ProviderKeyVault", lambda **kwargs: vault)
    monkeypatch.setattr(wizard_preflight, "prepare_protected_credentials", lambda payload, **kwargs: payload)
    payload = {**PAYLOAD, "reflection_model_config": {"name": model, "token_source": "byok", "byok_provider": provider}}
    response = client.post("/wizard/preflight", json=_request(_budget(client), payload))
    assert response.status_code == 200
    assert calls


def test_preflight_rejects_openrouter_prefix_with_native_provider(
    setup_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject an OpenRouter prefix before starting setup with a native key."""
    client, _engine, calls = setup_client
    view = SimpleNamespace(provider="openai", api_base=None, status="verified")
    vault = SimpleNamespace(has_verified_connection=lambda *_: True, list_keys=lambda *_: SimpleNamespace(keys=[view]))
    monkeypatch.setattr(submission_router, "ProviderKeyVault", lambda **kwargs: vault)
    payload = {
        **PAYLOAD,
        "reflection_model_config": {
            "name": "openrouter/openai/gpt-4o",
            "token_source": "byok",
            "byok_provider": "openai",
        },
    }
    response = client.post("/wizard/preflight", json=_request(_budget(client), payload))
    assert response.status_code == 400
    assert response.json()["code"] == "billing.byok_model_not_served"
    assert not calls


def test_preflight_execution_preserves_domain_error(setup_client, monkeypatch: pytest.MonkeyPatch) -> None:
    """Preserve a domain error's message and code in setup evidence."""
    client, _engine, _calls = setup_client

    def reject(*args, **kwargs):
        """Simulate a domain refusal inside the protected setup."""
        raise DomainError("submission.byok_novelty_unsupported", status=400)

    monkeypatch.setattr(_Gateway, "protect_payload", reject)
    response = client.post("/wizard/preflight", json={**_request(_budget(client)), "locale": "he"})
    assert response.status_code == 200
    check = response.json()["checks"][0]
    assert check["message_code"] == "submission.byok_novelty_unsupported"
    assert check["message"] == t_locale("he", "submission.byok_novelty_unsupported")


def test_evaluation_preflight_checks_supplied_reflection_route(setup_client, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject unsupported novelty in a partial Anything evaluation payload."""
    client, _engine, calls = setup_client
    view = SimpleNamespace(provider="anthropic", api_base=None, status="verified")
    vault = SimpleNamespace(has_verified_connection=lambda *_: True, list_keys=lambda *_: SimpleNamespace(keys=[view]))
    monkeypatch.setattr(submission_router, "ProviderKeyVault", lambda **kwargs: vault)
    payload = {
        **PAYLOAD,
        "reflection_model_config": {"name": "anthropic/claude-sonnet-4", "token_source": "byok"},
        "shinka": {"novelty": True},
    }
    response = client.post("/wizard/preflight", json={**_request(_budget(client), payload), "scope": "evaluation"})
    assert response.status_code == 400
    assert response.json()["code"] == "submission.byok_novelty_unsupported"
    assert not calls


def test_preflight_accepts_openai_custom_endpoint(setup_client, monkeypatch: pytest.MonkeyPatch) -> None:
    """Admit an OpenAI-compatible custom endpoint before setup."""
    client, _engine, calls = setup_client
    view = SimpleNamespace(provider="openai", api_base="https://custom.example/v1", status="verified")
    vault = SimpleNamespace(has_verified_connection=lambda *_: True, list_keys=lambda *_: SimpleNamespace(keys=[view]))
    monkeypatch.setattr(submission_router, "ProviderKeyVault", lambda **kwargs: vault)
    budget = _budget(client)

    def run(request, user, store):
        """Confirm the admitted setup reaches the executor."""
        calls.append(request)
        return wizard_preflight.WizardPreflightResponse(
            id="test", fingerprint="test", status="succeeded", may_advance=True, checks=[], budget=budget
        )

    monkeypatch.setattr(preflight_router, "run_preflight", run)
    payload = {**PAYLOAD, "reflection_model_config": {"name": "openai/custom-model", "token_source": "byok"}}
    response = client.post("/wizard/preflight", json=_request(budget, payload))
    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"
    assert len(calls) == 1


def test_preflight_accepts_together_bare_slash_model(setup_client, monkeypatch: pytest.MonkeyPatch) -> None:
    """Admit a namespaced Together model with an explicit provider."""
    client, _engine, calls = setup_client
    view = SimpleNamespace(provider="together", api_base=None, status="verified")
    vault = SimpleNamespace(has_verified_connection=lambda *_: True, list_keys=lambda *_: SimpleNamespace(keys=[view]))
    monkeypatch.setattr(submission_router, "ProviderKeyVault", lambda **kwargs: vault)
    budget = _budget(client)

    def run(request, user, store):
        """Record the admitted request without provider traffic."""
        calls.append(request)
        return wizard_preflight.WizardPreflightResponse(
            id="test", fingerprint="test", status="succeeded", may_advance=True, checks=[], budget=budget
        )

    monkeypatch.setattr(preflight_router, "run_preflight", run)
    payload = {
        **PAYLOAD,
        "reflection_model_config": {
            "name": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
            "token_source": "byok",
            "byok_provider": "together",
        },
    }
    response = client.post("/wizard/preflight", json=_request(budget, payload))
    assert response.status_code == 200
    assert len(calls) == 1


def test_byok_continue_needs_no_managed_model_key(setup_client, monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve the owner's verified key and keep managed provider funding out of a BYOK setup."""
    client, engine, calls = setup_client
    cipher_key = Fernet.generate_key()
    monkeypatch.setattr(wizard_preflight.settings, "byok_vault_key", SecretStr(cipher_key.decode()))
    monkeypatch.setattr(wizard_preflight.settings, "openrouter_api_key", None)
    with Session(engine) as session:
        session.add(
            BillingProviderKeyModel(
                username="alice",
                provider="openrouter",
                secret_ciphertext=Fernet(cipher_key).encrypt(b"account-owned-key"),
                last4="-key",
                status="verified",
                params={},
            )
        )
        session.commit()
    budget = _budget(client)
    payload = {
        **PAYLOAD,
        "token_source": "byok",
        "reflection_model_config": {
            "name": "openrouter/fixture/text",
            "token_source": "byok",
            "byok_provider": "openrouter",
        },
    }

    response = client.post("/wizard/preflight", json=_request(budget, payload))

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "succeeded"
    assert calls[0]["reflection_model_config"]["extra"]["api_key"] == "account-owned-key"


def test_scorer_changes_require_new_paid_verification(setup_client) -> None:
    """Bind executable changes to new checks while preserving the same cumulative budget."""
    client, _engine, calls = setup_client
    budget = _budget(client)
    first = client.post("/wizard/preflight", json=_request(budget)).json()
    changed = {**PAYLOAD, "scorer": {"kind": "python", "metric_code": "def score(candidate): return 0.5"}}
    second = client.post("/wizard/preflight", json=_request(budget, changed)).json()
    assert second["fingerprint"] != first["fingerprint"]
    assert second["budget"]["setup_spent_cents"] == "2"
    assert len(calls) == 2


def test_submission_rejects_stale_or_foreign_evidence(setup_client) -> None:
    """Revalidate account, content, and current provider identity before budget attachment."""
    client, engine, _calls = setup_client
    budget = _budget(client)
    checked = client.post("/wizard/preflight", json=_request(budget)).json()
    typed = BlackboxRunRequest.model_validate(
        {
            **PAYLOAD,
            "username": "alice",
            "seed": setup_seed(budget["id"]),
            "execution_budget_id": budget["id"],
            "execution_budget_revision": budget["revision"],
        }
    )
    normalize_model_token_sources(typed)
    payload = protect_execution_credentials(
        typed.model_dump(mode="json", by_alias=True),
        username="alice",
        binding_id=budget["id"],
        vault=ProtectedCredentialVault(engine=engine),
    )
    store = PreflightStore(engine)
    kwargs = {
        "username": "alice",
        "budget_id": budget["id"],
        "identity": checked["id"],
        "fingerprint": checked["fingerprint"],
        "workflow": "anything",
        "payload": payload,
    }
    store.require_current(**kwargs)
    with pytest.raises(BudgetConflictError):
        store.require_current(**{**kwargs, "username": "bob"})
    with pytest.raises(BudgetConflictError):
        store.require_current(**{**kwargs, "payload": {**payload, "seed_candidate": "Changed"}})


@pytest.mark.parametrize("key", ["_gepa_log_dir", "_budget_gateway_descriptor", "_skynet_tools_route", "_preflight"])
def test_public_preflight_rejects_parent_runtime_controls(setup_client, key: str) -> None:
    """Reject forged filesystem and relay authority before claiming setup or spending funds."""
    client, _engine, calls = setup_client
    budget = _budget(client)
    response = client.post("/wizard/preflight", json=_request(budget, {**PAYLOAD, key: "/"}))
    assert response.status_code == 422
    assert "Runtime control fields" in response.text
    assert calls == []
    assert client.get(f"/execution-budgets/{budget['id']}").json()["setup_spent_cents"] == "0"


def test_unresolved_paid_usage_blocks_stage_advancement(setup_client, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the wizard on its current stage until provider or sandbox usage settles."""
    client, _engine, _calls = setup_client
    budget = _budget(client)

    def pending(_gateway: Any, _payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        """Simulate a paid setup call whose final receipt is unavailable.

        Args:
            _gateway: Unused protected model gateway.
            _payload: Unused canonical setup payload.
            **_kwargs: Unused scope and evidence identity.

        Raises:
            UsagePendingError: Always, to model unresolved provider usage.
        """
        raise UsagePendingError("The final provider receipt is unavailable.")

    monkeypatch.setattr(wizard_preflight, "_verify_anything", pending)
    response = client.post("/wizard/preflight", json=_request(budget))
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "pending"
    assert result["may_advance"] is False
    assert result["pending_reason"]["category"] == "usage_reconciliation"
    assert any(check["key"] == "usage" and check["status"] == "pending" for check in result["checks"])


@pytest.mark.parametrize(
    ("metric_call", "expected_model"),
    [("llm(output, model='fixture/judge')", "fixture/judge"), ("llm(output)", "fixture/text")],
)
def test_unified_metric_model_gets_a_metered_route(
    setup_client, monkeypatch: pytest.MonkeyPatch, metric_call: str, expected_model: str
) -> None:
    """Route a unified run's metric model like the worker does, then hand the guest no legacy aliases."""
    client, _engine, _calls = setup_client
    seen: list[dict] = []

    class _RoutingGateway(_Gateway):
        """Stamp a route on the legacy scorer key only, as the real seam does."""

        def protect_payload(self, payload: dict, **kwargs) -> dict:
            """Mark the model reachable through the legacy ``scorer`` key."""
            result = copy.deepcopy(payload)
            scorer = result.get("scorer")
            if isinstance(scorer, dict) and isinstance(scorer.get("model"), dict):
                scorer["model"].setdefault("extra", {})[ROUTE_KEY] = {"url": "http://parent", "token": "t"}
            return result

    def verify(gateway, payload, **kwargs) -> dict:
        """Record what the guest would receive."""
        seen.append(payload)
        return {"checks": [{"key": "metric.contract", "status": "succeeded"}]}

    monkeypatch.setattr(wizard_preflight, "ModelGateway", _RoutingGateway)
    monkeypatch.setattr(wizard_preflight, "_verify_unified", verify)
    budget = _budget(client)
    payload = {
        "task": {
            "objective": "Answer in one word.",
            "artifact": {"kind": "dspy_program", "starting_version": "Answer: "},
        },
        "data": {
            "mode": "pairs",
            "cases": [{"input": {"question": f"Q{index}?"}, "expected": "A"} for index in range(10)],
            "input_fields": ["question"],
        },
        "optimizer": {"choice": "dspy_gepa"},
        "models": {
            "reflection_model": {"name": "fixture/text"},
            "task_model": {"name": "fixture/task"},
        },
        "workspace": {
            "run_code": "def run(artifact, input):\n    return artifact + input['question']\n",
            "metric_code": f"from skynet import llm\ndef score(input, output, expected=None):\n    return float({metric_call} == expected), {{}}\n",
        },
        "max_cost_cents": 20,
    }
    response = client.post("/wizard/preflight", json={**_request(budget, payload), "workflow": "unified"})
    assert response.status_code == 200, response.text
    assert ROUTE_KEY in seen[0]["models"]["metric_model"]["extra"]
    assert seen[0]["models"]["metric_model"]["name"] == expected_model
    assert not {"scorer", "task_model_config", "reflection_model_config"} & seen[0].keys()


def test_unified_byok_roles_are_aliased_before_setup_protection(setup_client, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep task, reflection, and BYOK metric aliases available at setup protection."""
    client, engine, _calls = setup_client
    monkeypatch.setattr(settings, "byok_vault_key", SecretStr(Fernet.generate_key().decode()))
    monkeypatch.setattr("core.billing.byok_vault._pinned_address", lambda *args, **kwargs: "8.8.8.8")
    monkeypatch.setattr(ProviderKeyVault, "_probe", lambda *args, **kwargs: "verified")
    ProviderKeyVault(engine=engine).save_key("alice", "openai", "metric-private", api_base="https://public.example/v1")
    events: list[str] = []
    alias = wizard_preflight.alias_unified_model_roles

    def alias_spy(payload: dict) -> None:
        """Record when the setup adds its aliases."""
        events.append("alias")
        alias(payload)

    class _RoutingGateway(_Gateway):
        """Assert the protected boundary receives every aliased model."""

        def protect_payload(self, payload: dict, **kwargs) -> dict:
            """Check the roles and the resolved BYOK metric key."""
            events.append("protect")
            assert payload["task_model_config"] is payload["models"]["task_model"]
            assert payload["reflection_model_config"] is payload["models"]["reflection_model"]
            metric = payload["scorer"]["model"]
            assert metric is payload["models"]["metric_model"]
            assert metric["token_source"] == "byok"
            assert metric["extra"]["api_key"] == "metric-private"
            result = copy.deepcopy(payload)
            result["scorer"]["model"]["extra"].pop("api_key")
            return result

    def verify(_gateway, payload, **kwargs) -> dict:
        """Finish the setup without executing a sandbox."""
        assert not {"scorer", "task_model_config", "reflection_model_config"} & payload.keys()
        return {"checks": [{"key": "metric.contract", "status": "succeeded"}]}

    monkeypatch.setattr(wizard_preflight, "alias_unified_model_roles", alias_spy)
    monkeypatch.setattr(wizard_preflight, "ModelGateway", _RoutingGateway)
    monkeypatch.setattr(wizard_preflight, "_verify_unified", verify)
    payload = {
        "task": {"objective": "Answer.", "artifact": {"kind": "dspy_program", "starting_version": "Answer: "}},
        "data": {
            "mode": "pairs",
            "cases": [{"input": {"question": "Q"}, "expected": "A"}, {"input": {"question": "Q2"}, "expected": "A2"}],
            "input_fields": ["question"],
        },
        "optimizer": {"choice": "dspy_gepa"},
        "models": {
            "task_model": {"name": "fixture/task"},
            "reflection_model": {"name": "fixture/reflection"},
            "metric_model": {"name": "openai/judge", "token_source": "byok", "byok_provider": "openai"},
        },
        "workspace": {
            "run_code": "def run(artifact, input): return artifact",
            "metric_code": "def score(input, output, expected=None): return 1.0, {}",
        },
        "max_cost_cents": 20,
    }
    response = client.post("/wizard/preflight", json={**_request(_budget(client), payload), "workflow": "unified"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "succeeded"
    assert events == ["alias", "protect"]


def test_preflight_shows_every_gate_failure_before_any_paid_setup(
    setup_client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Refuse an unenforceable optimize.toml in the setup check, in the user's language, before the setup model runs."""
    client, _engine, calls = setup_client
    archive = tmp_path / "tree.tgz"
    raw = (
        b'[proposer]\nnetwork = []\n[services.maps]\nurl = "https://maps.example.com"\n'
        b'headers = { Authorization = "Bearer ${GOOGLE_MAPS_API_KEY}" }\n'
    )
    with tarfile.open(archive, "w:gz") as packed:
        info = tarfile.TarInfo("optimize.toml")
        info.size = len(raw)
        packed.addfile(info, io.BytesIO(raw))
    cleaned = []
    staged = SimpleNamespace(
        snapshot=SimpleNamespace(archive=archive),
        secrets={},
        close_scorers=lambda: cleaned.append("scorers"),
        cleanup=lambda: cleaned.append("tree"),
    )
    inferred = []
    monkeypatch.setattr(wizard_preflight.settings, "local_mode", False)
    monkeypatch.setattr(wizard_preflight, "is_repo_payload", lambda payload: True)
    monkeypatch.setattr(wizard_preflight, "stage_repository", lambda payload, **kwargs: (payload, staged))
    monkeypatch.setattr(wizard_preflight, "infer_repo_setup", lambda *args: inferred.append(args))

    response = client.post("/wizard/preflight", json={**_request(_budget(client)), "locale": "he"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert [(check["key"], check["message_code"]) for check in body["checks"]] == [
        ("setup", "repo_gate.unenforced_cloud"),
        ("setup", "repo_gate.unenforced_cloud"),
        ("setup", "repo_gate.missing_secret"),
    ]
    assert body["checks"][0]["message"] == t_locale("he", "repo_gate.unenforced_cloud", key="[proposer] network")
    assert not inferred
    assert not calls
    assert body["budget"]["setup_spent_cents"] == "0"
    assert cleaned == ["scorers", "tree"]
