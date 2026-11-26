"""Verify paid Continue deduplication and current evidence ownership at submission."""

from __future__ import annotations

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
from core.api.routers import execution_budgets
from core.api.routers import submissions as submission_router
from core.api.routers import wizard_preflight as preflight_router
from core.api.routers.execution_budgets import create_execution_budgets_router
from core.billing.budgets import BudgetConflictError
from core.billing.operation_pricing import ChargePolicy, operation_quote
from core.billing.protected_credentials import (
    ProtectedCredentialVault,
    protect_execution_credentials,
)
from core.billing.runtime import PaidResult, UsagePendingError
from core.config import settings
from core.i18n import t_locale
from core.i18n_keys import I18nKey
from core.models import BlackboxRunRequest
from core.service_gateway.optimization.blackbox.preflight import verify_anything_in_sandbox
from core.service_gateway.optimization.blackbox.sandbox import LocalSubprocessRuntime
from core.storage.models import Base, BillingCustomerModel, BillingProviderKeyModel
from core.storage.preflights import PreflightStore, setup_seed
from core.worker.repo_gate import GateFailure, check_repository
from core.worker.repo_staging import APT_PACKAGES_KEY, StagedRepository
from core.worker.tests.test_repo_staging import (
    _BUILT_IMAGE,
    _FakeBuilder,
    _FakeRegistry,
    _ImageRuntime,
    _ImageStore,
    _tree,
)

PAYLOAD = {
    "seed_candidate": "A real starting candidate",
    "scorer": {"kind": "python", "metric_code": "def score(candidate): return 1.0"},
    "reflection_model_config": {"name": "fixture/text"},
    "strategy": {"mode": "single", "engine": "gepa"},
    "max_cost_cents": 20,
    "target": {"kind": "repo", "repo": {"repository": "o/r", "editable_paths": ["src"]}},
}

# Kept before the fixture replaces it, for tests that need the real parent check.
_REAL_VERIFY_ANYTHING = wizard_preflight._verify_anything


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
    """Serve real evidence and ledger tables around one synthetic paid setup adapter.

    Args:
        tmp_path: Holds the SQLite database.
        monkeypatch: Stands in the paid setup adapter and the app settings.

    Yields:
        The test client, the database engine, and the setup calls it records.
    """
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

    def stage(payload: dict, **kwargs) -> tuple[dict, SimpleNamespace]:
        """Stage no checkout: these scenarios test the paid setup ledger, not the repository."""
        repo = {**payload["target"]["repo"], "environment": {}}
        staged = SimpleNamespace(
            snapshot=SimpleNamespace(archive=tmp_path / "tree.tgz"),
            secrets={},
            box_sources={},
            close_scorers=lambda: None,
            cleanup=lambda: None,
        )
        return {**payload, "target": {**payload["target"], "repo": repo}}, staged

    monkeypatch.setattr(wizard_preflight, "stage_repository", stage)
    monkeypatch.setattr(wizard_preflight, "check_repository", lambda *args, **kwargs: [])
    monkeypatch.setattr(wizard_preflight, "infer_repo_setup", lambda *args, **kwargs: None)
    monkeypatch.setattr(wizard_preflight, "bind_repo_scorer", lambda *args, **kwargs: None)
    monkeypatch.setattr(wizard_preflight, "ModelGateway", _Gateway)
    monkeypatch.setattr(wizard_preflight, "bind_protected_sandbox", lambda *args, **kwargs: None)
    monkeypatch.setattr(wizard_preflight, "_verify_anything", verify)
    monkeypatch.setattr(preflight_router, "enforce_submission_rate", lambda username: None)
    monkeypatch.setattr(wizard_preflight.settings, "openrouter_api_key", SecretStr("test-only"))
    # These scenarios size budgets in cents to test setup holds, so the one-lease floor of a run
    # limit, and the lease a check must leave of it, stay out of the way.
    monkeypatch.setattr(execution_budgets, "CHUNK_CENTS", 1)
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


def test_checked_preflight_runs_the_route_check_for_other_callers(setup_client) -> None:
    """Give the MCP preflight tool the route's own check and evidence, not a copy (SKY-600)."""
    client, engine, calls = setup_client
    budget = _budget(client)
    first = client.post("/wizard/preflight", json=_request(budget)).json()
    request = wizard_preflight.WizardPreflightRequest.model_validate(_request(budget))
    response = preflight_router.checked_preflight(
        request, AuthenticatedUser("alice", "user", ()), SimpleNamespace(engine=engine)
    )
    assert (response.id, response.status, response.may_advance) == (first["id"], "succeeded", True)
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("sources", "unchecked", "codes"),
    [
        ({}, (), []),
        ({"run": ("image", "run@sha256:" + "a" * 64)}, ("run",), ["submit.preflight.run_setup_not_checked"]),
        ({"metric": ("dockerfile", "eval/Dockerfile")}, ("metric",), ["submit.preflight.metric_setup_not_checked"]),
        (
            {"metric": ("dockerfile", "eval/Dockerfile"), "run": ("image", "run@sha256:" + "a" * 64)},
            ("run", "metric"),
            ["submit.preflight.run_setup_not_checked", "submit.preflight.metric_setup_not_checked"],
        ),
    ],
)
def test_a_role_with_a_user_image_reports_its_setup_as_not_checked(
    setup_client, monkeypatch: pytest.MonkeyPatch, sources: dict, unchecked: tuple, codes: list
) -> None:
    """Skip only the setup of a role with a user image, and report it as not checked (SKY-893).

    Args:
        setup_client: The test client, engine and recorded calls.
        monkeypatch: Pytest fixture that replaces the staging and check steps.
        sources: The box sources of the staged checkout.
        unchecked: The roles the check must not set up.
        codes: The message codes of the skipped setup checks.
    """
    client, _engine, _calls = setup_client
    stage, verify = wizard_preflight.stage_repository, wizard_preflight._verify_anything
    bound: list[dict] = []
    verified: list[dict] = []

    def staged_with_sources(payload: dict, **kwargs: Any) -> tuple[dict, SimpleNamespace]:
        """Stage the fixture checkout with this case's box sources.

        Args:
            payload: The run request to stage.
            **kwargs: The other staging arguments.

        Returns:
            The protected payload and the staged checkout.
        """
        protected, staged = stage(payload, **kwargs)
        staged.box_sources = sources
        return protected, staged

    def recorded_verify(gateway: Any, payload: dict, **kwargs: Any) -> dict:
        """Record the roles the guest check skips.

        Args:
            gateway: The model gateway of the check.
            payload: The run request to check.
            **kwargs: The other check arguments, with ``unchecked``.

        Returns:
            The result of the real check.
        """
        verified.append(kwargs)
        return verify(gateway, payload, **kwargs)

    monkeypatch.setattr(wizard_preflight, "stage_repository", staged_with_sources)
    monkeypatch.setattr(wizard_preflight, "bind_repo_scorer", lambda *args, **kwargs: bound.append(kwargs))
    monkeypatch.setattr(wizard_preflight, "_verify_anything", recorded_verify)
    body = client.post("/wizard/preflight", json=_request(_budget(client)), headers={"Accept-Language": "en"}).json()
    assert bound[0]["skip_setup"] == unchecked
    assert verified[0]["unchecked"] == unchecked
    skipped = [check for check in body["checks"] if check["status"] == "skipped"]
    assert [(check["key"], check["message_code"]) for check in skipped] == [("setup", code) for code in codes]
    assert [check["message"] for check in skipped] == [wizard_preflight.SETUP_NOT_CHECKED[role] for role in unchecked]
    assert (body["status"], body["may_advance"]) == ("succeeded", True)


def test_the_setup_check_builds_and_pays_for_the_apt_image_and_checks_setup_in_it(
    setup_client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Build the generated [run] image as the run does, charge it, and check the run setup in it (SKY-967).

    Args:
        setup_client: The test client, engine and recorded calls.
        monkeypatch: Pytest fixture that replaces the staging, build and check steps.
        tmp_path: Holds the fetched tree.
    """
    client, _engine, _calls = setup_client
    # The check deletes the staged folder, and tmp_path also holds the database.
    workdir = tmp_path / "staged"
    workdir.mkdir()
    staged = StagedRepository(
        snapshot=SimpleNamespace(archive=_tree(workdir, {"src/app.py": "x = 1\n"})),
        secrets={},
        workdir=workdir,
    )
    builder, runtime = _FakeBuilder(), _ImageRuntime()
    charges: list[str] = []
    bound: list[tuple[StagedRepository, dict]] = []
    verify = wizard_preflight._verify_anything
    verified: list[dict] = []

    class _ImageGateway(_Gateway):
        """Give the check a sandbox runtime that starts user images, and record each charge."""

        def __init__(self, budget_runtime: Any) -> None:
            """Keep the real spending authority and record the key of each paid call.

            Args:
                budget_runtime: The setup budget passed by the real API.
            """
            super().__init__(budget_runtime)
            execute = budget_runtime.execute

            def recorded(*args: Any, **kwargs: Any) -> Any:
                """Record the operation key, then pay through the real ledger.

                Args:
                    *args: The paid call's arguments.
                    **kwargs: The paid call's keyword arguments, with ``operation_key``.

                Returns:
                    What the real ledger returns.
                """
                charges.append(kwargs["operation_key"])
                return execute(*args, **kwargs)

            budget_runtime.execute = recorded

        def parent_sandbox_runtime(self) -> _ImageRuntime:
            """Return the runtime that starts the image check box.

            Returns:
                The fake runtime.
            """
            return runtime

    def stage(payload: dict, **kwargs: Any) -> tuple[dict, StagedRepository]:
        """Stage a real tree with no [run] image, on Skynet's default image.

        Args:
            payload: The run request to stage.
            **kwargs: The other staging arguments.

        Returns:
            The protected payload and the staged tree.
        """
        repo = {**payload["target"]["repo"], "environment": {}}
        protected = {**payload, "target": {**payload["target"], "repo": repo}}
        protected["_budget_gateway_descriptor"] = {"image": "skynet-default@sha256:" + "d" * 64}
        return protected, staged

    def infer(payload: dict, *args: Any) -> None:
        """Answer as a setup model that names one Debian package.

        Args:
            payload: The protected payload, updated in place.
            *args: The staged tree and the gateway, not used.
        """
        payload["target"][APT_PACKAGES_KEY] = ["gcc"]

    def recorded_verify(gateway: Any, payload: dict, **kwargs: Any) -> dict:
        """Record the roles the guest check skips.

        Args:
            gateway: The model gateway of the check.
            payload: The run request to check.
            **kwargs: The other check arguments, with ``unchecked``.

        Returns:
            The result of the fixture check.
        """
        verified.append(kwargs)
        return verify(gateway, payload, **kwargs)

    monkeypatch.setattr(wizard_preflight, "ModelGateway", _ImageGateway)
    monkeypatch.setattr(wizard_preflight, "stage_repository", stage)
    monkeypatch.setattr(wizard_preflight, "infer_repo_setup", infer)
    monkeypatch.setattr(wizard_preflight, "image_builder", lambda settings: builder)
    monkeypatch.setattr(wizard_preflight, "image_registry", lambda settings: _FakeRegistry({"sha256:" + "b" * 64}))
    monkeypatch.setattr(wizard_preflight, "get_job_store", _ImageStore)
    monkeypatch.setattr(
        wizard_preflight, "bind_repo_scorer", lambda payload, staged, *args, **kwargs: bound.append((staged, kwargs))
    )
    monkeypatch.setattr(wizard_preflight, "_verify_anything", recorded_verify)
    body = client.post("/wizard/preflight", json=_request(_budget(client))).json()

    checked, kwargs = bound[0]
    assert len(builder.requests) == 1
    assert [key for key in charges if key.startswith("image-build:")] == [f"image-build:{kwargs['owner_id']}:run"]
    assert kwargs["owner_id"].startswith("preflight-")
    assert checked.box_sources["run"][0] == "apt"
    assert checked.box_user_images["run"].reference == _BUILT_IMAGE
    assert [check.commands[0] for check in runtime.checks] == ["ascent check --image --role candidate"]
    assert kwargs["skip_setup"] == ()
    assert verified[0]["unchecked"] == ()
    assert (body["status"], body["may_advance"]) == ("succeeded", True)


@pytest.mark.parametrize("scope", ["evaluation", "execution"])
def test_a_check_where_both_roles_use_a_user_image_succeeds_on_its_other_checks(
    setup_client, monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    """Accept an all-image check as passed: the run checks both setups in the images (SKY-893).

    The setups and the sample score are skipped. An execution check still
    runs the engine and model route checks.

    Args:
        setup_client: The test client, engine and recorded calls.
        monkeypatch: Pytest fixture that replaces the staging and sandbox steps.
        scope: The scope of the check.
    """
    client, _engine, _calls = setup_client
    stage = wizard_preflight.stage_repository
    routes: list[bool] = []

    def staged_with_images(payload: dict, **kwargs: Any) -> tuple[dict, SimpleNamespace]:
        """Stage the fixture checkout with a user image for both roles.

        Args:
            payload: The run request to stage.
            **kwargs: The other staging arguments.

        Returns:
            The protected payload and the staged checkout.
        """
        protected, staged = stage(payload, **kwargs)
        staged.box_sources = {"run": ("image", "run@sha256:" + "a" * 64), "metric": ("dockerfile", "eval/Dockerfile")}
        return protected, staged

    def guest(payload: dict, _artifact_id: str, events: Any, _start_method: str) -> None:
        """Run the real guest check on the payload the parent sends.

        Args:
            payload: The guest payload, with its ``_preflight`` settings.
            _artifact_id: The artifact id, not used.
            events: The queue that gets the check result.
            _start_method: The process start method, not used.
        """
        preflight = payload.pop("_preflight")
        result = verify_anything_in_sandbox(
            payload,
            scope=preflight["scope"],
            identity=preflight["identity"],
            runtime=LocalSubprocessRuntime(),
            unchecked=tuple(preflight["unchecked"]),
        )
        events.put({"type": "preflight_result", "result": result})

    def model_routes(_gateway: Any, *, native: bool) -> list[dict]:
        """Record that the model routes are checked.

        Args:
            _gateway: The model gateway, not used.
            native: Whether the check uses the native engines.

        Returns:
            One passed model route check.
        """
        routes.append(native)
        return [{"key": "model.optimization", "status": "succeeded"}]

    monkeypatch.setattr(wizard_preflight, "stage_repository", staged_with_images)
    monkeypatch.setattr(wizard_preflight, "_verify_anything", _REAL_VERIFY_ANYTHING)
    monkeypatch.setattr(wizard_preflight, "run_vercel_dspy", guest)
    monkeypatch.setattr(wizard_preflight, "_verify_model_routes", model_routes)
    monkeypatch.setattr(
        wizard_preflight, "_engine_readiness", lambda _request: {"key": "optimizer", "status": "succeeded"}
    )
    body = client.post("/wizard/preflight", json={**_request(_budget(client)), "scope": scope}).json()
    performed = [(check["key"], check["status"]) for check in body["checks"]]
    skipped = [("setup", "skipped"), ("setup", "skipped"), ("scorer", "skipped")]
    if scope == "evaluation":
        assert performed == skipped
        assert routes == []
    else:
        assert performed == [*skipped, ("model.optimization", "succeeded"), ("optimizer", "succeeded")]
        assert routes == [True]
    assert (body["status"], body["may_advance"]) == ("succeeded", True)


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


def test_preflight_shows_every_gate_failure_before_any_paid_setup(
    setup_client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Refuse an unenforceable ascent.toml in the setup check, in the user's language, before the setup model runs."""
    client, _engine, calls = setup_client
    archive = tmp_path / "tree.tgz"
    raw = (
        b'version = 1\n[target.planner]\nentry = "agents/planner.py:plan"\nnetwork = ["api.example.com"]\n'
        b'[services.maps]\nurl = "https://maps.example.com"\n'
        b'headers = { Authorization = "Bearer ${GOOGLE_MAPS_API_KEY}" }\n'
    )
    with tarfile.open(archive, "w:gz") as packed:
        info = tarfile.TarInfo("ascent.toml")
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
    monkeypatch.setattr(wizard_preflight, "check_repository", check_repository)
    monkeypatch.setattr(wizard_preflight, "infer_repo_setup", lambda *args: inferred.append(args))

    response = client.post("/wizard/preflight", json={**_request(_budget(client)), "locale": "he"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert [(check["key"], check["message_code"]) for check in body["checks"]] == [
        ("setup", "repo_gate.unenforced_cloud"),
        ("setup", "repo_gate.missing_secret"),
    ]
    assert body["checks"][0]["message"] == t_locale("he", "repo_gate.unenforced_cloud", key="[target.planner] network")
    assert not inferred
    assert not calls
    assert body["budget"]["setup_spent_cents"] == "0"
    assert cleaned == ["scorers", "tree"]


def test_the_rest_setup_check_rewrites_an_mcp_contract_source(
    setup_client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Stage a client's ``mcp`` contract as ``setup_screen`` in the REST setup check, as ``/blackbox/run`` does."""
    client, _engine, calls = setup_client
    archive = tmp_path / "tree.tgz"
    raw = b'version = 1\n[target.planner]\nentry = "agents/planner.py:plan"\nnetwork = ["api.example.com"]\n'
    with tarfile.open(archive, "w:gz") as packed:
        info = tarfile.TarInfo("ascent.toml")
        info.size = len(raw)
        packed.addfile(info, io.BytesIO(raw))
    staged = SimpleNamespace(
        snapshot=SimpleNamespace(archive=archive), secrets={}, close_scorers=lambda: None, cleanup=lambda: None
    )
    sources = []

    def stage(payload: dict, **kwargs) -> tuple[dict, SimpleNamespace]:
        """Record the contract source that reaches staging."""
        sources.append(payload["target"]["repo"]["contract_source"])
        return payload, staged

    monkeypatch.setattr(wizard_preflight.settings, "local_mode", False)
    monkeypatch.setattr(wizard_preflight, "is_repo_payload", lambda payload: True)
    monkeypatch.setattr(wizard_preflight, "stage_repository", stage)
    monkeypatch.setattr(wizard_preflight, "check_repository", check_repository)
    repo = {
        "repository": "example/site",
        "commit": "0" * 40,
        "editable_paths": ["src"],
        "draft_files": {"ascent.toml": "version = 1\n"},
        "draft_base": "0" * 40,
        "contract_source": "mcp",
    }
    payload = {**PAYLOAD, "target": {"kind": "repo", "repo": repo}}

    response = client.post("/wizard/preflight", json=_request(_budget(client), payload))

    assert response.status_code == 200, response.json()
    assert sources == ["setup_screen"]
    assert not calls


def test_a_payload_with_no_repository_reaches_the_gate_with_no_bindings(
    setup_client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The setup check reads secret bindings safely when the payload has no repository mapping (SKY-385)."""
    client, _engine, calls = setup_client
    staged = SimpleNamespace(
        snapshot=SimpleNamespace(archive=tmp_path / "tree.tgz"),
        secrets={},
        close_scorers=lambda: None,
        cleanup=lambda: None,
    )
    seen: list[dict[str, Any]] = []

    def gate(archive: Path, secrets: dict, **kwargs: Any) -> list[GateFailure]:
        """Keep the gate inputs and refuse, so no paid setup runs."""
        seen.append(kwargs)
        return [
            GateFailure("secrets", "cloud", "Secret X is missing.", I18nKey.REPO_GATE_MISSING_SECRET, {"name": "X"})
        ]

    monkeypatch.setattr(wizard_preflight.settings, "local_mode", False)
    monkeypatch.setattr(wizard_preflight, "is_repo_payload", lambda payload: True)
    monkeypatch.setattr(wizard_preflight, "stage_repository", lambda payload, **kwargs: (payload, staged))
    monkeypatch.setattr(wizard_preflight, "check_repository", gate)

    response = client.post("/wizard/preflight", json=_request(_budget(client)))

    assert response.status_code == 200
    body = response.json()
    assert [check.get("message_code") for check in body["checks"]] == ["repo_gate.missing_secret"]
    assert [(kwargs["bindings"], kwargs["allowed_hosts"]) for kwargs in seen] == [({}, {})]
    assert not calls


def test_setup_check_keeps_no_lease_of_the_limit_or_the_balance(setup_client, monkeypatch: pytest.MonkeyPatch) -> None:
    """Let a setup check spend into the run's first lease, because setup bills only the wallet (SKY-925)."""
    client, engine, calls = setup_client
    # The balance and the limit each hold exactly one lease, so any setup charge spends into it.
    monkeypatch.setattr(execution_budgets, "CHUNK_CENTS", 20)
    with Session(engine) as session:
        session.get(BillingCustomerModel, "alice").balance_cents = 20
        session.commit()
    budget = _budget(client)

    response = client.post("/wizard/preflight", json=_request(budget))

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status"], body["may_advance"]) == ("succeeded", True)
    assert body["budget"]["setup_spent_cents"] == "1"
    assert body["budget"]["available_cents"] == "20"
    with Session(engine) as session:
        assert session.get(BillingCustomerModel, "alice").balance_cents == 19
    assert len(calls) == 1
