"""Tests for the submission router: ``/optimize`` billing gates, engine catalog and spend ceiling.

The unified-run specifics (staged datasets, seeding, overview keys) live in
``test_unified_submit.py``; the optimizer catalog in ``test_optimizer_catalog.py``.
Both reuse the fake store and client factory defined here.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ...config import Settings
from ...models.common import ModelConfig
from ...service_gateway.optimization.blackbox import service as _bb_service
from ...storage.models import Base, BillingCustomerModel, BillingProviderKeyModel, WalletLedgerModel
from ...storage.remote import RemoteDBJobStore
from ...storage.usage import StorageUsage
from ..errors import DomainError
from ..routers import submissions as _sub_mod
from ..routers.submissions import create_submissions_router
from .conftest import bypass_auth
from .mocks import fake_background_worker

_RUN_CODE = 'def run(artifact, input):\n    return artifact + input["question"]\n'
_METRIC_CODE = (
    "def score(input, output, expected=None):\n"
    '    return float(output == expected), {"feedback": "compare to the answer"}\n'
)


@pytest.mark.parametrize("name", ["gpt-4o", "unknown/model"])
def test_byok_submission_rejects_unknown_provider(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject BYOK models without a resolvable provider."""
    monkeypatch.setattr(_sub_mod, "ProviderKeyVault", lambda **kwargs: object())
    with pytest.raises(DomainError) as error:
        _sub_mod._enforce_byok_connections(
            type("Store", (), {"engine": object()})(), "user", [ModelConfig(name=name, token_source="byok")]
        )
    assert error.value.code == "submission.byok_provider_unsupported"


def test_byok_submission_accepts_bare_model_with_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Accept a bare model when its explicit provider has a verified key."""
    view = SimpleNamespace(provider="openai", api_base=None, status="verified")
    vault = SimpleNamespace(has_verified_connection=lambda *_: True, list_keys=lambda *_: SimpleNamespace(keys=[view]))
    monkeypatch.setattr(_sub_mod, "ProviderKeyVault", lambda **kwargs: vault)
    config = ModelConfig(name="gpt-4o", token_source="byok", byok_provider="openai")
    assert _sub_mod._enforce_byok_connections(SimpleNamespace(engine=object()), "user", [config]) == {"openai": None}


@pytest.mark.parametrize(
    ("provider", "name", "base", "accepted"),
    [
        ("together", "together_ai/model", "https://api.together.xyz/v1", True),
        ("groq", "openai/meta-llama/model", "https://custom.example/v1", False),
        ("anthropic", "openai/gpt-4o", "https://api.anthropic.com/v1", True),
        ("openai", "openai/custom-model", "https://custom.example/v1", True),
    ],
)
def test_byok_submission_checks_saved_endpoint(
    provider: str, name: str, base: str, accepted: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validate saved endpoints against the actual provider route."""
    view = SimpleNamespace(provider=provider, api_base=base, status="verified")
    vault = SimpleNamespace(has_verified_connection=lambda *_: True, list_keys=lambda *_: SimpleNamespace(keys=[view]))
    monkeypatch.setattr(_sub_mod, "ProviderKeyVault", lambda **kwargs: vault)
    store = SimpleNamespace(engine=object())
    config = ModelConfig(name=name, token_source="byok", byok_provider=provider)
    if accepted:
        assert _sub_mod._enforce_byok_connections(store, "user", [config]) == {provider: base}
    else:
        with pytest.raises(DomainError) as error:
            _sub_mod._enforce_byok_connections(store, "user", [config])
        assert error.value.status_code == 400


def test_byok_submission_ignores_caller_endpoint_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the verified connection rather than caller-supplied endpoint overrides."""
    view = SimpleNamespace(provider="anthropic", api_base=None, status="verified")
    vault = SimpleNamespace(has_verified_connection=lambda *_: True, list_keys=lambda *_: SimpleNamespace(keys=[view]))
    monkeypatch.setattr(_sub_mod, "ProviderKeyVault", lambda **kwargs: vault)
    config = ModelConfig(
        name="anthropic/claude-sonnet-4",
        token_source="byok",
        extra={"api_base": "https://x.example.com"},
    )
    assert _sub_mod._enforce_byok_connections(SimpleNamespace(engine=object()), "user", [config]) == {"anthropic": None}


class _FakeJobStore:
    """Keep inspectable rows while exercising real budget and job attachment storage."""

    def __init__(self, *, job_count: int = 0, storage_used: int = 0, storage_quota: int = 1 << 40) -> None:
        """Initialise with canned counts/quota and an empty job map.

        Args:
            job_count: Value returned from ``count_jobs``.
            storage_used: Canned total returned from ``compute_user_storage``.
            storage_quota: Canned budget returned from
                ``get_effective_user_storage_quota`` (defaults high enough that
                the byte gate is a no-op unless a test lowers it).
        """
        self._count = job_count
        self._storage_used = storage_used
        self._storage_quota = storage_quota
        self._jobs: dict[str, dict] = {}

    def _durable_store(self) -> RemoteDBJobStore:
        """Use the configured test database with production job persistence methods."""
        store = object.__new__(RemoteDBJobStore)
        store._engine = self.engine
        store._session_factory = sessionmaker(bind=self.engine)
        return store

    def compute_user_storage(self, username: str) -> StorageUsage:
        """Return the canned unified storage usage for any caller.

        Args:
            username: Ignored; the canned total applies to every user.

        Returns:
            A :class:`StorageUsage` with the canned total and empty breakdown
            (the gate reads only ``.total``).
        """
        return StorageUsage(total=self._storage_used, breakdown={})

    def get_effective_user_storage_quota(self, username: str) -> int:
        """Return the canned storage budget for any caller.

        Args:
            username: Ignored; the canned budget applies to every user.

        Returns:
            The canned byte budget from construction.
        """
        return self._storage_quota

    def count_jobs(self, *, username: str | None = None, **_: Any) -> int:
        """Return the canned job count regardless of filter args.

        Args:
            username: Ignored; preserved to match the real signature.
            **_: Ignored extra filters.

        Returns:
            The canned job count from construction.
        """
        return self._count

    def create_job(
        self,
        optimization_id: str,
        estimated_remaining_seconds: float | None = None,
        *,
        username: str | None = None,
        idempotency_key: str | None = None,
        **authority: Any,
    ) -> dict:
        """Record a new job entry under ``optimization_id``.

        Args:
            optimization_id: Identifier of the new job.
            estimated_remaining_seconds: Ignored; matches the real signature.
            username: Owner stored for idempotency lookups.
            idempotency_key: Optional dedup key stored alongside the row.
            **authority: Budget and preflight references verified in the real attachment transaction.

        Returns:
            The persisted job, including its authoritative execution generation.
        """
        job = self._durable_store().create_job(
            optimization_id,
            estimated_remaining_seconds,
            username=username,
            idempotency_key=idempotency_key,
            **authority,
        )
        self._jobs[optimization_id] = {
            "username": username,
            "idempotency_key": idempotency_key,
            "overview": {},
        }
        return job

    def set_payload_overview(self, optimization_id: str, overview: dict) -> None:
        """Persist an overview dict against an existing job id.

        Args:
            optimization_id: Job id to attach the overview to.
            overview: Overview payload dict (deep-copied).
        """
        self._durable_store().set_payload_overview(optimization_id, overview)
        self._jobs.setdefault(optimization_id, {})["overview"] = dict(overview)

    def update_job(self, optimization_id: str, **kwargs: Any) -> None:
        """Update fields on an existing fake job row.

        Args:
            optimization_id: Job id to update.
            **kwargs: Fields to store on the row.

        Raises:
            KeyError: When the job does not exist.
        """
        self._durable_store().update_job(optimization_id, **kwargs)
        self._jobs[optimization_id].update(kwargs)

    def find_job_by_idempotency_key(self, username: str, idempotency_key: str) -> str | None:
        """Return the first job id matching ``(username, idempotency_key)``.

        Args:
            username: Submitter scope.
            idempotency_key: Client-supplied dedup key.

        Returns:
            The matching optimization id or ``None`` when not present.
        """
        if not username or not idempotency_key:
            return None
        for job_id, row in self._jobs.items():
            if row.get("username") == username and row.get("idempotency_key") == idempotency_key:
                return job_id
        return None

    def get_job(self, optimization_id: str) -> dict:
        """Return a copy of the stored job row plus its rehydrated overview.

        Args:
            optimization_id: Identifier of the job to fetch.

        Returns:
            A dict shaped like the real ``JobRecord`` consumed by the
            idempotency rehydration helper.

        Raises:
            KeyError: When the job does not exist.
        """
        row = self._jobs[optimization_id]
        return {
            "optimization_id": optimization_id,
            "status": row.get("status", "pending"),
            "created_at": row.get("created_at"),
            "payload_overview": row.get("overview", {}),
            "username": row.get("username"),
        }

    def list_jobs(self, *, status: str | None = None, username: str | None = None, **_: Any) -> list[dict]:
        """Return stored jobs filtered by status and owner.

        Args:
            status: Exact status filter; ``None`` matches every status. Rows
                without an explicit status count as ``pending``, matching
                ``get_job``.
            username: Owner filter; ``None`` matches every owner.
            **_: Ignored extra filters (``limit``, ``with_counts``, ...).

        Returns:
            Job rows shaped like ``get_job`` output, in insertion order.
        """
        rows = []
        for job_id, row in self._jobs.items():
            if status is not None and row.get("status", "pending") != status:
                continue
            if username is not None and row.get("username") != username:
                continue
            rows.append(self.get_job(job_id))
        return rows

    def created_ids(self) -> list[str]:
        """Return all job ids that were created via ``create_job``.

        Returns:
            List of optimization ids in insertion order.
        """
        return list(self._jobs.keys())

    def stage_dataset(self, username: str, dataset_filename: str, rows: list[dict[str, Any]]) -> str:
        """Persist staged rows and return an opaque id.

        Args:
            username: Submitter owner.
            dataset_filename: Original filename (kept for diagnostics).
            rows: Non-empty dataset rows.

        Returns:
            Newly minted staged dataset id.

        Raises:
            ValueError: When ``rows`` is empty.
        """
        if not rows:
            raise ValueError("staged dataset rows must be non-empty")
        staged = getattr(self, "_staged", None) or {}
        staged_id = f"staged-{len(staged) + 1}"
        staged[staged_id] = {"username": username, "filename": dataset_filename, "rows": list(rows)}
        self._staged = staged
        return staged_id

    def get_staged_dataset(self, staged_dataset_id: str, username: str) -> list[dict[str, Any]] | None:
        """Return staged rows when owned by ``username``.

        Args:
            staged_dataset_id: Id previously returned by ``stage_dataset``.
            username: Authenticated caller.

        Returns:
            The staged rows, or ``None`` when the id is unknown or owned by
            another user.
        """
        row = getattr(self, "_staged", {}).get(staged_dataset_id)
        if row is None or row["username"] != username:
            return None
        return list(row["rows"])

    def delete_staged_dataset(self, staged_dataset_id: str, username: str) -> bool:
        """Drop a staged row by id when owned by ``username``.

        Args:
            staged_dataset_id: Id to evict.
            username: Authenticated caller.

        Returns:
            ``True`` when a row was deleted.
        """
        staged = getattr(self, "_staged", {})
        row = staged.get(staged_dataset_id)
        if row is None or row["username"] != username:
            return False
        del staged[staged_dataset_id]
        return True


def _billing_engine() -> Any:
    """Build a StaticPool in-memory engine with the billing schema."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _verified_byok_row(monkeypatch: pytest.MonkeyPatch) -> BillingProviderKeyModel:
    """Build a decryptable verified OpenRouter connection for a router test.

    Args:
        monkeypatch: Pytest helper used to install the matching vault key.

    Returns:
        Provider row whose ciphertext is valid for the current test process.
    """
    key = Fernet.generate_key()
    monkeypatch.setattr(_sub_mod.settings, "byok_vault_key", SecretStr(key.decode("utf-8")))
    return BillingProviderKeyModel(
        username="alice",
        provider="openrouter",
        secret_ciphertext=Fernet(key).encrypt(b"fixture-openrouter-key"),
        last4="-key",
        status="verified",
    )


def unified_payload(**overrides: Any) -> dict:
    """Build a minimal valid ``/optimize`` body: a prompt optimized on six pairs.

    Args:
        **overrides: Top-level keys replacing the defaults.

    Returns:
        A dict matching ``UnifiedRunRequest``.
    """
    body: dict[str, Any] = {
        "name": "unified-run",
        "username": "alice",
        "task": {
            "objective": "Answer the question in one word.",
            "background": "",
            "artifact": {"kind": "dspy_program", "starting_version": "Answer briefly: "},
        },
        "data": {
            "mode": "pairs",
            "cases": [{"input": {"question": f"Q{i}?"}, "expected": f"A{i}"} for i in range(6)],
            "input_fields": ["question"],
        },
        "optimizer": {"choice": "dspy_gepa"},
        "models": {"reflection_model": {"name": "openai/gpt-4o-mini"}},
        "workspace": {"run_code": _RUN_CODE, "metric_code": _METRIC_CODE},
    }
    body.update(overrides)
    return body


def make_client(store: _FakeJobStore, *, monkeypatch: pytest.MonkeyPatch, balance_cents: int = 10000) -> TestClient:
    """Build a funded ``/optimize`` client whose sandbox setup check is replaced.

    The paid execution preflight (budget creation plus a sandboxed contract
    run) is exercised by ``test_protected_submissions.py``; here it is a
    no-op so the router's own gates are tested in isolation.

    Args:
        store: Fake job store wired into the router factory.
        monkeypatch: Pytest monkeypatch fixture.
        balance_cents: Wallet balance seeded for ``alice`` when the store has no engine yet.

    Returns:
        A ``TestClient`` over a minimal FastAPI app.
    """
    worker = fake_background_worker()
    if not hasattr(store, "engine"):
        store.engine = _billing_engine()
        with Session(store.engine) as session:
            session.add(
                BillingCustomerModel(username="alice", stripe_customer_id="fixture-alice", balance_cents=balance_cents)
            )
            session.commit()

    monkeypatch.setattr(_sub_mod.settings, "worker_enabled", True)
    monkeypatch.setattr(_sub_mod.settings, "openrouter_api_key", SecretStr("fixture-only"))
    monkeypatch.setattr(_sub_mod, "get_worker", lambda *a, **kw: worker)
    monkeypatch.setattr(_sub_mod, "_ensure_api_budget", lambda *args, **kwargs: None)
    monkeypatch.setattr(_sub_mod, "_protected_submission", lambda *args, **kwargs: None)
    monkeypatch.setattr(_sub_mod, "validate_unified_payload", lambda payload: None)

    app = FastAPI()
    app.include_router(create_submissions_router(service=object(), job_store=store))
    bypass_auth(app)

    @app.exception_handler(HTTPException)
    async def _http_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        """Mirror the production HTTPException handler so ``code``/``params`` round-trip."""
        content: dict[str, object] = {"detail": exc.detail}
        code = getattr(exc, "code", None)
        if code:
            content["code"] = code
            content["params"] = getattr(exc, "params", None) or {}
        return JSONResponse(status_code=exc.status_code, content=content, headers=getattr(exc, "headers", None))

    return TestClient(app, raise_server_exceptions=False)


def _funded_store(**customer: Any) -> _FakeJobStore:
    """Build a fake store whose billing DB holds one ``alice`` customer row.

    Args:
        **customer: Column overrides for the customer row.

    Returns:
        The store with its ``engine`` set.
    """
    engine = _billing_engine()
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username="alice", stripe_customer_id="cus_alice", **{"balance_cents": 0, **customer})
        )
        session.commit()
    store = _FakeJobStore()
    store.engine = engine
    return store


def test_optimize_returns_201_and_enqueues_one_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid unified run is accepted as a pending black-box job and persisted."""
    store = _FakeJobStore()
    client = make_client(store, monkeypatch=monkeypatch)

    resp = client.post("/optimize", json=unified_payload())

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["optimization_type"] == "blackbox"
    assert body["optimizer_name"] == "dspy_gepa"
    assert store.created_ids() == [body["optimization_id"]]
    assert store._jobs[body["optimization_id"]]["payload"]["payload_kind"] == "unified_v1"


def test_optimize_overrides_posted_username(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stored owner is the authenticated caller, not the posted username."""
    store = _FakeJobStore()
    client = make_client(store, monkeypatch=monkeypatch)

    resp = client.post("/optimize", json=unified_payload(username="mallory"))

    assert resp.status_code == 201, resp.text
    assert resp.json()["username"] == "alice"
    assert store._jobs[store.created_ids()[0]]["username"] == "alice"


def test_optimize_returns_402_when_balance_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A managed run is blocked at submit when the account has no spendable balance."""
    store = _funded_store(balance_cents=0, grant_remaining=0)
    client = make_client(store, monkeypatch=monkeypatch)

    resp = client.post("/optimize", json=unified_payload())

    assert resp.status_code == 402
    assert resp.json()["code"] == "billing.insufficient_funds"
    assert store.created_ids() == []


def test_optimize_caps_managed_ceiling_to_balance(monkeypatch: pytest.MonkeyPatch) -> None:
    """A managed run's ``max_cost_cents`` is clamped to the spendable balance."""
    store = _funded_store(balance_cents=0, grant_remaining=200)
    client = make_client(store, monkeypatch=monkeypatch)

    resp = client.post("/optimize", json=unified_payload(max_cost_cents=10_000))

    assert resp.status_code == 201, resp.text
    assert store._jobs[store.created_ids()[0]]["payload"]["max_cost_cents"] == 200


def test_optimize_byok_without_connection_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A BYOK run is refused at submit when the account saved no key for the provider."""
    store = _funded_store(balance_cents=500)
    client = make_client(store, monkeypatch=monkeypatch)
    body = unified_payload(
        token_source="byok",
        models={"reflection_model": {"name": "openrouter/openai/gpt-4o"}},
    )

    resp = client.post("/optimize", json=body)

    assert resp.status_code == 400
    assert resp.json()["code"] == "billing.byok_missing_connection"
    assert store.created_ids() == []


def test_optimize_byok_with_verified_connection_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A BYOK run with a verified provider key is accepted and stamps the per-model source."""
    store = _funded_store(balance_cents=500)
    with Session(store.engine) as session:
        session.add(_verified_byok_row(monkeypatch))
        session.commit()
    client = make_client(store, monkeypatch=monkeypatch)
    body = unified_payload(
        token_source="byok",
        models={"reflection_model": {"name": "openrouter/openai/gpt-4o"}},
    )

    resp = client.post("/optimize", json=body)

    assert resp.status_code == 201, resp.text
    overview = store._jobs[store.created_ids()[0]]["overview"]
    assert overview["token_source"] == "byok"
    assert set(overview["token_sources_by_model"].values()) == {"byok"}


def test_optimize_returns_409_when_over_job_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-user job quota refuses a new run before anything is stored."""
    store = _FakeJobStore(job_count=10_000)
    client = make_client(store, monkeypatch=monkeypatch)

    resp = client.post("/optimize", json=unified_payload())

    assert resp.status_code == 409
    assert store.created_ids() == []


def test_optimize_returns_409_when_over_storage_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run whose payload would exceed the storage quota is refused."""
    store = _FakeJobStore(storage_used=100, storage_quota=100)
    client = make_client(store, monkeypatch=monkeypatch)

    resp = client.post("/optimize", json=unified_payload())

    assert resp.status_code == 409
    assert store.created_ids() == []


def test_optimize_idempotent_retry_returns_same_optimization_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reusing an ``Idempotency-Key`` returns the original job without enqueuing another."""
    store = _FakeJobStore()
    client = make_client(store, monkeypatch=monkeypatch)
    headers = {"Idempotency-Key": "retry-1"}

    first = client.post("/optimize", json=unified_payload(), headers=headers)
    second = client.post("/optimize", json=unified_payload(), headers=headers)

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["optimization_id"] == second.json()["optimization_id"]
    assert len(store.created_ids()) == 1


@pytest.mark.parametrize("path", ["/run", "/grid-search", "/blackbox/scorer/dry-run"])
def test_retired_submit_endpoints_are_gone(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    """The per-family submit routes were replaced by ``/optimize``.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        path: A retired submit path.
    """
    client = make_client(_FakeJobStore(), monkeypatch=monkeypatch)

    assert client.post(path, json={}).status_code in {404, 405}


def test_blackbox_engine_catalog_resolves_availability_per_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expose native engines for either evaluation target when the proposer runtime is available.

    Args:
        monkeypatch: Pytest fixture for deterministic runtime capabilities.
    """
    store = _FakeJobStore()
    client = make_client(store, monkeypatch=monkeypatch)
    monkeypatch.setattr(_bb_service, "agent_target_unavailable_reason", lambda _settings: None)
    monkeypatch.setattr(_bb_service, "native_runtime_unavailable_reason", lambda _runtime, _settings: None)

    text = client.get("/blackbox/engines")
    agent = client.get("/blackbox/engines", params={"target": "agent"})

    assert text.status_code == 200
    assert text.json()["target_kind"] == "text"
    assert text.json()["sandbox_available"] is True
    by_id = {engine["id"]: engine for engine in text.json()["engines"]}
    assert list(by_id) == ["gepa", "best_of_n", "autoresearch", "meta_harness", "autosaddler", "shinka_evolve"]
    assert by_id["gepa"]["available"] is True
    assert by_id["gepa"]["supports_parts"] is True
    assert by_id["gepa"]["checkpoint_recovery_supported"] is True
    assert by_id["gepa"]["checkpoint_recovery_reason"] is None
    assert by_id["autoresearch"]["available"] is True
    assert by_id["autoresearch"]["unavailable_reason"] is None
    assert by_id["autoresearch"]["checkpoint_recovery_supported"] is False
    assert "does not expose" in by_id["autoresearch"]["checkpoint_recovery_reason"]
    assert by_id["meta_harness"]["available"] is True
    assert by_id["meta_harness"]["requires_agent_target"] is False
    assert {key for key, engine in by_id.items() if engine["supports_parts"]} == {
        "gepa",
        "autosaddler",
        "shinka_evolve",
    }
    assert text.json()["auto_engines"] == ["gepa", "autoresearch", "meta_harness", "autosaddler", "shinka_evolve"]
    assert text.json()["auto_available"] is True
    assert text.json()["auto_unavailable_reason"] is None
    assert text.json()["auto_checkpoint_recovery_supported"] is False
    assert "multi-engine" in text.json()["auto_checkpoint_recovery_reason"]
    assert "compatible saved checkpoint" in text.json()["run_recovery_eligibility"]
    assert text.json()["upstream_revision"] == "0632cdb5dcc052e690eab439e1b4a7e3e9cfe407"
    runtimes = {runtime["id"]: runtime for runtime in text.json()["proposer_runtimes"]}
    assert list(runtimes) == ["vercel"]
    assert runtimes["vercel"]["checkpoint_restore_supported"] is True
    assert runtimes["vercel"]["checkpoint_restore_reason"] is None
    assert runtimes["vercel"]["cost"]["billing_basis"] == "at_cost"
    assert agent.status_code == 200
    agent_by_id = {engine["id"]: engine for engine in agent.json()["engines"]}
    assert agent_by_id["meta_harness"]["available"] is True
    assert agent_by_id["meta_harness"]["unavailable_reason"] is None
    assert agent.json()["auto_engines"] == ["gepa", "autoresearch", "meta_harness", "autosaddler", "shinka_evolve"]


def test_blackbox_engine_catalog_surfaces_the_missing_sandbox_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep evaluator sandbox availability separate from native proposer capabilities.

    Args:
        monkeypatch: Pytest fixture for deterministic runtime capabilities.
    """
    store = _FakeJobStore()
    client = make_client(store, monkeypatch=monkeypatch)
    monkeypatch.setattr(_bb_service, "agent_target_unavailable_reason", lambda _settings: "no sandbox here")
    monkeypatch.setattr(_bb_service, "native_runtime_unavailable_reason", lambda _runtime, _settings: None)

    resp = client.get("/blackbox/engines", params={"target": "agent"})

    assert resp.status_code == 200
    assert resp.json()["sandbox_available"] is False
    assert resp.json()["sandbox_reason"] == "no sandbox here"
    by_id = {engine["id"]: engine for engine in resp.json()["engines"]}
    assert by_id["meta_harness"]["available"] is True
    assert by_id["meta_harness"]["unavailable_reason"] is None
    assert by_id["gepa"]["available"] is True


def test_blackbox_engine_catalog_checks_the_managed_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block the full Auto recipe when the managed sandbox cannot launch native engines.

    Args:
        monkeypatch: Pytest fixture for deterministic runtime capabilities.
    """
    client = make_client(_FakeJobStore(), monkeypatch=monkeypatch)
    monkeypatch.setattr(_bb_service, "agent_target_unavailable_reason", lambda _settings: None)
    monkeypatch.setattr(
        _bb_service,
        "native_runtime_unavailable_reason",
        lambda runtime, _settings: "Managed sandbox is missing" if runtime == "vercel" else None,
    )

    catalog = client.get("/blackbox/engines").json()

    assert catalog["auto_engines"] == ["gepa", "autoresearch", "meta_harness", "autosaddler", "shinka_evolve"]
    assert catalog["auto_available"] is False
    assert "Managed sandbox is missing" in catalog["auto_unavailable_reason"]
    assert catalog["proposer_runtimes"][0]["available"] is False
    by_id = {engine["id"]: engine for engine in catalog["engines"]}
    assert by_id["gepa"]["available"] is True
    assert by_id["autoresearch"]["available"] is False
    assert by_id["meta_harness"]["available"] is False


def test_blackbox_engine_catalog_rejects_unknown_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    """A target kind outside text|agent is a 422, not a silent fallback to text."""
    store = _FakeJobStore()
    client = make_client(store, monkeypatch=monkeypatch)

    resp = client.get("/blackbox/engines", params={"target": "robot"})

    assert resp.status_code == 422


def test_blackbox_engine_catalog_ignores_retired_runtime_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep old clients compatible while returning only the managed sandbox.

    Args:
        monkeypatch: Pytest fixture used by the API client factory.
    """
    client = make_client(_FakeJobStore(), monkeypatch=monkeypatch)

    response = client.get("/blackbox/engines", params={"proposer_runtime": "local"})

    assert response.status_code == 200
    assert [runtime["id"] for runtime in response.json()["proposer_runtimes"]] == ["vercel"]


class _EngineStore:
    """Job-store double exposing only the SQL engine the spend ceiling reads."""

    def __init__(self, engine: object) -> None:
        """Store the engine."""
        self.engine = engine


def _ledger_engine(spent_cents: int) -> object:
    """Build an in-memory billing DB whose trailing 24h holds one run charge of ``spent_cents``."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(WalletLedgerModel(username="u@x.io", delta_cents=-spent_cents, kind="run", description="r"))
        session.commit()
    return engine


def test_global_daily_spend_ceiling_defaults_on() -> None:
    """The platform spend backstop ships enabled at 5000 cents ($50) rather than disabled."""
    assert Settings.model_fields["global_daily_spend_ceiling_cents"].default == 5000


def test_global_daily_spend_ceiling_refuses_loudly_when_reached(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Hitting the ceiling refuses with a 503 and logs at ERROR so operators are alerted."""
    monkeypatch.setattr(_sub_mod.settings, "global_daily_spend_ceiling_cents", 100)
    with pytest.raises(DomainError) as err:
        _sub_mod._enforce_global_daily_spend_ceiling(_EngineStore(_ledger_engine(100)))
    assert err.value.status_code == 503
    assert err.value.code == "submission.capacity_reached"
    assert any(r.levelname == "ERROR" and "spend ceiling" in r.getMessage() for r in caplog.records)


def test_global_daily_spend_ceiling_admits_below_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spend under the ceiling passes the gate."""
    monkeypatch.setattr(_sub_mod.settings, "global_daily_spend_ceiling_cents", 100)
    _sub_mod._enforce_global_daily_spend_ceiling(_EngineStore(_ledger_engine(99)))
