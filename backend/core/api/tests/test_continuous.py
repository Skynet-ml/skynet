"""Verify continuous optimization against real jobs, preflight and protected budgets."""

from __future__ import annotations

import calendar
import importlib.util
import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import JSON, Column, MetaData, String, Table, create_engine, func, select
from sqlalchemy.orm import Session

from core.api import preflight_execution
from core.api.auth import AuthenticatedUser, get_authenticated_user
from core.api.errors import DomainError
from core.api.model_catalog import CatalogModel, ModelCatalogResponse
from core.api.routers import submissions
from core.api.routers._helpers import automated_run_ids
from core.api.routers.continuous import _HISTORY_LIMIT, create_continuous_router
from core.api.tests.test_protected_submissions import _Gateway, _Harness, _SQLiteJobStore
from core.billing.budget_amounts import CENT_SCALE, budget_wallet_hold
from core.billing.budgets import BudgetService
from core.storage.models import (
    BillingCustomerModel,
    ContinuousCycleModel,
    ContinuousPlanModel,
    ExecutionBudgetModel,
    JobModel,
)
from core.worker import continuous

BALANCE = 1000
WEEKLY = {"frequency": "weekly", "weekdays": [0], "time": "09:00", "timezone": "UTC"}
DAILY = {"frequency": "daily", "time": "09:00", "timezone": "UTC"}
CYCLE_BUDGET = 20
# The incumbent's saved test score, which every check's run is compared with.
INCUMBENT_SCORE = 0.7
# The incumbent's own models: rated the same and priced the same.
CURRENT_MODELS = {"task": (40.0, 1e-6), "text": (40.0, 1e-6)}


@dataclass
class _Env:
    """Hold the API client, the store and the scripted model catalog."""

    harness: _Harness
    monkeypatch: pytest.MonkeyPatch

    @property
    def store(self) -> _SQLiteJobStore:
        """Return the shared store."""
        return self.harness.store

    @property
    def client(self) -> TestClient:
        """Return the API client."""
        assert self.harness.client is not None
        return self.harness.client

    def catalog(self, **models: tuple[float | None, float | None]) -> None:
        """Serve a hosted catalog whose models have the given rating and price.

        Args:
            **models: ``(rating, price per token)`` per model, keyed by the name after ``fixture/``.
        """
        entries = [
            CatalogModel(
                value=f"fixture/{name}",
                label=name,
                provider="fixture",
                available=True,
                intelligence_index=score,
                input_cost_per_token=price,
                output_cost_per_token=price,
            )
            for name, (score, price) in models.items()
        ]
        self.monkeypatch.setattr(
            continuous, "get_catalog_cached", lambda: ModelCatalogResponse(providers=[], models=entries)
        )

    def better(self) -> None:
        """Release a model rated higher than both current models at the same price."""
        self.catalog(**CURRENT_MODELS, strong=(60.0, 1e-6))

    def sweep(self, now: datetime) -> list[str]:
        """Run one sweep.

        Args:
            now: Sweep time.

        Returns:
            Checked cycle ids.
        """
        return continuous.run_due_checks(self.store, object(), now=now)

    def count(self, model: Any) -> int:
        """Count rows of one table.

        Args:
            model: ORM model.

        Returns:
            Row count.
        """
        with Session(self.store.engine) as session:
            return session.scalar(select(func.count()).select_from(model))

    def plan(self) -> ContinuousPlanModel:
        """Read the only plan.

        Returns:
            The plan row, detached.
        """
        with Session(self.store.engine, expire_on_commit=False) as session:
            return session.scalars(select(ContinuousPlanModel)).one()

    def cycles(self) -> list[ContinuousCycleModel]:
        """Read every cycle in claim order.

        Returns:
            Detached cycle rows.
        """
        with Session(self.store.engine, expire_on_commit=False) as session:
            return list(session.scalars(select(ContinuousCycleModel).order_by(ContinuousCycleModel.slot)).all())

    def edit_plan(self, **values: Any) -> None:
        """Overwrite plan columns.

        Args:
            **values: Column values.
        """
        with Session(self.store.engine) as session, session.begin():
            plan = session.scalars(select(ContinuousPlanModel)).one()
            for key, value in values.items():
                setattr(plan, key, value)

    def edit_cycle(self, cycle_id: str, **values: Any) -> None:
        """Overwrite cycle columns.

        Args:
            cycle_id: The cycle.
            **values: Column values.
        """
        with Session(self.store.engine) as session, session.begin():
            cycle = session.get(ContinuousCycleModel, cycle_id)
            for key, value in values.items():
                setattr(cycle, key, value)

    def set_balance(self, cents: int) -> None:
        """Set the wallet balance.

        Args:
            cents: New balance.
        """
        with Session(self.store.engine) as session, session.begin():
            session.get(BillingCustomerModel, "alice").balance_cents = cents

    def balance(self) -> int:
        """Read the wallet balance.

        Returns:
            Balance cents.
        """
        with Session(self.store.engine) as session:
            return session.get(BillingCustomerModel, "alice").balance_cents

    def payload(self, optimization_id: str) -> dict[str, Any]:
        """Read a run's stored request.

        Args:
            optimization_id: The run.

        Returns:
            The request.
        """
        return self.store.get_job(optimization_id, include_payload=True)["payload"]


def _cases(answers: list[str]) -> list[dict[str, Any]]:
    """Build rows with the incumbent's case schema.

    Args:
        answers: One expected answer per row.

    Returns:
        Rows with ``question`` and ``answer`` columns.
    """
    return [{"question": f"Q{index}?", "answer": answer} for index, answer in enumerate(answers)]


def _incumbent_payload() -> dict[str, Any]:
    """Build a unified run request whose cases come from a dataset table.

    Returns:
        A request body without budget evidence.
    """
    rows = _cases(["A"] * 10)
    return {
        "name": "Support answers",
        "task": {
            "objective": "Answer the question in one word.",
            "artifact": {"kind": "dspy_program", "starting_version": "Answer briefly: "},
        },
        "data": {
            "mode": "pairs",
            "cases": submissions.rows_to_cases(rows, input_fields=["question"], expected_field="answer"),
            "input_fields": ["question"],
            "expected_field": "answer",
        },
        "optimizer": {"choice": "dspy_gepa"},
        "models": {"reflection_model": {"name": "fixture/text"}, "task_model": {"name": "fixture/task"}},
        "workspace": {
            "run_code": "def run(artifact, input):\n    return artifact + input['question']\n",
            "metric_code": (
                "def score(input, output, expected=None):\n    return float(output == expected), {'feedback': ''}\n"
            ),
        },
        "max_cost_cents": CYCLE_BUDGET,
    }


def _result(baseline: float, best: float, *, test: int = 2, held_out: bool = True, guard: bool = False) -> dict:
    """Build a finished unified run result.

    Args:
        baseline: Final-run score of the starting version.
        best: Final-run score of the winner.
        test: Held-out test cases in the split.
        held_out: Whether the report used unseen test cases.
        guard: Whether the regression guard kept the starting version.

    Returns:
        A stored result mapping.
    """
    return {
        "optimizer_name": "gepa",
        "strategy_mode": "single",
        "engine_used": "gepa",
        "baseline_score": baseline,
        "best_score": best,
        "best_candidate": "Answer in one word: ",
        "regression_guard_applied": guard,
        "runtime_seconds": 1.0,
        "split_counts": {
            "train": 6,
            "val": 2,
            "test": test,
            "held_out": held_out,
            "selection_split": "val",
            "report_split": "test" if held_out else "val",
        },
    }


def _finish(env: _Env, optimization_id: str, status: str = "success", result: dict | None = None) -> None:
    """Mark a run finished with a result.

    Args:
        env: Test environment.
        optimization_id: The run.
        status: Terminal status.
        result: Stored result.
    """
    env.store.update_job(optimization_id, status=status, result=result)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Env]:
    """Serve submissions and continuous routes on a real SQLite store.

    Args:
        tmp_path: Disposable database directory.
        monkeypatch: Isolates provider adapters, the model catalog and deployment settings.

    Yields:
        The environment.
    """
    store = _SQLiteJobStore(tmp_path / "continuous.db")
    fixture = _Harness(store)
    with Session(store.engine) as session:
        session.add_all(
            BillingCustomerModel(
                username=name, stripe_customer_id=f"fixture-{name}", balance_cents=BALANCE, grant_remaining=0
            )
            for name in ("alice", "bob")
        )
        session.commit()

    def verify(gateway: _Gateway, payload: dict[str, Any], *, scope: str, identity: str) -> dict:
        """Run the measured fixture preflight instead of a sandbox."""
        return fixture.verify(gateway.runtime, payload, identity)

    monkeypatch.setattr(preflight_execution, "ModelGateway", _Gateway)
    monkeypatch.setattr(preflight_execution, "bind_protected_sandbox", lambda *args, **kwargs: None)
    monkeypatch.setattr(preflight_execution, "_verify_anything", verify)
    monkeypatch.setattr(preflight_execution, "_verify_unified", verify)
    monkeypatch.setattr(submissions.settings, "usage_markup", 1.0)
    monkeypatch.setattr(submissions.settings, "worker_enabled", False)
    monkeypatch.setattr(submissions.settings, "submissions_paused", False)
    monkeypatch.setattr(submissions.settings, "max_concurrent_jobs_per_user", 0)
    monkeypatch.setattr(submissions.settings, "global_daily_spend_ceiling_cents", 0)
    monkeypatch.setattr(submissions.settings, "rate_limit_submissions_per_minute", 0)
    monkeypatch.setattr(submissions.settings, "openrouter_api_key", SecretStr("fixture-only"))
    monkeypatch.setattr(submissions.settings, "byok_vault_key", SecretStr(Fernet.generate_key().decode()))
    app = FastAPI()

    @app.exception_handler(DomainError)
    async def _domain_error(request: Request, exc: DomainError) -> JSONResponse:
        """Render domain errors with their stable code."""
        return JSONResponse(status_code=exc.status_code, content={"code": exc.code, "params": exc.params})

    app.include_router(submissions.create_submissions_router(service=object(), job_store=store))
    app.include_router(create_continuous_router(job_store=store))
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser(fixture.username, fixture.role, ())
    environment = _Env(fixture, monkeypatch)
    environment.catalog(**CURRENT_MODELS)
    with TestClient(app) as client:
        fixture.client = client
        yield environment
    store.engine.dispose()


def _incumbent(env: _Env, score: float = INCUMBENT_SCORE, name: str | None = None) -> str:
    """Submit and finish the run a plan is enabled from.

    Args:
        env: Test environment.
        score: The incumbent's saved test score.
        name: Run name; a new name makes a new run instead of replaying an earlier submit.

    Returns:
        The incumbent's optimization id.
    """
    response = env.client.post("/optimize", json={**_incumbent_payload(), **({"name": name} if name else {})})
    assert response.status_code == 201, response.text
    optimization_id = response.json()["optimization_id"]
    _finish(env, optimization_id, result=_result(0.5, score))
    return optimization_id


def _enable(env: _Env, optimization_id: str, **overrides: Any) -> dict[str, Any]:
    """Enable a weekly plan that may switch both models.

    Args:
        env: Test environment.
        optimization_id: The incumbent.
        **overrides: Request field overrides.

    Returns:
        The plan response.
    """
    body = {
        "schedule": WEEKLY,
        "permitted": {"task_model": True, "reflection_model": True},
        "monthly_limit_cents": 100,
        "cycle_budget_cents": CYCLE_BUDGET,
        **overrides,
    }
    response = env.client.post(f"/optimizations/{optimization_id}/continuous", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _due(env: _Env) -> datetime:
    """Return a moment just after the plan's next check.

    Args:
        env: Test environment.

    Returns:
        A sweep time at which the plan is due.
    """
    return continuous.aware(env.plan().next_check_at) + timedelta(seconds=1)


def _started(env: _Env, **overrides: Any) -> tuple[str, dict[str, Any], ContinuousCycleModel]:
    """Enable a plan, release a better model and run the check that starts its run.

    Args:
        env: Test environment.
        **overrides: Plan request overrides.

    Returns:
        The incumbent's id, the plan and the optimizing cycle.
    """
    source_id = _incumbent(env)
    plan = _enable(env, source_id, **overrides)
    env.better()
    env.sweep(_due(env))
    cycle = env.cycles()[-1]
    assert cycle.status == "optimizing", cycle.detail_code
    return source_id, plan, cycle


def _refuse_after_charging(env: _Env, code: str, billed: int) -> None:
    """Make a submit create its budget and bill setup usage, then refuse the run.

    Args:
        env: Test environment.
        code: The refusal's error code.
        billed: Cents the preflight bills before it refuses.
    """

    def refuse(payload: Any, **kwargs: Any) -> Any:
        """Create the run's budget as a real preflight does, then refuse it.

        Args:
            payload: The run's request.
            **kwargs: Submission arguments, including the replay key.

        Raises:
            DomainError: Always, with ``code``.
        """
        budget_key = continuous.resolve_submission_replay_keys(
            payload, username="alice", workflow=continuous.UNIFIED_WORKFLOW, supplied_key=kwargs["idempotency_key"]
        ).budget
        budget = BudgetService(engine=env.store.engine).create("alice", CYCLE_BUDGET, idempotency_key=budget_key)
        with Session(env.store.engine) as session, session.begin():
            session.get(ExecutionBudgetModel, budget.id).billed_cents = billed
        raise DomainError(code, status=409)

    env.monkeypatch.setattr(continuous, "submit_unified_payload", refuse)


def test_enable_inspect_pause_resume_and_next_check(env: _Env) -> None:
    """A successful run enables one plan with a visible next check; pause and resume toggle it."""
    source_id = _incumbent(env)
    state = env.client.get(f"/optimizations/{source_id}/continuous").json()
    assert state == {"plan": None, "eligible": True, "held_out": True}

    before = datetime.now(UTC)
    plan = _enable(env, source_id)
    next_check = datetime.fromisoformat(plan["next_check_at"])
    assert before < next_check <= datetime.now(UTC) + timedelta(days=7)
    assert (next_check.weekday(), next_check.hour, next_check.minute) == (0, 9, 0)
    assert plan["schedule"] == {**WEEKLY, "every": 1, "month_day": 1, "skip": [], "extra": []}
    assert plan["permitted"] == {"task_model": True, "reflection_model": True}
    assert "source" not in plan
    assert "pending_inputs" not in plan
    assert len(plan["upcoming_checks"]) == 3
    assert plan["upcoming_checks"][0] == plan["next_check_at"]
    assert plan["status"] == "active"
    assert plan["usage"] == {
        "month": continuous.month_key(datetime.now(UTC)),
        "limit_cents": 100,
        "committed_cents": 0,
        "resets_at": plan["usage"]["resets_at"],
    }

    again = env.client.post(
        f"/optimizations/{source_id}/continuous",
        json={
            "schedule": DAILY,
            "permitted": {"task_model": True},
            "monthly_limit_cents": 50,
            "cycle_budget_cents": 10,
        },
    )
    assert again.status_code == 409
    assert again.json()["code"] == "continuous.already_enabled"

    paused = env.client.post(f"/continuous/{plan['id']}/pause").json()
    assert (paused["status"], paused["pause_reason"], paused["next_check_at"]) == ("paused", "user", None)
    assert env.sweep(datetime.now(UTC) + timedelta(days=30)) == []

    resumed = env.client.post(f"/continuous/{plan['id']}/resume").json()
    assert (resumed["status"], resumed["pause_reason"]) == ("active", None)
    resumed_at = datetime.fromisoformat(resumed["next_check_at"])
    assert datetime.now(UTC) < resumed_at <= datetime.now(UTC) + timedelta(days=7)
    assert resumed_at.weekday() == 0
    assert env.client.get(f"/optimizations/{source_id}/continuous").json()["plan"]["id"] == plan["id"]


def test_only_a_successful_unified_run_can_enable(env: _Env) -> None:
    """A failed run is not eligible, and a plan needs at least one model switch on."""
    response = env.client.post("/optimize", json=_incumbent_payload())
    optimization_id = response.json()["optimization_id"]
    _finish(env, optimization_id, status="failed")
    assert env.client.get(f"/optimizations/{optimization_id}/continuous").json()["eligible"] is False
    refused = env.client.post(
        f"/optimizations/{optimization_id}/continuous",
        json={"schedule": WEEKLY, "permitted": {"task_model": True}, "cycle_budget_cents": 10},
    )
    assert refused.status_code == 409
    _finish(env, optimization_id, result=_result(0.5, INCUMBENT_SCORE))
    for permitted in ({}, {"task_model": False, "reflection_model": False}, {"data": True, "models": True}):
        no_switch = env.client.post(
            f"/optimizations/{optimization_id}/continuous",
            json={"schedule": WEEKLY, "permitted": permitted, "cycle_budget_cents": 10},
        )
        assert no_switch.status_code == 422, permitted


def test_a_better_model_starts_one_run_on_the_incumbents_exact_request(env: _Env) -> None:
    """A better model starts one funded run: the incumbent's request and cases, with only the models swapped."""
    source_id, _, cycle = _started(env)

    assert cycle.changes == ["reflection_model", "task_model"]
    assert cycle.detail_code == "continuous.run_started"
    assert cycle.snapshot["compared_against"] == source_id
    assert cycle.snapshot["new_models"] == {
        "task_model": {"name": "fixture/strong", "byok_provider": None},
        "reflection_model": {"name": "fixture/strong", "byok_provider": None},
    }
    incumbent, payload = env.payload(source_id), env.payload(cycle.optimization_id)
    assert cycle.snapshot["old_models"] == {
        role: incumbent["models"][role]["name"] for role in ("task_model", "reflection_model")
    }
    assert payload["models"]["task_model"]["name"] == "fixture/strong"
    assert payload["models"]["reflection_model"]["name"] == "fixture/strong"
    for key in ("task", "workspace", "optimizer", "seed"):
        assert payload[key] == incumbent[key], key
    assert payload["data"]["cases"] == incumbent["data"]["cases"]
    assert payload["data"]["split"] == incumbent["data"]["split"]
    assert continuous.held_out_fingerprint(payload) == continuous.held_out_fingerprint(incumbent) is not None
    assert payload["preflight_id"]
    job = env.store.get_job(cycle.optimization_id, include_payload=False)
    assert BudgetService(engine=env.store.engine).get(job["execution_budget_id"], "alice").total_cents == CYCLE_BUDGET
    assert cycle.execution_budget_id == job["execution_budget_id"]
    assert env.count(JobModel) == 2


@pytest.mark.parametrize(
    "models",
    [
        # Rated higher, but priced higher.
        {"pricier": (60.0, 2e-6)},
        # Cheaper, but rated lower.
        {"weaker": (30.0, 5e-7)},
        # Rated the same.
        {"equal": (40.0, 1e-6)},
        # No rating, or no price, so nothing to compare.
        {"unrated": (None, 1e-6)},
        {"unpriced": (90.0, None)},
    ],
)
def test_no_better_model_starts_no_run_and_cannot_be_rerun(env: _Env, models: dict) -> None:
    """With no better model the check is a visible skip that starts nothing, charges nothing and offers no rerun."""
    source_id = _incumbent(env)
    plan = _enable(env, source_id)
    env.catalog(**CURRENT_MODELS, **models)
    jobs_before = env.count(JobModel)
    budgets_before = env.count(ExecutionBudgetModel)
    balance_before = env.balance()

    env.sweep(_due(env))

    [cycle] = env.cycles()
    assert (cycle.status, cycle.detail_code, cycle.optimization_id) == ("skipped", "continuous.skip_unchanged", None)
    assert (cycle.execution_budget_id, cycle.spent_cents) == (None, None)
    assert env.count(JobModel) == jobs_before
    assert env.count(ExecutionBudgetModel) == budgets_before
    assert env.balance() == balance_before
    state = env.client.get(f"/optimizations/{source_id}/continuous").json()["plan"]
    assert state["cycles"] == []
    assert env.client.post(f"/continuous/{plan['id']}/rerun").status_code in {404, 405}
    assert env.client.post(f"/continuous/{plan['id']}/cycles/{cycle.id}/rerun").status_code in {404, 405}


def test_a_current_model_missing_its_rating_or_price_is_never_switched(env: _Env) -> None:
    """A current model the catalog cannot rate or price has nothing to compare with, so nothing beats it."""
    _incumbent_id = _incumbent(env)
    _enable(env, _incumbent_id)
    env.catalog(task=(None, 1e-6), text=(40.0, None), strong=(90.0, 1e-7))

    env.sweep(_due(env))

    assert env.cycles()[-1].detail_code == "continuous.skip_unchanged"


@pytest.mark.parametrize(
    ("price_in", "price_out"),
    [
        # Cheaper for input, dearer for output.
        (5e-7, 2e-6),
        # Cheaper for output, dearer for input.
        (2e-6, 5e-7),
    ],
)
def test_a_price_that_rises_on_one_side_is_not_better(env: _Env, price_in: float, price_out: float) -> None:
    """A model must cost no more for input and for output; a higher price on either side rules it out."""
    entries = [
        CatalogModel(
            value=f"fixture/{name}",
            label=name,
            provider="fixture",
            available=True,
            intelligence_index=score,
            input_cost_per_token=price_in,
            output_cost_per_token=price_out,
        )
        for name, score, price_in, price_out in (
            ("task", 40.0, 1e-6, 1e-6),
            ("text", 40.0, 1e-6, 1e-6),
            ("lopsided", 90.0, price_in, price_out),
        )
    ]
    env.monkeypatch.setattr(
        continuous, "get_catalog_cached", lambda: ModelCatalogResponse(providers=[], models=entries)
    )
    _enable(env, _incumbent(env))

    env.sweep(_due(env))

    assert env.cycles()[-1].detail_code == "continuous.skip_unchanged"


def test_the_highest_rated_qualifying_model_is_tried(env: _Env) -> None:
    """Of the models that qualify, the highest-rated one is tried, even when a cheaper one also qualifies."""
    _enable(env, _incumbent(env), permitted={"task_model": True})
    env.catalog(**CURRENT_MODELS, cheap=(50.0, 5e-7), best=(55.0, 1e-6), pricey=(90.0, 3e-6))

    env.sweep(_due(env))

    [cycle] = env.cycles()
    assert env.payload(cycle.optimization_id)["models"]["task_model"]["name"] == "fixture/best"


def test_only_switched_on_roles_change(env: _Env) -> None:
    """With only the reflection switch on, the task model stays the incumbent's own."""
    _, _, cycle = _started(env, permitted={"reflection_model": True})

    payload = env.payload(cycle.optimization_id)
    assert cycle.changes == ["reflection_model"]
    assert payload["models"]["task_model"]["name"] == "fixture/task"
    assert payload["models"]["reflection_model"]["name"] == "fixture/strong"


def test_an_old_plan_with_the_models_switch_tries_a_better_model(env: _Env) -> None:
    """A plan saved with the old ``models`` switch on may switch both roles, before and after the migration."""
    _enable(env, _incumbent(env))
    env.edit_plan(permitted={"data": True, "models": True, "optimizers": False})
    plan = env.client.get(f"/optimizations/{env.plan().source_optimization_id}/continuous").json()["plan"]
    assert plan["permitted"] == {"task_model": True, "reflection_model": True}
    env.better()

    env.sweep(_due(env))

    [cycle] = env.cycles()
    assert (cycle.status, cycle.changes) == ("optimizing", ["reflection_model", "task_model"])


def test_an_old_plan_without_the_models_switch_stays_skipped(env: _Env) -> None:
    """A plan saved with only the removed data and optimizer switches has no model switch on, so it never runs."""
    _enable(env, _incumbent(env))
    env.edit_plan(permitted={"data": True, "models": False, "optimizers": True})
    env.better()

    env.sweep(_due(env))

    assert env.cycles()[-1].detail_code == "continuous.skip_unchanged"
    plan = env.client.get(f"/optimizations/{env.plan().source_optimization_id}/continuous").json()["plan"]
    assert plan["permitted"] == {"task_model": False, "reflection_model": False}


def _load_switches_migration() -> Any:
    """Load the migration that rewrites old plan switches from its file.

    Returns:
        The imported migration module.
    """
    path = Path(__file__).resolve().parents[3] / "alembic/versions/d2a8f4c6e1b9_split_continuous_model_switch.py"
    spec = importlib.util.spec_from_file_location("split_continuous_model_switch", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_switch_migration_rewrites_only_old_plans_and_can_run_twice() -> None:
    """Old ``models`` switches become both role switches; new and empty rows stay as they are."""
    migration = _load_switches_migration()
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    plans = Table("continuous_plans", metadata, Column("id", String(36), primary_key=True), Column("permitted", JSON))
    metadata.create_all(engine)
    rows = {
        "old-on": {"data": True, "models": True, "optimizers": False},
        "old-off": {"data": True, "models": False, "optimizers": True},
        "new": {"task_model": True, "reflection_model": False},
        "empty": {},
    }
    with engine.begin() as connection:
        connection.execute(plans.insert(), [{"id": key, "permitted": value} for key, value in rows.items()])
        for _ in range(2):
            with patch.object(migration, "op", Operations(MigrationContext.configure(connection))):
                migration.upgrade()
        stored = dict(connection.execute(select(plans.c.id, plans.c.permitted)).all())
    assert stored == {
        "old-on": {"task_model": True, "reflection_model": True},
        "old-off": {"task_model": False, "reflection_model": False},
        "new": {"task_model": True, "reflection_model": False},
        "empty": {},
    }


def test_the_removed_inputs_route_is_gone(env: _Env) -> None:
    """No route stages a model or optimizer change any more."""
    plan = _enable(env, _incumbent(env))
    staged = env.client.post(f"/continuous/{plan['id']}/inputs", json={"task_model": "fixture/strong"})
    assert staged.status_code in {404, 405}


def test_overlapping_sweeps_and_retries_create_one_cycle_and_one_spend(env: _Env) -> None:
    """Parallel workers and a retried check produce one cycle, one run and one budget."""
    source_id = _incumbent(env)
    _enable(env, source_id)
    env.better()
    jobs_before = env.count(JobModel)
    budgets_before = env.count(ExecutionBudgetModel)
    now = _due(env)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: env.sweep(now), range(4)))
    env.sweep(now)

    [cycle] = env.cycles()
    assert env.count(JobModel) == jobs_before + 1
    assert env.count(ExecutionBudgetModel) == budgets_before + 1

    # A worker that died after submitting leaves the check in ``checking``;
    # the takeover replays the same run and budget instead of a second one.
    # Its own reservation must not make the wallet look short and pause the plan,
    # and an even better model released meanwhile must not relabel what that run used.
    env.set_balance(0)
    env.catalog(**CURRENT_MODELS, strong=(60.0, 1e-6), stronger=(70.0, 1e-6))
    env.edit_cycle(cycle.id, status="checking", optimization_id=None, lease_until=now - timedelta(seconds=1))
    env.sweep(now)
    [retried] = env.cycles()
    assert (retried.status, retried.optimization_id) == ("optimizing", cycle.optimization_id)
    assert retried.snapshot == cycle.snapshot
    assert env.plan().status == "active"
    assert env.count(JobModel) == jobs_before + 1
    assert env.count(ExecutionBudgetModel) == budgets_before + 1


def test_check_waits_while_a_cycle_runs_or_awaits_review(env: _Env) -> None:
    """A new check does not start a second run while one is active or unreviewed."""
    _, _, first = _started(env)
    env.sweep(_due(env))
    first, second = env.cycles()
    assert (second.status, second.detail_code) == ("skipped", "continuous.skip_busy")
    _finish(env, first.optimization_id, result=_result(0.5, 0.8))
    env.sweep(_due(env))
    first, second, third = env.cycles()
    assert first.status == "awaiting_review"
    assert (third.status, third.detail_code) == ("skipped", "continuous.skip_review_pending")


def test_allowance_exhaustion_pauses_and_rollover_keeps_history(env: _Env) -> None:
    """A spent allowance pauses paid work; the next month resumes it and keeps every record."""
    source_id = _incumbent(env)
    _enable(env, source_id, monthly_limit_cents=CYCLE_BUDGET, cycle_budget_cents=CYCLE_BUDGET)
    env.better()
    october = datetime(2026, 10, 20, tzinfo=UTC)
    env.edit_plan(next_check_at=october)
    env.sweep(october + timedelta(seconds=1))
    [first] = env.cycles()
    assert first.month == "2026-10"
    budget_before = BudgetService(engine=env.store.engine).get(first.execution_budget_id, "alice")
    _finish(env, first.optimization_id, status="failed")
    env.sweep(october + timedelta(seconds=2))
    env.edit_cycle(first.id, spent_cents=CYCLE_BUDGET)

    env.edit_plan(next_check_at=october + timedelta(days=5))
    jobs_before = env.count(JobModel)
    env.sweep(october + timedelta(days=5, seconds=1))
    plan = env.plan()
    assert (plan.status, plan.pause_reason) == ("paused", "allowance")
    assert env.cycles()[-1].detail_code == "continuous.paused_allowance"
    assert env.count(JobModel) == jobs_before

    balance_before = env.balance()
    november = datetime(2026, 11, 3, tzinfo=UTC)
    env.edit_plan(next_check_at=november)
    env.sweep(november + timedelta(seconds=1))
    plan = env.plan()
    assert (plan.status, plan.pause_reason) == ("active", None)
    cycles = env.cycles()
    assert [c.month for c in cycles] == ["2026-10", "2026-10", "2026-11"]
    assert cycles[0].spent_cents == CYCLE_BUDGET
    assert cycles[-1].status == "optimizing"
    assert env.count(JobModel) == jobs_before + 1
    budget_after = BudgetService(engine=env.store.engine).get(first.execution_budget_id, "alice")
    assert budget_after.total_cents == budget_before.total_cents
    assert budget_after.billed_cents == budget_before.billed_cents
    assert env.balance() <= balance_before


def test_a_paused_plan_with_no_better_model_resumes_from_a_system_pause(env: _Env) -> None:
    """A check that finds no better model costs nothing, so an allowance pause lifts once the allowance has room."""
    _enable(env, _incumbent(env))
    env.edit_plan(status="paused", pause_reason="allowance")

    env.sweep(_due(env))

    plan = env.plan()
    assert (plan.status, plan.pause_reason) == ("active", None)
    assert env.cycles()[-1].detail_code == "continuous.skip_unchanged"


def test_a_finished_cycle_counts_usage_still_awaiting_reconciliation(env: _Env) -> None:
    """A finished cycle's held coverage stays in the allowance until it settles, not just what it billed."""
    source_id = _incumbent(env)
    _enable(env, source_id, monthly_limit_cents=CYCLE_BUDGET + 1, cycle_budget_cents=CYCLE_BUDGET)
    env.better()
    october = datetime(2026, 10, 20, tzinfo=UTC)
    env.edit_plan(next_check_at=october)
    env.sweep(october + timedelta(seconds=1))
    [first] = env.cycles()
    _finish(env, first.optimization_id, status="failed")
    continuous.review_cycle(env.store, first.id)
    # Usage the run reported but billing has not settled yet keeps its wallet hold after the run ends.
    with Session(env.store.engine) as session, session.begin():
        budget = session.get(ExecutionBudgetModel, first.execution_budget_id)
        budget.wallet_reserved_units += CYCLE_BUDGET * CENT_SCALE
        held = budget.billed_cents + budget_wallet_hold(budget)
    [first] = env.cycles()
    assert held > (first.spent_cents or 0)

    with Session(env.store.engine) as session:
        assert continuous.month_committed(session, env.plan(), first.month) == held

    env.edit_plan(next_check_at=october + timedelta(days=5))
    env.sweep(october + timedelta(days=5, seconds=1))
    assert env.cycles()[-1].detail_code == "continuous.paused_allowance"


def test_a_pending_review_stays_visible_past_the_history_window(env: _Env) -> None:
    """A candidate waiting for approval is returned even after many newer checks."""
    source_id, _, cycle = _started(env)
    _finish(env, cycle.optimization_id, result=_result(0.5, 0.9))
    continuous.review_cycle(env.store, cycle.id)
    plan_id = env.plan().id
    later = datetime.now(UTC) + timedelta(minutes=1)
    with Session(env.store.engine) as session, session.begin():
        session.add_all(
            ContinuousCycleModel(
                plan_id=plan_id,
                slot=f"later-{index}",
                month=cycle.month,
                status="failed",
                detail_code="continuous.run_failed",
                created_at=later + timedelta(minutes=index),
            )
            for index in range(_HISTORY_LIMIT)
        )

    cycles = env.client.get(f"/optimizations/{source_id}/continuous").json()["plan"]["cycles"]

    assert len(cycles) == _HISTORY_LIMIT + 1
    assert (cycles[-1]["id"], cycles[-1]["status"]) == (cycle.id, "awaiting_review")


def test_skipped_checks_never_push_runs_out_of_the_history(env: _Env) -> None:
    """Most checks find no better model; the history lists runs only, so skips cannot crowd them out."""
    source_id, _, cycle = _started(env)
    plan_id = env.plan().id
    later = datetime.now(UTC) + timedelta(minutes=1)
    with Session(env.store.engine) as session, session.begin():
        session.add_all(
            ContinuousCycleModel(
                plan_id=plan_id,
                slot=f"later-{index}",
                month=cycle.month,
                status="skipped",
                detail_code="continuous.skip_unchanged",
                created_at=later + timedelta(minutes=index),
            )
            for index in range(_HISTORY_LIMIT)
        )

    cycles = env.client.get(f"/optimizations/{source_id}/continuous").json()["plan"]["cycles"]

    assert [c["id"] for c in cycles] == [cycle.id]


def test_rollover_never_resumes_a_user_pause(env: _Env) -> None:
    """A new month leaves a customer's pause in place."""
    source_id = _incumbent(env)
    plan = _enable(env, source_id)
    env.client.post(f"/continuous/{plan['id']}/pause")
    env.better()
    november = datetime(2026, 11, 3, tzinfo=UTC)
    env.edit_plan(next_check_at=november)
    jobs_before = env.count(JobModel)
    env.sweep(november + timedelta(seconds=1))
    current = env.plan()
    assert (current.status, current.pause_reason) == ("paused", "user")
    assert env.cycles()[-1].detail_code == "continuous.skip_paused"
    assert env.count(JobModel) == jobs_before


def test_wallet_exhaustion_pauses_without_buying_credit(env: _Env) -> None:
    """An empty wallet pauses paid work and the balance is never topped up."""
    source_id = _incumbent(env)
    _enable(env, source_id)
    env.set_balance(5)
    env.better()
    jobs_before = env.count(JobModel)
    env.sweep(_due(env))
    plan = env.plan()
    assert (plan.status, plan.pause_reason) == ("paused", "funds")
    assert env.cycles()[-1].detail_code == "continuous.paused_funds"
    assert env.count(JobModel) == jobs_before
    assert env.balance() == 5


@pytest.mark.parametrize(
    ("result", "status", "code"),
    [
        (_result(0.6, 0.5), "regressed", "continuous.review_regressed"),
        (_result(0.6, INCUMBENT_SCORE), "regressed", "continuous.review_regressed"),
        (_result(0.6, 0.9, test=3, held_out=False), "regressed", "continuous.review_not_held_out"),
        (_result(0.6, 0.9, test=0, held_out=False), "regressed", "continuous.review_not_held_out"),
        (_result(0.6, 0.705), "regressed", "continuous.review_within_margin"),
        (_result(0.6, 0.7099), "regressed", "continuous.review_within_margin"),
        (_result(0.6, 0.71), "awaiting_review", "continuous.review_ready"),
        (_result(0.6, 0.9), "awaiting_review", "continuous.review_ready"),
        # The guard kept the old prompt, but it scored higher on the new model, which is the change under test.
        (_result(0.6, 0.9, guard=True), "awaiting_review", "continuous.review_ready"),
    ],
)
def test_review_compares_with_the_incumbents_saved_test_score(env: _Env, result: dict, status: str, code: str) -> None:
    """Only a run that beats the incumbent's saved test score by the margin, on the same test cases, waits for review."""
    source_id, plan, cycle = _started(env)
    _finish(env, cycle.optimization_id, result=result)

    continuous.review_cycle(env.store, cycle.id)

    [cycle] = env.cycles()
    assert (cycle.status, cycle.detail_code) == (status, code)
    assert (cycle.incumbent_score, cycle.candidate_score) == (INCUMBENT_SCORE, result["best_score"])
    assert env.plan().incumbent_optimization_id == source_id
    approve = env.client.post(f"/continuous/{plan['id']}/cycles/{cycle.id}/approve")
    if status == "regressed":
        assert approve.status_code == 409
        assert env.plan().incumbent_optimization_id == source_id
    else:
        assert approve.status_code == 200, approve.text
        assert env.plan().incumbent_optimization_id == cycle.optimization_id


def test_a_gain_of_exactly_the_margin_is_reviewable(env: _Env) -> None:
    """A gain of exactly 0.01 wins, though 0.57 - 0.56 is a hair under 0.01 in floating point."""
    _enable(env, _incumbent(env, 0.56))
    env.better()
    env.sweep(_due(env))
    [cycle] = env.cycles()
    _finish(env, cycle.optimization_id, result=_result(0.5, 0.57))

    continuous.review_cycle(env.store, cycle.id)

    [cycle] = env.cycles()
    assert (cycle.status, cycle.detail_code) == ("awaiting_review", "continuous.review_ready")


def test_a_model_that_loses_is_not_tried_again_and_the_next_best_gets_its_turn(env: _Env) -> None:
    """A compared loss marks the model tried, so the next check picks the next best model instead."""
    source_id = _incumbent(env)
    _enable(env, source_id, permitted={"task_model": True})
    env.catalog(**CURRENT_MODELS, middle=(50.0, 1e-6), strong=(60.0, 1e-6))
    env.sweep(_due(env))
    [first] = env.cycles()
    assert env.payload(first.optimization_id)["models"]["task_model"]["name"] == "fixture/strong"
    _finish(env, first.optimization_id, result=_result(0.8, 0.5))
    env.sweep(_due(env) - timedelta(days=1))
    assert env.plan().baseline_snapshot == {"incumbent": source_id, "tried_models": ["task_model:fixture/strong"]}

    env.sweep(_due(env))
    latest = env.cycles()[-1]
    assert latest.status == "optimizing"
    assert env.payload(latest.optimization_id)["models"]["task_model"]["name"] == "fixture/middle"
    _finish(env, latest.optimization_id, result=_result(0.8, 0.705))
    env.sweep(_due(env) - timedelta(days=1))

    jobs = env.count(JobModel)
    env.sweep(_due(env))
    assert env.cycles()[-1].detail_code == "continuous.skip_unchanged"
    assert env.count(JobModel) == jobs


def test_a_failed_run_keeps_the_incumbent_and_leaves_its_model_untried(env: _Env) -> None:
    """A failed run (a provider outage) never replaces the incumbent, and the same model runs again next check."""
    source_id, _, cycle = _started(env)
    baseline = env.plan().baseline_snapshot
    _finish(env, cycle.optimization_id, status="failed")
    env.sweep(_due(env) - timedelta(days=1))
    [cycle] = env.cycles()
    assert (cycle.status, cycle.detail_code) == ("failed", "continuous.run_failed")
    assert env.plan().incumbent_optimization_id == source_id
    assert env.plan().baseline_snapshot == baseline

    env.sweep(_due(env))

    latest = env.cycles()[-1]
    assert latest.status == "optimizing"
    assert env.payload(latest.optimization_id)["models"]["task_model"]["name"] == "fixture/strong"


def test_a_run_on_other_test_cases_is_unscored_and_leaves_its_model_untried(env: _Env) -> None:
    """A run whose test cases differ from the incumbent's cannot be compared, so its model is tried again."""
    _, plan, cycle = _started(env)
    with Session(env.store.engine) as session, session.begin():
        job = session.get(JobModel, cycle.optimization_id)
        payload = dict(job.payload)
        payload["data"] = {
            **payload["data"],
            "cases": [{**case, "expected": {"answer": "Z"}} for case in payload["data"]["cases"]],
        }
        job.payload = payload
    _finish(env, cycle.optimization_id, result=_result(0.5, 0.9))

    env.sweep(_due(env) - timedelta(days=1))

    [cycle] = env.cycles()
    assert (cycle.status, cycle.detail_code) == ("failed", "continuous.review_unscored")
    assert env.client.post(f"/continuous/{plan['id']}/cycles/{cycle.id}/approve").status_code == 409
    assert env.plan().baseline_snapshot["tried_models"] == []
    env.sweep(_due(env))
    latest = env.cycles()[-1]
    assert latest.status == "optimizing"
    assert env.payload(latest.optimization_id)["models"]["task_model"]["name"] == "fixture/strong"


def test_a_better_model_waits_for_explicit_approval_and_a_rejection_keeps_the_incumbent(env: _Env) -> None:
    """A winning run only offers a review; rejecting it keeps the incumbent and its model counts as tried."""
    source_id, plan, cycle = _started(env)
    _finish(env, cycle.optimization_id, result=_result(0.5, 0.9))
    env.sweep(_due(env) - timedelta(days=1))
    assert env.cycles()[0].status == "awaiting_review"
    assert env.plan().incumbent_optimization_id == source_id

    rejected = env.client.post(f"/continuous/{plan['id']}/cycles/{cycle.id}/reject")
    assert rejected.status_code == 200
    assert env.cycles()[0].status == "rejected"
    assert env.plan().incumbent_optimization_id == source_id
    assert env.client.post(f"/continuous/{plan['id']}/cycles/{cycle.id}/approve").status_code == 409

    jobs = env.count(JobModel)
    env.sweep(_due(env))
    assert env.cycles()[-1].detail_code == "continuous.skip_unchanged"
    assert env.count(JobModel) == jobs


def test_approval_makes_the_new_run_current_and_later_checks_start_from_its_models(env: _Env) -> None:
    """After approval the new run is the incumbent, and later checks compare against its models and score."""
    _, plan, cycle = _started(env)
    _finish(env, cycle.optimization_id, result=_result(0.5, 0.9))
    env.sweep(_due(env) - timedelta(days=1))

    approved = env.client.post(f"/continuous/{plan['id']}/cycles/{cycle.id}/approve")

    assert approved.status_code == 200, approved.text
    assert approved.json()["incumbent_optimization_id"] == cycle.optimization_id
    assert env.plan().baseline_snapshot == {"incumbent": cycle.optimization_id, "tried_models": []}
    jobs = env.count(JobModel)
    env.sweep(_due(env))
    assert env.cycles()[-1].detail_code == "continuous.skip_unchanged"
    assert env.count(JobModel) == jobs

    env.catalog(**CURRENT_MODELS, strong=(60.0, 1e-6), stronger=(70.0, 1e-6))
    env.sweep(_due(env))
    latest = env.cycles()[-1]
    assert latest.snapshot["compared_against"] == cycle.optimization_id
    assert latest.snapshot["old_models"] == {"task_model": "fixture/strong", "reflection_model": "fixture/strong"}
    assert env.payload(latest.optimization_id)["models"]["task_model"]["name"] == "fixture/stronger"
    _finish(env, latest.optimization_id, result=_result(0.5, 0.905))
    env.sweep(_due(env) - timedelta(days=1))
    latest = env.cycles()[-1]
    assert (latest.status, latest.incumbent_score) == ("regressed", 0.9)


def test_pending_setup_usage_retries_the_same_check(env: _Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A check whose setup usage awaits confirmation stays claimed and later replays the same budget."""
    source_id = _incumbent(env)
    _enable(env, source_id)
    env.better()
    budgets_before = env.count(ExecutionBudgetModel)
    submit = continuous.submit_unified_payload
    _refuse_after_charging(env, "budget.pending", 2)
    now = _due(env)
    env.sweep(now)
    [cycle] = env.cycles()
    assert (cycle.status, cycle.optimization_id) == ("checking", None)
    assert cycle.lease_until is not None
    assert cycle.execution_budget_id is not None
    assert cycle.detail_params["attempts"] == 0

    monkeypatch.setattr(continuous, "submit_unified_payload", submit)
    env.sweep(now + continuous.CYCLE_LEASE + timedelta(seconds=1))
    [retried] = env.cycles()
    assert retried.id == cycle.id
    assert retried.status == "optimizing"
    assert retried.execution_budget_id == cycle.execution_budget_id
    assert env.count(ExecutionBudgetModel) == budgets_before + 1


def test_a_pending_retry_after_a_catalog_change_keeps_its_first_models_and_budget(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retried check replays the models it first chose, so its key meets the same request and budget."""
    _enable(env, _incumbent(env))
    env.better()
    submit = continuous.submit_unified_payload
    _refuse_after_charging(env, "budget.pending", 2)
    now = _due(env)
    env.sweep(now)
    [cycle] = env.cycles()
    first_old = {"task_model": "fixture/first-task", "reflection_model": "fixture/first-reflection"}
    env.edit_cycle(cycle.id, snapshot={**cycle.snapshot, "old_models": first_old})
    env.catalog(**CURRENT_MODELS, strong=(60.0, 1e-6), stronger=(70.0, 1e-6))

    monkeypatch.setattr(continuous, "submit_unified_payload", submit)
    env.sweep(now + continuous.CYCLE_LEASE + timedelta(seconds=1))

    [retried] = env.cycles()
    assert (retried.id, retried.status) == (cycle.id, "optimizing")
    assert retried.snapshot["budget_key"] == cycle.snapshot["budget_key"]
    assert retried.snapshot["old_models"] == first_old
    assert retried.execution_budget_id == cycle.execution_budget_id
    models = env.payload(retried.optimization_id)["models"]
    assert (models["task_model"]["name"], models["reflection_model"]["name"]) == ("fixture/strong", "fixture/strong")


@pytest.mark.parametrize("lacking", ["absent", None, {}])
def test_a_retry_naming_a_role_the_incumbent_lacks_fails_instead_of_sticking(
    env: _Env, monkeypatch: pytest.MonkeyPatch, lacking: str | dict | None
) -> None:
    """A retry whose saved models name a role the incumbent lacks fails with a reason, not a crash."""
    incumbent_id = _incumbent(env)
    _enable(env, incumbent_id)
    env.better()
    submit = continuous.submit_unified_payload
    _refuse_after_charging(env, "budget.pending", 2)
    now = _due(env)
    env.sweep(now)
    [cycle] = env.cycles()
    env.edit_cycle(cycle.id, snapshot={k: v for k, v in cycle.snapshot.items() if k != "old_models"})
    payload = env.payload(incumbent_id)
    models = {**payload["models"], "task_model": lacking}
    if lacking == "absent":
        del models["task_model"]
    env.store.update_job(incumbent_id, payload={**payload, "models": models})

    monkeypatch.setattr(continuous, "submit_unified_payload", submit)
    env.sweep(now + continuous.CYCLE_LEASE + timedelta(seconds=1))

    [retried] = env.cycles()
    assert (retried.id, retried.status, retried.detail_code) == (cycle.id, "failed", "continuous.run_rejected")
    assert retried.detail_params == {"reason": "the incumbent has no task_model"}


def test_a_crashed_check_never_takes_another_users_budget_with_its_key(env: _Env) -> None:
    """Crash recovery finds the check's budget by its key among the owner's budgets only."""
    _enable(env, _incumbent(env))
    [claimed] = continuous._claim_due(env.store.engine, _due(env))
    env.edit_cycle(claimed, snapshot={"budget_key": "shared-key"})
    BudgetService(engine=env.store.engine).create("bob", CYCLE_BUDGET, idempotency_key="shared-key")

    continuous.process_cycle(env.store, object(), claimed)

    [cycle] = env.cycles()
    assert (cycle.status, cycle.execution_budget_id) == ("skipped", None)


def test_the_new_run_inlines_the_cases_instead_of_a_staged_upload(env: _Env) -> None:
    """A run copied from one that also named a staged upload is sent with the cases inline and no upload."""
    source_id = _incumbent(env)
    payload = env.payload(source_id)
    env.store.update_job(source_id, payload={**payload, "data": {**payload["data"], "staged_dataset_id": "gone"}})
    _enable(env, source_id)
    env.better()
    submit = continuous.submit_unified_payload
    sent: list[Any] = []

    def record(request: Any, **kwargs: Any) -> Any:
        """Keep the request the check sends, then submit it.

        Args:
            request: The run's request.
            **kwargs: Submission arguments.

        Returns:
            The submission response.
        """
        sent.append(request.data.staged_dataset_id)
        return submit(request, **kwargs)

    env.monkeypatch.setattr(continuous, "submit_unified_payload", record)

    env.sweep(_due(env))

    assert sent == [None]
    [cycle] = env.cycles()
    assert cycle.status == "optimizing", cycle.detail_code
    copied = env.payload(cycle.optimization_id)["data"]
    assert copied.get("staged_dataset_id") is None
    assert copied["cases"] == payload["data"]["cases"]


def test_an_unavailable_hosted_model_is_never_chosen(env: _Env) -> None:
    """A better hosted model the catalog marks unavailable is not tried."""
    entries = [
        CatalogModel(
            value=f"fixture/{name}",
            label=name,
            provider="fixture",
            available=available,
            intelligence_index=score,
            input_cost_per_token=1e-6,
            output_cost_per_token=1e-6,
        )
        for name, score, available in (("task", 40.0, True), ("text", 40.0, True), ("strong", 90.0, False))
    ]
    env.monkeypatch.setattr(
        continuous, "get_catalog_cached", lambda: ModelCatalogResponse(providers=[], models=entries)
    )
    _enable(env, _incumbent(env))

    env.sweep(_due(env))

    assert env.cycles()[-1].detail_code == "continuous.skip_unchanged"


def test_a_cycle_of_another_plan_cannot_be_reviewed_through_this_plan(env: _Env) -> None:
    """Approve and reject refuse a cycle that belongs to another plan, the owner's own or another user's."""
    _, plan, cycle = _started(env)
    _finish(env, cycle.optimization_id, result=_result(0.5, 0.9))
    env.sweep(_due(env) - timedelta(days=1))
    other = _enable(env, _incumbent(env, name="Other answers"))
    env.harness.username = "bob"
    bobs = _enable(env, _incumbent(env, name="Bob answers"))
    for action in ("approve", "reject"):
        assert env.client.post(f"/continuous/{bobs['id']}/cycles/{cycle.id}/{action}").status_code == 409
    env.harness.username = "alice"
    for action in ("approve", "reject"):
        assert env.client.post(f"/continuous/{other['id']}/cycles/{cycle.id}/{action}").status_code == 409

    assert env.cycles()[0].status == "awaiting_review"
    with Session(env.store.engine) as session:
        incumbents = {
            row.id: row.incumbent_optimization_id for row in session.scalars(select(ContinuousPlanModel)).all()
        }
    assert cycle.optimization_id not in incumbents.values()
    assert plan["id"] in incumbents


def test_a_pending_retry_at_the_allowance_edge_is_not_paused(env: _Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A retried check that already holds its budget does not need a second cycle budget of allowance."""
    source_id = _incumbent(env)
    _enable(env, source_id, monthly_limit_cents=CYCLE_BUDGET, cycle_budget_cents=CYCLE_BUDGET)
    env.better()
    submit = continuous.submit_unified_payload
    _refuse_after_charging(env, "budget.pending", 2)
    now = _due(env)
    env.sweep(now)
    [cycle] = env.cycles()
    assert cycle.execution_budget_id is not None

    monkeypatch.setattr(continuous, "submit_unified_payload", submit)
    env.sweep(now + continuous.CYCLE_LEASE + timedelta(seconds=1))
    [retried] = env.cycles()
    assert (retried.id, retried.status) == (cycle.id, "optimizing")
    plan = env.plan()
    assert (plan.status, plan.pause_reason) == ("active", None)


def test_a_refused_preflight_counts_its_charge_toward_the_monthly_limit(env: _Env) -> None:
    """A run refused after its preflight billed setup usage keeps that budget, and the allowance counts it."""
    source_id = _incumbent(env)
    _enable(env, source_id, monthly_limit_cents=CYCLE_BUDGET * 2, cycle_budget_cents=CYCLE_BUDGET)
    env.better()
    _refuse_after_charging(env, "submission.validation_failed", 3)

    env.sweep(_due(env))

    [cycle] = env.cycles()
    assert (cycle.status, cycle.detail_code) == ("failed", "continuous.run_rejected")
    assert cycle.execution_budget_id is not None
    assert cycle.spent_cents == 3
    with Session(env.store.engine) as session:
        assert continuous.month_committed(session, env.plan(), cycle.month) >= 3


def test_a_refused_run_for_funds_pauses_and_keeps_its_charge(env: _Env) -> None:
    """A run refused for funds pauses the plan for funds and still records what its preflight billed."""
    _enable(env, _incumbent(env))
    env.better()
    _refuse_after_charging(env, "billing.insufficient_funds", 4)

    env.sweep(_due(env))

    [cycle] = env.cycles()
    assert (cycle.status, cycle.detail_code, cycle.spent_cents) == ("paused", "continuous.paused_funds", 4)
    assert (env.plan().status, env.plan().pause_reason) == ("paused", "funds")


def test_a_deleted_incumbent_fails_the_cycle_without_wedging_the_plan(env: _Env) -> None:
    """A check whose incumbent run is gone records why instead of staying in ``checking``."""
    source_id = _incumbent(env)
    _enable(env, source_id)
    env.better()
    env.store.delete_job(source_id)

    env.sweep(_due(env))
    env.sweep(_due(env))

    cycles = env.cycles()
    assert [cycle.status for cycle in cycles] == ["failed", "failed"]
    assert cycles[0].detail_params["reason"] == "the incumbent run was deleted"


def test_other_users_cannot_read_or_change_a_plan(env: _Env) -> None:
    """A plan, its checks and its run stay private to the owner on every route."""
    source_id, plan, cycle = _started(env)
    _finish(env, cycle.optimization_id, result=_result(0.5, 0.9))
    env.sweep(_due(env) - timedelta(days=1))
    env.harness.username = "bob"
    assert env.client.get(f"/optimizations/{source_id}/continuous").status_code == 404
    assert env.client.get(f"/optimizations/{cycle.optimization_id}/continuous").status_code == 404
    assert env.client.post(f"/optimizations/{source_id}/continuous", json={}).status_code in {404, 422}
    assert env.client.post(f"/continuous/{plan['id']}/pause").status_code == 404
    assert env.client.post(f"/continuous/{plan['id']}/resume").status_code == 404
    assert env.client.put(f"/continuous/{plan['id']}/schedule", json=DAILY).status_code == 404
    assert env.client.post(f"/continuous/{plan['id']}/cycles/{cycle.id}/approve").status_code == 404
    assert env.client.post(f"/continuous/{plan['id']}/cycles/{cycle.id}/reject").status_code == 404
    assert env.client.delete(f"/continuous/{plan['id']}").status_code == 404
    env.harness.username = "alice"
    assert env.cycles()[0].status == "awaiting_review"
    assert env.plan().incumbent_optimization_id == source_id


def test_another_users_run_with_a_checks_key_is_never_linked(env: _Env) -> None:
    """A run another user sent with a check's idempotency key does not count as that check's run."""
    _enable(env, _incumbent(env))
    env.better()
    [claimed] = continuous._claim_due(env.store.engine, _due(env))
    env.harness.username = "bob"
    forged = env.client.post(
        "/optimize", json=_incumbent_payload(), headers={"Idempotency-Key": f"continuous:{claimed}"}
    )
    assert forged.status_code == 201, forged.text
    env.harness.username = "alice"

    continuous.process_cycle(env.store, object(), claimed)

    [cycle] = env.cycles()
    assert cycle.status == "optimizing"
    assert cycle.optimization_id != forged.json()["optimization_id"]
    assert env.store.get_job(cycle.optimization_id, include_payload=False)["username"] == "alice"


def test_schedule_change_moves_the_next_check_and_keeps_a_pause(env: _Env) -> None:
    """A new rule sets the next check from now; a paused plan stays paused with no check."""
    plan = _enable(env, _incumbent(env))
    rule = {"frequency": "monthly", "month_day": 31, "time": "18:30", "timezone": "Asia/Jerusalem"}
    changed = env.client.put(f"/continuous/{plan['id']}/schedule", json=rule).json()
    first = datetime.fromisoformat(changed["next_check_at"]).astimezone(ZoneInfo("Asia/Jerusalem"))
    assert (first.hour, first.minute) == (18, 30)
    assert first.day == calendar.monthrange(first.year, first.month)[1]
    assert changed["schedule"]["frequency"] == "monthly"
    assert len(changed["upcoming_checks"]) == 3

    env.client.post(f"/continuous/{plan['id']}/pause")
    paused = env.client.put(f"/continuous/{plan['id']}/schedule", json=DAILY).json()
    assert (paused["status"], paused["next_check_at"], paused["upcoming_checks"]) == ("paused", None, [])
    assert paused["schedule"]["frequency"] == "daily"


@pytest.mark.parametrize(
    "rule",
    [
        {"frequency": "weekly", "weekdays": [], "time": "09:00"},
        {"frequency": "daily", "time": "25:00"},
        {"frequency": "hourly", "every": 13},
        {"frequency": "daily", "timezone": "Mars/Olympus"},
        {"frequency": "weekly", "weekdays": [7]},
    ],
)
def test_an_impossible_schedule_is_refused(env: _Env, rule: dict[str, Any]) -> None:
    """A rule that can't run, or names an unknown zone, is refused on enable, edit and preview."""
    source_id = _incumbent(env)
    assert env.client.post("/continuous/schedule-preview", json=rule).status_code == 422
    body = {"schedule": rule, "permitted": {"task_model": True}, "monthly_limit_cents": 50, "cycle_budget_cents": 10}
    assert env.client.post(f"/optimizations/{source_id}/continuous", json=body).status_code == 422
    plan = _enable(env, source_id)
    assert env.client.put(f"/continuous/{plan['id']}/schedule", json=rule).status_code == 422


def test_preview_lists_the_next_three_checks(env: _Env) -> None:
    """The preview gives the rule's next three times, at the chosen local time."""
    rule = {"frequency": "weekly", "weekdays": [0, 2, 4], "time": "07:15", "timezone": "America/New_York"}
    times = [
        datetime.fromisoformat(t)
        for t in env.client.post("/continuous/schedule-preview", json=rule).json()["next_checks"]
    ]
    local = [t.astimezone(ZoneInfo("America/New_York")) for t in times]
    assert len(local) == 3
    assert local == sorted(local)
    assert all(t.weekday() in {0, 2, 4} and (t.hour, t.minute) == (7, 15) for t in local)
    assert times[0] > datetime.now(UTC)


def test_a_plan_saved_before_rules_reads_as_a_rule(env: _Env) -> None:
    """A stored ``daily`` plan with no rule shows and schedules as daily at 09:00 UTC."""
    source_id = _incumbent(env)
    _enable(env, source_id)
    env.edit_plan(schedule="daily", schedule_rule=None)
    env.sweep(_due(env))
    following = continuous.aware(env.plan().next_check_at)
    assert (following.hour, following.minute) == (9, 0)
    plan = env.client.get(f"/optimizations/{source_id}/continuous").json()["plan"]
    assert (plan["schedule"]["frequency"], plan["schedule"]["time"]) == ("daily", "09:00")


def test_the_sidebar_flags_every_run_of_a_plan(env: _Env) -> None:
    """The source run and a run a check started are automated; an unrelated run is not."""
    other_id = env.client.post("/optimize", json=_incumbent_payload()).json()["optimization_id"]
    source_id, _, cycle = _started(env)
    assert automated_run_ids(env.store, [source_id, cycle.optimization_id, other_id]) == {
        source_id,
        cycle.optimization_id,
    }


def test_a_plan_without_amounts_reuses_the_run_budget_and_has_no_monthly_limit(env: _Env) -> None:
    """With no amounts, each run gets the source run's budget and only the wallet gates it."""
    source_id = _incumbent(env)
    source = env.store.get_job(source_id, include_payload=True)
    expected = source["payload"].get("max_cost_cents") or (
        BudgetService(engine=env.store.engine).get(source["execution_budget_id"], "alice").total_cents
    )
    plan = _enable(env, source_id, monthly_limit_cents=None, cycle_budget_cents=None)
    assert plan["cycle_budget_cents"] == expected
    assert plan["usage"]["limit_cents"] is None
    env.catalog(**CURRENT_MODELS, first=(50.0, 1e-6), second=(55.0, 1e-6), third=(60.0, 1e-6))

    for _ in range(3):
        env.sweep(_due(env))
        cycle = env.cycles()[-1]
        assert cycle.status == "optimizing", cycle.detail_code
        _finish(env, cycle.optimization_id, result=_result(0.7, 0.6))
        env.sweep(datetime.now(UTC))
    assert env.plan().status == "active"


def test_a_plan_set_up_while_the_run_works_waits_for_its_success(env: _Env) -> None:
    """A plan made from the submit wizard waits, then starts when the run succeeds."""
    optimization_id = env.client.post("/optimize", json=_incumbent_payload()).json()["optimization_id"]
    plan = _enable(env, optimization_id, monthly_limit_cents=None, cycle_budget_cents=None)
    assert (plan["status"], plan["next_check_at"]) == ("waiting", None)
    assert env.client.post(f"/continuous/{plan['id']}/pause").status_code == 409
    assert env.sweep(datetime.now(UTC) + timedelta(days=30)) == []

    _finish(env, optimization_id, result=_result(0.5, INCUMBENT_SCORE))
    started = env.client.get(f"/optimizations/{optimization_id}/continuous").json()["plan"]
    assert started["status"] == "active"
    assert started["next_check_at"] is not None


def test_a_waiting_plan_ends_with_a_failed_run(env: _Env) -> None:
    """A waiting plan ends, with the reason kept, when its run fails; the owner can then delete it."""
    optimization_id = env.client.post("/optimize", json=_incumbent_payload()).json()["optimization_id"]
    plan = _enable(env, optimization_id, permitted={"task_model": True})
    _finish(env, optimization_id, status="failed")
    env.sweep(datetime.now(UTC))
    state = env.client.get(f"/optimizations/{optimization_id}/continuous").json()
    assert state["eligible"] is False
    assert (state["plan"]["status"], state["plan"]["pause_reason"], state["plan"]["next_check_at"]) == (
        "ended",
        "failed",
        None,
    )
    assert env.client.post(f"/continuous/{plan['id']}/resume").status_code == 409

    assert env.client.delete(f"/continuous/{plan['id']}").status_code == 204
    assert env.client.get(f"/optimizations/{optimization_id}/continuous").json() == {
        "plan": None,
        "eligible": False,
        "held_out": True,
    }
    assert env.client.delete(f"/continuous/{plan['id']}").status_code == 404


def test_a_run_without_test_cases_cannot_start_a_plan(env: _Env) -> None:
    """A plan could never replace a run that kept no test cases apart, so none starts from it."""
    optimization_id = env.client.post("/optimize", json=_incumbent_payload()).json()["optimization_id"]
    _finish(env, optimization_id, result=_result(0.5, 0.7, test=0, held_out=False))
    state = env.client.get(f"/optimizations/{optimization_id}/continuous").json()
    assert (state["eligible"], state["held_out"]) == (False, False)
    refused = env.client.post(
        f"/optimizations/{optimization_id}/continuous",
        json={"schedule": WEEKLY, "permitted": {"task_model": True}, "cycle_budget_cents": CYCLE_BUDGET},
    )
    assert (refused.status_code, refused.json()["code"]) == (409, "continuous.no_test_split")


def test_a_run_in_flight_without_a_test_split_cannot_start_a_plan(env: _Env) -> None:
    """The submit wizard cannot set up a plan for a run whose split keeps no test cases."""
    payload = _incumbent_payload()
    payload["data"]["split"] = {"train": 0.8, "val": 0.2, "test": 0.0}
    optimization_id = env.client.post("/optimize", json=payload).json()["optimization_id"]
    refused = env.client.post(
        f"/optimizations/{optimization_id}/continuous",
        json={"schedule": WEEKLY, "permitted": {"task_model": True}, "cycle_budget_cents": CYCLE_BUDGET},
    )
    assert (refused.status_code, refused.json()["code"]) == (409, "continuous.no_test_split")


def test_a_waiting_plan_ends_when_its_run_keeps_no_test_cases(env: _Env) -> None:
    """A run with too few cases for a test split succeeds, and its waiting plan ends with the reason."""
    optimization_id = env.client.post("/optimize", json=_incumbent_payload()).json()["optimization_id"]
    plan = _enable(env, optimization_id, monthly_limit_cents=None, cycle_budget_cents=None)
    assert plan["status"] == "waiting"
    _finish(env, optimization_id, result=_result(0.5, 0.7, test=0, held_out=False))
    env.sweep(datetime.now(UTC))
    ended = env.client.get(f"/optimizations/{optimization_id}/continuous").json()["plan"]
    assert (ended["status"], ended["pause_reason"], ended["next_check_at"]) == ("ended", "no_test_split", None)


def test_a_plan_deleted_while_its_check_submits_keeps_the_run(
    env: _Env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A plan deleted mid-check leaves its run as an ordinary run and the check finishes quietly."""
    source_id = _incumbent(env)
    plan = _enable(env, source_id)
    env.better()
    submit = continuous.submit_unified_payload
    submitted: list[str] = []

    def delete_then_submit(*args: Any, **kwargs: Any) -> Any:
        """Delete the plan, then submit as usual.

        Args:
            *args: Forwarded to the real submit.
            **kwargs: Forwarded to the real submit.

        Returns:
            The real submit's response.
        """
        assert env.client.delete(f"/continuous/{plan['id']}").status_code == 204
        response = submit(*args, **kwargs)
        submitted.append(response.optimization_id)
        return response

    monkeypatch.setattr(continuous, "submit_unified_payload", delete_then_submit)
    env.sweep(_due(env))

    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert env.cycles() == []
    assert env.store.get_job(submitted[0], include_payload=False)["optimization_id"] == submitted[0]


def test_a_check_that_keeps_crashing_is_closed_and_frees_the_plan(env: _Env) -> None:
    """A check that loses its worker three times is failed, so later checks run again."""
    source_id = _incumbent(env)
    _enable(env, source_id)
    env.better()
    now = _due(env)
    [claimed] = continuous._claim_due(env.store.engine, now)
    for attempt in range(continuous.MAX_CHECK_ATTEMPTS):
        now += continuous.CYCLE_LEASE + timedelta(seconds=1)
        reclaimed = continuous._reclaim_stale(env.store, now)
        assert reclaimed == ([claimed] if attempt < continuous.MAX_CHECK_ATTEMPTS - 1 else [])

    [cycle] = env.cycles()
    assert (cycle.status, cycle.detail_code) == ("failed", "continuous.check_stuck")


def test_the_last_crash_retry_still_links_a_run_its_worker_started(env: _Env) -> None:
    """A worker that died right after its run was accepted leaves a paid run, which the last retry links."""
    _, _, cycle = _started(env)
    now = _due(env)
    env.edit_cycle(
        cycle.id,
        status="checking",
        optimization_id=None,
        execution_budget_id=None,
        detail_params={"attempts": continuous.MAX_CHECK_ATTEMPTS},
        lease_until=now - timedelta(seconds=1),
    )

    assert continuous._reclaim_stale(env.store, now) == []

    [linked] = env.cycles()
    assert (linked.status, linked.optimization_id) == ("optimizing", cycle.optimization_id)
    assert linked.execution_budget_id == cycle.execution_budget_id


def test_the_last_crash_retry_keeps_a_budget_its_preflight_charged(env: _Env) -> None:
    """A worker that died after a preflight billed its budget leaves that charge on the closed check."""
    _enable(env, _incumbent(env), monthly_limit_cents=CYCLE_BUDGET * 2)
    now = _due(env)
    [claimed] = continuous._claim_due(env.store.engine, now)
    budget = BudgetService(engine=env.store.engine).create("alice", CYCLE_BUDGET, idempotency_key="fixture-budget")
    with Session(env.store.engine) as session, session.begin():
        session.get(ExecutionBudgetModel, budget.id).billed_cents = 5
    later = now + continuous.CYCLE_LEASE + timedelta(seconds=1)
    env.edit_cycle(
        claimed,
        snapshot={"new_models": {}, "budget_key": "fixture-budget"},
        detail_params={"attempts": continuous.MAX_CHECK_ATTEMPTS},
        lease_until=later - timedelta(seconds=1),
    )

    continuous._reclaim_stale(env.store, later)

    [cycle] = env.cycles()
    assert (cycle.status, cycle.detail_code) == ("failed", "continuous.check_stuck")
    assert cycle.execution_budget_id == budget.id
    with Session(env.store.engine) as session:
        assert continuous.month_committed(session, env.plan(), cycle.month) >= 5


def test_a_worker_that_lost_its_lease_stops_before_it_submits(env: _Env) -> None:
    """A worker whose check another worker took over submits nothing and writes nothing."""
    source_id = _incumbent(env)
    _enable(env, source_id)
    [claimed] = continuous._claim_due(env.store.engine, _due(env))
    [cycle] = env.cycles()
    payload = continuous._cycle_payload(
        env.plan(),
        continuous._incumbent(env.store, source_id),
        {"task_model": {"name": "fixture/strong", "byok_provider": None}},
        1,
    )
    jobs = env.count(JobModel)

    continuous._submit_run(env.store, object(), claimed, "alice", payload, cycle.lease_until - timedelta(minutes=1))

    [after] = env.cycles()
    assert env.count(JobModel) == jobs
    assert (after.status, after.optimization_id, after.lease_until) == ("checking", None, cycle.lease_until)


def test_a_worker_that_lost_its_lease_while_submitting_writes_nothing(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A late worker leaves the outcome to the worker that took over, which links the same run."""
    _enable(env, _incumbent(env))
    env.better()
    submit = continuous.submit_unified_payload
    taken_over = datetime.now(UTC) + timedelta(hours=1)

    def submit_then_lose_the_lease(*args: Any, **kwargs: Any) -> Any:
        """Submit as usual, then let another worker take the check over.

        Args:
            *args: Forwarded to the real submit.
            **kwargs: Forwarded to the real submit.

        Returns:
            The real submit's response.
        """
        response = submit(*args, **kwargs)
        env.edit_cycle(env.cycles()[0].id, lease_until=taken_over)
        return response

    monkeypatch.setattr(continuous, "submit_unified_payload", submit_then_lose_the_lease)
    env.sweep(_due(env))
    [cycle] = env.cycles()
    assert (cycle.status, cycle.optimization_id) == ("checking", None)
    jobs = env.count(JobModel)

    monkeypatch.setattr(continuous, "submit_unified_payload", submit)
    env.sweep(taken_over + timedelta(seconds=1))
    [linked] = env.cycles()
    assert linked.status == "optimizing"
    assert env.count(JobModel) == jobs


def test_failing_checks_in_a_row_pause_the_plan_until_resumed(env: _Env) -> None:
    """Runs that keep being refused pause the plan after a few checks instead of failing forever."""
    source_id = _incumbent(env)
    plan = _enable(env, source_id, schedule=DAILY)
    env.better()
    _refuse_after_charging(env, "submission.validation_failed", 0)
    for _ in range(continuous.FAILURE_STREAK):
        env.sweep(_due(env))

    current = env.plan()
    assert (current.status, current.pause_reason, current.next_check_at) == ("paused", "failing", None)
    assert [c.status for c in env.cycles()] == ["failed"] * continuous.FAILURE_STREAK
    assert env.sweep(datetime.now(UTC) + timedelta(days=30)) == []

    resumed = env.client.post(f"/continuous/{plan['id']}/resume").json()
    assert (resumed["status"], resumed["pause_reason"]) == ("active", None)
    assert resumed["next_check_at"] is not None


def test_a_waiting_check_is_reported_as_busy(env: _Env) -> None:
    """A check behind another check still running says it is busy, not waiting for a review."""
    source_id = _incumbent(env)
    _enable(env, source_id)
    env.better()
    now = _due(env)
    continuous._claim_due(env.store.engine, now)
    env.edit_plan(next_check_at=now)
    [second] = continuous._claim_due(env.store.engine, now + timedelta(seconds=1))
    continuous.process_cycle(env.store, object(), second, now=now)
    assert {c.id: c.detail_code for c in env.cycles()}[second] == "continuous.skip_busy"


def test_a_schedule_change_reaches_a_plan_paused_for_its_allowance(env: _Env) -> None:
    """A plan the system paused still checks, so a new rule moves its next check."""
    plan = _enable(env, _incumbent(env))
    env.edit_plan(status="paused", pause_reason="allowance")
    changed = env.client.put(f"/continuous/{plan['id']}/schedule", json=DAILY).json()
    first = datetime.fromisoformat(changed["next_check_at"])
    assert changed["status"] == "paused"
    assert (first.hour, first.minute) == (9, 0)
    assert first - datetime.now(UTC) <= timedelta(days=1)


def test_a_schedule_save_forgets_single_checks_that_passed(env: _Env) -> None:
    """Old single-date changes are dropped on save, so they never fill the slot limit."""
    plan = _enable(env, _incumbent(env))
    past, future = "2020-01-01T09:00", "2099-01-01T09:00"
    rule = {**DAILY, "skip": [past, future], "extra": [past]}
    saved = env.client.put(f"/continuous/{plan['id']}/schedule", json=rule).json()
    assert (saved["schedule"]["skip"], saved["schedule"]["extra"]) == ([future], [])


def _byok(monkeypatch: pytest.MonkeyPatch, connections: dict[str, str | None], models: dict[str, float]) -> None:
    """Serve saved BYOK connections and the models they reach, all at the same price.

    Args:
        monkeypatch: Pytest monkeypatch.
        connections: ``{provider: api_base}``; ``None`` is a native key.
        models: ``{"provider/model": rating}``; the provider is the part before the slash.
    """
    views = [SimpleNamespace(provider=provider, api_base=base) for provider, base in connections.items()]
    entries = [
        CatalogModel(
            value=name,
            label=name,
            provider=name.split("/", 1)[0],
            byok_provider=name.split("/", 1)[0],
            available=True,
            intelligence_index=rating,
            input_cost_per_token=1e-6,
            output_cost_per_token=1e-6,
        )
        for name, rating in models.items()
    ]
    vault = SimpleNamespace(list_keys=lambda username: SimpleNamespace(keys=views))
    monkeypatch.setattr(continuous, "ProviderKeyVault", lambda engine: vault)
    monkeypatch.setattr(
        continuous, "_byok_catalog_for_user", lambda vault, username: ModelCatalogResponse(providers=[], models=entries)
    )
    monkeypatch.setattr(continuous, "get_catalog_cached", lambda: ModelCatalogResponse(providers=[], models=[]))


def test_a_model_on_a_saved_custom_endpoint_is_never_switched(monkeypatch: pytest.MonkeyPatch) -> None:
    """A role on a connection with its own endpoint keeps its model even when a better one is listed."""
    _byok(monkeypatch, {"openai": "https://llm.test/v1"}, {"openai/now": 40.0, "openai/strong": 60.0})
    current = {"name": "openai/now", "token_source": "byok", "byok_provider": "openai"}

    assert continuous.better_model(SimpleNamespace(engine=None), "alice", current) is None


def test_a_custom_endpoint_model_without_its_provider_named_is_never_switched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A block that names no provider is matched to its connection by its model prefix, as submission does."""
    _byok(monkeypatch, {"openai": "https://llm.test/v1"}, {"openai/strong": 60.0})
    monkeypatch.setattr(
        continuous,
        "_byok_catalog_for_user",
        lambda vault, username: ModelCatalogResponse(
            providers=[],
            models=[
                CatalogModel(
                    value=name,
                    label=name,
                    provider="openrouter",
                    byok_provider="openrouter",
                    available=True,
                    intelligence_index=rating,
                    input_cost_per_token=1e-6,
                    output_cost_per_token=1e-6,
                )
                for name, rating in (("openai/now", 40.0), ("openai/strong", 60.0))
            ],
        ),
    )
    current = {"name": "openai/now", "token_source": "byok", "byok_provider": None}

    assert continuous.better_model(SimpleNamespace(engine=None), "alice", current) is None


def test_a_model_on_a_custom_endpoint_is_never_a_replacement(monkeypatch: pytest.MonkeyPatch) -> None:
    """A native-key role never moves to a model that only a custom endpoint serves."""
    _byok(
        monkeypatch,
        {"openrouter": None, "openai": "https://llm.test/v1"},
        {"openrouter/now": 40.0, "openai/strong": 90.0, "openrouter/better": 60.0},
    )
    current = {"name": "openrouter/now", "token_source": "byok", "byok_provider": "openrouter"}

    better = continuous.better_model(SimpleNamespace(engine=None), "alice", current)

    assert better == {"name": "openrouter/better", "byok_provider": "openrouter"}


def _picks(*ranked: str) -> continuous.ModelPicker:
    """Pick the best model in ``ranked`` the plan has not skipped.

    Args:
        *ranked: Model names, best first.

    Returns:
        A model picker.
    """

    def pick(job_store: Any, username: str, current: dict[str, Any], skip: frozenset[str]) -> dict[str, Any] | None:
        name = next((name for name in ranked if name not in skip and name != current["name"]), None)
        return None if name is None else {"name": name, "byok_provider": None}

    return pick


@pytest.mark.parametrize("other_role", ["reflection_model", "metric_model", "extra_reflection_models"])
@pytest.mark.parametrize(
    ("ranked", "expected"),
    [(("fixture/b", "fixture/c"), {"task_model": "fixture/c"}), (("fixture/b",), {})],
)
def test_a_replacement_billed_another_way_in_another_role_gives_the_next_best_its_turn(
    ranked: tuple[str, ...], expected: dict[str, str], other_role: str
) -> None:
    """One model id under two billing sources is refused at submission, so it is never picked."""
    plan = SimpleNamespace(
        permitted={"task_model": True}, username="alice", baseline_snapshot=None, incumbent_optimization_id=None
    )
    other = {"name": "fixture/b", "token_source": "byok", "byok_provider": "openrouter"}
    models = {
        "task_model": {"name": "fixture/a", "token_source": "managed"},
        "reflection_model": {"name": "fixture/r", "token_source": "managed"},
        other_role: [other] if other_role == "extra_reflection_models" else other,
    }

    found = continuous.find_better_models(None, plan, models, _picks(*ranked))

    assert {role: better["name"] for role, better in found.items()} == expected
