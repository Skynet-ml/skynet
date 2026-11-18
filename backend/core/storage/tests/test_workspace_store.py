"""Tests for the workspaces table store: one workspace for each user, its policy and its budget (SKY-717)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from core.billing.budgets import BudgetService
from core.storage import storage_version
from core.storage.models import Base, BillingCustomerModel, JobModel, WorkspaceModel
from core.storage.workspace_store import BudgetAdmission, WorkspaceStore, workspace_id_for


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Yield a fresh SQLite file engine with the schema, then dispose of it.

    Args:
        tmp_path: Pytest's temporary folder.

    Yields:
        The engine.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def test_the_workspace_id_is_fixed_for_each_user() -> None:
    """The same username gives the same id, and another username gives another id."""
    assert workspace_id_for("ada") == workspace_id_for("ada")
    assert workspace_id_for("ada") != workspace_id_for("bob")
    assert len(workspace_id_for("ada")) == 36


def test_get_or_create_keeps_one_workspace_for_each_user(engine: Engine) -> None:
    """A second call returns the first workspace and adds no row."""
    store = WorkspaceStore(engine)
    first = store.get_or_create("ada")
    second = store.get_or_create("ada")
    other = store.get_or_create("bob")

    assert first == second
    assert first.id == workspace_id_for("ada")
    assert (first.username, first.policy, first.execution_budget_id) == ("ada", {}, None)
    assert other.id != first.id
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(WorkspaceModel)) == 2


def test_a_new_workspace_starts_at_the_current_storage_version(engine: Engine, monkeypatch) -> None:
    """A new workspace has no old data, so it needs no upgrade step."""
    monkeypatch.setattr(storage_version, "UPGRADES", (lambda session, workspace_id: None,))

    assert WorkspaceStore(engine).get_or_create("ada").storage_version == storage_version.current_version() == 2


def test_get_or_create_keeps_the_stored_row(engine: Engine) -> None:
    """An existing workspace keeps its policy, budget and version."""
    with Session(engine) as session:
        session.add(
            WorkspaceModel(
                id=workspace_id_for("ada"),
                username="ada",
                policy={"net": False},
                execution_budget_id="budget-1",
                storage_version=1,
            )
        )
        session.commit()

    record = WorkspaceStore(engine).get_or_create("ada")

    assert (record.policy, record.execution_budget_id, record.storage_version) == ({"net": False}, "budget-1", 1)


def test_set_budget_changes_only_the_budget(engine: Engine) -> None:
    """The budget is set, then cleared, and the policy stays."""
    store = WorkspaceStore(engine)
    workspace = store.get_or_create("ada")

    assert store.set_budget(workspace.id, "budget-1").execution_budget_id == "budget-1"
    assert store.get(workspace.id).execution_budget_id == "budget-1"
    assert store.set_budget(workspace.id, None).execution_budget_id is None
    assert store.get(workspace.id).policy == {}


def test_a_missing_workspace_raises_lookup_error(engine: Engine) -> None:
    """``get`` and ``set_budget`` refuse an unknown id."""
    store = WorkspaceStore(engine)
    with pytest.raises(LookupError):
        store.get("missing")
    with pytest.raises(LookupError):
        store.set_budget("missing", "budget-1")


def test_budget_admission_reads_the_owners_budget_and_the_job(engine: Engine) -> None:
    """The read follows the ledger's state and generation and the job's generation, and refuses another user."""
    with Session(engine) as session:
        session.add(BillingCustomerModel(username="ada", stripe_customer_id="local-ada", balance_cents=100))
        session.add(JobModel(optimization_id="job-1", username="ada", execution_generation=3))
        session.commit()
    budgets = BudgetService(engine=engine)
    budget = budgets.create("ada", 10, idempotency_key="budget-1")
    store = WorkspaceStore(engine)

    assert store.budget_admission(budget.id, "ada", "job-1") == BudgetAdmission("open", 0, 3)
    assert store.budget_admission(budget.id, "ada", "missing-job") == BudgetAdmission("open", 0, None)
    budgets.fence_generation(budget.id, "ada", expected_generation=0)
    with Session(engine) as session:
        session.execute(update(JobModel).where(JobModel.optimization_id == "job-1").values(execution_generation=4))
        session.commit()
    assert store.budget_admission(budget.id, "ada", "job-1") == BudgetAdmission("open", 1, 4)
    budgets.stop_admission(budget.id, "ada", reason="cancelled")
    assert store.budget_admission(budget.id, "ada", "job-1").state == "closed"
    with pytest.raises(LookupError):
        store.budget_admission(budget.id, "bob", "job-1")
    with pytest.raises(LookupError):
        store.budget_admission("missing", "ada", "job-1")


def test_an_empty_username_is_refused(engine: Engine) -> None:
    """A workspace always has an owner."""
    with pytest.raises(ValueError):
        WorkspaceStore(engine).get_or_create("")
