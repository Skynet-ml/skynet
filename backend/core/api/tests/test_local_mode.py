"""Tests for the local-mode API: hidden routes, the local session, and submission gates."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ...billing.service import FREE_GRANT_CENTS, free_grant_cents
from ...config import settings
from ...local.cli_probe import clear_probe_cache
from ...models.blackbox import BlackboxProposer
from ...registry.core import ServiceRegistry
from ...storage.models import (
    AgentEnginePreferenceModel,
    BillingCustomerModel,
    ExecutionBudgetModel,
    UserIdentityModel,
    UserModel,
)
from .. import agent_engine
from ..agent_engine import fill_proposer_host_cli
from ..app import create_app
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..model_billing import enforce_local_mode_sources, enforce_raw_local_mode_sources
from ..preflight_execution import WizardPreflightRequest, _canonical_execution_payload
from ..routers import submissions as _sub_mod
from ..routers import wizard_preflight
from ..routers.accounts import create_accounts_router
from ..routers.local_runtime import create_local_runtime_router
from .conftest import bypass_auth
from .test_agent_engine import _CODEX_ID, _status
from .test_protected_submissions import _Harness, _payload
from .test_protected_submissions import harness as _harness_fixture
from .test_submissions_router import (
    _billing_engine,
    _FakeJobStore,
    _verified_byok_row,
    make_client,
    unified_payload,
)

harness = _harness_fixture

_SECRET = "test-internal-secret"
_REAL_ENSURE_API_BUDGET = _sub_mod._ensure_api_budget
_LOCAL_PATHS = ("/local/status", "/auth/local/session")
_SUBSCRIPTION_MODELS = {
    "reflection_model": {
        "name": "anthropic/claude-sonnet-4.6",
        "token_source": "subscription",
        "cli_provider": "claude",
    }
}


class _Store:
    """Minimal store exposing only the SQLAlchemy engine the account routes need."""

    def __init__(self, engine: Any) -> None:
        """Hold the engine the accounts router opens sessions on.

        Args:
            engine: A SQLAlchemy engine with the account tables created.
        """
        self.engine = engine


@pytest.fixture
def local_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Build a client for the account and local-status routers.

    Args:
        monkeypatch: Pytest fixture used to set the shared secret and CLI paths.

    Returns:
        A ``TestClient`` over in-memory SQLite. Local mode stays off until a
        test turns it on.
    """
    monkeypatch.setattr(settings, "backend_auth_secret", SecretStr(_SECRET))
    for name in ("codex_bin", "claude_bin", "docker_bin"):
        monkeypatch.setattr(settings, name, "/nonexistent/skynet-test-binary")
    clear_probe_cache()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    UserModel.__table__.create(engine)
    UserIdentityModel.__table__.create(engine)
    store = _Store(engine)
    app = FastAPI()
    app.include_router(create_accounts_router(job_store=store))
    app.include_router(create_local_runtime_router())
    bypass_auth(app)
    return TestClient(app)


def test_local_routes_are_404_outside_local_mode(local_client: TestClient) -> None:
    """Hosted deployments answer 404 on both local-only routes."""
    status = local_client.get("/local/status")
    session = local_client.post("/auth/local/session", headers={"X-Internal-Auth": _SECRET})

    assert status.status_code == 404
    assert session.status_code == 404


def test_local_session_signs_in_the_local_user(local_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The right secret returns the local account; the first call creates it.

    Args:
        local_client: Client over the account routes.
        monkeypatch: Pytest fixture used to turn on local mode.
    """
    monkeypatch.setattr(settings, "local_mode", True)

    first = local_client.post("/auth/local/session", headers={"X-Internal-Auth": _SECRET})
    second = local_client.post("/auth/local/session", headers={"X-Internal-Auth": _SECRET})

    assert first.status_code == 200, first.text
    assert first.json() == {
        "email": "local@skynet.local",
        "name": "local@skynet.local",
        "role": "user",
        "first_login": True,
    }
    assert second.json()["first_login"] is False


def test_local_account_has_no_password_login(local_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The local row has an empty hash, so ``/auth/login`` never opens it.

    Args:
        local_client: Client over the account routes.
        monkeypatch: Pytest fixture used to turn on local mode.
    """
    monkeypatch.setattr(settings, "local_mode", True)
    local_client.post("/auth/local/session", headers={"X-Internal-Auth": _SECRET})

    login = local_client.post(
        "/auth/login",
        json={"email": "local@skynet.local", "password": ""},
        headers={"X-Internal-Auth": _SECRET},
    )

    assert login.status_code != 200


def test_local_session_rejects_a_wrong_secret(local_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A wrong or missing internal secret gets 403.

    Args:
        local_client: Client over the account routes.
        monkeypatch: Pytest fixture used to turn on local mode.
    """
    monkeypatch.setattr(settings, "local_mode", True)

    wrong = local_client.post("/auth/local/session", headers={"X-Internal-Auth": "wrong"})
    missing = local_client.post("/auth/local/session")

    assert wrong.status_code == 403
    assert missing.status_code == 403


def test_local_status_reports_docker_and_clis(local_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """In local mode the status route reports each dependency.

    Args:
        local_client: Client over the local-status route.
        monkeypatch: Pytest fixture used to turn on local mode.
    """
    monkeypatch.setattr(settings, "local_mode", True)

    body = local_client.get("/local/status").json()

    assert body["local_mode"] is True
    assert body["docker"]["available"] is False
    assert "Docker CLI not found" in body["docker"]["error"]
    assert body["sandbox_image"] is None
    assert [cli["id"] for cli in body["clis"]] == ["codex", "claude"]
    assert all(cli["path"] is None for cli in body["clis"])


def test_app_hides_local_routes_from_openapi() -> None:
    """The full app serves both routes but keeps them out of the schema."""
    with (
        patch("core.api.app.get_job_store"),
        patch("core.api.app.get_worker"),
        patch("core.api.app.DspyService"),
    ):
        application = create_app(registry=ServiceRegistry())

    served = {getattr(route, "path", None) for route in application.routes}
    schema_paths = set(application.openapi()["paths"])

    assert set(_LOCAL_PATHS) <= served
    assert not set(_LOCAL_PATHS) & schema_paths


def _full_app(monkeypatch: pytest.MonkeyPatch, *, local_mode: bool) -> FastAPI:
    """Build the full app with local mode set before ``create_app`` reads it.

    Args:
        monkeypatch: Pytest fixture used to set local mode.
        local_mode: Whether the app starts in local mode.

    Returns:
        The assembled application with its stores and worker stubbed.
    """
    monkeypatch.setattr(settings, "local_mode", local_mode)
    with (
        patch("core.api.app.get_job_store"),
        patch("core.api.app.get_worker"),
        patch("core.api.app.DspyService"),
    ):
        return create_app(registry=ServiceRegistry())


@pytest.mark.parametrize("host", ["127.0.0.1:48100", "localhost:48100", "127.0.0.1", "localhost:8000"])
def test_local_mode_serves_loopback_host_names(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    """The desktop app and local dev reach the API as 127.0.0.1 or localhost.

    Args:
        monkeypatch: Pytest fixture used to set local mode.
        host: The Host header the browser or the web server sends.
    """
    client = TestClient(_full_app(monkeypatch, local_mode=True))

    assert client.get("/openapi.json", headers={"host": host}).status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "evil.example:48100", "127.0.0.1.evil.example", "[::1]:48100", ""])
def test_local_mode_refuses_a_foreign_host(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    """A DNS-rebound page keeps its own name in Host, so the API refuses it.

    Args:
        monkeypatch: Pytest fixture used to set local mode.
        host: A Host header that does not name this machine's loopback.
    """
    client = TestClient(_full_app(monkeypatch, local_mode=True))

    assert client.get("/openapi.json", headers={"host": host}).status_code == 400
    assert client.post("/auth/local/session", headers={"host": host}).status_code == 400


def test_hosted_mode_does_not_check_the_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hosted deployments sit behind their own load balancer host rules.

    Args:
        monkeypatch: Pytest fixture used to keep local mode off.
    """
    client = TestClient(_full_app(monkeypatch, local_mode=False))

    assert client.get("/openapi.json", headers={"host": "api-staging.skynetml.com"}).status_code == 200


def _local_submit_client(monkeypatch: pytest.MonkeyPatch, *, local_mode: bool) -> tuple[TestClient, _FakeJobStore]:
    """Build an ``/optimize`` client that runs the real budget step.

    The billing database has no wallet for ``alice``, so the account has a $0
    Stripe balance. The setup check itself is replaced; its behavior is
    covered by ``test_protected_submissions.py``.

    Args:
        monkeypatch: Pytest fixture used to set local mode and the stubs.
        local_mode: Whether the deployment runs in local mode.

    Returns:
        The client and its fake store.
    """
    store = _FakeJobStore()
    store.engine = _billing_engine()
    client = make_client(store, monkeypatch=monkeypatch)
    monkeypatch.setattr(_sub_mod.settings, "local_mode", local_mode)
    monkeypatch.setattr(_sub_mod, "_ensure_api_budget", _REAL_ENSURE_API_BUDGET)
    # No CLI is ready unless a test says so, so the host's own CLIs never fill the proposer.
    monkeypatch.setattr(agent_engine, "probe_clis", lambda _settings: [])
    monkeypatch.setattr(
        _sub_mod,
        "run_preflight",
        lambda *args, **kwargs: SimpleNamespace(
            pending_reason=None, status="succeeded", id="preflight-fixture", fingerprint="fixture"
        ),
    )
    return client, store


def test_local_subscription_run_creates_an_uncapped_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """In local mode a subscription run with a $0 balance is accepted, not refused with 402.

    Args:
        monkeypatch: Pytest fixture used to set local mode.
    """
    client, store = _local_submit_client(monkeypatch, local_mode=True)

    resp = client.post("/optimize", json=unified_payload(token_source="subscription", models=_SUBSCRIPTION_MODELS))

    assert resp.status_code == 201, resp.text
    with Session(store.engine) as session:
        budget = session.scalars(select(ExecutionBudgetModel)).one()
        wallet = session.get(BillingCustomerModel, "alice")
    assert budget.username == "alice"
    assert budget.uncapped is True
    assert wallet is not None
    assert wallet.balance_cents == 0
    payload = store._jobs[resp.json()["optimization_id"]]["payload"]
    assert payload["token_source"] == "subscription"
    assert payload["execution_budget_id"] == budget.id


def test_local_subscription_run_naming_droid_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run that names Droid gets 422 with a clear message and creates no job.

    Args:
        monkeypatch: Pytest fixture used to set local mode.
    """
    client, store = _local_submit_client(monkeypatch, local_mode=True)
    models = {"reflection_model": {**_SUBSCRIPTION_MODELS["reflection_model"], "cli_provider": "droid"}}

    resp = client.post("/optimize", json=unified_payload(token_source="subscription", models=models))

    assert resp.status_code == 422, resp.text
    assert "Droid is no longer supported" in resp.text
    assert store.created_ids() == []


def test_subscription_run_is_refused_outside_local_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hosted deployment refuses the same submission with 422.

    Args:
        monkeypatch: Pytest fixture used to keep local mode off.
    """
    client, store = _local_submit_client(monkeypatch, local_mode=False)

    resp = client.post("/optimize", json=unified_payload(token_source="subscription", models=_SUBSCRIPTION_MODELS))

    assert resp.status_code == 422
    assert resp.json()["code"] == "models.subscription_local_only"
    assert store.created_ids() == []


def test_host_cli_proposer_is_refused_outside_local_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host-CLI proposer on a managed run is still a local-only run.

    Args:
        monkeypatch: Pytest fixture used to keep local mode off.
    """
    client, store = _local_submit_client(monkeypatch, local_mode=False)

    resp = client.post(
        "/optimize", json=unified_payload(optimizer={"choice": "dspy_gepa", "proposer": {"host_cli": "codex"}})
    )

    assert resp.status_code == 422
    assert resp.json()["code"] == "models.subscription_local_only"
    assert store.created_ids() == []


def test_droid_is_not_a_host_cli_option(monkeypatch: pytest.MonkeyPatch) -> None:
    """Droid cannot run isolated, so the submit schema refuses it as a host proposer.

    Args:
        monkeypatch: Pytest fixture used to set local mode.
    """
    client, store = _local_submit_client(monkeypatch, local_mode=True)

    resp = client.post(
        "/optimize",
        json=unified_payload(
            token_source="subscription",
            models=_SUBSCRIPTION_MODELS,
            optimizer={"choice": "dspy_gepa", "proposer": {"host_cli": "droid"}},
        ),
    )

    assert resp.status_code == 422, resp.text
    [issue] = [issue for issue in resp.json()["detail"] if issue["loc"][-1] == "host_cli"]
    assert issue["loc"] == ["body", "optimizer", "proposer", "host_cli"]
    assert "Droid is no longer supported" in issue["msg"]
    assert store.created_ids() == []
    schema = client.get("/openapi.json").json()["components"]["schemas"]["BlackboxProposer"]
    assert schema["properties"]["host_cli"]["anyOf"][0]["enum"] == ["codex", "claude"]


@pytest.mark.parametrize("field", ["execution_runtime", "proposer_runtime"])
def test_docker_runtime_is_refused_outside_local_mode(monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    """A hosted deployment runs only Vercel sandboxes, so a Docker run gets 422.

    Args:
        monkeypatch: Pytest fixture used to switch local mode.
        field: The runtime field that names Docker.
    """
    payload = SimpleNamespace(**{field: "docker"})
    monkeypatch.setattr(settings, "local_mode", False)

    with pytest.raises(DomainError) as refused:
        enforce_local_mode_sources(payload, "byok", [])

    assert refused.value.status_code == 422
    assert refused.value.code == "local.not_available"
    monkeypatch.setattr(settings, "local_mode", True)
    enforce_local_mode_sources(payload, "subscription", [])


def test_managed_run_is_refused_in_local_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Mac app has no managed routing, so a managed model gets 422.

    Args:
        monkeypatch: Pytest fixture used to set local mode.
    """
    client, store = _local_submit_client(monkeypatch, local_mode=True)

    resp = client.post("/optimize", json=unified_payload())

    assert resp.status_code == 422
    assert resp.json()["code"] == "models.managed_unavailable_local"
    assert store.created_ids() == []


def test_own_key_run_is_accepted_in_local_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Mac app runs an optimization on the user's own OpenRouter key, billed as BYOK.

    Args:
        monkeypatch: Pytest fixture used to set local mode and the vault key.
    """
    client, store = _local_submit_client(monkeypatch, local_mode=True)
    with Session(store.engine) as session:
        session.add(_verified_byok_row(monkeypatch))
        session.commit()

    resp = client.post(
        "/optimize",
        json=unified_payload(token_source="byok", models={"reflection_model": {"name": "openrouter/openai/gpt-4o"}}),
    )

    assert resp.status_code == 201, resp.text
    overview = store._jobs[store.created_ids()[0]]["overview"]
    assert overview["token_source"] == "byok"
    assert set(overview["token_sources_by_model"].values()) == {"byok"}


def test_local_mode_skips_the_global_spend_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """The platform-wide ceiling never reads the ledger in local mode.

    Args:
        monkeypatch: Pytest fixture used to set local mode and the ceiling.
    """
    monkeypatch.setattr(_sub_mod.settings, "local_mode", True)
    monkeypatch.setattr(_sub_mod.settings, "global_daily_spend_ceiling_cents", 1)

    _sub_mod._enforce_global_daily_spend_ceiling(SimpleNamespace(engine=object()))


def test_local_mode_grants_the_local_credit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A new local account gets ``local_grant_cents`` instead of the hosted grant.

    Args:
        monkeypatch: Pytest fixture used to set local mode.
    """
    monkeypatch.setattr(settings, "local_grant_cents", 12_345)
    assert free_grant_cents() == FREE_GRANT_CENTS

    monkeypatch.setattr(settings, "local_mode", True)
    assert free_grant_cents() == 12_345


def _execution_request(**overrides: Any) -> WizardPreflightRequest:
    """Build a unified execution setup check around ``unified_payload``.

    Args:
        **overrides: Top-level payload keys replacing the defaults.

    Returns:
        A request in the shape the wizard sends before a run.
    """
    return WizardPreflightRequest(
        scope="execution",
        workflow="unified",
        payload=unified_payload(**overrides),
        execution_budget_id="draft",
        execution_budget_revision=1,
    )


def test_hosted_execution_check_refuses_a_subscription_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hosted setup check must not charge a subscription model as managed.

    Args:
        monkeypatch: Pytest fixture used to keep local mode off.
    """
    monkeypatch.setattr(settings, "local_mode", False)

    with pytest.raises(DomainError) as refused:
        _canonical_execution_payload(_execution_request(models=_SUBSCRIPTION_MODELS), "alice", None)

    assert refused.value.status_code == 422
    assert refused.value.code == "models.subscription_local_only"


def test_hosted_execution_checks_local_mode_before_byok_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the local-only refusal ahead of BYOK admission for a full request."""
    monkeypatch.setattr(settings, "local_mode", False)
    monkeypatch.setattr(wizard_preflight, "enforce_submission_rate", lambda *_: None)
    app = FastAPI()
    app.include_router(wizard_preflight.create_wizard_preflight_router(job_store=object()))
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser("alice", "user", ())
    models = {**_SUBSCRIPTION_MODELS, "task_model": {"name": "bare-model", "token_source": "byok"}}
    with TestClient(app) as client:
        response = client.post("/wizard/preflight", json=_execution_request(models=models).model_dump(mode="json"))
    assert response.status_code == 422
    assert response.json()["detail"] == DomainError("models.subscription_local_only").detail


def test_local_execution_check_keeps_a_subscription_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local mode accepts the same check and keeps its subscription source.

    Args:
        monkeypatch: Pytest fixture used to set local mode.
    """
    monkeypatch.setattr(settings, "local_mode", True)

    payload = _canonical_execution_payload(_execution_request(models=_SUBSCRIPTION_MODELS), "alice", None)

    assert payload["token_source"] == "subscription"


def test_local_execution_check_refuses_a_managed_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local mode refuses a managed check, as it refuses the managed run.

    Args:
        monkeypatch: Pytest fixture used to set local mode.
    """
    monkeypatch.setattr(settings, "local_mode", True)

    with pytest.raises(DomainError) as refused:
        _canonical_execution_payload(_execution_request(), "alice", None)

    assert refused.value.code == "models.managed_unavailable_local"


# The evaluation check validates the scorer before it runs the gate, so these
# requests carry a valid scorer to reach the gate.
_VALID_SCORER = {"kind": "python", "metric_code": "def score(p):\n    return 1\n"}


@pytest.mark.parametrize(
    "payload",
    [
        {"token_source": "subscription"},
        {"proposer": {"host_cli": "codex"}},
        {"task_model_config": {"name": "anthropic/claude-sonnet-4.6", "token_source": "subscription"}},
        {"execution_runtime": "docker"},
        {"proposer_runtime": "docker"},
    ],
)
def test_hosted_wizard_check_refuses_local_only_work(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    """The hosted wizard check route refuses subscription work before it claims a budget.

    The job store is a bare object, so the request would fail if the check
    went past the gate.

    Args:
        monkeypatch: Pytest fixture used to keep local mode off.
        payload: Raw evaluation inputs that only a local deployment can run.
    """
    monkeypatch.setattr(settings, "local_mode", False)
    monkeypatch.setattr(wizard_preflight, "enforce_submission_rate", lambda username: None)
    app = FastAPI()
    app.include_router(wizard_preflight.create_wizard_preflight_router(job_store=object()))
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser("alice", "user", ())

    with TestClient(app) as client:
        response = client.post(
            "/wizard/preflight",
            json={
                "workflow": "anything",
                "scope": "evaluation",
                "payload": {**payload, "scorer": _VALID_SCORER},
                "execution_budget_id": "draft",
                "execution_budget_revision": 1,
            },
        )

    assert response.status_code == 422
    runtime_fields = {"execution_runtime", "proposer_runtime"}
    code = "local.not_available" if runtime_fields & payload.keys() else "models.subscription_local_only"
    assert response.json()["detail"] == DomainError(code).detail


def test_local_wizard_check_passes_the_raw_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local mode lets raw subscription and host-CLI inputs through the gate.

    Args:
        monkeypatch: Pytest fixture used to set local mode.
    """
    monkeypatch.setattr(settings, "local_mode", True)

    enforce_raw_local_mode_sources({"token_source": "subscription", "proposer": {"host_cli": "codex"}})


def _engine_setup(monkeypatch: pytest.MonkeyPatch, store: Any, *, ready: tuple[str, ...], stored: str | None) -> None:
    """Make CLIs look ready and store the caller's engine choice.

    Args:
        monkeypatch: Pytest fixture used to replace the CLI probe.
        store: Job store whose engine holds the engine setting.
        ready: The CLIs the probe reports as ready, in probe order.
        stored: The engine ``alice`` chose in Settings, or ``None`` for no row.
    """
    monkeypatch.setattr(agent_engine, "probe_clis", lambda _settings: [_status(cli) for cli in ready])
    if stored is not None:
        with Session(store.engine) as session:
            session.add(AgentEnginePreferenceModel(username="alice", engine=stored))
            session.commit()


def _stored_proposer(store: _FakeJobStore, resp: Any) -> dict[str, Any]:
    """Return the proposer settings the accepted run stored.

    Args:
        store: The fake job store.
        resp: The accepted ``/optimize`` response.

    Returns:
        The stored ``optimizer.proposer`` object.
    """
    assert resp.status_code == 201, resp.text
    return store._jobs[resp.json()["optimization_id"]]["payload"]["optimizer"]["proposer"]


@pytest.mark.parametrize(
    ("ready", "stored", "expected"),
    [(("codex", "claude"), None, "codex"), (("codex",), "claude", "claude")],
)
def test_local_proposer_follows_the_agent_engine(
    monkeypatch: pytest.MonkeyPatch, ready: tuple[str, ...], stored: str | None, expected: str
) -> None:
    """A local run with no proposer CLI pick runs its proposer on the engine CLI.

    Args:
        monkeypatch: Pytest fixture used to set local mode and the probe.
        ready: The CLIs the probe reports as ready.
        stored: The engine stored in Settings, if any.
        expected: The proposer CLI the stored run must name.
    """
    client, store = _local_submit_client(monkeypatch, local_mode=True)
    _engine_setup(monkeypatch, store, ready=ready, stored=stored)

    resp = client.post("/optimize", json=unified_payload(token_source="subscription", models=_SUBSCRIPTION_MODELS))

    assert _stored_proposer(store, resp)["host_cli"] == expected


@pytest.mark.parametrize(("pick", "expected"), [("claude", "claude"), (None, None)])
def test_explicit_proposer_pick_overrides_the_agent_engine(
    monkeypatch: pytest.MonkeyPatch, pick: str | None, expected: str | None
) -> None:
    """A per-run pick, including an explicit sandbox proposer, wins over the engine.

    Args:
        monkeypatch: Pytest fixture used to set local mode and the probe.
        pick: The run's explicit ``host_cli``.
        expected: The proposer CLI the stored run must name.
    """
    client, store = _local_submit_client(monkeypatch, local_mode=True)
    _engine_setup(monkeypatch, store, ready=("codex", "claude"), stored="codex")

    resp = client.post(
        "/optimize",
        json=unified_payload(
            token_source="subscription",
            models=_SUBSCRIPTION_MODELS,
            optimizer={"choice": "dspy_gepa", "proposer": {"host_cli": pick}},
        ),
    )

    assert _stored_proposer(store, resp)["host_cli"] == expected


def _save_openrouter_key(monkeypatch: pytest.MonkeyPatch, store: Any, status: str | None) -> None:
    """Save an OpenRouter key for ``alice`` with the given status.

    Args:
        monkeypatch: Pytest fixture used to install the vault key.
        store: Job store whose engine holds the key vault.
        status: The key's status, or ``None`` to save no key.
    """
    if status is None:
        return
    row = _verified_byok_row(monkeypatch)
    row.status = status
    with Session(store.engine) as session:
        session.add(row)
        session.commit()


@pytest.mark.parametrize("key", [None, "unverified", "invalid"])
def test_local_proposer_follows_the_default_cli_when_the_openrouter_key_is_gone(
    monkeypatch: pytest.MonkeyPatch, key: str | None
) -> None:
    """A stored OpenRouter engine with no verified key runs the proposer on the default CLI.

    Settings shows that CLI in this state ("agents run on Codex"), so the
    proposer must not stay in the sandbox.

    Args:
        monkeypatch: Pytest fixture used to set local mode and the probe.
        key: The status of the caller's saved OpenRouter key, or ``None`` for no key.
    """
    client, store = _local_submit_client(monkeypatch, local_mode=True)
    _engine_setup(monkeypatch, store, ready=("codex", "claude"), stored="openrouter")
    _save_openrouter_key(monkeypatch, store, key)

    resp = client.post("/optimize", json=unified_payload(token_source="subscription", models=_SUBSCRIPTION_MODELS))

    assert _stored_proposer(store, resp)["host_cli"] == "codex"


@pytest.mark.parametrize(
    ("ready", "key", "expected"),
    [
        (("codex", "claude"), None, _CODEX_ID),
        (("codex", "claude"), "unverified", _CODEX_ID),
        (("codex", "claude"), "verified", None),
        ((), None, None),
    ],
)
def test_local_agent_turn_with_no_model_follows_the_default_cli_when_the_openrouter_key_is_gone(
    monkeypatch: pytest.MonkeyPatch, ready: tuple[str, ...], key: str | None, expected: str | None
) -> None:
    """A stored OpenRouter engine with no verified key fills an empty agent pick from the default CLI.

    With the verified key the pick stays empty, so the router runs it on the
    key. With no ready CLI it also stays empty, and the router refuses it.

    Args:
        monkeypatch: Pytest fixture used to set local mode and the probe.
        ready: The CLIs the probe reports as ready.
        key: The status of the caller's saved OpenRouter key, or ``None`` for no key.
        expected: The filled model, or ``None`` for an unchanged empty pick.
    """
    _client, store = _local_submit_client(monkeypatch, local_mode=True)
    _engine_setup(monkeypatch, store, ready=ready, stored="openrouter")
    _save_openrouter_key(monkeypatch, store, key)

    assert agent_engine.engine_agent_model(store, "alice", None) == expected


@pytest.mark.parametrize(
    ("ready", "stored", "key", "proposer"),
    [
        (("codex",), "openrouter", "verified", {}),
        ((), "openrouter", None, {}),
        ((), None, None, {}),
        (("codex",), None, None, {"harness": "custom", "run_command": "my-agent --model {model}"}),
    ],
)
def test_local_proposer_stays_in_the_sandbox_without_an_engine_cli(
    monkeypatch: pytest.MonkeyPatch,
    ready: tuple[str, ...],
    stored: str | None,
    key: str | None,
    proposer: dict[str, Any],
) -> None:
    """The OpenRouter engine with its key, no ready CLI, or a custom harness leaves ``host_cli`` unset.

    Args:
        monkeypatch: Pytest fixture used to set local mode and the probe.
        ready: The CLIs the probe reports as ready.
        stored: The engine stored in Settings, if any.
        key: The status of the caller's saved OpenRouter key, or ``None`` for no key.
        proposer: The run's proposer settings.
    """
    client, store = _local_submit_client(monkeypatch, local_mode=True)
    _engine_setup(monkeypatch, store, ready=ready, stored=stored)
    _save_openrouter_key(monkeypatch, store, key)

    resp = client.post(
        "/optimize",
        json=unified_payload(
            token_source="subscription",
            models=_SUBSCRIPTION_MODELS,
            optimizer={"choice": "dspy_gepa", "proposer": proposer},
        ),
    )

    assert _stored_proposer(store, resp)["host_cli"] is None


def test_hosted_proposer_ignores_the_agent_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hosted mode never probes a CLI or reads the setting for a proposer.

    Args:
        monkeypatch: Pytest fixture used to keep local mode off.
    """
    store = _FakeJobStore()
    client = make_client(store, monkeypatch=monkeypatch)
    monkeypatch.setattr(settings, "local_mode", False)

    def _no_probe(_settings: Any) -> list[Any]:
        """Fail the test if hosted mode probes a CLI."""
        raise AssertionError("hosted mode probed a CLI")

    monkeypatch.setattr(agent_engine, "probe_clis", _no_probe)

    resp = client.post("/optimize", json=unified_payload())

    assert _stored_proposer(store, resp)["host_cli"] is None


def test_local_execution_check_fills_the_proposer_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """The setup check fills the same proposer CLI as the run, so its fingerprint matches.

    Args:
        monkeypatch: Pytest fixture used to set local mode and the probe.
    """
    monkeypatch.setattr(settings, "local_mode", True)
    store = SimpleNamespace(engine=_billing_engine())
    _engine_setup(monkeypatch, store, ready=("codex", "claude"), stored="claude")

    payload = _canonical_execution_payload(_execution_request(models=_SUBSCRIPTION_MODELS), "alice", store)

    assert payload["optimizer"]["proposer"]["host_cli"] == "claude"


def test_blackbox_proposer_follows_the_agent_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """A black-box request's top-level proposer follows the engine too.

    Args:
        monkeypatch: Pytest fixture used to set local mode and the probe.
    """
    monkeypatch.setattr(settings, "local_mode", True)
    monkeypatch.setattr(agent_engine, "probe_clis", lambda _settings: [_status("claude")])
    request = SimpleNamespace(proposer=BlackboxProposer())

    fill_proposer_host_cli(None, "alice", request)

    assert request.proposer.host_cli == "claude"


@pytest.mark.parametrize("route", ["/wizard/preflight", "/wizard/preflight/stream"])
def test_local_wizard_check_evidence_starts_a_run_on_the_engine_cli(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """The wizard check fills the proposer CLI like the run, so its evidence starts the run.

    Args:
        harness: Real setup, wallet, and job services.
        monkeypatch: Pytest fixture used to set local mode and the probe.
        route: The plain or streamed wizard check route.
    """
    monkeypatch.setattr(settings, "local_mode", True)
    monkeypatch.setattr(agent_engine, "probe_clis", lambda _settings: [_status("claude")])
    budget = harness.budgets.create(harness.username, 20, idempotency_key="engine")
    payload = {
        **_payload(),
        "token_source": "subscription",
        "models": _SUBSCRIPTION_MODELS,
        "execution_budget_id": budget.id,
        "execution_budget_revision": budget.revision,
    }

    response = harness.client.post(
        route,
        json={
            "scope": "execution",
            "workflow": "unified",
            "payload": payload,
            "execution_budget_id": budget.id,
            "execution_budget_revision": budget.revision,
        },
    )
    assert response.status_code == 200, response.text
    frames = [
        json.loads(line.removeprefix("data: ")) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    checked = frames[-1] if route.endswith("/stream") else response.json()
    assert checked["status"] == "succeeded", checked
    assert harness.calls[0]["payload"]["optimizer"]["proposer"]["host_cli"] == "claude"

    submitted = harness.client.post(
        "/optimize",
        json={
            **payload,
            "execution_budget_revision": checked["budget"]["revision"],
            "preflight_id": checked["id"],
            "preflight_fingerprint": checked["fingerprint"],
        },
    )

    assert submitted.status_code == 201, submitted.text
    job = harness.store.get_job(submitted.json()["optimization_id"], include_payload=True)
    assert job["payload"]["optimizer"]["proposer"]["host_cli"] == "claude"


def test_local_wizard_check_evidence_survives_a_cli_readiness_change(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A submit that sends the CLI its check resolved keeps that check current.

    The wizard reuses a passed check. If the engine CLI it resolved stops being
    ready before submit, the server must not resolve another CLI and refuse
    the evidence as a budget conflict on every retry.

    Args:
        harness: Real setup, wallet, and job services.
        monkeypatch: Pytest fixture used to set local mode and the probe.
    """
    monkeypatch.setattr(settings, "local_mode", True)
    monkeypatch.setattr(agent_engine, "probe_clis", lambda _settings: [_status("codex"), _status("claude")])
    budget = harness.budgets.create(harness.username, 20, idempotency_key="engine-change")
    payload = {
        **_payload(),
        "token_source": "subscription",
        "models": _SUBSCRIPTION_MODELS,
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
    assert response.status_code == 200, response.text
    checked = response.json()
    assert checked["status"] == "succeeded", checked
    monkeypatch.setattr(agent_engine, "probe_clis", lambda _settings: [_status("claude")])
    optimizer = payload["optimizer"]
    proposer = {**optimizer.get("proposer", {}), "host_cli": checked.get("proposer_host_cli")}

    submitted = harness.client.post(
        "/optimize",
        json={
            **payload,
            "optimizer": {**optimizer, "proposer": proposer},
            "execution_budget_revision": checked["budget"]["revision"],
            "preflight_id": checked["id"],
            "preflight_fingerprint": checked["fingerprint"],
        },
    )

    assert submitted.status_code == 201, submitted.text
    job = harness.store.get_job(submitted.json()["optimization_id"], include_payload=True)
    assert job["payload"]["optimizer"]["proposer"]["host_cli"] == "codex"


@pytest.mark.parametrize("route", ["/wizard/preflight", "/wizard/preflight/stream"])
def test_local_wizard_check_without_a_cli_survives_a_new_engine_cli(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """A check that ran with no proposer CLI sends that null back to the submit.

    The routes drop null fields. Without the key, the wizard leaves host_cli
    unset, the submit picks the CLI that became ready after the check, and
    every retry gets a budget conflict.

    Args:
        harness: Real setup, wallet, and job services.
        monkeypatch: Pytest fixture used to set local mode and the probe.
        route: The plain or streamed wizard check route.
    """
    monkeypatch.setattr(settings, "local_mode", True)
    monkeypatch.setattr(agent_engine, "probe_clis", lambda _settings: [])
    budget = harness.budgets.create(harness.username, 20, idempotency_key="no-cli")
    payload = {
        **_payload(),
        "token_source": "subscription",
        "models": _SUBSCRIPTION_MODELS,
        "execution_budget_id": budget.id,
        "execution_budget_revision": budget.revision,
    }

    response = harness.client.post(
        route,
        json={
            "scope": "execution",
            "workflow": "unified",
            "payload": payload,
            "execution_budget_id": budget.id,
            "execution_budget_revision": budget.revision,
        },
    )
    assert response.status_code == 200, response.text
    frames = [
        json.loads(line.removeprefix("data: ")) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    checked = frames[-1] if route.endswith("/stream") else response.json()
    assert checked["status"] == "succeeded", checked
    assert checked["proposer_host_cli"] is None
    assert "pending_reason" not in checked
    monkeypatch.setattr(agent_engine, "probe_clis", lambda _settings: [_status("codex")])
    optimizer = payload["optimizer"]
    proposer = {**optimizer.get("proposer", {}), "host_cli": checked["proposer_host_cli"]}

    submitted = harness.client.post(
        "/optimize",
        json={
            **payload,
            "optimizer": {**optimizer, "proposer": proposer},
            "execution_budget_revision": checked["budget"]["revision"],
            "preflight_id": checked["id"],
            "preflight_fingerprint": checked["fingerprint"],
        },
    )

    assert submitted.status_code == 201, submitted.text
    job = harness.store.get_job(submitted.json()["optimization_id"], include_payload=True)
    assert job["payload"]["optimizer"]["proposer"].get("host_cli") is None
