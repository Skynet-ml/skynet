"""Tests for the User, Workspace, Run and Agent actors and their chunked budget leases (SKY-717).

The tests use a SQLite file, the local-filesystem object store and fake paid
work: no sandbox, no model call and no network.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from core.billing.budgets import (
    MAX_LEASE_SLICES,
    BudgetBoundExceededError,
    BudgetConflictError,
    BudgetFundingLostError,
    BudgetService,
    BudgetUnreconciledError,
)
from core.billing.signals import BudgetReached
from core.storage import storage_version
from core.storage.models import (
    AgentStepModel,
    Base,
    BillingCustomerModel,
    ExecutionOperationModel,
    ExecutionUsageEvidenceModel,
    JobModel,
    WorkspaceModel,
)
from core.storage.object_store import LocalObjectStore
from core.storage.step_log_store import SEQ_MAX, StepLogStore
from core.storage.workspace_store import WorkspaceStore, workspace_id_for
from core.worker.actors import (
    LEASE_CLOSE_KEY,
    RUN_AGENT_ID,
    Agent,
    LeaseExhaustedError,
    Run,
    StepResult,
    User,
    Workspace,
)

RUN_ID = "run-1"
WALLET_CENTS = 10_000


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Yield a SQLite file engine with a funded wallet for ``alice`` and the job ``run-1``.

    Args:
        tmp_path: Pytest's temporary folder.

    Yields:
        The engine.
    """
    database = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}", connect_args={"timeout": 30})
    Base.metadata.create_all(database)
    with Session(database) as session:
        session.add(
            BillingCustomerModel(
                username="alice", stripe_customer_id="local-alice", balance_cents=WALLET_CENTS, grant_remaining=0
            )
        )
        session.add(JobModel(optimization_id=RUN_ID, username="alice", execution_generation=1))
        session.commit()
    yield database
    database.dispose()


@pytest.fixture
def step_log(engine: Engine, tmp_path: Path) -> Iterator[StepLogStore]:
    """Yield a step log with a short flush window, and close it after the test.

    Args:
        engine: The database.
        tmp_path: Pytest's temporary folder.

    Yields:
        The step log.
    """
    store = StepLogStore(engine, LocalObjectStore(tmp_path / "objects"), "worker-1", flush_window_seconds=0.05)
    yield store
    store.close()


def _start_run(
    engine: Engine, step_log: StepLogStore, *, total_cents: int, chunk_cents: int = 2, lease_slices: int = 4
) -> tuple[Run, BudgetService, str]:
    """Wake alice's workspace with a new run budget and start ``run-1`` on it.

    Args:
        engine: The database.
        step_log: The worker's step log.
        total_cents: The budget limit.
        chunk_cents: The size of one agent lease.
        lease_slices: How many leases one ledger call reserves.

    Returns:
        The run, the ledger and the budget id.
    """
    budgets = BudgetService(engine=engine)
    budget = budgets.create("alice", total_cents, idempotency_key="workspace-budget")
    budget = budgets.attach_to_job(budget.id, "alice", RUN_ID, expected_revision=budget.revision)
    user = User("alice", engine=engine, budgets=budgets)
    WorkspaceStore(engine).set_budget(user.workspace().id, budget.id)
    run = user.workspace().start_run(
        RUN_ID,
        step_log=step_log,
        execution_generation=1,
        budget_generation=budget.generation,
        grant_list_id="grants-1",
        chunk_cents=chunk_cents,
        lease_slices=lease_slices,
    )
    return run, budgets, budget.id


def _model_step(cost: str = "0.5") -> StepResult:
    """Return a model step result with a fixed cost.

    Args:
        cost: The cost in cents.

    Returns:
        The result.
    """
    return StepResult(body={"response": "ok"}, cost_cents=Decimal(cost))


def _fail_first(call: Callable[..., Any], *, commit: bool, times: int = 1) -> Callable[..., Any]:
    """Make the first calls of a ledger call fail, as a lost connection does.

    Args:
        call: The real call.
        commit: Whether a failed call commits first, so that only its reply is lost.
        times: How many calls fail.

    Returns:
        The call with the failures.
    """
    left = [times]

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        """Fail while failures are left, else run the real call.

        Args:
            *args: The call's arguments.
            **kwargs: The call's keyword arguments.

        Returns:
            The call's result.

        Raises:
            ConnectionError: While failures are left.
        """
        if left[0] <= 0:
            return call(*args, **kwargs)
        left[0] -= 1
        if commit:
            call(*args, **kwargs)
        raise ConnectionError("lost")

    return wrapped


def test_waking_a_workspace_runs_the_lazy_migration_first(engine: Engine, monkeypatch) -> None:
    """The workspace that ``wake`` loads already has the upgrade step's write and the new version."""
    calls: list[str] = []

    def upgrade(session: Session, workspace_id: str) -> None:
        """Record the step and write a policy key in the upgrade transaction.

        Args:
            session: The upgrade transaction.
            workspace_id: The workspace to upgrade.
        """
        calls.append(workspace_id)
        row = session.get(WorkspaceModel, workspace_id)
        row.policy = {**row.policy, "upgraded": True}

    with Session(engine) as session:
        session.add(WorkspaceModel(id=workspace_id_for("alice"), username="alice", policy={}, storage_version=1))
        session.commit()
    monkeypatch.setattr(storage_version, "UPGRADES", (upgrade,))

    workspace = User("alice", engine=engine, budgets=BudgetService(engine=engine)).workspace()

    assert calls == [workspace.id]
    assert workspace.policy == {"upgraded": True}
    assert WorkspaceStore(engine).get(workspace.id).storage_version == 2


def test_a_user_has_one_workspace(engine: Engine) -> None:
    """Two wakes of the same user give the same workspace."""
    user = User("alice", engine=engine, budgets=BudgetService(engine=engine))

    assert user.workspace().id == user.workspace().id == workspace_id_for("alice")


def test_waking_another_users_workspace_changes_nothing(engine: Engine, monkeypatch) -> None:
    """The owner check comes before the upgrade, so another user cannot start a write."""
    calls: list[str] = []

    def upgrade(session: Session, workspace_id: str) -> None:
        """Record the step.

        Args:
            session: The upgrade transaction.
            workspace_id: The workspace to upgrade.
        """
        calls.append(workspace_id)

    with Session(engine) as session:
        session.add(WorkspaceModel(id=workspace_id_for("alice"), username="alice", policy={}, storage_version=1))
        session.commit()
    monkeypatch.setattr(storage_version, "UPGRADES", (upgrade,))
    bob = User("bob", engine=engine, budgets=BudgetService(engine=engine))

    with pytest.raises(PermissionError):
        Workspace.wake(bob, workspace_id_for("alice"))

    assert calls == []
    assert WorkspaceStore(engine).get(workspace_id_for("alice")).storage_version == 1


def test_a_run_refuses_a_workspace_with_no_budget(engine: Engine, step_log: StepLogStore) -> None:
    """The run itself checks the budget, so neither ``start_run`` nor a direct build holds a ``None`` budget id."""
    workspace = User("alice", engine=engine, budgets=BudgetService(engine=engine)).workspace()

    for start in (workspace.start_run, lambda *args, **kwargs: Run(workspace, *args, **kwargs)):
        with pytest.raises(LookupError):
            start(
                RUN_ID,
                step_log=step_log,
                execution_generation=1,
                budget_generation=0,
                grant_list_id="grants-1",
                chunk_cents=Decimal(2),
                lease_slices=4,
            )


def test_each_agent_commits_only_its_own_steps(engine: Engine, step_log: StepLogStore) -> None:
    """Each agent's steps carry its own id, and no two agents share an id or a step row."""
    run, _, _ = _start_run(engine, step_log, total_cents=100)
    first = run.spawn("agent-a")
    second = run.spawn("agent-b")
    with pytest.raises(ValueError):
        run.spawn("agent-a")
    with pytest.raises(ValueError):
        run.spawn(RUN_AGENT_ID)

    ids = [
        first.step("model", 1, _model_step),
        second.step("model", 1, _model_step),
        first.step("model", 1, _model_step),
    ]
    run.close()
    step_log.flush()

    assert ids == [f"{RUN_ID}:agent-a:0", f"{RUN_ID}:agent-b:0", f"{RUN_ID}:agent-a:1"]
    with Session(engine) as session:
        rows = session.execute(select(AgentStepModel.step_id, AgentStepModel.agent_id)).all()
    assert sorted(rows) == sorted((step_id, step_id.split(":")[1]) for step_id in ids)


def test_the_run_commits_population_and_champion_once_per_generation(engine: Engine, step_log: StepLogStore) -> None:
    """Each generation is one commit in the run's own log, with the population and the champion."""
    run, _, _ = _start_run(engine, step_log, total_cents=100)

    first = run.end_generation([{"id": "c1"}, {"id": "c2"}], {"id": "c2"})
    second = run.end_generation([{"id": "c3"}], {"id": "c3"})
    step_log.flush()

    assert (first, second) == (f"{RUN_ID}:{RUN_AGENT_ID}:0", f"{RUN_ID}:{RUN_AGENT_ID}:1")
    assert (run.generation, run.population, run.champion) == (2, [{"id": "c3"}], {"id": "c3"})
    stored = step_log.get_step(first)
    assert stored["body"] == {"generation": 0, "population": [{"id": "c1"}, {"id": "c2"}], "champion": {"id": "c2"}}
    with Session(engine) as session:
        assert (
            session.scalar(
                select(func.count()).select_from(AgentStepModel).where(AgentStepModel.agent_id == RUN_AGENT_ID)
            )
            == 2
        )


def test_an_agent_takes_a_new_chunk_only_when_its_lease_runs_low(engine: Engine, step_log: StepLogStore) -> None:
    """A lease of 2 cents covers three steps of 0.5 with a bound of 1; the fourth step takes a new chunk."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=100, chunk_cents=2, lease_slices=4)
    agent = run.spawn("agent-a")

    for _ in range(4):
        agent.step("model", 1, _model_step)
    with Session(engine) as session:
        operations = session.scalars(select(ExecutionOperationModel).order_by(ExecutionOperationModel.created_at)).all()
        taken = [operation.state for operation in operations if operation.dispatched_at is not None]
    # One ledger call reserved four slices; the agent took two of them.
    assert len(operations) == 4
    assert sorted(taken) == ["pending", "settled"]
    run.close()

    budget = budgets.get(budget_id, "alice")
    assert (budget.run_spent_cents, budget.reserved_cents) == (Decimal(2), Decimal(0))


def test_an_agent_with_no_lease_stops_before_paid_work(engine: Engine, step_log: StepLogStore) -> None:
    """When the budget's room is below one chunk, the run stops on its budget and the work never starts (SKY-914).

    A capped run whose cap has less room than one lease must end as a budget stop with the ledger's message, not
    as an internal ``LeaseExhaustedError`` failure.
    """
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=1, chunk_cents=2, lease_slices=4)
    agent = run.spawn("agent-a")
    calls: list[int] = []

    def work() -> StepResult:
        """Record the paid call.

        Returns:
            A model step result.
        """
        calls.append(1)
        return _model_step()

    with pytest.raises(BudgetReached, match="limit remain"):
        agent.step("model", 1, work)
    # The stop does not block the run, so each later step gets the same budget stop.
    with pytest.raises(BudgetReached):
        agent.step("model", 1, work)
    run.close()
    step_log.flush()

    assert calls == []
    assert agent.next_seq == 0
    assert budgets.get(budget_id, "alice").run_spent_cents == 0
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AgentStepModel)) == 0


def test_a_step_bound_above_one_lease_is_refused(engine: Engine, step_log: StepLogStore) -> None:
    """A step that one lease cannot cover is refused before any chunk is taken."""
    run, _, _ = _start_run(engine, step_log, total_cents=100, chunk_cents=2)

    with pytest.raises(ValueError):
        run.spawn("agent-a").step("model", 3, _model_step)


@pytest.mark.parametrize("bound", ["0.0000000015", "NaN", "Infinity"])
def test_a_step_bound_the_ledger_cannot_hold_is_refused_before_the_work(
    engine: Engine, step_log: StepLogStore, bound: str
) -> None:
    """A bound with more than nine fractional digits, or not finite, is refused before any paid work."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=20)
    calls: list[int] = []

    def work() -> StepResult:
        """Record the paid call.

        Returns:
            A step result whose cost is the bound.
        """
        calls.append(1)
        return StepResult(body={}, cost_cents=bound)

    with pytest.raises(ValueError):
        run.spawn("agent-a").step("model", bound, work)
    run.close()

    assert calls == []
    assert budgets.get(budget_id, "alice").run_spent_cents == Decimal(0)


def test_a_cost_equal_to_a_nine_digit_bound_is_billed_exactly(engine: Engine, step_log: StepLogStore) -> None:
    """A bound the ledger can hold takes a cost equal to it, and the close bills that cost."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=20)
    run.spawn("agent-a").step("model", "0.000000002", lambda: StepResult(body={}, cost_cents="0.000000002"))
    run.close()

    budget = budgets.get(budget_id, "alice")
    assert (budget.run_spent_cents, budget.reserved_cents) == (Decimal("0.000000002"), Decimal(0))


@pytest.mark.parametrize("chunk_cents", [Decimal("0.0000000015"), Decimal(1000000001)])
def test_a_chunk_size_the_ledger_cannot_hold_is_refused(
    engine: Engine, step_log: StepLogStore, chunk_cents: Decimal
) -> None:
    """A chunk with more than nine fractional digits, or above the ledger maximum, cannot start a run."""
    with pytest.raises(ValueError):
        _start_run(engine, step_log, total_cents=20, chunk_cents=chunk_cents)


@pytest.mark.parametrize("lease_slices", [257, True, 4.0])
def test_a_slice_count_the_ledger_refuses_cannot_start_a_run(
    engine: Engine, step_log: StepLogStore, lease_slices: Any
) -> None:
    """A slice count above ``MAX_LEASE_SLICES``, a bool or a float cannot start a run."""
    with pytest.raises(ValueError):
        _start_run(engine, step_log, total_cents=20, lease_slices=lease_slices)


def test_the_ledger_maximum_slice_count_is_accepted(engine: Engine, step_log: StepLogStore) -> None:
    """A pool of ``MAX_LEASE_SLICES`` slices starts and pays a step."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=600, lease_slices=MAX_LEASE_SLICES)
    run.spawn("agent-a").step("model", 1, lambda: StepResult(body={}, cost_cents=1))
    run.close()

    budget = budgets.get(budget_id, "alice")
    assert (budget.run_spent_cents, budget.reserved_cents) == (Decimal(1), Decimal(0))


@pytest.mark.parametrize(
    "change",
    [
        {"run_id": "r" * 37},
        {"run_id": "run:1"},
        {"run_id": ""},
        {"grant_list_id": "g" * 65},
        {"grant_list_id": "grants\n1"},
        {"grant_list_id": ""},
        {"execution_generation": True},
        {"execution_generation": 1.0},
        {"execution_generation": "1"},
        {"execution_generation": 2**31},
        {"execution_generation": -1},
        {"budget_generation": -1},
        {"budget_generation": 1.0},
        {"budget_generation": 2**63},
        {"next_generation": -1},
        {"next_generation": True},
        {"next_generation": SEQ_MAX + 1},
    ],
)
def test_a_run_input_the_step_log_or_the_ledger_refuses_cannot_start_a_run(
    engine: Engine, step_log: StepLogStore, change: dict[str, Any]
) -> None:
    """An id or a generation that the step log or the ledger refuses later is refused when the run starts."""
    budgets = BudgetService(engine=engine)
    budget = budgets.create("alice", 20, idempotency_key="workspace-budget")
    user = User("alice", engine=engine, budgets=budgets)
    WorkspaceStore(engine).set_budget(user.workspace().id, budget.id)
    arguments: dict[str, Any] = {
        "run_id": RUN_ID,
        "step_log": step_log,
        "execution_generation": 1,
        "budget_generation": budget.generation,
        "grant_list_id": "grants-1",
        "chunk_cents": 2,
        "lease_slices": 4,
    }
    with pytest.raises(ValueError):
        user.workspace().start_run(**{**arguments, **change})


@pytest.mark.parametrize(
    "change",
    [
        {"agent_id": "a" * 65},
        {"agent_id": ""},
        {"agent_id": "agent-é"},
        {"agent_id": "agent\x00a"},
        {"next_seq": -1},
        {"next_seq": True},
        {"next_seq": SEQ_MAX + 1},
    ],
)
def test_an_agent_input_the_step_log_refuses_cannot_spawn(
    engine: Engine, step_log: StepLogStore, change: dict[str, Any]
) -> None:
    """An agent id or a next seq that the step log refuses at commit is refused at spawn."""
    run, _budgets, _budget_id = _start_run(engine, step_log, total_cents=20)
    arguments: dict[str, Any] = {"agent_id": "agent-a", "next_seq": 0}
    with pytest.raises(ValueError):
        run.spawn(**{**arguments, **change})
    run.close()


def test_a_step_kind_the_step_log_refuses_fails_before_the_work(engine: Engine, step_log: StepLogStore) -> None:
    """An unknown step kind fails before any paid work."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=20)
    calls: list[int] = []

    def work() -> StepResult:
        """Record the paid call.

        Returns:
            A step result with a cost of 1.
        """
        calls.append(1)
        return StepResult(body={}, cost_cents=1)

    with pytest.raises(ValueError):
        run.spawn("agent-a").step("shell", 1, work)
    run.close()

    assert calls == []
    assert budgets.get(budget_id, "alice").run_spent_cents == Decimal(0)


def test_a_step_past_the_last_seq_fails_before_the_work(engine: Engine, step_log: StepLogStore) -> None:
    """An agent whose next seq is past ``SEQ_MAX`` fails before any paid work."""
    run, _budgets, _budget_id = _start_run(engine, step_log, total_cents=20)
    agent = run.spawn("agent-a", next_seq=SEQ_MAX)
    agent.next_seq = SEQ_MAX + 1
    calls: list[int] = []
    with pytest.raises(ValueError):
        agent.step("model", 1, lambda: StepResult(body={}, cost_cents=calls.append(1) or 1))
    run.close()

    assert calls == []


def test_two_steps_at_the_last_seq_pay_for_one_call_only(engine: Engine, step_log: StepLogStore) -> None:
    """A step that waits for the agent at ``SEQ_MAX`` sees the used seq and does no paid work."""
    run, _budgets, _budget_id = _start_run(engine, step_log, total_cents=20)
    agent = run.spawn("agent-a", next_seq=SEQ_MAX)
    entered, gate = threading.Event(), threading.Event()
    paid: list[int] = []
    errors: list[Exception] = []

    def work() -> StepResult:
        """Record the paid call, and hold the first one until the second step waits for the agent.

        Returns:
            A step result with a cost of 1.
        """
        paid.append(1)
        if len(paid) == 1:
            entered.set()
            gate.wait(5)
        return StepResult(body={}, cost_cents=1)

    def drive() -> None:
        """Run one step and keep its error."""
        try:
            agent.step("model", 1, work)
        except Exception as error:
            errors.append(error)

    first = threading.Thread(target=drive)
    first.start()
    assert entered.wait(5)
    second = threading.Thread(target=drive)
    second.start()
    # The second step needs time to pass its unlocked checks and wait for the agent lock.
    second.join(0.3)
    gate.set()
    first.join(5)
    second.join(5)
    run.close()

    assert len(paid) == 1
    assert [type(error) for error in errors] == [ValueError]


def test_ids_at_their_limits_commit_a_durable_step(engine: Engine, step_log: StepLogStore) -> None:
    """A 36-character run id, a 64-character agent id and a 64-character grant list id fit every key."""
    run_id = str(uuid.uuid4())
    with Session(engine) as session:
        session.add(JobModel(optimization_id=run_id, username="alice", execution_generation=1))
        session.commit()
    budgets = BudgetService(engine=engine)
    budget = budgets.create("alice", 20, idempotency_key="workspace-budget")
    budget = budgets.attach_to_job(budget.id, "alice", run_id, expected_revision=budget.revision)
    user = User("alice", engine=engine, budgets=budgets)
    WorkspaceStore(engine).set_budget(user.workspace().id, budget.id)
    run = user.workspace().start_run(
        run_id,
        step_log=step_log,
        execution_generation=1,
        budget_generation=budget.generation,
        grant_list_id="g" * 64,
        chunk_cents=2,
        lease_slices=MAX_LEASE_SLICES,
    )
    step_id = run.spawn("a" * 64).step("model", 1, lambda: StepResult(body={}, cost_cents=1))
    run.close()
    step_log.flush()
    step_log.durable(step_id, 1, timeout=5)

    assert len(run_id) == 36
    assert step_id == f"{run_id}:{'a' * 64}:0"
    assert budgets.get(budget.id, "alice").run_spent_cents == Decimal(1)


def test_the_closed_property_shows_a_direct_close(engine: Engine, step_log: StepLogStore) -> None:
    """``Run.closed`` is False while the run runs and True after ``Run.close``."""
    run, _budgets, _budget_id = _start_run(engine, step_log, total_cents=20)
    assert run.closed is False
    run.close()
    assert run.closed is True


def test_a_ledger_input_refusal_fails_the_step_and_the_close_succeeds(engine: Engine, step_log: StepLogStore) -> None:
    """A plain ValueError from ``reserve_lease`` fails the step, reserves nothing and leaves no replay for the close."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=20)
    # The run's own checks refuse this count; set it after them, so the ledger raises its own input refusal.
    run._lease_slices = MAX_LEASE_SLICES + 1
    calls: list[int] = []

    def work() -> StepResult:
        """Record the paid call.

        Returns:
            A step result with a cost of 1.
        """
        calls.append(1)
        return StepResult(body={}, cost_cents=1)

    with pytest.raises(ValueError):
        run.spawn("agent-a").step("model", 1, work)
    run.close()

    budget = budgets.get(budget_id, "alice")
    assert calls == []
    assert (budget.run_spent_cents, budget.reserved_cents) == (Decimal(0), Decimal(0))


def _restart(run: Run, *, next_generation: int = 0) -> Run:
    """Start a second instance of the same run, as a restarted worker does.

    Args:
        run: The first instance.
        next_generation: The number of the run's next generation.

    Returns:
        The new instance.
    """
    return run.workspace.start_run(
        run.run_id,
        step_log=run.step_log,
        execution_generation=run.execution_generation,
        budget_generation=run.budget_generation,
        grant_list_id=run.grant_list_id,
        chunk_cents=run.chunk_cents,
        lease_slices=4,
        next_generation=next_generation,
    )


def test_a_step_replayed_on_its_lease_never_settles_twice(engine: Engine, step_log: StepLogStore) -> None:
    """A step replayed through ``Agent.step`` on the same lease settles at most once under its step id.

    The same spend is a no-op, and another spend is refused. The guarantee holds
    only on the same lease: evidence is unique per (lease operation, step id).
    A crash between the step commit and its settle is a gap that this test does
    not cover: the step stays unsettled and its slice stays dispatched.
    """
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=100)
    agent = run.spawn("agent-a")
    step_id = agent.step("model", 1, _model_step)
    lease = agent._lease
    assert lease is not None
    before = budgets.get(budget_id, "alice")

    agent.next_seq, lease.spent_cents = 0, Decimal(0)
    assert agent.step("model", 1, _model_step) == step_id
    agent.next_seq = 0
    with pytest.raises(BudgetConflictError):
        agent.step("model", 1, _model_step)

    after = budgets.get(budget_id, "alice")
    assert (after.run_spent_cents, after.billed_cents) == (before.run_spent_cents, before.billed_cents)
    assert after.run_spent_cents == Decimal("0.5")
    with Session(engine) as session:
        keys = session.scalars(
            select(ExecutionUsageEvidenceModel.evidence_key).where(
                ExecutionUsageEvidenceModel.operation_id == lease.operation_id
            )
        ).all()
    assert keys == [step_id]


def test_a_restarted_run_never_hands_out_a_slice_that_an_agent_holds(engine: Engine, step_log: StepLogStore) -> None:
    """A second instance of the run reserves new slices, so each step is billed on its own lease."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=100)
    other = _restart(run)
    old, new = run.take_chunk(), other.take_chunk()
    assert old is not None
    assert new is not None
    assert old.id != new.id
    budgets.release(old.id, "alice")
    budgets.release(new.id, "alice")
    other.close()

    first = run.spawn("agent-a")
    first.step("model", 1, _model_step)
    restarted = _restart(run)
    second = restarted.spawn("agent-b")
    second.step("model", 1, _model_step)

    assert first._lease is not None
    assert second._lease is not None
    assert first._lease.operation_id != second._lease.operation_id
    restarted.close()
    run.close()
    budget = budgets.get(budget_id, "alice")
    assert (budget.run_spent_cents, budget.reserved_cents) == (Decimal(1), Decimal(0))


def test_a_dispatched_slice_whose_release_failed_is_never_handed_out_again(
    engine: Engine, step_log: StepLogStore, monkeypatch
) -> None:
    """The dispatch commits and loses its reply, then the release fails: the next step takes a new slice."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=100)
    agent = run.spawn("agent-a")
    monkeypatch.setattr(budgets, "mark_dispatched", _fail_first(budgets.mark_dispatched, commit=True))
    monkeypatch.setattr(budgets, "release", _fail_first(budgets.release, commit=False))
    with pytest.raises(ConnectionError):
        agent.step("model", 1, _model_step)
    [lost] = run._returned
    agent.step("model", 1, _model_step)

    assert agent._lease is not None
    assert agent._lease.operation_id != lost.id
    run.close()
    budget = budgets.get(budget_id, "alice")
    assert (budget.run_spent_cents, budget.reserved_cents) == (Decimal("0.5"), Decimal(0))


def test_a_dispatched_slice_whose_release_failed_is_settled_at_zero_by_the_close(
    engine: Engine, step_log: StepLogStore, monkeypatch
) -> None:
    """The close final-settles a returned slice that dispatched, at 0, because no work ran on it."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=100)
    monkeypatch.setattr(budgets, "mark_dispatched", _fail_first(budgets.mark_dispatched, commit=True))
    monkeypatch.setattr(budgets, "release", _fail_first(budgets.release, commit=False))
    with pytest.raises(ConnectionError):
        run.spawn("agent-a").step("model", 1, _model_step)
    run.close()

    assert run._returned == []
    budget = budgets.get(budget_id, "alice")
    assert (budget.run_spent_cents, budget.reserved_cents) == (Decimal(0), Decimal(0))


def test_a_lost_zero_settle_reply_is_replayed_by_the_next_close(
    engine: Engine, step_log: StepLogStore, monkeypatch
) -> None:
    """The close keeps a returned slice whose settle lost its reply, and the next close settles it once."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=100)
    monkeypatch.setattr(budgets, "mark_dispatched", _fail_first(budgets.mark_dispatched, commit=True))
    monkeypatch.setattr(budgets, "release", _fail_first(budgets.release, commit=False))
    monkeypatch.setattr(budgets, "settle", _fail_first(budgets.settle, commit=True))
    with pytest.raises(ConnectionError):
        run.spawn("agent-a").step("model", 1, _model_step)
    with pytest.raises(ConnectionError):
        run.close()
    assert len(run._returned) == 1
    run.close()

    assert run._returned == []
    budget = budgets.get(budget_id, "alice")
    assert (budget.run_spent_cents, budget.reserved_cents) == (Decimal(0), Decimal(0))


def test_a_failed_settle_is_covered_by_the_next_cumulative_settle(
    engine: Engine, step_log: StepLogStore, monkeypatch
) -> None:
    """The lease counts a step's spend before its settle, so a lost settle is billed by the next one."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=100)
    agent = run.spawn("agent-a")
    real_settle = budgets.settle
    failed: list[int] = []

    def flaky(*args, **kwargs) -> object:
        """Fail the first settle, as a lost connection does.

        Args:
            *args: The settle's arguments.
            **kwargs: The settle's keyword arguments.

        Returns:
            The settle's result.
        """
        if not failed:
            failed.append(1)
            raise ConnectionError("lost")
        return real_settle(*args, **kwargs)

    monkeypatch.setattr(budgets, "settle", flaky)
    with pytest.raises(ConnectionError):
        agent.step("model", 1, _model_step)
    agent.step("model", 1, _model_step)
    run.close()

    assert budgets.get(budget_id, "alice").run_spent_cents == Decimal(1)


def test_a_step_cost_outside_its_bound_is_refused_but_still_billed(engine: Engine, step_log: StepLogStore) -> None:
    """A cost above the bound commits no step, but the paid work is billed in full; a negative cost counts 0."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=100)
    agent = run.spawn("agent-a")

    for cost in ("1.5", "-0.5"):
        with pytest.raises(ValueError):
            agent.step("model", 1, lambda cost=cost: _model_step(cost))
    run.close()
    step_log.flush()

    assert agent.next_seq == 0
    assert budgets.get(budget_id, "alice").run_spent_cents == Decimal("1.5")
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AgentStepModel)) == 0


def test_paid_work_is_billed_when_the_step_commit_fails(engine: Engine, step_log: StepLogStore, monkeypatch) -> None:
    """The work ran before the commit failed, so the lease close bills it."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=100)
    agent = run.spawn("agent-a")

    def refuse(**kwargs) -> str:
        """Fail the commit, as a fenced or closed step log does.

        Args:
            **kwargs: The commit's keyword arguments.

        Returns:
            Never returns.

        Raises:
            ValueError: Always.
        """
        raise ValueError("step log is closed")

    monkeypatch.setattr(step_log, "commit", refuse)
    with pytest.raises(ValueError):
        agent.step("model", 1, _model_step)
    run.close()

    assert budgets.get(budget_id, "alice").run_spent_cents == Decimal("0.5")


def test_an_agent_does_no_paid_work_after_the_run_closes(engine: Engine, step_log: StepLogStore, monkeypatch) -> None:
    """An agent whose final settle failed keeps its lease, but the closed run stops its next step."""
    run, budgets, _ = _start_run(engine, step_log, total_cents=100)
    agent = run.spawn("agent-a")
    agent.step("model", 1, _model_step)
    real_settle = budgets.settle
    calls: list[int] = []

    def flaky(*args, **kwargs) -> object:
        """Fail every final settle.

        Args:
            *args: The settle's arguments.
            **kwargs: The settle's keyword arguments.

        Returns:
            The settle's result.
        """
        if kwargs.get("final"):
            raise ConnectionError("lost")
        return real_settle(*args, **kwargs)

    def work() -> StepResult:
        """Record the paid call.

        Returns:
            A model step result.
        """
        calls.append(1)
        return _model_step()

    monkeypatch.setattr(budgets, "settle", flaky)
    with pytest.raises(ConnectionError):
        run.close()
    assert agent._lease is not None
    with pytest.raises(RuntimeError):
        agent.step("model", 1, work)

    assert calls == []
    monkeypatch.setattr(budgets, "settle", real_settle)
    run.close()


def test_a_lost_dispatch_reply_leaves_the_slice_with_the_agent(
    engine: Engine, step_log: StepLogStore, monkeypatch
) -> None:
    """The dispatch committed but its reply was lost: the agent keeps the slice, and the close settles it."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=100)
    agent = run.spawn("agent-a")
    real_mark = budgets.mark_dispatched
    lost: list[int] = []

    def mark(*args, **kwargs) -> object:
        """Commit the first dispatch, then lose its reply.

        Args:
            *args: The call's arguments.
            **kwargs: The call's keyword arguments.

        Returns:
            The call's result.

        Raises:
            ConnectionError: On the first call, after the commit.
        """
        result = real_mark(*args, **kwargs)
        if not lost:
            lost.append(1)
            raise ConnectionError("reply lost")
        return result

    monkeypatch.setattr(budgets, "mark_dispatched", mark)
    with pytest.raises(ConnectionError):
        agent.step("model", 1, _model_step)
    assert agent._lease is not None
    agent.step("model", 1, _model_step)
    run.close()

    budget = budgets.get(budget_id, "alice")
    assert (budget.run_spent_cents, budget.reserved_cents) == (Decimal("0.5"), Decimal(0))


def test_a_slice_with_an_unknown_state_is_released_by_the_close(
    engine: Engine, step_log: StepLogStore, monkeypatch
) -> None:
    """The dispatch and the release both fail: the first error reaches the caller, and the close releases the slice."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=100)
    real_release = budgets.release
    failed: list[int] = []

    def mark(*args, **kwargs) -> object:
        """Fail the dispatch before any commit.

        Args:
            *args: The call's arguments.
            **kwargs: The call's keyword arguments.

        Returns:
            Never returns.

        Raises:
            TimeoutError: Always.
        """
        raise TimeoutError("dispatch timed out")

    def release(*args, **kwargs) -> object:
        """Fail the first release.

        Args:
            *args: The call's arguments.
            **kwargs: The call's keyword arguments.

        Returns:
            The call's result.

        Raises:
            ConnectionError: On the first call.
        """
        if not failed:
            failed.append(1)
            raise ConnectionError("lost")
        return real_release(*args, **kwargs)

    monkeypatch.setattr(budgets, "mark_dispatched", mark)
    monkeypatch.setattr(budgets, "release", release)
    with pytest.raises(TimeoutError):
        run.spawn("agent-a").step("model", 1, _model_step)
    run.close()

    budget = budgets.get(budget_id, "alice")
    assert (budget.run_spent_cents, budget.reserved_cents) == (Decimal(0), Decimal(0))


def test_a_slice_returned_during_the_close_is_still_released(
    engine: Engine, step_log: StepLogStore, monkeypatch
) -> None:
    """A step that fails while the run closes returns its slice to the run, and that close releases it."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=100)
    agent = run.spawn("agent-a")
    entered, go = threading.Event(), threading.Event()
    real_release = budgets.release
    failed: list[int] = []

    def mark(*args, **kwargs) -> object:
        """Hold the dispatch until the close started, then fail it before any commit.

        Args:
            *args: The call's arguments.
            **kwargs: The call's keyword arguments.

        Returns:
            Never returns.

        Raises:
            TimeoutError: Always.
        """
        entered.set()
        go.wait(5)
        raise TimeoutError("dispatch timed out")

    def release(*args, **kwargs) -> object:
        """Fail the step's release, so the slice state stays unknown.

        Args:
            *args: The call's arguments.
            **kwargs: The call's keyword arguments.

        Returns:
            The call's result.

        Raises:
            ConnectionError: On the first call.
        """
        if not failed:
            failed.append(1)
            raise ConnectionError("lost")
        return real_release(*args, **kwargs)

    monkeypatch.setattr(budgets, "mark_dispatched", mark)
    monkeypatch.setattr(budgets, "release", release)
    errors: list[Exception] = []

    def drive() -> None:
        """Run one step and keep its error."""
        try:
            agent.step("model", 1, _model_step)
        except Exception as error:
            errors.append(error)

    stepper = threading.Thread(target=drive)
    stepper.start()
    assert entered.wait(5)
    closer = threading.Thread(target=run.close)
    closer.start()
    while not run._closed:
        go.wait(0.01)
    # The close's first locked block is over before the step goes on.
    with run._lock:
        pass
    go.set()
    stepper.join(5)
    closer.join(5)

    assert [type(error) for error in errors] == [TimeoutError]
    assert (run._pool, run._returned) == ([], [])
    assert budgets.get(budget_id, "alice").reserved_cents == Decimal(0)


def test_a_failed_pool_release_is_retried_by_the_next_close(
    engine: Engine, step_log: StepLogStore, monkeypatch
) -> None:
    """The run keeps a pool whose release failed, and the next close releases it."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=100)
    run.spawn("agent-a").step("model", 1, _model_step)
    real_release_slices = budgets.release_slices
    failed: list[int] = []

    def release_slices(*args, **kwargs) -> object:
        """Fail the first pool release.

        Args:
            *args: The call's arguments.
            **kwargs: The call's keyword arguments.

        Returns:
            The call's result.

        Raises:
            ConnectionError: On the first call.
        """
        if not failed:
            failed.append(1)
            raise ConnectionError("lost")
        return real_release_slices(*args, **kwargs)

    monkeypatch.setattr(budgets, "release_slices", release_slices)
    with pytest.raises(ConnectionError):
        run.close()
    assert budgets.get(budget_id, "alice").reserved_cents == Decimal(6)
    run.close()

    budget = budgets.get(budget_id, "alice")
    assert (budget.run_spent_cents, budget.reserved_cents) == (Decimal("0.5"), Decimal(0))


def test_a_lost_lease_reply_is_replayed_with_the_same_key(engine: Engine, step_log: StepLogStore, monkeypatch) -> None:
    """A lease that committed but lost its reply comes back on the next try, so the close releases it."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=100)
    agent = run.spawn("agent-a")
    real_reserve_lease = budgets.reserve_lease
    lost: list[int] = []

    def reserve_lease(*args, **kwargs) -> object:
        """Commit the first lease, then lose its reply.

        Args:
            *args: The call's arguments.
            **kwargs: The call's keyword arguments.

        Returns:
            The call's result.

        Raises:
            ConnectionError: On the first call, after the commit.
        """
        result = real_reserve_lease(*args, **kwargs)
        if not lost:
            lost.append(1)
            raise ConnectionError("reply lost")
        return result

    monkeypatch.setattr(budgets, "reserve_lease", reserve_lease)
    with pytest.raises(ConnectionError):
        agent.step("model", 1, _model_step)
    agent.step("model", 1, _model_step)
    run.close()

    budget = budgets.get(budget_id, "alice")
    assert (budget.run_spent_cents, budget.reserved_cents) == (Decimal("0.5"), Decimal(0))
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(ExecutionOperationModel)) == 4


def test_a_lost_release_reply_leaves_nothing_reserved(engine: Engine, step_log: StepLogStore, monkeypatch) -> None:
    """The dispatch fails before its commit and the release commits but loses its reply: the close ends clean."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=100)
    monkeypatch.setattr(budgets, "mark_dispatched", _fail_first(budgets.mark_dispatched, commit=False))
    monkeypatch.setattr(budgets, "release", _fail_first(budgets.release, commit=True))
    with pytest.raises(ConnectionError):
        run.spawn("agent-a").step("model", 1, _model_step)
    run.close()

    assert run._returned == []
    budget = budgets.get(budget_id, "alice")
    assert (budget.run_spent_cents, budget.reserved_cents) == (Decimal(0), Decimal(0))


def test_a_lost_pool_release_reply_is_replayed_by_the_next_close(
    engine: Engine, step_log: StepLogStore, monkeypatch
) -> None:
    """The pool release commits but loses its reply: the next close replays it without an error."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=100)
    run.spawn("agent-a").step("model", 1, _model_step)
    monkeypatch.setattr(budgets, "release_slices", _fail_first(budgets.release_slices, commit=True))
    with pytest.raises(ConnectionError):
        run.close()
    run.close()

    assert run._pool == []
    budget = budgets.get(budget_id, "alice")
    assert (budget.run_spent_cents, budget.reserved_cents) == (Decimal("0.5"), Decimal(0))


def test_a_lost_step_settle_reply_is_billed_once(engine: Engine, step_log: StepLogStore, monkeypatch) -> None:
    """A step settle commits but loses its reply: the next cumulative settle does not bill the step twice."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=100)
    agent = run.spawn("agent-a")
    monkeypatch.setattr(budgets, "settle", _fail_first(budgets.settle, commit=True))
    with pytest.raises(ConnectionError):
        agent.step("model", 1, _model_step)
    agent.step("model", 1, _model_step)
    run.close()

    budget = budgets.get(budget_id, "alice")
    assert (budget.run_spent_cents, budget.reserved_cents) == (Decimal(1), Decimal(0))


def test_a_lost_lease_reply_is_released_by_an_immediate_close(
    engine: Engine, step_log: StepLogStore, monkeypatch
) -> None:
    """The close replays a lease whose reply was lost, so its slices go back to the budget."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=100)
    monkeypatch.setattr(budgets, "reserve_lease", _fail_first(budgets.reserve_lease, commit=True))
    with pytest.raises(ConnectionError):
        run.spawn("agent-a").step("model", 1, _model_step)
    run.close()

    assert run._retry is None
    assert budgets.get(budget_id, "alice").reserved_cents == Decimal(0)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(ExecutionOperationModel)) == 4


def test_a_failed_lease_replay_is_retried_by_the_next_close(
    engine: Engine, step_log: StepLogStore, monkeypatch
) -> None:
    """A close whose lease replay fails keeps the lease key, and the next close replays and releases it."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=100)
    real_reserve_lease = budgets.reserve_lease
    monkeypatch.setattr(budgets, "reserve_lease", _fail_first(real_reserve_lease, commit=True))
    with pytest.raises(ConnectionError):
        run.spawn("agent-a").step("model", 1, _model_step)
    monkeypatch.setattr(budgets, "reserve_lease", _fail_first(real_reserve_lease, commit=False))
    with pytest.raises(ConnectionError):
        run.close()
    assert budgets.get(budget_id, "alice").reserved_cents == Decimal(8)
    run.close()

    assert run._retry is None
    assert budgets.get(budget_id, "alice").reserved_cents == Decimal(0)


def test_a_bound_exceeded_block_stops_the_other_agents(engine: Engine, step_log: StepLogStore) -> None:
    """After one agent's settle blocks the budget, an agent with lease room starts no more paid work."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=100, chunk_cents=2, lease_slices=4)
    agent_a, agent_b = run.spawn("agent-a"), run.spawn("agent-b")
    agent_a.step("model", 1, lambda: _model_step("1"))
    agent_b.step("model", 1, _model_step)
    with pytest.raises(ValueError, match="outside its bound"):
        agent_a.step("model", 1, lambda: _model_step("1.5"))
    with pytest.raises(BudgetBoundExceededError):
        agent_a.step("model", 1, _model_step)
    # The pool still holds slices, and take_chunk is public, so only the run's flag keeps it from handing one out.
    real_reserve_lease = budgets.reserve_lease
    reserve_calls: list[int] = []

    def counted_reserve_lease(*args: Any, **kwargs: Any) -> Any:
        """Count the reservation, then make it.

        Args:
            *args: The call's arguments.
            **kwargs: The call's keyword arguments.

        Returns:
            The reserved slices.
        """
        reserve_calls.append(1)
        return real_reserve_lease(*args, **kwargs)

    budgets.reserve_lease = counted_reserve_lease
    assert run.take_chunk() is None
    assert reserve_calls == []
    calls: list[int] = []

    def work() -> StepResult:
        """Count the paid call.

        Returns:
            A model step result.
        """
        calls.append(1)
        return _model_step()

    with pytest.raises(LeaseExhaustedError):
        agent_b.step("model", 1, work)
    assert calls == []
    with pytest.raises(BudgetBoundExceededError):
        run.close()
    assert budgets.get(budget_id, "alice").state == "blocked"


def test_a_funding_lost_block_stops_the_other_agents(engine: Engine, step_log: StepLogStore) -> None:
    """After a settle finds the wallet empty, no agent of the run starts unbilled paid work."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=100)
    agent_a, agent_b = run.spawn("agent-a"), run.spawn("agent-b")
    agent_a.step("model", 1, _model_step)
    agent_b.step("model", 1, _model_step)
    with Session(engine) as session:
        wallet = session.get(BillingCustomerModel, "alice")
        wallet.balance_cents = 0
        wallet.grant_remaining = 0
        session.commit()
    with pytest.raises(BudgetFundingLostError):
        agent_a.step("model", 1, _model_step)
    calls: list[int] = []

    def work() -> StepResult:
        """Count the paid call.

        Returns:
            A model step result.
        """
        calls.append(1)
        return _model_step()

    for agent in (agent_a, agent_b):
        with pytest.raises(LeaseExhaustedError):
            agent.step("model", 1, work)
    assert calls == []
    with pytest.raises(BudgetFundingLostError):
        run.close()
    assert budgets.get(budget_id, "alice").state == "blocked"


def test_a_closed_budget_stops_an_agent_that_holds_a_lease(engine: Engine, step_log: StepLogStore) -> None:
    """A cancel closes the budget (stop_admission): the agent's next step stops before paid work."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=20, chunk_cents=4)
    agent = run.spawn("agent-a")
    agent.step("model", 1, _model_step)
    budgets.stop_admission(budget_id, "alice", reason="cancelled")
    calls: list[int] = []

    def work() -> StepResult:
        """Record the paid call.

        Returns:
            A model step result.
        """
        calls.append(1)
        return _model_step()

    with pytest.raises(LeaseExhaustedError):
        agent.step("model", 1, work)
    with pytest.raises(LeaseExhaustedError):
        run.spawn("agent-b").step("model", 1, work)
    run.close()

    assert calls == []
    budget = budgets.get(budget_id, "alice")
    assert (budget.run_spent_cents, budget.reserved_cents) == (Decimal("0.5"), Decimal(0))


@pytest.mark.parametrize(("fence_budget", "move_job"), [(True, True), (True, False), (False, True)])
def test_a_fenced_run_stops_an_agent_that_holds_a_lease(
    engine: Engine, step_log: StepLogStore, fence_budget: bool, move_job: bool
) -> None:
    """A recovery fences the old run but keeps the budget attached: its agents make no paid call and bill nothing."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=20, chunk_cents=4)
    agent = run.spawn("agent-a")
    agent.step("model", 1, _model_step)
    if fence_budget:
        budgets.fence_generation(budget_id, "alice", expected_generation=run.budget_generation)
    if move_job:
        with Session(engine) as session:
            session.execute(update(JobModel).where(JobModel.optimization_id == RUN_ID).values(execution_generation=2))
            session.commit()
    calls: list[int] = []

    def work() -> StepResult:
        """Record the paid call.

        Returns:
            A model step result.
        """
        calls.append(1)
        return _model_step()

    for _ in range(3):
        with pytest.raises(LeaseExhaustedError):
            agent.step("model", 1, work)
    with pytest.raises(LeaseExhaustedError):
        run.spawn("agent-b").step("model", 1, work)
    run.close()

    assert calls == []
    budget = budgets.get(budget_id, "alice")
    assert budget.state == "attached"
    assert (budget.run_spent_cents, budget.reserved_cents) == (Decimal("0.5"), Decimal(0))


def test_a_recovery_while_the_agent_gets_its_lease_stops_the_work(engine: Engine, step_log: StepLogStore) -> None:
    """A recovery moves the job during reserve_lease: the step makes no paid call, and the close bills nothing."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=20, chunk_cents=4)
    real_reserve_lease = budgets.reserve_lease

    def reserve_during_recovery(*args: Any, **kwargs: Any) -> Any:
        """Move the job to a new execution generation, then reserve.

        Args:
            *args: The call's arguments.
            **kwargs: The call's keyword arguments.

        Returns:
            The reserved slices.
        """
        with Session(engine) as session:
            session.execute(update(JobModel).where(JobModel.optimization_id == RUN_ID).values(execution_generation=2))
            session.commit()
        return real_reserve_lease(*args, **kwargs)

    budgets.reserve_lease = reserve_during_recovery
    calls: list[int] = []

    def work() -> StepResult:
        """Record the paid call.

        Returns:
            A model step result.
        """
        calls.append(1)
        return _model_step()

    agent = run.spawn("agent-a")
    with pytest.raises(LeaseExhaustedError):
        agent.step("model", 1, work)
    assert agent._lease is not None
    run.close()

    assert calls == []
    budget = budgets.get(budget_id, "alice")
    assert (budget.run_spent_cents, budget.reserved_cents) == (Decimal(0), Decimal(0))


def test_a_fenced_run_reserves_no_new_lease(engine: Engine, step_log: StepLogStore) -> None:
    """After a recovery moved the job, a new agent's step reserves nothing, so the stale run holds no budget."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=20, chunk_cents=4)
    with Session(engine) as session:
        session.execute(update(JobModel).where(JobModel.optimization_id == RUN_ID).values(execution_generation=2))
        session.commit()
    real_reserve_lease = budgets.reserve_lease
    reserve_calls: list[int] = []

    def counted_reserve_lease(*args: Any, **kwargs: Any) -> Any:
        """Count the reservation, then make it.

        Args:
            *args: The call's arguments.
            **kwargs: The call's keyword arguments.

        Returns:
            The reserved slices.
        """
        reserve_calls.append(1)
        return real_reserve_lease(*args, **kwargs)

    budgets.reserve_lease = counted_reserve_lease
    with pytest.raises(LeaseExhaustedError):
        run.spawn("agent-a").step("model", 1, _model_step)
    run.close()

    assert reserve_calls == []
    assert budgets.get(budget_id, "alice").reserved_cents == Decimal(0)


def test_a_cancel_while_the_agent_gets_its_lease_stops_the_work(engine: Engine, step_log: StepLogStore) -> None:
    """A cancel comes during mark_dispatched: the step makes no paid call, and the close bills nothing."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=20, chunk_cents=4)
    real_mark_dispatched = budgets.mark_dispatched

    def dispatch_then_cancel(*args: Any, **kwargs: Any) -> Any:
        """Dispatch the slice, then cancel the run.

        Args:
            *args: The call's arguments.
            **kwargs: The call's keyword arguments.

        Returns:
            The dispatched slice.
        """
        result = real_mark_dispatched(*args, **kwargs)
        budgets.stop_admission(budget_id, "alice", reason="cancelled")
        return result

    budgets.mark_dispatched = dispatch_then_cancel
    calls: list[int] = []

    def work() -> StepResult:
        """Record the paid call.

        Returns:
            A model step result.
        """
        calls.append(1)
        return _model_step()

    agent = run.spawn("agent-a")
    with pytest.raises(LeaseExhaustedError):
        agent.step("model", 1, work)
    assert agent._lease is not None
    run.close()

    assert calls == []
    budget = budgets.get(budget_id, "alice")
    assert (budget.run_spent_cents, budget.reserved_cents) == (Decimal(0), Decimal(0))


def _reconcile_and_resume(run: Run, budgets: BudgetService, budget_id: str, agent: Agent) -> None:
    """Final-settle the agent's lease as a usage reconciliation does, then resume admission on the budget.

    The settle sends the same key, spend and evidence as the agent's own close, so that close is a no-op replay.
    The agent still holds the lease object, with room left on it.

    Args:
        run: The run.
        budgets: The ledger.
        budget_id: The run's budget.
        agent: The agent whose lease to settle.
    """
    lease = agent._lease
    assert lease is not None
    budgets.settle(
        lease.operation_id,
        "alice",
        evidence_key=LEASE_CLOSE_KEY,
        actual_cents=lease.spent_cents,
        evidence={"closed": True},
        final=True,
    )
    budgets.resume_admission(budget_id, "alice", expected_generation=run.budget_generation)
    assert budgets.get(budget_id, "alice").state == "attached"


def test_a_cancelled_run_stays_stopped_after_a_resume(engine: Engine, step_log: StepLogStore) -> None:
    """A run that the pre-step read stopped does no paid work after a reconciliation and resume_admission."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=20, chunk_cents=4, lease_slices=1)
    agent = run.spawn("agent-a")
    agent.step("model", 1, _model_step)
    budgets.stop_admission(budget_id, "alice", reason="cancelled")
    with pytest.raises(LeaseExhaustedError):
        agent.step("model", 1, _model_step)
    _reconcile_and_resume(run, budgets, budget_id, agent)
    calls: list[int] = []

    def work() -> StepResult:
        """Record the paid call.

        Returns:
            A model step result.
        """
        calls.append(1)
        return _model_step()

    with pytest.raises(LeaseExhaustedError):
        agent.step("model", 1, work)
    run.close()

    assert calls == []
    assert budgets.get(budget_id, "alice").run_spent_cents == Decimal("0.5")


def test_a_cancel_seen_by_a_settle_stays_after_a_resume(engine: Engine, step_log: StepLogStore) -> None:
    """A cancel during the work reaches the run only through the settle, and the run stays stopped after a resume."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=20, chunk_cents=4, lease_slices=1)
    agent = run.spawn("agent-a")

    def cancelled_work() -> StepResult:
        """Cancel the run while its paid call is in flight.

        Returns:
            A model step result.
        """
        budgets.stop_admission(budget_id, "alice", reason="cancelled")
        return _model_step()

    agent.step("model", 1, cancelled_work)
    _reconcile_and_resume(run, budgets, budget_id, agent)
    calls: list[int] = []

    def work() -> StepResult:
        """Record the paid call.

        Returns:
            A model step result.
        """
        calls.append(1)
        return _model_step()

    with pytest.raises(LeaseExhaustedError):
        agent.step("model", 1, work)
    run.close()

    assert calls == []
    assert budgets.get(budget_id, "alice").run_spent_cents == Decimal("0.5")


def test_a_lost_lease_reply_then_a_cancel_lets_the_close_finish(engine: Engine, step_log: StepLogStore) -> None:
    """The close replays a lease reservation that never committed; the ledger refuses it, and the close ends clean."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=20, chunk_cents=4)
    agent = run.spawn("agent-a")
    budgets.reserve_lease = _fail_first(budgets.reserve_lease, commit=False)
    with pytest.raises(ConnectionError):
        agent.step("model", 1, _model_step)
    assert run._retry is not None
    budgets.stop_admission(budget_id, "alice", reason="cancelled")
    run.close()

    assert run._retry is None
    assert budgets.get(budget_id, "alice").reserved_cents == Decimal(0)


@pytest.mark.parametrize(
    ("cost", "billed"),
    [
        (Decimal("0.0000000001"), Decimal("0.000000001")),
        (Decimal("0.1234567891"), Decimal("0.123456790")),
        (0.5, Decimal("0.5")),
        ("0.25", Decimal("0.25")),
    ],
)
def test_a_cost_the_ledger_cannot_hold_is_rounded_up_and_billed(
    engine: Engine, step_log: StepLogStore, cost: object, billed: Decimal
) -> None:
    """A cost with ten fractional digits, or a float, is billed rounded up, and the lease still closes."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=20)
    agent = run.spawn("agent-a")
    agent.step("model", 1, lambda: StepResult(body={}, cost_cents=cost))
    run.close()

    budget = budgets.get(budget_id, "alice")
    assert (budget.run_spent_cents, budget.reserved_cents) == (billed, Decimal(0))


def test_a_step_on_a_held_lease_locks_the_budget_rows_once(engine: Engine, step_log: StepLogStore, monkeypatch) -> None:
    """The admission check before the work reads the budget state with no lock; only the settle locks the rows."""
    run, budgets, _ = _start_run(engine, step_log, total_cents=20)
    agent = run.spawn("agent-a")
    agent.step("model", 1, _model_step)
    real_locked = budgets._locked
    locks: list[int] = []

    def locked(*args: Any, **kwargs: Any) -> Any:
        """Count each lock of the wallet and budget rows.

        Args:
            *args: The call's arguments.
            **kwargs: The call's keyword arguments.

        Returns:
            The call's result.
        """
        locks.append(1)
        return real_locked(*args, **kwargs)

    monkeypatch.setattr(budgets, "_locked", locked)
    agent.step("model", 1, _model_step)

    assert len(locks) == 1
    monkeypatch.setattr(budgets, "_locked", real_locked)
    run.close()


@pytest.mark.parametrize(
    ("cost", "text"),
    [
        (0, "0"),
        ("-0", "0"),
        (Decimal("1E-7"), "0.0000001"),
        (1.5, "1.5"),
        (Decimal("1.500"), "1.5"),
        ("0.0000000001", "0.000000001"),
    ],
)
def test_a_step_cost_is_stored_in_plain_notation(
    engine: Engine, step_log: StepLogStore, monkeypatch, cost: object, text: str
) -> None:
    """The step body and the settle evidence keep one plain text for each cost, so the evidence stays stable."""
    run, budgets, _ = _start_run(engine, step_log, total_cents=20)
    real_commit, real_settle = step_log.commit, budgets.settle
    seen: list[str] = []

    def commit(**kwargs: Any) -> str:
        """Record the step body's cost.

        Args:
            **kwargs: The commit's keyword arguments.

        Returns:
            The step id.
        """
        seen.append(kwargs["body"]["cost_cents"])
        return real_commit(**kwargs)

    def settle(*args: Any, **kwargs: Any) -> Any:
        """Record the evidence's cost.

        Args:
            *args: The settle's arguments.
            **kwargs: The settle's keyword arguments.

        Returns:
            The settle's result.
        """
        seen.append(kwargs["evidence"]["cost_cents"])
        return real_settle(*args, **kwargs)

    monkeypatch.setattr(step_log, "commit", commit)
    monkeypatch.setattr(budgets, "settle", settle)
    run.spawn("agent-a").step("model", 2, lambda: StepResult(body={}, cost_cents=cost))

    assert seen == [text, text]
    monkeypatch.setattr(budgets, "settle", real_settle)
    run.close()


def test_a_large_negative_cost_is_refused_and_billed_nothing(engine: Engine, step_log: StepLogStore) -> None:
    """A negative cost too large to round raises the step's ValueError, and it counts 0."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=20)
    agent = run.spawn("agent-a")
    with pytest.raises(ValueError, match="outside its bound"):
        agent.step("model", 1, lambda: StepResult(body={}, cost_cents="-1E+30"))
    run.close()

    budget = budgets.get(budget_id, "alice")
    assert (budget.run_spent_cents, budget.reserved_cents) == (Decimal(0), Decimal(0))


@pytest.mark.parametrize("cost", ["NaN", "Infinity", "not a number", None])
def test_a_cost_that_is_not_a_number_is_billed_at_the_bound(
    engine: Engine, step_log: StepLogStore, cost: object
) -> None:
    """The work ran but its cost is unknown: the step is refused, and the close bills the step's bound."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=20)
    agent = run.spawn("agent-a")
    with pytest.raises(ValueError, match="outside its bound"):
        agent.step("model", 1, lambda: StepResult(body={}, cost_cents=cost))
    run.close()

    assert agent.next_seq == 0
    budget = budgets.get(budget_id, "alice")
    assert (budget.run_spent_cents, budget.reserved_cents) == (Decimal(1), Decimal(0))


def test_a_cost_above_the_ledger_maximum_still_blocks_the_budget(engine: Engine, step_log: StepLogStore) -> None:
    """A lease spend too large for the ledger counts at the ledger maximum, so the settle records bound_exceeded."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=20)
    agent = run.spawn("agent-a")
    # The earlier spend puts the lease total past the ledger maximum, not only the one cost.
    agent.step("model", 1, _model_step)
    with pytest.raises(ValueError, match="outside its bound"):
        agent.step("model", 1, lambda: StepResult(body={}, cost_cents=Decimal("1e30")))
    with pytest.raises(BudgetBoundExceededError):
        run.close()

    assert budgets.get(budget_id, "alice").state == "blocked"


def test_a_lease_whose_final_settle_lost_its_reply_takes_no_more_spend(
    engine: Engine, step_log: StepLogStore, monkeypatch
) -> None:
    """After a final settle commits and loses its reply, no step spends from that lease, and a retry closes it."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=100, chunk_cents=2)
    agent = run.spawn("agent-a")
    agent.step("model", "1.5", lambda: _model_step("1.5"))
    old_lease = agent._lease
    assert old_lease is not None
    real_settle = budgets.settle
    lost: list[int] = []

    def settle(*args, **kwargs) -> object:
        """Commit the first final settle, then lose its reply.

        Args:
            *args: The call's arguments.
            **kwargs: The call's keyword arguments.

        Returns:
            The call's result.

        Raises:
            ConnectionError: On the first final settle, after the commit.
        """
        result = real_settle(*args, **kwargs)
        if kwargs.get("final") and not lost:
            lost.append(1)
            raise ConnectionError("reply lost")
        return result

    monkeypatch.setattr(budgets, "settle", settle)
    with pytest.raises(ConnectionError):
        agent.step("model", 1, _model_step)
    # The old lease still has 0.5 of room, but it must take no spend after its final settle.
    agent.step("model", "0.5", lambda: _model_step("0.5"))
    assert agent._lease is not None
    assert agent._lease.operation_id != old_lease.operation_id
    run.close()

    budget = budgets.get(budget_id, "alice")
    assert (budget.run_spent_cents, budget.reserved_cents) == (Decimal(2), Decimal(0))


def test_a_failing_work_call_is_billed_nothing_and_the_agent_goes_on(engine: Engine, step_log: StepLogStore) -> None:
    """``work`` must catch its own failures: an exception reaches the caller, commits nothing and bills nothing."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=100)
    agent = run.spawn("agent-a")
    agent.step("model", 1, _model_step)
    lease = agent._lease
    assert lease is not None

    def broken() -> StepResult:
        """Fail as a crashed tool does.

        Returns:
            Never returns.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("tool crashed")

    with pytest.raises(RuntimeError, match="tool crashed"):
        agent.step("model", 1, broken)
    assert (lease.spent_cents, agent.next_seq) == (Decimal("0.5"), 1)
    agent.step("model", 1, _model_step)
    run.close()
    step_log.flush()

    assert agent.next_seq == 2
    assert budgets.get(budget_id, "alice").run_spent_cents == Decimal(1)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AgentStepModel)) == 2


def test_a_cost_past_the_lease_blocks_the_budget_until_reconciliation(engine: Engine, step_log: StepLogStore) -> None:
    """A spend above the lease bound blocks the budget; the agent keeps its lease and every close raises."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=100, chunk_cents=2, lease_slices=4)
    agent = run.spawn("agent-a")
    agent.step("model", 1, lambda: _model_step("1"))
    with pytest.raises(ValueError, match="outside its bound"):
        agent.step("model", 1, lambda: _model_step("1.5"))
    with pytest.raises(BudgetBoundExceededError):
        agent.step("model", 1, _model_step)
    with pytest.raises(BudgetBoundExceededError):
        run.close()

    assert agent._lease is not None
    assert budgets.get(budget_id, "alice").state == "blocked"
    with Session(engine) as session:
        states = session.execute(select(ExecutionOperationModel.id, ExecutionOperationModel.state)).all()
    others = sorted(state for operation_id, state in states if operation_id != agent._lease.operation_id)
    assert others == ["released", "released", "released"]


def test_an_unreconciled_budget_is_not_hidden_as_an_empty_lease(
    engine: Engine, step_log: StepLogStore, monkeypatch
) -> None:
    """``BudgetUnreconciledError`` reaches the agent; the run does not turn it into ``LeaseExhaustedError``."""
    run, budgets, _ = _start_run(engine, step_log, total_cents=100)

    def unreconciled(*args, **kwargs) -> object:
        """Refuse the lease, as a budget with unreconciled work does.

        Args:
            *args: The call's arguments.
            **kwargs: The call's keyword arguments.

        Returns:
            Never returns.

        Raises:
            BudgetUnreconciledError: Always.
        """
        raise BudgetUnreconciledError("unreconciled usage")

    monkeypatch.setattr(budgets, "reserve_lease", unreconciled)
    with pytest.raises(BudgetUnreconciledError):
        run.spawn("agent-a").step("model", 1, _model_step)


def test_a_run_close_waits_for_an_in_flight_step_and_stops_the_run(engine: Engine, step_log: StepLogStore) -> None:
    """A close during a step waits for its settle; after the close the run hands out nothing."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=100)
    agent = run.spawn("agent-a")
    started, release = threading.Event(), threading.Event()
    errors: list[Exception] = []

    def work() -> StepResult:
        """Hold the step open until the test releases it.

        Returns:
            A model step result.
        """
        started.set()
        release.wait(5)
        return _model_step()

    def drive() -> None:
        """Run one step and keep its error."""
        try:
            agent.step("model", 1, work)
        except Exception as error:
            errors.append(error)

    stepper = threading.Thread(target=drive)
    stepper.start()
    assert started.wait(5)
    closer = threading.Thread(target=run.close)
    closer.start()
    closer.join(0.3)
    assert closer.is_alive(), "the close must wait for the in-flight step"
    release.set()
    stepper.join(5)
    closer.join(5)

    assert errors == []
    assert run.take_chunk() is None
    with pytest.raises(RuntimeError):
        run.spawn("agent-b")
    budget = budgets.get(budget_id, "alice")
    assert (budget.run_spent_cents, budget.reserved_cents) == (Decimal("0.5"), Decimal(0))


def test_one_failed_agent_close_does_not_stop_the_run_close(
    engine: Engine, step_log: StepLogStore, monkeypatch
) -> None:
    """The other agents close and the pool is released; the failed agent keeps its lease for a retry."""
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=100)
    first, second = run.spawn("agent-a"), run.spawn("agent-b")
    first.step("model", 1, _model_step)
    second.step("model", 1, _model_step)
    assert first._lease is not None
    held = first._lease.operation_id
    real_settle = budgets.settle
    failed: list[int] = []

    def flaky(operation_id: str, *args, **kwargs) -> object:
        """Fail the first final settle of the first agent's lease.

        Args:
            operation_id: The lease operation.
            *args: The settle's arguments.
            **kwargs: The settle's keyword arguments.

        Returns:
            The settle's result.
        """
        if operation_id == held and kwargs.get("final") and not failed:
            failed.append(1)
            raise ConnectionError("lost")
        return real_settle(operation_id, *args, **kwargs)

    monkeypatch.setattr(budgets, "settle", flaky)
    with pytest.raises(ConnectionError):
        run.close()

    assert first._lease is not None
    assert second._lease is None
    assert budgets.get(budget_id, "alice").reserved_cents == Decimal("1.5")
    run.close()
    budget = budgets.get(budget_id, "alice")
    assert (budget.run_spent_cents, budget.reserved_cents) == (Decimal(1), Decimal(0))


def test_a_restarted_run_keeps_its_generation_records(engine: Engine, step_log: StepLogStore) -> None:
    """A run that resumes at its next generation commits a new record and keeps the old ones."""
    run, _, _ = _start_run(engine, step_log, total_cents=100)
    run.end_generation([{"id": "c1"}], {"id": "c1"})
    run.end_generation([{"id": "c2"}], {"id": "c2"})

    restarted = _restart(run, next_generation=run.generation)
    step_id = restarted.end_generation([{"id": "c3"}], {"id": "c3"})
    step_log.flush()

    assert step_id == f"{RUN_ID}:{RUN_AGENT_ID}:2"
    assert step_log.get_step(step_id)["body"]["generation"] == 2
    with Session(engine) as session:
        assert (
            session.scalar(
                select(func.count()).select_from(AgentStepModel).where(AgentStepModel.agent_id == RUN_AGENT_ID)
            )
            == 3
        )


def test_a_thousand_agents_never_spend_past_the_workspace_budget(engine: Engine, step_log: StepLogStore) -> None:
    """1,000 agents of one run spend in parallel from one budget; the total stays within its limit."""
    total_cents = 300
    run, budgets, budget_id = _start_run(engine, step_log, total_cents=total_cents, chunk_cents=2, lease_slices=8)
    agents = [run.spawn(f"agent-{index}") for index in range(1000)]
    lock = threading.Lock()
    paid: list[str] = []
    stopped: list[str] = []

    def drive(index: int) -> None:
        """Run up to four paid steps of one agent, then close its lease.

        Args:
            index: The agent's position.
        """
        agent = agents[index]

        def work() -> StepResult:
            """Record the paid call.

            Returns:
                A model step result.
            """
            with lock:
                paid.append(agent.agent_id)
            return _model_step()

        try:
            for _ in range(4):
                agent.step("model", 1, work)
        # The spent cap gives BudgetReached; holds of other agents in flight give LeaseExhaustedError (SKY-914).
        except (LeaseExhaustedError, BudgetReached):
            with lock:
                stopped.append(agent.agent_id)
        finally:
            agent.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(drive, range(len(agents))))
    run.close()
    step_log.flush()

    budget = budgets.get(budget_id, "alice")
    spent = Decimal("0.5") * len(paid)
    assert 0 < budget.run_spent_cents <= total_cents
    assert budget.run_spent_cents == spent
    assert budget.reserved_cents == 0
    assert stopped, "the demand of 2,000 cents must pass the 300-cent budget"
    with Session(engine) as session:
        assert session.get(BillingCustomerModel, "alice").balance_cents == WALLET_CENTS - budget.billed_cents
        rows = session.execute(select(AgentStepModel.step_id, AgentStepModel.agent_id)).all()
    assert len(rows) == len(paid)
    assert all(step_id.startswith(f"{RUN_ID}:{agent_id}:") for step_id, agent_id in rows)
    assert sorted(agent_id for _, agent_id in rows) == sorted(paid)
