"""Exercise actual PostgreSQL row locks against an explicitly supplied disposable database."""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Engine, create_engine, event, func, select, text
from sqlalchemy.orm import Session

from core.billing.budget_amounts import CENT_SCALE, wallet_reserved_cents
from core.billing.budgets import (
    AGENT_LEASE_COST_KIND,
    BudgetBoundExceededError,
    BudgetConflictError,
    BudgetInFlightError,
    BudgetService,
)
from core.config import app_env, settings
from core.storage.models import (
    AgentConversationModel,
    AgentStepModel,
    Base,
    BillingCustomerModel,
    BillingProviderKeyModel,
    ExecutionBudgetModel,
    ExecutionOperationModel,
    ExecutionUsageEvidenceModel,
    JobModel,
    StoragePackModel,
    TreeObjectModel,
    WalletLedgerModel,
    WorkspaceModel,
)
from core.storage.object_store import LocalObjectStore
from core.storage.preflights import PreflightStore, WizardPreflightModel
from core.storage.step_log_store import StepLogStore
from core.storage.workspace_store import WorkspaceStore
from core.worker.actors import LeaseExhaustedError, StepResult, User

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
            AgentConversationModel,
            JobModel,
            AgentStepModel,
            StoragePackModel,
            TreeObjectModel,
            WorkspaceModel,
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


def test_old_unconfirmed_hold_releases_on_postgres(database: Engine) -> None:
    """Free the account hold of a call whose bill never came, with time-zone-aware times (SKY-784)."""
    service = BudgetService(engine=database)
    budget = service.create("alice", 50, idempotency_key="draft")
    operation = _reserve(service, budget.id, "lost-bill")
    service.mark_dispatched(operation.id, "alice", "gen-lost")
    service.mark_pending(operation.id, "alice")
    with Session(database) as session:
        assert wallet_reserved_cents(session, "alice") == 40
        session.get(ExecutionOperationModel, operation.id).dispatched_at = datetime.now(UTC) - timedelta(minutes=20)
        session.commit()
    released = service.release_unconfirmed(
        operation.id, "alice", dispatched_before=datetime.now(UTC) - timedelta(minutes=15), reason="no bill"
    )
    assert (released.state, released.budget.billed_cents) == ("released", 0)
    with Session(database) as session:
        assert wallet_reserved_cents(session, "alice") == 0


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
        # Migration c4f1a7e9d2b6 (SKY-710) adds this column, and ExecutionBudgetModel maps it.
        connection.execute(text("ALTER TABLE execution_budgets ADD COLUMN workspace_id VARCHAR(36)"))
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


def _agent_lease(service: BudgetService, budget_id: str) -> str:
    """Reserve and dispatch one run-phase agent lease slice of 10 cents, as a worker's agent does."""
    (operation,) = service.reserve_lease(
        budget_id,
        "alice",
        lease_key="agent",
        slices=1,
        generation=0,
        phase="run",
        cost_kind=AGENT_LEASE_COST_KIND,
        request_fingerprint="agent",
        price_snapshot={"version": "agent-lease-v1"},
        max_cents=10,
    )
    service.mark_dispatched(operation.id, "alice")
    return operation.id


def test_fence_settles_a_dead_workers_lease_slice_on_postgres(database: Engine) -> None:
    """End an open lease slice at its last settle and release the rest of its coverage (SKY-829)."""
    service = BudgetService(engine=database)
    budget = service.create("alice", 50, idempotency_key="draft")
    service.attach_to_job(budget.id, "alice", "run-job", expected_revision=budget.revision)
    lease = _agent_lease(service, budget.id)
    service.settle(lease, "alice", evidence_key="step-1", actual_cents=3, evidence={"step": 1}, final=False)
    assert _budget_units(database, budget.id) == (7 * CENT_SCALE, 3 * CENT_SCALE)

    service.fence_generation(budget.id, "alice", expected_generation=0)

    assert _budget_units(database, budget.id) == (0, 3 * CENT_SCALE)
    with Session(database) as session:
        assert session.get(ExecutionOperationModel, lease).state == "settled"
        assert session.get(BillingCustomerModel, "alice").balance_cents == 47


@pytest.mark.parametrize(
    "zone", [UTC, timezone(timedelta(hours=-8))], ids=["checkpoint-in-utc", "checkpoint-in-another-zone"]
)
def test_lease_waiver_keeps_the_usage_that_settled_before_the_checkpoint_on_postgres(
    database: Engine, zone: timezone
) -> None:
    """Credit only a lease's usage after the checkpoint, with aware times in any zone (SKY-829).

    The session time zone is neither UTC nor the machine's zone, so a naive time on either side of the
    ``created_at <= settled_after`` compare moves by hours and keeps the wrong record.
    """
    database.dispose()

    @event.listens_for(database, "connect")
    def set_zone(connection, record) -> None:
        """Give every new session a zone that differs from UTC and from this machine's zone."""
        with connection.cursor() as cursor:
            cursor.execute("SET TIME ZONE 'Asia/Kolkata'")

    service = BudgetService(engine=database)
    budget = service.create("alice", 40, idempotency_key="restart")
    service.attach_to_job(budget.id, "alice", "restart-job", expected_revision=budget.revision)
    lease = _agent_lease(service, budget.id)
    service.settle(lease, "alice", evidence_key="kept", actual_cents=2, evidence={"step": "kept"}, final=False)
    checkpoint = datetime.now(UTC).astimezone(zone)
    service.settle(lease, "alice", evidence_key="lost", actual_cents=5, evidence={"step": "lost"}, final=False)
    service.fence_generation(budget.id, "alice", expected_generation=0)

    waived = service.waive_interrupted_work(budget.id, "alice", generation=0, settled_after=checkpoint)

    assert waived.billed_cents == 2
    assert _budget_units(database, budget.id) == (0, 2 * CENT_SCALE)
    with Session(database) as session:
        assert session.get(BillingCustomerModel, "alice").balance_cents == 48


def test_a_fenced_lease_late_settle_keeps_evidence_and_bills_nothing_on_postgres(database: Engine) -> None:
    """A fenced worker's late call is repeated by the resume, so its settle charges nothing (SKY-829)."""
    service = BudgetService(engine=database)
    budget = service.create("alice", 50, idempotency_key="draft")
    service.attach_to_job(budget.id, "alice", "run-job", expected_revision=budget.revision)
    lease = _agent_lease(service, budget.id)
    service.settle(lease, "alice", evidence_key="step-1", actual_cents=2, evidence={"step": 1}, final=False)
    service.fence_generation(budget.id, "alice", expected_generation=0)

    late = service.settle(lease, "alice", evidence_key="late", actual_cents=4, evidence={"step": 2}, final=True)

    assert late.budget.billed_cents == 2
    assert _budget_units(database, budget.id) == (0, 2 * CENT_SCALE)
    with Session(database) as session:
        evidence = session.scalars(
            select(ExecutionUsageEvidenceModel).where(ExecutionUsageEvidenceModel.evidence_key == "late")
        ).one()
        assert (evidence.actual_units, evidence.billed_cents, evidence.issue) == (4 * CENT_SCALE, 0, None)
        assert session.get(ExecutionOperationModel, lease).actual_units == 2 * CENT_SCALE
        assert session.get(BillingCustomerModel, "alice").balance_cents == 48


def _fenced_lease(service: BudgetService) -> tuple[str, str]:
    """Settle 2 cents on a run's lease slice, then fence its generation.

    Args:
        service: The budget service.

    Returns:
        The budget id and the lease id.
    """
    budget = service.create("alice", 50, idempotency_key="draft")
    service.attach_to_job(budget.id, "alice", "run-job", expected_revision=budget.revision)
    lease = _agent_lease(service, budget.id)
    service.settle(lease, "alice", evidence_key="step-1", actual_cents=2, evidence={"step": 1}, final=False)
    service.fence_generation(budget.id, "alice", expected_generation=0)
    return budget.id, lease


def _budget_state(database: Engine, budget_id: str) -> tuple[str, str | None, int]:
    """Read the state, blocked reason and billed cents of one budget."""
    with Session(database) as session:
        budget = session.get(ExecutionBudgetModel, budget_id)
        return budget.state, budget.blocked_reason, budget.billed_cents


def test_a_fenced_late_settle_above_the_bound_does_not_block_the_resumed_budget_on_postgres(
    database: Engine,
) -> None:
    """The resumed generation uses the same budget, so an old worker's bad settle must not block it (SKY-829)."""
    service = BudgetService(engine=database)
    budget_id, lease = _fenced_lease(service)
    before = _budget_state(database, budget_id)

    with pytest.raises(BudgetBoundExceededError):
        service.settle(lease, "alice", evidence_key="late", actual_cents=15, evidence={"step": 2}, final=True)

    assert _budget_state(database, budget_id) == before == ("attached", None, 2)
    with Session(database) as session:
        evidence = session.scalars(
            select(ExecutionUsageEvidenceModel).where(ExecutionUsageEvidenceModel.evidence_key == "late")
        ).one()
        assert (evidence.billed_cents, evidence.issue) == (0, "bound_exceeded")
        assert session.get(ExecutionOperationModel, lease).state == "settled"
        assert session.get(BillingCustomerModel, "alice").balance_cents == 48


def test_a_fenced_late_settle_cannot_go_below_an_earlier_late_settle_on_postgres(database: Engine) -> None:
    """Usage is cumulative across late settles too, not only from the value at fence time (SKY-829)."""
    service = BudgetService(engine=database)
    _, lease = _fenced_lease(service)
    service.settle(lease, "alice", evidence_key="late-1", actual_cents=6, evidence={"step": 2}, final=False)

    with pytest.raises(BudgetConflictError, match="cannot decrease"):
        service.settle(lease, "alice", evidence_key="late-2", actual_cents=4, evidence={"step": 3}, final=False)


def test_a_fenced_late_settle_floor_counts_scope_usage_when_the_wallet_charge_is_smaller_on_postgres(
    database: Engine,
) -> None:
    """A BYOK-shaped late settle charges the wallet less than its scope, so the scope floor must hold (SKY-829)."""
    service = BudgetService(engine=database)
    _, lease = _fenced_lease(service)
    service.settle(
        lease, "alice", evidence_key="late-1", actual_cents=6, actual_wallet_cents=2, evidence={"step": 2}, final=False
    )

    with pytest.raises(BudgetConflictError, match="cannot decrease"):
        service.settle(
            lease,
            "alice",
            evidence_key="late-2",
            actual_cents=4,
            actual_wallet_cents=3,
            evidence={"step": 3},
            final=False,
        )


def test_a_fenced_late_settle_floor_counts_wallet_usage_when_the_scope_grows_on_postgres(database: Engine) -> None:
    """A later late settle can raise its scope, but its wallet charge must not go below an earlier one (SKY-829)."""
    service = BudgetService(engine=database)
    _, lease = _fenced_lease(service)
    service.settle(
        lease, "alice", evidence_key="late-1", actual_cents=6, actual_wallet_cents=4, evidence={"step": 2}, final=False
    )

    with pytest.raises(BudgetConflictError, match="cannot decrease"):
        service.settle(
            lease,
            "alice",
            evidence_key="late-2",
            actual_cents=7,
            actual_wallet_cents=3,
            evidence={"step": 3},
            final=False,
        )


def test_a_refused_fenced_late_settle_does_not_raise_the_floor_on_postgres(database: Engine) -> None:
    """A late settle that the bound refused counts for nothing, as on the live path (SKY-829)."""
    service = BudgetService(engine=database)
    _, lease = _fenced_lease(service)
    with pytest.raises(BudgetBoundExceededError):
        service.settle(lease, "alice", evidence_key="late-1", actual_cents=15, evidence={"step": 2}, final=False)

    late = service.settle(lease, "alice", evidence_key="late-2", actual_cents=8, evidence={"step": 3}, final=True)

    assert late.budget.billed_cents == 2
    with Session(database) as session:
        evidence = session.scalars(
            select(ExecutionUsageEvidenceModel).where(ExecutionUsageEvidenceModel.evidence_key == "late-2")
        ).one()
        assert (evidence.actual_units, evidence.billed_cents, evidence.issue) == (8 * CENT_SCALE, 0, None)


def test_a_current_generation_lease_cannot_settle_again_after_its_final_settle_on_postgres(
    database: Engine,
) -> None:
    """Only a fenced generation's lease takes a late settle; a live lease that settled is closed (SKY-829)."""
    service = BudgetService(engine=database)
    budget = service.create("alice", 50, idempotency_key="draft")
    service.attach_to_job(budget.id, "alice", "run-job", expected_revision=budget.revision)
    lease = _agent_lease(service, budget.id)
    service.settle(lease, "alice", evidence_key="step-1", actual_cents=3, evidence={"step": 1}, final=True)

    with pytest.raises(BudgetConflictError, match="dispatched attempt"):
        service.settle(lease, "alice", evidence_key="step-2", actual_cents=4, evidence={"step": 2}, final=True)


def test_lease_waiver_returns_only_the_grant_that_the_lost_settle_drew_on_postgres(database: Engine) -> None:
    """The grant that the kept settle drew stays spent; the lost settle's grant and paid cents return (SKY-829)."""
    with Session(database) as session:
        session.get(BillingCustomerModel, "alice").grant_remaining = 4
        session.commit()
    service = BudgetService(engine=database)
    budget = service.create("alice", 40, idempotency_key="restart")
    service.attach_to_job(budget.id, "alice", "restart-job", expected_revision=budget.revision)
    lease = _agent_lease(service, budget.id)
    service.settle(lease, "alice", evidence_key="kept", actual_cents=2, evidence={"step": "kept"}, final=False)
    checkpoint = datetime.now(UTC)
    # The lost settle draws the last 2 grant cents and 1 paid cent.
    service.settle(lease, "alice", evidence_key="lost", actual_cents=5, evidence={"step": "lost"}, final=False)
    service.fence_generation(budget.id, "alice", expected_generation=0)

    waived = service.waive_interrupted_work(budget.id, "alice", generation=0, settled_after=checkpoint)

    assert waived.billed_cents == 2
    with Session(database) as session:
        wallet = session.get(BillingCustomerModel, "alice")
        assert (wallet.grant_remaining, wallet.balance_cents) == (2, 50)


def test_lease_waiver_keeps_a_settle_saved_at_the_checkpoint_time_on_postgres(database: Engine) -> None:
    """A settle at the exact checkpoint time is part of the kept work (SKY-829)."""
    service = BudgetService(engine=database)
    budget = service.create("alice", 40, idempotency_key="restart")
    service.attach_to_job(budget.id, "alice", "restart-job", expected_revision=budget.revision)
    lease = _agent_lease(service, budget.id)
    service.settle(lease, "alice", evidence_key="kept", actual_cents=2, evidence={"step": "kept"}, final=False)
    with Session(database) as session:
        checkpoint = session.scalars(
            select(ExecutionUsageEvidenceModel.created_at).where(ExecutionUsageEvidenceModel.evidence_key == "kept")
        ).one()
    service.settle(lease, "alice", evidence_key="lost", actual_cents=5, evidence={"step": "lost"}, final=False)
    service.fence_generation(budget.id, "alice", expected_generation=0)

    waived = service.waive_interrupted_work(budget.id, "alice", generation=0, settled_after=checkpoint)

    assert waived.billed_cents == 2


def test_lease_waiver_does_not_keep_a_refused_settle_from_before_the_checkpoint_on_postgres(
    database: Engine,
) -> None:
    """A refused settle billed nothing, so it is no kept work, also when the lease settles later (SKY-829)."""
    service = BudgetService(engine=database)
    budget = service.create("alice", 40, idempotency_key="restart")
    service.attach_to_job(budget.id, "alice", "restart-job", expected_revision=budget.revision)
    lease = _agent_lease(service, budget.id)
    with pytest.raises(BudgetBoundExceededError):
        service.settle(lease, "alice", evidence_key="refused", actual_cents=15, evidence={"step": 1}, final=False)
    checkpoint = datetime.now(UTC)
    service.settle(lease, "alice", evidence_key="lost", actual_cents=5, evidence={"step": 2}, final=True)

    waived = service.waive_interrupted_work(budget.id, "alice", generation=0, settled_after=checkpoint)

    assert waived.billed_cents == 0
    with Session(database) as session:
        assert session.get(BillingCustomerModel, "alice").balance_cents == 50


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


@pytest.mark.parametrize("reason", ["user_cancelled", "user_paused"])
def test_an_automatic_resume_never_reopens_a_user_stop_on_postgres(database: Engine, reason: str) -> None:
    """Keep a user's stop closed against a racing sweep resume; only the user's own resume reopens it (SKY-814).

    Args:
        database: Engine on the disposable test schema.
        reason: The reason the user's cancel or pause closes admission with.
    """
    service = BudgetService(engine=database)
    budget = service.create("alice", 40, idempotency_key="sweep-race")
    service.attach_to_job(budget.id, "alice", "sweep-race-job", expected_revision=budget.revision)
    generation = service.get(budget.id, "alice").generation
    barrier = Barrier(2)

    def run(action: str) -> None:
        """Start the user's stop and the sweep's resume together on a budget that a failure closed.

        Args:
            action: ``stop`` for the user's stop, ``resume`` for the sweep's resume.
        """
        barrier.wait(timeout=5)
        if action == "stop":
            service.stop_admission(budget.id, "alice", reason=reason)
        else:
            service.resume_admission(budget.id, "alice", expected_generation=generation, automatic=True)

    for _ in range(5):
        service.stop_admission(budget.id, "alice", reason="job_failed")
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(run, ["stop", "resume"]))
        closed = service.get(budget.id, "alice")
        assert (closed.state, closed.blocked_reason) == ("closed", reason)

    swept = service.resume_admission(budget.id, "alice", expected_generation=generation, automatic=True)
    assert (swept.state, swept.blocked_reason) == ("closed", reason)
    resumed = service.resume_admission(budget.id, "alice", expected_generation=generation)
    assert (resumed.state, resumed.blocked_reason) == ("attached", None)


@pytest.mark.parametrize("reason", ["user_cancelled", "user_paused"])
def test_a_later_non_user_stop_keeps_the_user_stop_reason_on_postgres(database: Engine, reason: str) -> None:
    """Keep a user's stop reason against a later budget_reached; only the user's own resume reopens it (SKY-814).

    Args:
        database: Engine on the disposable test schema.
        reason: The reason the user's cancel or pause closes admission with.
    """
    service = BudgetService(engine=database)
    budget = service.create("alice", 40, idempotency_key="user-stop")
    service.attach_to_job(budget.id, "alice", "user-stop-job", expected_revision=budget.revision)
    generation = service.get(budget.id, "alice").generation
    service.stop_admission(budget.id, "alice", reason=reason)

    kept = service.stop_admission(budget.id, "alice", reason="budget_reached")
    assert (kept.state, kept.blocked_reason) == ("closed", reason)
    swept = service.resume_admission(budget.id, "alice", expected_generation=generation, automatic=True)
    assert (swept.state, swept.blocked_reason) == ("closed", reason)
    resumed = service.resume_admission(budget.id, "alice", expected_generation=generation)
    assert (resumed.state, resumed.blocked_reason) == ("attached", None)


def test_a_thousand_agents_spend_chunked_leases_within_one_budget(database: Engine, tmp_path: Path) -> None:
    """Run 1,000 agents of one run in parallel against one workspace budget, under real row locks (SKY-717)."""
    service = BudgetService(engine=database)
    budget = service.create("alice", 40, idempotency_key="workspace")
    budget = service.attach_to_job(budget.id, "alice", "run-1", expected_revision=budget.revision)
    with Session(database) as session:
        session.add(JobModel(optimization_id="run-1", username="alice", execution_generation=1))
        session.commit()
    user = User("alice", engine=database, budgets=service)
    WorkspaceStore(database).set_budget(user.workspace().id, budget.id)
    step_log = StepLogStore(database, LocalObjectStore(tmp_path / "objects"), "worker-1", flush_window_seconds=0.05)
    run = user.workspace().start_run(
        "run-1",
        step_log=step_log,
        execution_generation=1,
        budget_generation=budget.generation,
        grant_list_id="grants-1",
        chunk_cents=2,
        lease_slices=8,
    )
    agents = [run.spawn(f"agent-{index}") for index in range(1000)]
    paid: list[str] = []

    def drive(index: int) -> None:
        """Run up to three paid steps of one agent, then close its lease.

        Args:
            index: The agent's position.
        """
        agent = agents[index]

        def work() -> StepResult:
            """Record the paid call and report its fixed cost.

            Returns:
                A model step result.
            """
            paid.append(agent.agent_id)
            return StepResult(body={}, cost_cents=Decimal("0.5"))

        try:
            for _ in range(3):
                agent.step("model", 1, work)
        except LeaseExhaustedError:
            pass
        finally:
            agent.close()

    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(drive, range(len(agents))))
        run.close()
    finally:
        step_log.close()

    reserved, settled = _budget_units(database, budget.id)
    assert reserved == 0
    assert 0 < settled <= 40 * CENT_SCALE
    assert settled == len(paid) * CENT_SCALE // 2
    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(AgentStepModel)) == len(paid)
        step_keys = session.scalar(
            select(func.count())
            .select_from(ExecutionUsageEvidenceModel)
            .where(ExecutionUsageEvidenceModel.evidence_key.like("run-1:%"))
        )
        assert step_keys == len(paid)
        billed = service.get(budget.id, "alice").billed_cents
        assert session.get(BillingCustomerModel, "alice").balance_cents == 50 - billed
