"""Exercise actual PostgreSQL row locks against an explicitly supplied disposable database."""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Engine, create_engine, event, func, select, text
from sqlalchemy.orm import Session

from core.billing.budget_amounts import CENT_SCALE
from core.billing.budgets import BudgetConflictError, BudgetInFlightError, BudgetService
from core.config import app_env, settings
from core.storage.models import (
    Base,
    BillingCustomerModel,
    BillingProviderKeyModel,
    ExecutionBudgetModel,
    ExecutionOperationModel,
    ExecutionUsageEvidenceModel,
    JobModel,
    WalletLedgerModel,
)
from core.storage.preflights import PreflightStore, WizardPreflightModel

TEST_DB_URL = app_env("APP_BUDGET_TEST_DB_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL or not TEST_DB_URL.startswith("postgresql"),
    reason="Set APP_BUDGET_TEST_DB_URL to an explicitly disposable PostgreSQL database.",
)


@pytest.fixture
def database() -> Iterator[Engine]:
    """Isolate each race in its own schema without touching pre-existing data."""
    schema = f"budget_test_{uuid4().hex}"
    admin = create_engine(TEST_DB_URL)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(TEST_DB_URL, connect_args={"options": f"-csearch_path={schema}"})
    tables = [
        model.__table__
        for model in (
            BillingCustomerModel,
            WalletLedgerModel,
            ExecutionBudgetModel,
            ExecutionOperationModel,
            ExecutionUsageEvidenceModel,
            JobModel,
        )
    ]
    try:
        Base.metadata.create_all(engine, tables=tables)
        with Session(engine) as session:
            session.add(
                BillingCustomerModel(
                    username="alice", stripe_customer_id="fixture", balance_cents=50, grant_remaining=0
                )
            )
            session.commit()
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def _reserve(service: BudgetService, budget_id: str, key: str, maximum: int = 40):
    """Reserve one fixture attempt through the production database transaction."""
    return service.reserve(
        budget_id,
        "alice",
        operation_key=key,
        generation=0,
        phase="setup",
        cost_kind="model",
        request_fingerprint="fixture",
        price_snapshot={"version": "fixture-v1"},
        max_cents=maximum,
    )


@pytest.mark.parametrize("shared_budget", [False, True])
def test_parallel_admission_cannot_overcommit(database: Engine, shared_budget: bool) -> None:
    """Enforce wallet-wide and budget-specific limits under real overlapping transactions."""
    service = BudgetService(engine=database)
    first = service.create("alice", 50, idempotency_key="first")
    second = first if shared_budget else service.create("alice", 50, idempotency_key="second")
    barrier = Barrier(2)

    def run(value: tuple[str, str]) -> str:
        """Begin both admissions together before either can claim account funding."""
        barrier.wait(timeout=5)
        try:
            return _reserve(service, value[0], value[1]).state
        except BudgetInFlightError:
            return "contended"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(run, [(first.id, "one"), (second.id, "two")]))
    assert sorted(outcomes) == ["contended", "reserved"]
    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(ExecutionOperationModel)) == 1


def test_parallel_first_budgets_of_a_new_account_share_one_wallet(
    database: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fund every first budget of a new preview account when they race to create its wallet."""
    monkeypatch.setattr(settings, "preview_grant_cents", 1000)
    monkeypatch.setattr(settings, "app_environment", "skynet-pr-87")
    service = BudgetService(engine=database)
    barrier = Barrier(8)

    def run(index: int) -> int:
        """Create one budget at the same moment as the others."""
        barrier.wait(timeout=5)
        return service.create("carol", 10, idempotency_key=f"first-{index}").total_cents

    with ThreadPoolExecutor(max_workers=8) as executor:
        totals = list(executor.map(run, range(8)))
    assert totals == [10] * 8
    with Session(database) as session:
        assert session.get(BillingCustomerModel, "carol").grant_remaining == 1000
        assert session.scalar(select(func.count()).select_from(ExecutionBudgetModel)) == 8


def test_parallel_duplicate_attempt_has_one_physical_dispatch(database: Engine) -> None:
    """Deduplicate concurrent creation and grant physical dispatch to one caller."""
    service = BudgetService(engine=database)
    budget = service.create("alice", 50, idempotency_key="draft")
    barrier = Barrier(2)

    def run(_: int) -> tuple[str, bool]:
        """Race the same operation identity through reserve and dispatch."""
        barrier.wait(timeout=5)
        operation = _reserve(service, budget.id, "same")
        return operation.id, service.mark_dispatched(operation.id, "alice").dispatch_claimed

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(run, range(2)))
    assert len({operation_id for operation_id, _ in outcomes}) == 1
    assert sorted(claimed for _, claimed in outcomes) == [False, True]
    assert service.get(budget.id, "alice").reserved_cents == 40


def test_parallel_settlement_replay_debits_once(database: Engine) -> None:
    """Apply concurrent delivery of the same provider evidence exactly once."""
    service = BudgetService(engine=database)
    budget = service.create("alice", 50, idempotency_key="draft")
    operation = _reserve(service, budget.id, "call")
    service.mark_dispatched(operation.id, "alice")
    barrier = Barrier(2)

    def run(_: int) -> int:
        """Race two identical completion callbacks against one physical attempt."""
        barrier.wait(timeout=5)
        result = service.settle(
            operation.id, "alice", evidence_key="provider-event", actual_cents="1.2", evidence={"tokens": 42}
        )
        return result.budget.billed_cents

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(run, range(2))) == [2, 2]
    with Session(database) as session:
        assert session.get(BillingCustomerModel, "alice").balance_cents == 48
        assert session.scalar(select(func.count()).select_from(WalletLedgerModel)) == 1
        assert session.scalar(select(func.count()).select_from(ExecutionUsageEvidenceModel)) == 1


def test_migration_applies_from_legacy_credit_ledger_and_is_idempotent(database: Engine) -> None:
    """Upgrade an existing wallet schema without requiring application-created budget tables."""
    path = Path(__file__).resolve().parents[3] / "alembic/versions/927cb56e104a_add_execution_budgets.py"
    spec = importlib.util.spec_from_file_location("budget_migration_fixture", path)
    assert spec is not None
    assert spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    extension_path = path.with_name("b7c2e4d9f130_add_execution_budget_uncapped.py")
    extension_spec = importlib.util.spec_from_file_location("uncapped_migration_fixture", extension_path)
    assert extension_spec is not None
    assert extension_spec.loader is not None
    extension = importlib.util.module_from_spec(extension_spec)
    extension_spec.loader.exec_module(extension)
    rename_path = path.with_name("d4f6a8c0e2b4_rename_credits_to_cents.py")
    rename_spec = importlib.util.spec_from_file_location("rename_migration_fixture", rename_path)
    assert rename_spec is not None
    assert rename_spec.loader is not None
    rename = importlib.util.module_from_spec(rename_spec)
    rename_spec.loader.exec_module(rename)
    with database.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        rename.downgrade()
        migration.downgrade()
        migration.upgrade()
        migration.upgrade()
        extension.upgrade()
        extension.upgrade()
        rename.upgrade()
        rename.upgrade()
    service = BudgetService(engine=database)
    budget = service.create("alice", 10, idempotency_key="migrated")
    assert budget.uncapped is False
    operation = _reserve(service, budget.id, "call", maximum=5)
    service.mark_dispatched(operation.id, "alice")
    result = service.settle(operation.id, "alice", evidence_key="usage", actual_cents=2, evidence={})
    assert result.budget.billed_cents == 2


def test_preflight_claim_and_fencing_are_atomic_on_postgres(database: Engine) -> None:
    """Recover an expired setup owner under real row locks without repeating outstanding usage.

    Args:
        database: Explicitly disposable PostgreSQL schema.
    """
    Base.metadata.create_all(database, tables=[BillingProviderKeyModel.__table__, WizardPreflightModel.__table__])
    store = PreflightStore(database)
    service = BudgetService(engine=database)
    budget = service.create("alice", 20, idempotency_key="setup")
    barrier = Barrier(2)

    def claim(_: int):
        """Race two replicas for one content-addressed setup owner.

        Args:
            _: Unused executor input.

        Returns:
            Private ownership claim and public evidence.
        """
        barrier.wait(timeout=5)
        return store.claim(
            username="alice",
            budget_id=budget.id,
            revision=1,
            workflow="anything",
            scope="execution",
            payload={"seed_candidate": "same"},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(claim, range(2)))
    assert sum(item.token is not None for item in claims) == 1
    owner = next(item for item in claims if item.token is not None)
    operation = _reserve(service, budget.id, "setup-call", maximum=5)
    service.mark_dispatched(operation.id, "alice")
    with Session(database) as session:
        session.get(WizardPreflightModel, owner.document["id"]).lease_expires_at = datetime.now(UTC) - timedelta(
            seconds=1
        )
        session.commit()
    replacement = store.claim(
        username="alice",
        budget_id=budget.id,
        revision=1,
        workflow="anything",
        scope="execution",
        payload={"seed_candidate": "same"},
    )
    assert replacement.token is None
    assert replacement.generation == 1
    assert service.get(budget.id, "alice").reserved_cents == 5
    with pytest.raises(BudgetConflictError):
        store.finish(owner.document["id"], claim_token=owner.token, status="succeeded", result={"checks": []})


def test_restart_waiver_cuts_at_the_checkpoint_time(database: Engine) -> None:
    """Compare timezone-aware settle times with the checkpoint time and credit only the later work."""
    service = BudgetService(engine=database)
    budget = service.create("alice", 40, idempotency_key="restart")
    service.attach_to_job(budget.id, "alice", "restart-job", expected_revision=budget.revision)

    def bill(key: str, cents: int) -> None:
        """Settle one run-phase call of the interrupted generation."""
        operation = service.reserve(
            budget.id,
            "alice",
            operation_key=key,
            generation=0,
            phase="run",
            cost_kind="model",
            request_fingerprint=key,
            price_snapshot={"version": "fixture-v1"},
            max_cents=cents,
        )
        service.mark_dispatched(operation.id, "alice")
        service.settle(operation.id, "alice", evidence_key=key, actual_cents=cents, evidence={"usage": key})

    bill("kept", 2)
    checkpoint = datetime.now(UTC)
    bill("lost", 3)
    service.fence_generation(budget.id, "alice", expected_generation=0)

    waived = service.waive_interrupted_work(budget.id, "alice", generation=0, settled_after=checkpoint)

    assert waived.billed_cents == 2
    with Session(database) as session:
        assert session.get(BillingCustomerModel, "alice").balance_cents == 48


def _lease(service: BudgetService, budget_id: str, key: str = "pool", slices: int = 4):
    """Reserve one fixture pool lease through the production database transaction."""
    return service.reserve_lease(
        budget_id,
        "alice",
        lease_key=key,
        slices=slices,
        generation=0,
        phase="setup",
        cost_kind="sandbox",
        request_fingerprint="fixture",
        price_snapshot={"version": "fixture-v1"},
        max_cents=10,
        role="runtime",
    )


def _budget_units(database: Engine, budget_id: str) -> tuple[int, int]:
    """Read the reserved and settled units of one budget."""
    with Session(database) as session:
        budget = session.get(ExecutionBudgetModel, budget_id)
        return budget.reserved_units, budget.settled_units


def test_lease_makes_one_budget_write_and_settles_each_box_once(database: Engine) -> None:
    """Reserve N slices with one budget write, settle each box once, and return the rest in one write."""
    service = BudgetService(engine=database)
    budget = service.create("alice", 50, idempotency_key="draft")
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        """Keep every SQL statement the ledger sends."""
        statements.append(statement)

    event.listen(database, "before_cursor_execute", record)
    try:
        slices = _lease(service, budget.id)
        lease_statements = list(statements)
        statements.clear()
        assert _lease(service, budget.id) == slices
        replay_statements = list(statements)
        statements.clear()
        costs = ["1.5", "2.5", "3"]
        for operation, cost in zip(slices, costs, strict=False):
            service.mark_dispatched(operation.id, "alice")
            for _ in range(2):
                service.settle(
                    operation.id, "alice", evidence_key=f"stop:{operation.id}", actual_cents=cost, evidence={"box": 1}
                )
        statements.clear()
        service.release_slices(budget.id, "alice", [operation.id for operation in slices])
        release_statements = list(statements)
    finally:
        event.remove(database, "before_cursor_execute", record)

    def budget_updates(sent: list[str]) -> int:
        """Count the writes to the budget row."""
        return sum(statement.lstrip().upper().startswith("UPDATE EXECUTION_BUDGETS") for statement in sent)

    assert len(slices) == 4
    assert budget_updates(lease_statements) == 1
    assert budget_updates(replay_statements) == 0
    assert budget_updates(release_statements) == 1
    reserved, settled = _budget_units(database, budget.id)
    assert reserved == 0
    assert settled == 7 * CENT_SCALE
    with Session(database) as session:
        states = session.scalars(
            select(ExecutionOperationModel.state).order_by(ExecutionOperationModel.operation_key)
        ).all()
        assert states == ["settled", "settled", "settled", "released"]
        assert session.scalar(select(func.count()).select_from(ExecutionUsageEvidenceModel)) == 3
        assert session.get(BillingCustomerModel, "alice").balance_cents == 43


def test_crashed_lease_returns_its_untaken_slices_on_fence(database: Engine) -> None:
    """Release the slices a crashed lease never handed out, and keep the settled box."""
    service = BudgetService(engine=database)
    budget = service.create("alice", 50, idempotency_key="draft")
    slices = _lease(service, budget.id)
    service.mark_dispatched(slices[0].id, "alice")
    service.settle(slices[0].id, "alice", evidence_key="stop", actual_cents=2, evidence={"box": 0})
    assert _budget_units(database, budget.id)[0] == 30 * CENT_SCALE
    service.fence_generation(budget.id, "alice", expected_generation=0)
    assert _budget_units(database, budget.id) == (0, 2 * CENT_SCALE)
    with Session(database) as session:
        states = session.scalars(
            select(ExecutionOperationModel.state).order_by(ExecutionOperationModel.operation_key)
        ).all()
        assert states == ["settled", "released", "released", "released"]


def test_parallel_duplicate_lease_reserves_once(database: Engine) -> None:
    """Serialize two concurrent leases with one key into one reservation."""
    service = BudgetService(engine=database)
    budget = service.create("alice", 50, idempotency_key="draft")
    barrier = Barrier(2)

    def run(_: int) -> tuple[str, ...]:
        """Race the same lease identity through admission."""
        barrier.wait(timeout=5)
        return tuple(operation.id for operation in _lease(service, budget.id))

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(run, range(2)))
    assert outcomes[0] == outcomes[1]
    assert _budget_units(database, budget.id)[0] == 40 * CENT_SCALE
    with pytest.raises(BudgetConflictError):
        _lease(service, budget.id, slices=3)


def test_lease_keys_match_exactly_on_postgres(database: Engine) -> None:
    """Keep a lease apart from another lease whose key shares a prefix or differs in case."""
    service = BudgetService(engine=database)
    budget = service.create("alice", 50, idempotency_key="draft")
    _lease(service, budget.id, key="a:slice-1", slices=1)
    assert len(_lease(service, budget.id, key="a", slices=2)) == 2
    _lease(service, budget.id, key="Pool", slices=1)
    assert len(_lease(service, budget.id, key="pool", slices=1)) == 1
    with pytest.raises(BudgetConflictError):
        _lease(service, budget.id, key="a", slices=1)
